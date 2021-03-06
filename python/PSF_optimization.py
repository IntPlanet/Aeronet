from functools import partial
import numpy as np
import gdal
import scipy
from scipy import ndimage, signal, optimize
from mgrspy import mgrs as mg 
from multi_process import parmap
from cloud import classification
from get_brdf import get_brdf_six
from spatial_mapping import mtile_cal,Find_corresponding_pixels
from read_s2_meta import readxml
from datetime import datetime
from spatial_mapping import mtile_cal,Find_corresponding_pixels, cloud_dilation
from create_training_set import create_training_set
from glob import glob

class PSF_optimize(object):
    def __init__(self, s2_dir=None, m_fname=None, patch = np.array([0,0,10980, 10980]), qa_thresh=2):
        
        self.s2_dir = s2_dir
        self.Lfile = m_fname
        self.patch = patch
        self.bands = 'B02', 'B03', 'B04', 'B08', 'B11', 'B12', 'B8A'
        self.slop = 0.95607605898444503
        self.off =  0.0086119174434039214
        self.parameters = ['xstd', 'ystd', 'angle', 'xs', 'ys']
        self.qa_thresh = qa_thresh
        
    def S2_PSF_optimization(self,):

        
        # open the created vrt file with 10 meter, 20 meter and 60 meter 
        # grouped togehter and use gdal memory map to open it
        g = gdal.Open(self.s2_dir+'10meter.vrt')
        data= g.GetVirtualMemArray()
        b2,b3,b4,b8 = data
        g1 = gdal.Open(self.s2_dir+'20meter.vrt')
        data1 = g1.GetVirtualMemArray()
        b8a, b11, b12 = data1[-3:,:,:]
        img = dict(zip(self.bands, [b2,b3,b4,b8, b11, b12, b8a]))
        
        if glob(self.s2_dir+'cloud.tiff')==[]:
            cl = classification(img = img)
            cl.Get_cm_p()
            g=None; g1=None
            self.cloud = cl.cm
            g = gdal.Open(self.s2_dir+'B04.jp2')
            driver = gdal.GetDriverByName('GTiff')
            g1 = driver.Create(self.s2_dir+'cloud.tiff', g.RasterXSize, g.RasterYSize, 1, gdal.GDT_Byte)

            projection   = g.GetProjection()
            geotransform = g.GetGeoTransform()
            g1.SetGeoTransform( geotransform )
            g1.SetProjection( projection )
            gcp_count = g.GetGCPs()
            if gcp_count != 0:
                g1.SetGCPs( gcp_count, g.GetGCPProjection() )
            g1.GetRasterBand(1).WriteArray(self.cloud)
            g1=None; g=None
            del cl
        else:
            self.cloud = cloud = gdal.Open(self.s2_dir+'cloud.tiff').ReadAsArray().astype(bool)
        cloud_cover = 1.*self.cloud.sum()/self.cloud.size
        cloud_cover = 1.*self.cloud.sum()/self.cloud.size
        if cloud_cover > 0.2:  
            print 'Too much cloud, cloud proportion: %.03f !!'%cloud_cover
            return []
        else:
        
            mete = readxml('%smetadata.xml'%self.s2_dir)
            self.sza = np.zeros(7)
            self.sza[:] = mete['mSz']
            self.saa = self.sza.copy()
            self.saa[:] = mete['mSa']
            try:
                self.vza = (mete['mVz'])[[1,2,3,7,11,12,8],]
                self.vaa = (mete['mVa'])[[1,2,3,7,11,12,8],]
            except:   
                self.vza = np.repeat(np.nanmean(mete['mVz']), 7)
                self.vaa = np.repeat(np.nanmean(mete['mVa']), 7)
            self.angles = (self.sza, self.vza,  (self.vaa - self.saa))
            

            tiles = Find_corresponding_pixels(self.s2_dir+'B04.jp2', destination_res=500)
            self.h,self.v = int(self.Lfile.split('.')[-4][1:3]), int(self.Lfile.split('.')[-4][4:])
            self.H_inds, self.L_inds = tiles['h%02dv%02d'%(self.h, self.v)]
            self.Lx, self.Ly = self.L_inds
            self.Hx, self.Hy = self.H_inds
            
            angles = (self.sza[-2], self.vza[-2], (self.vaa - self.saa)[-2])
            self.brdf, self.qa = get_brdf_six(self.Lfile, angles=angles, bands=(7,), Linds=list(self.L_inds))
            self.brdf, self.qa = self.brdf.flatten(), self.qa.flatten()
            
            # convolve band 12 using the generally used PSF value
            self.H_data = np.repeat(np.repeat(b12, 2, axis=1), 2, axis=0)
            size = 2*int(round(max(1.96*50, 1.96*50)))# set the largest possible PSF size
            self.H_data[0,:]=self.H_data[-1,:]=self.H_data[:,0]=self.H_data[:,-1]=0
            self.bad_pixs = cloud_dilation( (self.H_data <= 0) | self.cloud , iteration=size/2) 
            xstd, ystd = 29.75, 39
            ker = self.gaussian(xstd, ystd, 0)
            self.conved = signal.fftconvolve(self.H_data, ker, mode='same')
            
            m_mask = np.all(~self.brdf.mask,axis=0 ) & np.all(self.qa<=self.qa_thresh, axis=0)
            s_mask = ~self.bad_pixs[self.Hx, self.Hy]
            self.ms_mask = s_mask & m_mask
            
            
            '''self.in_patch_m = np.logical_and.reduce(((self.Hx>=self.patch[1]),
                                                    (self.Hx<=(self.patch[1]+self.patch[3])), 
                                                    (self.Hy>=self.patch[0]),
                                                    (self.Hy<=(self.patch[0]+self.patch[2]))))
            
            self.patch_s2_ind = self.Hx[self.in_patch_m]-self.patch[1], self.Hy[self.in_patch_m]-self.patch[0]
            self.patch_mod = self.brdf[self.in_patch_m]
            self.patch_qa = self.qa[self.in_patch_m]
'''

    def gaussian(self, xstd, ystd, angle, norm = True):
        win = 2*int(round(max(1.96*xstd, 1.96*ystd)))
        winx = int(round(win*(2**0.5)))
        winy = int(round(win*(2**0.5)))
        xgaus = signal.gaussian(winx, xstd)
        ygaus = signal.gaussian(winy, ystd)
        gaus  = np.outer(xgaus, ygaus)
        r_gaus = ndimage.interpolation.rotate(gaus, angle, reshape=True)
        center = np.array(r_gaus.shape)/2
        cgaus = r_gaus[center[0]-win/2: center[0]+win/2, center[1]-win/2:center[1]+win/2]
        if norm:
            return cgaus/cgaus.sum()
        else:
            return cgaus 


    def gaus_optimize(self, p0):
        return optimize.fmin_l_bfgs_b(self.gaus_cost, p0, approx_grad=1, iprint=-1,
                                      bounds=self.bounds,maxiter=10, maxfun=10)         


    def shift_optimize(self, p0):
        return optimize.fmin(self.shift_cost, p0, full_output=1, maxiter=100, maxfun=150, disp=0)


    def gaus_cost(self, para):
        # cost for a final psf optimization
        xstd,ystd,angle, xs, ys = para 
        ker = self.gaussian(xstd,ystd,angle,True)                              
        conved = signal.fftconvolve(self.H_data, ker, mode='same')
        # mask bad pixels
        cos = self.cost(xs=xs, ys=ys, conved=conved)
        return cos


    def shift_cost(self, shifts):
        # cost with different shits
        xs, ys = shifts
        cos = self.cost(xs=xs, ys=ys, conved=self.conved)
        return cos


    def cost(self, xs=None, ys=None, conved = None):
        # a common cost function can be reused
        shifted_mask = np.logical_and.reduce(((self.Hx+int(xs)>=self.patch[1]),
                                              (self.Hx+int(xs)<(self.patch[1]+self.patch[3])), 
                                              (self.Hy+int(ys)>=self.patch[0]),
                                              (self.Hy+int(ys)<(self.patch[0]+self.patch[2]))))
        mask = self.ms_mask & shifted_mask
        x_ind, y_ind = self.Hx + int(xs)- self.patch[1], self.Hy + int(ys) - self.patch[0]
        sb12, mb12 = conved[x_ind[mask], y_ind[mask]], self.brdf[mask]
        m_fed, s_fed = self.slop*mb12+self.off, sb12*0.0001
        try:
            r = scipy.stats.linregress(m_fed, s_fed)
            cost = abs(1-r.rvalue)
        except:
            cost = 100000000000
        return cost


    def fire_shift_optimize(self,):
        #self.S2_PSF_optimization()
        min_val = [-50,-50]
        max_val = [50,50]
        ps, distributions = create_training_set([ 'xs', 'ys'], min_val, max_val, n_train=50)
        self.shift_solved = parmap(self.shift_optimize, ps, nprocs=10)    
        self.paras, self.costs = np.array([i[0] for i in self.shift_solved]),np.array([i[1] for i in self.shift_solved])
        xs, ys = self.paras[self.costs==self.costs.min()][0].astype(int)
        print 'Best shift is ', xs, ys, 'with the correlation of', 1-self.costs.min()
        return xs, ys


    def fire_gaus_optimize(self,):
        xs, ys = self.fire_shift_optimize()
        if self.costs.min()<0.1:
            min_val = [12,12, -15,xs-2,ys-2]
            max_val = [50,50, 15, xs+2,ys+2]
            self.bounds = [12,50],[12,50],[-15,15],[xs-2,xs+2],[ys-2, ys+2]

            ps, distributions = create_training_set(self.parameters, min_val, max_val, n_train=50)
            print 'Start solving...'
            self.gaus_solved = parmap(self.gaus_optimize, ps, nprocs=5)
            result = np.array([np.hstack((i[0], i[1])) for i in  self.gaus_solved])
            print 'solved psf', dict(zip(self.parameters+['cost',],result[np.argmin(result[:,-1])]))
            return result[np.argmin(result[:,-1]),:]
        else:
            print 'Cost is too large, plese check!'
            return []
