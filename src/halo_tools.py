import numpy as np
from autograd import numpy as agnp
from autograd import grad 
import matplotlib.pyplot as plt
import matplotlib as mpl
from astropy.table import Table
import scipy.optimize as optimize
from scipy.signal import savgol_filter
from scipy import stats, ndimage
from astropy.io import fits
from time import time as clock
import astropy.table
from statsmodels.nonparametric.bandwidths import select_bandwidth
from statsmodels.nonparametric.kde import KDEUnivariate as KDE
from bottleneck import replace, nanmedian, ss
from sklearn.cluster import DBSCAN
from skimage.feature import peak_local_max
from skimage.morphology import watershed
import lightkurve

import warnings
warnings.filterwarnings("ignore",category =RuntimeWarning)
warnings.filterwarnings('ignore', category=FutureWarning)

'''-----------------------------------------------------------------
halo_tools.py 

In this package we include all the functions that are necessary for
halo photometry in Python.

-----------------------------------------------------------------'''

def softmax(x):
    '''From https://gist.github.com/stober/1946926'''
    e_x = agnp.exp(x - agnp.max(x))
    out = e_x / e_x.sum()
    return out

# =========================================================================
# =========================================================================


def sigma_clip(a, max_iter=10, max_sigma=5, separate_masks=False, mexc=None):
    """Iterative sigma-clipping routine that separates not finite points, and down- and upwards outliers.

    from k2sc, authors: Aigrain, Parviainen & Pope
    """
    mexc  = isfinite(a) if mexc is None else isfinite(a) & mexc
    mhigh = ones_like(mexc)
    mlow  = ones_like(mexc)
    mask  = ones_like(mexc)

    i, nm = 0, None
    while (nm != mask.sum()) and (i < max_iter):
        mask = mexc & mhigh & mlow
        nm = mask.sum()
        med, sig = medsig(a[mask])
        mhigh[mexc] = a[mexc] - med <  max_sigma*sig
        mlow[mexc]  = a[mexc] - med > -max_sigma*sig
        i += 1

    if separate_masks:
        return mlow, mhigh
    else:
        return mlow & mhigh

# =========================================================================
# =========================================================================


def print_time(t):
        if t>3600:
            print('Time taken: %d h %d m %3f s'\
            % (np.int(np.floor(t/3600)), np.int(np.floor(np.mod(t,3600)/60)),np.mod(t,60)))
        elif t>60:
            print( 'Time taken: %d m %3f s' % (np.int(np.floor(np.mod(t,3600)/60)),np.mod(t,60) ))
        else:
            print( 'Time taken: %3f s' % t)

# =========================================================================
# =========================================================================

def read_tpf(fname):
    target_fits = fits.open(fname)

    tpf = target_fits[1].data['FLUX'][:]

    t, x, y = target_fits[1].data['TIME'][:], target_fits[1].data['POS_CORR1'][:], target_fits[1].data['POS_CORR2'][:]
    cad = target_fits[1].data['CADENCENO'][:]
    quality = target_fits[1].data['QUALITY'][:].astype('int32')

    ts = Table({'time':t,
                'cadence':cad,
                'x':x,
                'y':y,
                'quality':quality})


    return tpf, ts

# =========================================================================
# =========================================================================

def censor_tpf(tpf,ts,thresh=-1,minflux=-100.,do_quality=True,verbose=True,order=1,sub=1):
    '''Throw away bad pixels and bad cadences'''

    dummy = tpf.copy()
    tsd = ts.copy()
    maxflux = np.nanmax(tpf)

    # find bad pixels

    if do_quality:
        m = (ts['quality'] == 0) # get bad quality 
        # dummy = dummy[m,:,:]
        # tsd = tsd[m]
    else:
    	m = np.ones_like(ts['quality'])

    dummy[m,:,:][dummy[m,:,:]<0] = 0 # just as a check!

    if thresh >= 0:
        saturated = np.unravel_index((-np.nanmax(dummy[m,:,:],axis=0)).argsort(axis=None)[:thresh],np.nanmax(dummy[m,:,:],axis=0).shape)
        # saturated=(flx_ord[0][-thresh:],flx_ord[1][-thresh:])
        dummy[:,saturated[0],saturated[1]] = np.nan 
        if verbose:
            print('%d saturated pixels' % np.sum(saturated[0].shape))

    # automatic saturation threshold
    if thresh < 0:
        nstart = max(0,np.sum(np.nanmax(dummy[m,:,:],axis=0) > 7e4) - 20)
        nfinish = np.sum(np.nanmax(dummy[m,:,:],axis=0) > 5e4)
        if verbose:
            print('Searching for number of saturated pixels to cut between %d and %d' % (nstart,nfinish))
        stds=[]
        threshs=np.arange(nstart,nfinish)
        for thr in threshs:
            pf, ts, weights, weightmap, pixels_sub = do_lc(dummy,tsd,(None,None),sub,order,maxiter=101,w_init=None,random_init=False,
            thresh=thr,minflux=-100,consensus=False,analytic=True,sigclip=False,verbose=False)
            fl=ts['corr_flux']
            fs=fl[~np.isnan(fl)]/np.nanmedian(fl)
            sfs=savgol_filter(fs,(np.floor(len(fs)/8)*2-1).astype(int),3)
            stds.append(np.std(fs/sfs))
        stds=np.asarray(stds)
        d1 = np.r_[0,stds[1:]-stds[:-1]]
        d2 = np.r_[0,stds[2:]-2*stds[1:-1]+stds[:-2],0]
        i1=[]
        cut = 3
        while len(i1) == 0:
            ind = (stds > cut*stds[0]) & (d1 > 0) & np.r_[True, d2[1:] < d2[:-1]] & np.r_[d2[:-1] < d2[1:], True] & (d2 < 0)
            i1=np.arange(len(ind))[ind]
            cut -= 1

        if len(i1) > 3: i1=i1[i1.argsort(axis=None)][0:3]

        if threshs[i1[np.argmax(d1[i1])]] == threshs[i1[np.argmin(d2[i1])]]: 
            pix=threshs[i1[np.argmax(d1[i1])]]
        else: 
            p1 = i1[np.argmax(d1[i1])]
            p2 = i1[np.argmin(d2[i1])]
            if abs(d1[p1]-d1[p2]) > abs(d2[p1]-d2[p2]):
                pix=threshs[p1]
            else:
                pix=threshs[p2]
        if verbose:
            print('Finished optimization: %d saturated pixels' % pix)
        saturated = np.unravel_index((-np.nanmax(dummy[m,:,:],axis=0)).argsort(axis=None)[:pix],np.nanmax(dummy[m,:,:],axis=0).shape)
        dummy[:,saturated[0],saturated[1]] = np.nan 

    # saturated = np.nanmax(dummy[m,:,:],axis=0) > thresh

    no_flux = np.nanmin(dummy[m,:,:],axis=0) < minflux
    dummy[:,no_flux] = np.nan
    
    xc, yc = np.nanmedian(ts['x'][m]), np.nanmedian(ts['y'][m])


    if np.sum(np.isfinite(ts['x']))>=0.8*tsd['x'][m].shape[0]:
        rr = np.sqrt((tsd['x'][m]-xc)**2 + (tsd['y'][m]-yc)**2)
        goodpos = (rr<5) * np.isfinite(tsd['x'][m]) * np.isfinite(tsd['y'][m])
        m[m][~goodpos] = 0
        if np.sum(~goodpos)>0:
            if verbose:
                print('Throwing out %d bad cadences' % np.sum(~goodpos))
    
    # dummy = dummy[goodpos,:,:] # some campaigns have a few extremely bad cadences
    # tsd = tsd[goodpos]

    # then pick only pixels which are mostly good

    pixels = np.reshape(dummy[m,:,:].T,((tpf.shape[1]*tpf.shape[2]),dummy[m,:,:].shape[0]))
    indic = np.array([np.sum(np.isfinite(pixels[j,:])) 
        for j in range(pixels.shape[0])])
    pixels = pixels[indic>60,:]

    # indic_cad = np.array([np.sum(np.isfinite(pixels[:,j])) 
    #   for j in range(pixels.shape[1])])

    # pixels = pixels[:,indic_cad>200]
    # ts = ts[indic_cad>200]
    m[m][~np.all(np.isfinite(pixels),axis=0)] = 0
    tsd = ts[m]
    pixels = pixels[:,np.all(np.isfinite(pixels),axis=0)]

    # this should get all the nans but if not just set them to 0

    pixels[~np.isfinite(pixels)] = 0

    return pixels, tsd, m, np.where(indic>60), np.sum(saturated[0].shape)

# =========================================================================
# =========================================================================

def get_slice(tpf,ts,start,stop):
    return tpf[start:stop,:,:], ts[start:stop]

# =========================================================================
# =========================================================================

def get_annulus(tpf,rmin,rmax):
    xs, ys = np.arange(tpf.shape[2])-tpf.shape[2]/2.,np.arange(tpf.shape[1])-tpf.shape[1]/2.
    xx, yy = np.meshgrid(xs,ys)
    rr = np.sqrt(xx**2 + yy **2)
    mask = (rr>rmax) + (rr<rmin)
    tpf[:,mask] = np.nan
    return tpf

# =========================================================================
# =========================================================================

def stitch(tslist):
    # key idea is to match GP values at the edge
    # m = np.isfinite(tslist[0]['corr_flux'])
    # final = np.nanmedian(tslist[0]['corr_flux'][m][-5:])
    # newts = tslist[0].copy()
    # for tsj in tslist[1:]:
    #     mm = np.isfinite(tsj['corr_flux'])
    #     initial = np.nanmedian(tsj['corr_flux'][mm][:5])
    #     tsj['corr_flux'] += final-initial
    #     final = np.nanmedian(tsj['corr_flux'][mm][-5:])
    #     newts = astropy.table.vstack([newts,tsj])
    # return newts 

    # m = np.isfinite(tslist[0]['corr_flux'])
    # final = np.nanmedian(tslist[0]['corr_flux'][m][-5:])
    newts = tslist[0].copy()
    newts['corr_flux'] /= np.nanmedian(tslist[0]['corr_flux'])
    for tsj in tslist[1:]:
        # mm = np.isfinite(tsj['corr_flux'])
        # initial = np.nanmedian(tsj['corr_flux'][mm][:5])
        tsj['corr_flux'] /= np.nanmedian(tsj['corr_flux'])
        # final = np.nanmedian(tsj['corr_flux'][mm][-5:])
        newts = astropy.table.vstack([newts,tsj])
    return newts 

'''-----------------------------------------------------------------
In this section we include the actual detrending code.
-----------------------------------------------------------------'''

## it seems to be faster than using np.diff?

def diff_1(z):
    return np.sum(np.abs(z[1:-1]-np.roll(z[1:-1],1)))

def diff_2(z):
    return np.sum(np.abs(2*z[1:-1]-np.roll(z[1:-1],1)-np.roll(z[1:-1],-1)))

# =========================================================================
# =========================================================================

def tv_tpf(pixelvector,order=1,w_init=None,maxiter=101,analytic=False,sigclip=False,verbose=True):
    '''
    This is the main function here - once you have loaded the data, pass it to this
    to do a TV-min light curve.

    Keywords

    order: int
        Run nth order TV - ie first order is L1 norm on first derivative,
        second order is L1 norm on second derivative, etc.
        This is part of the Pock generalized TV scheme, so that
        1st order gives you piecewise constant functions,
        2nd order gives you piecewise affine functions, etc. 
        Currently implemented only up to 2nd order in numerical, 1st in analytic!
        We recommend first order very strongly.
    maxiter: int
        Number of iterations to optimize. 101 is default & usually sufficient.
    w_init: None or array-like.
        Initialize weights with a particular weight vector - useful if you have
        already run TV-min and want to update, but otherwise set to None 
        and it will have default initialization.
    random_init: Boolean
        If False, and w_init is None, it will initialize with uniform weights; if True, it
        will initialize with random weights. False is usually better.
    thresh: float
        A float greater than 0. Pixels less than this fraction of the maximum
        flux at any pixel will be masked out - this is to deal with saturation.
        Because halo is usually intended for saturated stars, the default is 0.8, 
        to deal with saturated pixels. If your star is not saturated, set this 
        greater than 1.0. 
    consensus: Boolean
        If True, this will subsample the pixel space, separately calculate halo time 
        series for eah set of pixels, and merge these at the end. This is to check
        for validation, but is typically not useful, and is by default set False.
    analytic: Boolean
        If True, it will optimize the TV with autograd analytic derivatives, which is
        several orders of magnitude faster than with numerical derivatives. This is 
        by default True but you can run it numerically with False if you prefer.
    sigclip: Boolean
        If True, it will iteratively run the TV-min algorithm clipping outliers.
        Use this for data with a lot of outliers, but by default it is set False.
    '''

    npix = np.shape(pixelvector)[0]
    cons = ({'type': 'eq', 'fun': lambda z: z.sum() - 1.})
    bounds = npix*((0,1),)

    if w_init is None:
        w_init = np.ones(npix)/np.float(npix)

    if analytic: 
        if verbose:
            print('Using Analytic Derivatives')

        if order == 1:
            # only use first order, it appears to be strictly better
            def tv_soft(weights):
                flux = agnp.dot(softmax(weights).T,pixelvector)
                diff = agnp.sum(agnp.abs(flux[1:] - flux[:-1]))
                return diff/agnp.mean(flux)
        elif order == 2:
            def tv_soft(weights):
                flux = agnp.dot(softmax(weights).T,pixelvector)
                diff = agnp.sum(agnp.abs(2.*flux[1:-1] - flux[2:] - flux[:-2]))
                return diff/agnp.mean(flux)

        gradient = grad(tv_soft)

        res = optimize.minimize(tv_soft, w_init, method='L-BFGS-B', jac=gradient, 
            options={'disp': False,'maxiter':maxiter})

        w_best = softmax(res['x']) # softmax

        lc_first_try = np.dot(w_best.T,pixelvector)

        if sigclip:
            print('Sigma clipping')

            good = sigma_clip(lc_first_try,max_sigma=3.5)


            if np.sum(~good) > 0:
                if verbose:
                    print('Clipping %d bad points' % np.sum(~good))

                pixels_masked = pixelvector[:,good]

                def tv_masked(weights):
                    flux = agnp.dot(softmax(weights).T,pixels_masked)
                    diff = agnp.sum(agnp.abs(flux[1:] - flux[:-1]))
                    return diff/agnp.mean(flux)

                gradient_masked = grad(tv_masked)

                res = optimize.minimize(tv_masked, w_init, method='L-BFGS-B', jac=gradient_masked, 
                    options={'disp': False,'maxiter':maxiter})

                w_best = softmax(res['x']) # softmax
            else:
                if verbose:
                    print('No outliers found, continuing')
        else:
            pass

    else:
        if order==1:
            def obj(weights):
                flux = np.dot(weights.T,pixelvector)
                flux /= np.nanmedian(flux)
                return diff_1(flux)

        elif order==2:
            def obj(weights):
                flux = np.dot(weights.T,pixelvector)
                flux/= np.nanmedian(flux)
                return diff_2(flux)

        else:
            print('Order must be 1 or 2')

        res = optimize.minimize(obj, w_init, method='SLSQP', constraints=cons, 
            bounds = bounds, options={'disp': True,'maxiter':maxiter})

        if 'Positive directional derivative for linesearch' in res['message']:
            if verbose:
                print('Failed to converge well! Rescaling.')
            if order==1:
                def obj(weights):
                    flux = np.dot(weights.T,pixelvector)
                    flux /= np.nanmedian(flux)
                    return diff_1(flux)/10.

            elif order==2:
                def obj(weights):
                    flux = np.dot(weights.T,pixelvector)
                    flux/= np.nanmedian(flux)
                    return diff_2(flux)/10.
            w_init = np.random.rand(npix)
            w_init /= w_init.sum()
            res = optimize.minimize(obj, w_init, method='SLSQP', constraints=cons, 
                bounds = bounds, options={'disp': True,'maxiter':maxiter})
        
        w_best = res['x']

    lc_opt = np.dot(w_best.T,pixelvector)
    return w_best, lc_opt

# =========================================================================
# =========================================================================

def print_flex(splits):
    s = 'Taking cadences from: beginning to '
    for split in splits:
        s += ('%.1f; to ' % split)
    s += 'end'
    print(s)

# =========================================================================
# =========================================================================


def do_lc(tpf,ts,splits,sub,order,maxiter=101,split_times=None,w_init=None,random_init=False,
    thresh=-1.,minflux=-100.,consensus=False,analytic=False,sigclip=False,verbose=True):
    ### get a slice corresponding to the splits you want

    if split_times is not None:
        assert(np.min(split_times)>np.min(ts['time'])), "Minimum time split must be during campaign"
        splits = [np.min(np.where(ts['time']>split)) for split in split_times]
        all_splits = [None,*splits,None]
        tss = []
        cad1 = []
        cad2 = []
        sat = []
        weightmap = []
        
        for j, low in enumerate(all_splits[:-1]):
            high = all_splits[j+1]
            pff, tsj, weights, pmap, pixels_sub = do_lc(tpf,
                        ts,(low,high),sub,order,maxiter=101,split_times=None,w_init=w_init,random_init=random_init,
                thresh=thresh,minflux=minflux,consensus=consensus,analytic=analytic,sigclip=sigclip,verbose=verbose)
            tss.append(tsj)
            if low is None:
                cad1.append(ts['cadence'][0])
            else:
                cad1.append(ts['cadence'][low])
            if high is None:
                cad2.append(ts['cadence'][-1])
            else:
                cad2.append(ts['cadence'][high])
            sat.append(pmap["sat_pixels"])
            weightmap.append(pmap["weightmap"])
        wmap = {
        "initial_cadence": cad1,
        "final_cadence": cad2,
        "sat_pixels": sat,
        "weightmap": weightmap
        }
        ts = stitch(tss)
        
    else:
        # pf, ts, weights, weightmap, pixels_sub = do_lc(flux,
        #             ts,(None,None),sub,order,maxiter=101,split_times=None,w_init=w_init,random_init=random_init,
        #     thresh=thresh,minflux=minflux,consensus=consensus,analytic=analytic,sigclip=sigclip,verbose=verbose)

        if splits[0] is None and splits[1] is not None:
            c1 = ts['cadence'][0]
            c2 = ts['cadence'][splits[1]]
        elif splits[0] is not None and splits[1] is None:
            c1 = ts['cadence'][splits[0]]
            c2 = ts['cadence'][-1]
        elif splits[0] is None and splits[1] is None:
            c1 = ts['cadence'][0]
            c2 = ts['cadence'][-1]
        else:
            c1 = ts['cadence'][splits[0]]
            c2 = ts['cadence'][splits[1]]

        if verbose:
            if splits[0] is None and splits[1] is not None:
                print('Taking cadences from beginning to',splits[1])
            elif splits[0] is not None and splits[1] is None:
                print('Taking cadences from', splits[0],'to end')
            elif splits[0] is None and splits[1] is None:
                print('Taking cadences from beginning to end')
            else:
                print('Taking cadences from', splits[0],'to',splits[1])

        tpf, ts = get_slice(tpf,ts,splits[0],splits[1])

        ### now throw away saturated columns, nan pixels and nan cadences

        pixels, tsd, goodcad, mapping, sat = censor_tpf(tpf,ts,thresh=thresh,minflux=minflux,verbose=verbose,order=order,sub=sub)
        pixelmap = np.zeros((tpf.shape[2],tpf.shape[1]))
        if verbose:
            print('Censored TPF')

        ### subsample
        if consensus:           
            assert sub>1, "Must be subsampled to use consensus"
            if verbose:
                print('Subsampling by a factor of %d' % sub)

            weights = np.zeros(pixels.shape[0])
            opt_lcs = np.zeros((pixels[::sub,:].shape[1],sub))

            if random_init:
                w_init = np.random.rand(pixels[::sub,:].shape[0])
                w_init /= np.sum(w_init)

            for j in range(sub):
                pixels_sub = pixels[j::sub,:]
                ### now calculate the halo 
                if verbose:
                    print('Calculating weights')

                weights[j::sub], opt_lcs[:,j] = tv_tpf(pixels_sub,order=order,
                    maxiter=maxiter,w_init=w_init,analytic=analytic,sigclip=sigclip,verbose=verbose)
                if verbose:
                    print('Calculated weights!')

            norm_lcs = opt_lcs/np.nanmedian(opt_lcs,axis=0)
            opt_lc = np.nanmean(norm_lcs,axis=1)

        else:
            pixels_sub = pixels[::sub,:]
            if verbose:
                print('Subsampling by a factor of %d' % sub)

            ### now calculate the halo 

            if verbose:
                print('Calculating weights')
            if random_init:
                w_init = np.random.rand(pixels_sub.shape[0])
                w_init /= np.sum(w_init)

            weights, opt_lc = tv_tpf(pixels_sub,order=order,maxiter=maxiter,
                w_init=w_init,analytic=analytic,verbose=verbose)
            if verbose:
                print('Calculated weights!')

        # opt_lc = np.dot(weights.T,pixels_sub)
        ts['corr_flux'] = np.nan*np.ones_like(ts['x'])
        ts['corr_flux'][goodcad] = opt_lc

        if sub == 1:
            pixelmap.ravel()[mapping] = weights

        elif consensus:
            pixelmap.ravel()[mapping] = weights/float(sub)
        else:
            pixelmap.ravel()[mapping[0][::sub]] = weights
        wmap = {
        "initial_cadence": c1,
        "final_cadence": c2,
        "sat_pixels": sat,
        "weightmap": pixelmap
        }
    return tpf, ts, weights, wmap, pixels_sub

# =========================================================================
# Remove background stars
# =========================================================================

def remove_stars(tpf):

    sumimage = np.nansum(tpf,axis=0,dtype='float64')

    ny, nx = np.shape(sumimage)
    ori_mask = ~np.isnan(sumimage)

    X, Y = np.meshgrid(np.arange(nx), np.arange(ny))

    Flux = sumimage[ori_mask].flatten()
    Flux = Flux[Flux > 0]

    flux_cut = stats.trim1(np.sort(Flux), 0.15)

    background_bandwidth = select_bandwidth(flux_cut, bw='scott', kernel='gau')
    kernel = KDE(flux_cut)

    kernel.fit(kernel='gau', bw=background_bandwidth, fft=True, gridsize=100)
    
    def kernel_opt(x): return -1*kernel.evaluate(x)
    max_guess = kernel.support[np.argmax(kernel.density)]
    MODE = optimize.fmin_powell(kernel_opt, max_guess, disp=0)

    mad_to_sigma = 1.482602218505602
    MAD1 = mad_to_sigma * nanmedian( np.abs( Flux[(Flux < MODE)] - MODE ) )

    thresh= 2.
    CUT = MODE + thresh * MAD1

    idx = (sumimage > CUT)
    X2 = X[idx]
    Y2 = Y[idx]

    cluster_radius=np.sqrt(2)
    min_for_cluster=4

    XX, labels_ini, core_samples_mask = run_DBSCAN(X2, Y2, cluster_radius, min_for_cluster)

    DUMMY_MASKS = np.zeros((0, ny, nx), dtype='bool')
    DUMMY_MASKS_LABELS = []
    m = np.zeros_like(sumimage, dtype='bool')
    for lab in set(labels_ini):
        if lab == -1: continue
        # Create "image" of this mask:
        m[:,:] = False
        for x,y in XX[labels_ini == lab]:
            m[y, x] = True
        # Append them to lists:
        DUMMY_MASKS = np.append(DUMMY_MASKS, [m], axis=0)
        DUMMY_MASKS_LABELS.append(lab)
        
        smask, _ = k2p2_saturated(sumimage, DUMMY_MASKS, idx)
        
        if np.any(smask):
            saturated_masks = {}
            for u,sm in enumerate(smask):
                saturated_masks[DUMMY_MASKS_LABELS[u]] = sm
        else:
            saturated_masks = None
                
        ws_thres = 0.02
        ws_footprint = 3
        ws_blur = 0.2
        ws_alg = 'flux'
        plot_folder = None
        catalog = None
        
        labels, unique_labels, NoCluster = k2p2WS(X, Y, X2, Y2, sumimage, XX, labels_ini, core_samples_mask, 
                                                  saturated_masks=saturated_masks, ws_thres=ws_thres, 
                                                  ws_footprint=ws_footprint, ws_blur=ws_blur, ws_alg=ws_alg, 
                                                  output_folder=plot_folder, catalog=catalog)
        
    # Make sure it is a tuple and not a set - much easier to work with:
    unique_labels = tuple(unique_labels)

    # Create list of clusters and their number of pixels:
    No_pix_sort = np.zeros([len(unique_labels), 2])
    for u,lab in enumerate(unique_labels):
        No_pix_sort[u, 0] = np.sum(labels == lab)
        No_pix_sort[u, 1] = lab

    # Only select the clusters that are not the largest or noise:

    cluster_select = (No_pix_sort[:, 0] < np.max(No_pix_sort.T[0])) & (No_pix_sort[:, 1] != -1)
    # cluster_select = (No_pix_sort[:, 0] < np.max(No_pix_sort.T[0]))
    no_masks = sum(cluster_select)
    No_pix_sort = No_pix_sort[cluster_select, :]

    MASKS = np.zeros((no_masks, ny, nx))
    for u in range(no_masks):
        lab = No_pix_sort[u, 1]
        class_member_mask = (labels == lab)
        xy = XX[class_member_mask ,:]
        MASKS[u, xy[:,1], xy[:,0]] = 1

    maskimg = np.sum(MASKS,axis=0)
    invmaskimg = np.abs(maskimg-1)

    return invmaskimg*tpf




#==============================================================================
# DBSCAN subroutine
#==============================================================================
def run_DBSCAN(X2, Y2, cluster_radius, min_for_cluster):

    XX = np.array([[x,y] for x,y in zip(X2,Y2)])

    db = DBSCAN(eps=cluster_radius, min_samples=min_for_cluster)
    db.fit(XX)
    core_samples_mask = np.zeros_like(db.labels_, dtype=bool)
    core_samples_mask[db.core_sample_indices_] = True
    labels = db.labels_
    # Number of clusters in labels, ignoring noise if present.

    return XX, labels, core_samples_mask

#==============================================================================
# Segment clusters using watershed
#==============================================================================
def k2p2WS(X, Y, X2, Y2, flux0, XX, labels, core_samples_mask, saturated_masks=None, ws_thres=0.1, ws_footprint=3, ws_blur=0.5, ws_alg='flux', output_folder=None, catalog=None):

    # Get logger for printing messages:
    # logger = logging.getLogger(__name__)


    unique_labels_ini = set(labels)


    XX2 = np.array([[x,y] for x,y in zip(X.flatten(),Y.flatten())])

    Labels = np.ones_like(flux0)*-2
    Labels[XX[:,1], XX[:,0]] = labels

    Core_samples_mask = np.zeros_like(Labels, dtype=bool)
    Core_samples_mask[XX[:,1], XX[:,0]] = core_samples_mask

    # Set all non-core points to noise
    Labels[~Core_samples_mask] = -1

    max_label = np.max(labels)

    for i in range(len(unique_labels_ini)):


        lab = list(unique_labels_ini)[i]

        if lab == -1 or lab == -2:
            continue

        # select class members - non-core members have been set to noise
        class_member_mask = (Labels == lab).flatten()
        xy = XX2[class_member_mask,:]


        Z = np.zeros_like(flux0, dtype='float64')
        Z[xy[:,1], xy[:,0]] = flux0[xy[:,1], xy[:,0]] #y=row, x=column

        if ws_alg == 'dist':
            distance0 = ndimage.distance_transform_edt(Z)
        elif ws_alg == 'flux':
            distance0 = Z
        # else:
        #   logger.error("Unknown watershed algorithm: '%s'", ws_alg)

        # logger.debug("Using '%s' watershed algorithm", ws_alg)

        if not catalog is None:
            distance = distance0

            local_maxi = np.zeros_like(flux0, dtype='bool')
            #for c in catalog:
            #   local_maxi[int(np.floor(c[1])), int(np.floor(c[0]))] = True

            #print("Finding blobs...")
            #blobs = blob_dog(distance, min_sigma=1, max_sigma=20)
            #for c in blobs:
            #   local_maxi[int(np.floor(c[0])), int(np.floor(c[1]))] = True

            # Smooth the basin image with Gaussian filter:
            #distance = ndimage.gaussian_filter(distance0, ws_blur*0.5)

            # Find maxima in the basin image to use for markers:
            local_maxi_loc = peak_local_max(distance, indices=True, exclude_border=False, threshold_rel=0, footprint=np.ones((ws_footprint, ws_footprint)))

            for c in catalog:
                d = np.sqrt( ((local_maxi_loc[:,1]+0.5) - c[0])**2 + ((local_maxi_loc[:,0]+0.5) - c[1])**2 )
                indx = np.argmin(d)
                if d[indx] < 2.0*np.sqrt(2):
                    local_maxi[local_maxi_loc[indx,0], local_maxi_loc[indx,1]] = True

        else:
            # Smooth the basin image with Gaussian filter:
            distance = ndimage.gaussian_filter(distance0, ws_blur)

            # Find maxima in the basin image to use for markers:
            local_maxi = peak_local_max(distance, indices=False, exclude_border=False, threshold_rel=ws_thres, footprint=np.ones((ws_footprint, ws_footprint)))

        # If masks of saturated pixels are provided, clean out in the
        # found local maxima to make sure only one is found within
        # each patch of saturated pixels:
        if not saturated_masks is None and lab in saturated_masks:
            saturated_pixels = saturated_masks[lab]

            # Split the saturated pixels up into patches that are connected:
            sat_labels, numfeatures = ndimage.label(saturated_pixels)

            # Loop through the patches of saturated pixels:
            for k in range(1, numfeatures+1):
                # This mask of saturated pixels:
                sp = saturated_pixels & (sat_labels == k)

                # Check if there is more than one local maximum found
                # within this patch of saturated pixels:
                if np.sum(local_maxi & sp) > 1:
                    # Find the local maximum with the highest value that is also saturated:
                    imax = np.unravel_index(np.nanargmax(distance * local_maxi * sp), distance.shape)
                    # Only keep the maximum with the highest value and remove all
                    # the others if they are saturated:
                    local_maxi[sp] = False
                    local_maxi[imax] = True

        # Assign markers/labels to the found maxima:
        markers = ndimage.label(local_maxi)[0]

        # Run the watershed segmentation algorithm on the negative
        # of the basin image:
        labels_ws = watershed(-distance0, markers, mask=Z)

        # The number of masks after the segmentation:
        no_labels = len(set(labels_ws.flatten()))

        # Set all original cluster points to noise, in this way things that in the
        # end is not associated with a "new" cluster will not be used any more
        Labels[xy[:,1], xy[:,0]] = -1

        # Use the original label for a part of the new cluster -  if only
        # one cluster is identified by the watershed algorithm this will then
        # keep the original labeling
        idx = (labels_ws == 1) & (Z != 0)
        Labels[idx] = lab

        # If the cluster is segmented we will assign these new labels, starting from
        # the highest original label + 1
        for u in range(no_labels-2):
            max_label += 1

            idx = (labels_ws==u+2) & (Z!=0)
            Labels[idx] = max_label

        labels_new = Labels[Y2, X2]
        unique_labels = set(labels_new)
        NoCluster = len(unique_labels) - (1 if -1 in labels_new else 0)

        # Create plot of the watershed segmentation:
        if not output_folder is None:

            fig, axes = plt.subplots(ncols=3, figsize=(14, 6))
            fig.subplots_adjust(hspace=0.12, wspace=0.12)
            ax0, ax1, ax2 = axes

            plot_image(Z, ax=ax0, scale='log', title='Overlapping objects', xlabel=None, ylabel=None)

            # Plot the basin used for watershed:
            plot_image(distance, ax=ax1, scale='log', title='Basin', xlabel=None, ylabel=None)

            # Overplot the full catalog:
            if not catalog is None:
                ax1.scatter(catalog[:,0], catalog[:,1], color='y', s=5, alpha=0.3)

            #if local_maxi_all is not None:
            #   print(local_maxi_all)
            #   ax1.scatter(X[local_maxi_all[:,0]], Y[local_maxi_all[:,1]], color='g', marker='+', s=5, alpha=0.5)
            #ax1.scatter(X[local_maxi_before], Y[local_maxi_before], color='c', s=5, alpha=0.7)

            # Overplot the final markers for the watershed:
            ax1.scatter(X[local_maxi], Y[local_maxi], color='r', s=5, alpha=0.7)

            plot_image(labels_ws, scale='linear', percentile=100, cmap='nipy_spectral', title='Separated objects', xlabel=None, ylabel=None)

            for ax in axes:
                ax.set_xticklabels([])
                ax.set_yticklabels([])

            figname = 'seperated_cluster_%d' % i
            save_figure(os.path.join(output_folder, figname))
            plt.close(fig)

    return labels_new, unique_labels, NoCluster

#==============================================================================
#
#==============================================================================
def k2p2_saturated(SumImage, MASKS, idx):

    # # Get logger for printing messages:
    # logger = logging.getLogger(__name__)

    no_masks = MASKS.shape[0]

    column_mask = np.zeros_like(SumImage, dtype='bool')
    saturated_mask = np.zeros_like(MASKS, dtype='bool')
    pixels_added = 0

    # Loop through the different masks:
    for u in range(no_masks):
        # Create binary version of mask and extract
        # the rows and columns which it spans and
        # the highest value in it:
        mask = np.asarray(MASKS[u, :, :], dtype='bool')
        mask_rows, mask_columns = np.where(mask)
        mask_max = np.nanmax(SumImage[mask])

        # Loop through the columns of the mask:
        for c in set(mask_columns):

            column_mask[:, c] = True

            # Extract the pixels that are in this column and in the mask:
            pixels = SumImage[mask & column_mask]

            # Calculate ratio as defined in Lund & Handberg (2014):
            ratio = np.abs(nanmedian(np.diff(pixels)))/np.nanmax(pixels)
            if ratio < 0.01 and nanmedian(pixels) >= mask_max/2:
                # logger.debug("Column %d - RATIO = %f - Saturated", c, ratio)

                # Has significant flux and is in saturated column:
                add_to_mask = (idx & column_mask)

                # Make sure the pixels we add are directly connected to the highest flux pixel:
                new_mask_labels, numfeatures = ndimage.label(add_to_mask)
                imax = np.unravel_index(np.nanargmax(SumImage * mask * column_mask), SumImage.shape)
                add_to_mask &= (new_mask_labels == new_mask_labels[imax])

                # Modify the mask:
                pixels_added += np.sum(add_to_mask) - np.sum(mask[column_mask])
                # logger.debug("  %d pixels should be added to column %d", np.sum(add_to_mask) - np.sum(mask[column_mask]), c)
                saturated_mask[u][add_to_mask] = True
            # else:
                # logger.debug("Column %d - RATIO = %f", c, ratio)

            column_mask[:, c] = False

    return saturated_mask, pixels_added

'''-----------------------------------------------------------------
The cuts for Campaign 4 are

0:550
550:2200
2200:
-----------------------------------------------------------------'''

class halo_tpf(lightkurve.TessTargetPixelFile):
    
    def halo(self, aperture_mask='pipeline',split_times=None,sub=1,order=1,
        maxiter=101,w_init=None,random_init=False,
        thresh=-1,minflux=-100.,consensus=False,
        analytic=True,sigclip=False,mask=None,verbose=True):

        """Performs 'halo' TV-min weighted-aperture photometry.
             Parameters
            ----------
            aperture_mask : array-like, 'pipeline', or 'all'
                A boolean array describing the aperture such that `False` means
                that the pixel will be masked out.
                If the string 'all' is passed, all pixels will be used.
                The default behaviour is to use the Kepler pipeline mask.
             splits : tuple, (None, None) or (2152,2175) etc.
                A tuple including two times at which to split the light curve and run halo 
                separately outside these splits.
             sub : int
                Do you want to subsample every nth pixel in your light curve? Not advised, 
                but can come in handy for very large TPFs.
             order: int
                Run nth order TV - ie first order is L1 norm on first derivative,
                second order is L1 norm on second derivative, etc.
                This is part of the Pock generalized TV scheme, so that
                1st order gives you piecewise constant functions,
                2nd order gives you piecewise affine functions, etc. 
                Currently implemented only up to 2nd order in numerical, 1st in analytic!
                We recommend first order very strongly.
             maxiter: int
                Number of iterations to optimize. 101 is default & usually sufficient.
             w_init: None or array-like.
                Initialize weights with a particular weight vector - useful if you have
                already run TV-min and want to update, but otherwise set to None 
                and it will have default initialization.
             random_init: Boolean
                If False, and w_init is None, it will initialize with uniform weights; if True, it
                will initialize with random weights. False is usually better.
             thresh: float
                A float greater than 0. Pixels less than this fraction of the maximum
                flux at any pixel will be masked out - this is to deal with saturation.
                Because halo is usually intended for saturated stars, the default is 0.8, 
                to deal with saturated pixels. If your star is not saturated, set this 
                greater than 1.0. 
             consensus: Boolean
                If True, this will subsample the pixel space, separately calculate halo time 
                series for eah set of pixels, and merge these at the end. This is to check
                for validation, but is typically not useful, and is by default set False.
             analytic: Boolean
                If True, it will optimize the TV with autograd analytic derivatives, which is
                several orders of magnitude faster than with numerical derivatives. This is 
                by default True but you can run it numerically with False if you prefer.
             sigclip: Boolean
                If True, it will iteratively run the TV-min algorithm clipping outliers.
                Use this for data with a lot of outliers, but by default it is set False.
             Returns
            -------
            lc : KeplerLightCurve object
                Array containing the TV-min flux within the aperture for each
                cadence.
            """
    
        if mask is None:
            aperture_mask = self._parse_aperture_mask(aperture_mask)
        else:
            aperture_mask = mask

        centroid_col, centroid_row = self.estimate_centroids()

        x, y = self.hdu[1].data['POS_CORR1'][self.quality_mask], self.hdu[1].data['POS_CORR2'][self.quality_mask]
        quality = self.quality
        ts = Table({'time':self.time,
                    'cadence':self.cadenceno,
                    'x':x,
                    'y':y,
                    'quality':quality})

        

        flux = np.copy(self.flux)

        flux[:,~aperture_mask] = np.nan

        pf, ts, weights, weightmap, pixels_sub = do_lc(flux,
                    ts,(None,None),sub,order,maxiter=101,split_times=split_times,w_init=w_init,random_init=random_init,
            thresh=thresh,minflux=minflux,consensus=consensus,analytic=analytic,sigclip=sigclip,verbose=verbose)
        
        nanmask = np.isfinite(ts['corr_flux'])
         ### to do! Implement light curve POS_CORR1, POS_CORR2 attributes.
        lc_out = lightkurve.TessLightCurve(flux=ts['corr_flux'],
                                time=ts['time'],
                                flux_err=np.nan*ts['corr_flux'],
                                centroid_col=ts['x'],
                                centroid_row=ts['y'],
                                quality=ts['quality'],
                                # channel=self.channel,
                                # campaign=self.campaign,
                                # quarter=self.quarter,
                                targetid=self.targetid,
                                ccd = self.ccd,
                                sector=self.sector,
                                # mission=self.mission,
                                cadenceno=ts['cadence'])
        lc_out.pos_corr1 = self.pos_corr1
        lc_out.pos_corr2 = self.pos_corr2
        lc_out.primary_header = self.hdu[0].header
        lc_out.data_header = self.hdu[1].header
        return weightmap, lc_out