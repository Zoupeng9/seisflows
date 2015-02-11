
import subprocess
from os.path import join
from glob import glob

import numpy as np

import seisflows.seistools.specfem2d as solvertools
from seisflows.seistools.shared import getpar, setpar

from seisflows.tools import unix
from seisflows.tools.array import loadnpy, savenpy
from seisflows.tools.code import exists, setdiff
from seisflows.tools.config import findpath, loadclass, ParameterObj
from seisflows.tools.io import loadbin, savebin

PAR = ParameterObj('SeisflowsParameters')
PATH = ParameterObj('SeisflowsPaths')

import system
import preprocess


class specfem2d(loadclass('solver', 'base')):
    """ Python interface for SPECFEM2D

      See base class for method descriptions
    """

    # model parameters
    model_parameters = []
    model_parameters += ['rho']
    model_parameters += ['vp']
    model_parameters += ['vs']

    # inversion parameters
    inversion_parameters = []
    inversion_parameters += ['vs']

    kernel_map = {
        'rho': 'rho_kernel',
        'vp': 'alpha_kernel',
        'vs': 'beta_kernel'}

    def check(self):
        """ Checks parameters, paths, and dependencies
        """
        super(specfem2d, self).check()

        # check time stepping parameters
        if 'NT' not in PAR:
            raise Exception

        if 'DT' not in PAR:
            raise Exception

        if 'F0' not in PAR:
            raise Exception

        # check solver executables directory
        if 'SPECFEM2D_BIN' not in PATH:
            pass #raise Exception

        # check solver input files directory
        if 'SPECFEM2D_DATA' not in PATH:
            pass #raise Exception


    def generate_data(self, **model_kwargs):
        """ Generates data
        """
        self.generate_mesh(**model_kwargs)

        unix.cd(self.getpath)
        setpar('SIMULATION_TYPE', '1')
        setpar('SAVE_FORWARD', '.true.')
        self.mpirun('bin/xmeshfem2D')
        self.mpirun('bin/xspecfem2D')

        unix.mv(self.data_wildcard, 'traces/obs')
        self.export_traces(PATH.OUTPUT, 'traces/obs')


    def generate_mesh(self, model_path=None, model_name=None, model_type='gll'):
        """ Performs meshing and database generation
        """
        assert(model_name)
        assert(model_type)
        assert (exists(model_path))

        self.initialize_solver_directories()
        unix.cp(model_path, 'DATA/model_velocity.dat_input')
        self.export_model(PATH.OUTPUT +'/'+ model_name)


    ### low-level solver interface

    def forward(self):
        """ Calls SPECFEM2D forward solver
        """
        setpar('SIMULATION_TYPE', '1')
        setpar('SAVE_FORWARD', '.true.')
        self.mpirun('bin/xmeshfem2D')
        self.mpirun('bin/xspecfem2D')


    def adjoint(self):
        """ Calls SPECFEM2D adjoint solver
        """
        setpar('SIMULATION_TYPE', '3')
        setpar('SAVE_FORWARD', '.false.')
        unix.rm('SEM')
        unix.ln('traces/adj', 'SEM')

        self.mpirun('bin/xmeshfem2D')
        self.mpirun('bin/xspecfem2D')



    ### model input/output

    def load(self, filename, type='', verbose=False):
        """Reads SPECFEM2D kernel or model

           Models and kernels are read from 5 or 6 column text files whose
           format is described in the SPECFEM2D user manual. Once read, a model
           or kernel is stored in a dictionary containing mesh coordinates and
           corresponding material parameter values.
        """
        # read text file
        M = np.loadtxt(filename)
        nrow = M.shape[0]
        ncol = M.shape[1]

        if ncol == 5:
            ioff = 0
        elif ncol == 6:
            ioff = 1
        else:
            raise ValueError("Wrong number of columns.")

        # fill in dictionary
        parts = {}
        for key in ['x', 'z', 'rho', 'vp', 'vs']:
            parts[key] = [M[:,ioff]]
            ioff += 1
        return parts

    def save(self, filename, parts, type='model'):
        """writes SPECFEM2D kernel or model"""
        # allocate array
        if type == 'model':
            nrow = len(parts[parts.keys().pop()][0])
            ncol = 6
            ioff = 1
            M = np.zeros((nrow, ncol))
        elif type == 'kernel':
            nrow = len(parts[parts.keys().pop()][0])
            ncol = 5
            ioff = 0
            M = np.zeros((nrow, ncol))
        else:
            raise ValueError

        # fill in array
        for icol, key in enumerate(['x', 'z', 'rho', 'vp', 'vs']):
            M[:,icol+ioff] = parts[key][0]

        # write array
        np.savetxt(filename, M, '%16.10e')



    ### vector/dictionary conversion

    def merge(self, parts):
        """ merges dictionary into vector
        """
        v = np.array([])
        for key in self.inversion_parameters:
            for iproc in range(PAR.NPROC):
                v = np.append(v, parts[key][iproc])
        return v


    def split(self, v):
        """ splits vector into dictionary
        """
        parts = {}
        nrow = len(v)/(PAR.NPROC*len(self.inversion_parameters))
        j = 0
        for key in ['x', 'z', 'rho', 'vp', 'vs']:
            parts[key] = []
            if key in self.inversion_parameters:
                for i in range(PAR.NPROC):
                    imin = nrow*PAR.NPROC*j + nrow*i
                    imax = nrow*PAR.NPROC*j + nrow*(i + 1)
                    i += 1
                    parts[key].append(v[imin:imax])
                j += 1
            else:
                for i in range(PAR.NPROC):
                    proc = '%06d' % i
                    part = np.load(PATH.GLOBAL +'/'+ 'mesh' +'/'+ key +'/'+ proc)
                    parts[key].append(part)
        return parts



    ### postprocessing utilities

    def combine(self, path=''):
        """combines SPECFEM2D kernels"""
        subprocess.call(
            [self.getpath +'/'+ 'bin/xsmooth_sem'] +
            [str(len(unix.ls(path)))] +
            [path])

    def smooth(self, path='', tag='gradient', span=0.):
        """smooths SPECFEM2D kernels by convolving them with a Gaussian"""
        from seisflows.tools.array import meshsmooth

        parts = self.load(path +'/'+ tag)
        if not span:
            return parts

        # set up grid
        x = parts['x'][0]
        z = parts['z'][0]
        lx = x.max() - x.min()
        lz = z.max() - z.min()
        nn = x.size
        nx = np.around(np.sqrt(nn*lx/lz))
        nz = np.around(np.sqrt(nn*lx/lz))

        # perform smoothing
        for key in self.inversion_parameters:
            parts[key] = [meshsmooth(x, z, parts[key][0], span, nx, nz)]
        unix.mv(path +'/'+ tag, path +'/'+ '_nosmooth')
        self.save(path +'/'+ tag, parts)


    def clip(self, path='', tag='gradient', thresh=1.):
        """clips SPECFEM2D kernels"""
        parts = self.load(path +'/'+ tag)
        if thresh >= 1.:
            return parts

        for key in self.inversion_parameters:
            # scale to [-1,1]
            minval = parts[key][0].min()
            maxval = parts[key][0].max()
            np.clip(parts[key][0], thresh*minval, thresh*maxval, out=parts[key][0])
        unix.mv(path +'/'+ tag, path +'/'+ '_noclip')
        self.save(path +'/'+ tag, parts)


    ### file transfer utilities

    def import_model(self, path):
        src = join(path +'/'+ 'model')
        dst = join(self.getpath, 'DATA/model_velocity.dat_input')
        unix.cp(src, dst)

    def import_traces(self, path):
        src = glob(join(path, 'traces', self.getname, '*'))
        dst = join(self.getpath, 'traces/obs')
        unix.cp(src, dst)

    def export_model(self, path):
        if system.getnode() == 0:
            src = join(self.getpath, 'DATA/model_velocity.dat_input')
            dst = path
            unix.cp(src, dst)

    def export_kernels(self, path):
        unix.mkdir_gpfs(join(path, 'kernels'))
        src = join(self.getpath, 'OUTPUT_FILES/proc000000_rhop_alpha_beta_kernel.dat')
        dst = join(path, 'kernels', '%06d' % system.getnode())
        unix.cp(src, dst)

    def export_residuals(self, path):
        unix.mkdir_gpfs(join(path, 'residuals'))
        src = join(self.getpath, 'residuals')
        dst = join(path, 'residuals', self.getname)
        unix.mv(src, dst)

    def export_traces(self, path, prefix='traces/obs'):
        unix.mkdir_gpfs(join(path, 'traces'))
        src = join(self.getpath, prefix)
        dst = join(path, 'traces', self.getname)
        unix.cp(src, dst)


    ### setup utilities

    def initialize_solver_directories(self):
        """ Creates directory structure expected by SPECFEM2D, copies 
          executables, and prepares input files. Executables must be supplied 
          by user as there is currently no mechanism to automatically compile 
          from source.
        """
        unix.mkdir(self.getpath)
        unix.cd(self.getpath)

        # create directory structure
        unix.mkdir('bin')
        unix.mkdir('DATA')

        unix.mkdir('traces/obs')
        unix.mkdir('traces/syn')
        unix.mkdir('traces/adj')

        unix.mkdir(self.model_databases)

        # copy exectuables
        src = glob(PATH.SOLVER_BINARIES +'/'+ '*')
        dst = 'bin/'
        unix.cp(src, dst)

        # copy input files
        src = glob(PATH.SOLVER_FILES +'/'+ '*')
        dst = 'DATA/'
        unix.cp(src, dst)

        src = 'DATA/SOURCE_' + self.getname
        dst = 'DATA/SOURCE'
        unix.cp(src, dst)

        setpar('f0', PAR.F0, 'DATA/SOURCE')


    def initialize_io_machinery(self):
        """ Writes mesh files expected by input/output methods
        """
        if system.getnode() == 0:

            model_set = set(self.model_parameters)
            inversion_set = set(self.inversion_parameters)

            parts = self.load(PATH.MODEL_INIT)
            try:
                path = PATH.GLOBAL +'/'+ 'mesh'
            except:
                raise Exception
            if not exists(path):
                for key in list(setdiff(model_set, inversion_set)) + ['x', 'z']:
                    unix.mkdir(path +'/'+ key)
                    for proc in range(PAR.NPROC):
                        with open(path +'/'+ key +'/'+ '%06d' % proc, 'w') as file:
                            np.save(file, parts[key][proc])

            try:
                path = PATH.OPTIMIZE +'/'+ 'm_new'
            except:
                return
            if not exists(path):
                savenpy(path, self.merge(parts))
            #if not exists(path):
            #    for key in inversion_set:
            #        unix.mkdir(path +'/'+ key)
            #        for proc in range(PAR.NPROC):
            #            with open(path +'/'+ key +'/'+ '%06d' % proc, 'w') as file:
            #                np.save(file, parts[key][proc])


    ### input file writers

    def write_parameters(self):
        unix.cd(self.getpath)
        solvertools.write_parameters(vars(PAR))

    def write_receivers(self):
        unix.cd(self.getpath)
        key = 'use_existing_STATIONS'
        val = '.true.'
        setpar(key, val)
        _, h = preprocess.load('traces/obs')
        solvertools.write_receivers(h.nr, h.rx, h.rz)

    def write_sources(self):
        unix.cd(self.getpath)
        _, h = preprocess.load(dir='traces/obs')
        solvertools.write_sources(vars(PAR), h)


    ### utility functions

    def mpirun(self, script, output='/dev/null'):
        """ Wrapper for mpirun
        """
        with open(output,'w') as f:
            subprocess.call(
                script,
                shell=True,
                stdout=f)

    ### miscellaneous

    @property
    def data_wildcard(self):
        return glob('OUTPUT_FILES/U?_file_single.su')

    @property
    def model_databases(self):
        return join(self.getpath, 'OUTPUT_FILES/DATABASES_MPI')

    @property
    def source_prefix(self):
        return 'SOURCE'


