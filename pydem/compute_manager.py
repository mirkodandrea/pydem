# -*- coding: utf-8 -*-
# NOTES
# TODO: Fix edge resolution top-bottom bug
# TODO: Ensure that 'success' is written out to a file.
# TODO: Clean up filenames -- should be attributes of class
# TODO: Make a final seamless result with no overlaps -- option to save out to GeoTIFF, and quantize data for cheaper storage

import os
import traceback
import subprocess
import time
import numpy as np
import zarr
import scipy.interpolate as spinterp
import gc
import traitlets as tl
import traittypes as tt
import rasterio
from multiprocessing import Queue
from multiprocessing import Process
from multiprocessing.pool import Pool
from multiprocessing.context import TimeoutError as mpTimeoutError
# import ipydb

from .dem_processing import DEMProcessor
from .utils import dem_processor_from_raster_kwargs

EDGE_SLICES = {
        'left': (slice(0, None), 0),
        'right': (slice(0, None), -1),
        'top' : (0, slice(0, None)),
        'bottom' : (-1, slice(0, None))
        }

def calc_elev_cond(fn, out_fn, out_slice):
    try:
        kwargs = dem_processor_from_raster_kwargs(fn)
        dp = DEMProcessor(**kwargs)
        dp.calc_fill_flats()
        elev = dp.elev.astype(np.float32)
        save_result(elev, out_fn, out_slice)
    except Exception as e:
        return (0, fn + ':' + traceback.format_exc())
    return (1, "{}: success".format(fn))

def calc_aspect_slope(fn, out_fn_aspect, out_fn_slope, out_fn, out_slice):
    #if 1:
    try:
        kwargs = dem_processor_from_raster_kwargs(fn)
        kwargs['elev'] = zarr.open(out_fn, mode='a')['elev'][out_slice]
        kwargs['fill_flats'] = False  # assuming we already did this
        dp = DEMProcessor(**kwargs)
        dp.calc_slopes_directions()
        save_result(dp.direction.astype(np.float32), out_fn_aspect, out_slice)
        save_result(dp.mag.astype(np.float32), out_fn_slope, out_slice)
    except Exception as e:
        return (0, fn + ':' + traceback.format_exc())
    return (1, "{}: success".format(fn))

def calc_uca(fn, out_fn_uca, out_fn_todo, out_fn_done, out_fn, out_slice):
    try:
        kwargs = dem_processor_from_raster_kwargs(fn)
        kwargs['elev'] = zarr.open(out_fn, mode='a')['elev'][out_slice]
        kwargs['direction'] = zarr.open(out_fn, mode='r')['aspect'][out_slice]
        kwargs['mag'] = zarr.open(out_fn, mode='a')['slope'][out_slice]
        kwargs['fill_flats'] = False  # assuming we already did this
        dp = DEMProcessor(**kwargs)
        dp.find_flats()
        dp.calc_uca()
        save_result(dp.uca.astype(np.float32), out_fn_uca, out_slice)
        save_result(dp.edge_todo.astype(np.bool), out_fn_todo, out_slice, _test_bool)
        save_result(dp.edge_done.astype(np.bool), out_fn_done, out_slice, _test_bool)
    except Exception as e:
        return (0, fn + ':' + traceback.format_exc())
    return (1, "{}: success".format(fn))

def calc_uca_ec_metrics(fn_uca, fn_done, fn_todo, slc, edge_slc):
        # get the edge data
        edge_data = [zarr.open(fn_uca, mode='a')[edge_slc[key]] for key in edge_slc.keys()]
        edge_done = [zarr.open(fn_done, mode='a')[edge_slc[key]]
                for key in edge_slc.keys()]
        edge_todo = [zarr.open(fn_todo, mode='a')[slc][EDGE_SLICES[key]]
                for key in edge_slc.keys()]
        # n_coulddo = 0
        p_done = 0
        n_done = 0
        for ed, et, edn in zip(edge_data, edge_todo, edge_done):
            # coulddo_ = et & (ed > 0)  # previously
            coulddo = et & edn
            n_done += (coulddo).sum()
            p_done += et.sum()
            #n_coulddo += coulddo_.sum()
        p_done = n_done / (1e-16 + p_done)
        # return (n_coulddo, p_done, n_done)
        return [(p_done, n_done)]

def calc_uca_ec(fn, out_fn_uca, out_fn_todo, out_fn_done, out_fn, out_slice, edge_slice):
    #if 1:
    try:
        kwargs = dem_processor_from_raster_kwargs(fn)
        kwargs['elev'] = zarr.open(out_fn, mode='a')['elev'][out_slice]
        kwargs['direction'] = zarr.open(out_fn, mode='r')['aspect'][out_slice]
        kwargs['mag'] = zarr.open(out_fn, mode='a')['slope'][out_slice]
        kwargs['fill_flats'] = False  # assuming we already did this
        dp = DEMProcessor(**kwargs)
        dp.find_flats()

        # get the initial UCA
        uca_file = zarr.open(out_fn_uca, mode='a')
        uca_init = uca_file[out_slice]
        done_file = zarr.open(out_fn_done, mode='a')
        todo_file = zarr.open(out_fn_todo, mode='a')

        # get the edge data
        edge_init_data = {key: uca_file[edge_slice[key]] for key in edge_slice.keys()}
        edge_init_done = {key: done_file[edge_slice[key]] for key in edge_slice.keys()}
        edge_init_todo = {key: todo_file[out_slice][EDGE_SLICES[key]] for key in edge_slice.keys()}
        edge_init_todo_neighbor = {key: todo_file[edge_slice[key]] for key in edge_slice.keys()}

        # fix my TODO if my neitghbor has TODO on the same edge -- that should never happen except for floating point
        # rounding errors
        edge_init_todo = {k: v & (edge_init_todo_neighbor[k] == False) for k, v in edge_init_todo.items()}


        dp.calc_uca(uca_init=uca_init, edge_init_data=[edge_init_data, edge_init_done, edge_init_todo])
        save_result(dp.uca.astype(np.float32), out_fn_uca, out_slice)
        save_result(dp.edge_todo.astype(np.bool), out_fn_todo, out_slice, _test_bool)
        save_result(dp.edge_done.astype(np.bool), out_fn_done, out_slice, _test_bool)
    except Exception as e:
        return (0, fn + ':' + traceback.format_exc())
    return (1, "{}: success".format(fn))

def _test_float(a, b):
    return np.any(np.abs(a - b) > 1e-8)

def _test_bool(a, b):
    return np.any(a != b)

def save_result(result, out_fn, out_slice, test_func=_test_float):
    # save the results
    outstr = ''
    zf = zarr.open(out_fn, mode='a')
    try:
        zf[out_slice] = result
    except Exception as e:
        outstr += out_fn + '\n' + traceback.format_exc()
    # verify in case another process overwrote my changes
    count = 0
    while test_func(zf[out_slice], result):
        # try again
        try:
            zf[out_slice] = result
        except Exception as e:
            outstr += ':' + out_fn + '\n' + traceback.format_exc()
        count += 1
        if count > 5:
            outstr += ':' + out_fn + '\n' + 'OUTPUT IS NOT CORRECTLY WRITTEN TO FILE'
            raise Exception(outstr)

class ProcessManager(tl.HasTraits):
    _debug = tl.Bool(False)

    # Parameters controlling the computation
    grid_round_decimals = tl.Int(2)  # Number of decimals to round the lat/lon to for determining the size of the full dataset


    n_workers = tl.Int(1)
    in_path = tl.Unicode('.')
    out_format = tl.Enum(['zarr'], default_value='zarr')

    out_path = tl.Unicode()
    @tl.default('out_path')
    def _out_path_default(self):
        out = 'results'
        if self.out_format == 'zarr':
            out += '.zarr'
        return os.path.join(self.in_path, out)
    _INPUT_FILE_TYPES = tl.List(["tif", "tiff", "vrt", "hgt", 'flt', 'adf', 'grib',
                                 'grib2', 'grb', 'gr1'])

    elev_source_files = tl.List()
    @tl.default('elev_source_files')
    def _elev_source_files_default(self):
        esf =  [os.path.join(self.in_path, fn)
                  for fn in os.listdir(self.in_path)
                  if os.path.splitext(fn)[-1].replace('.', '')
                  in self._INPUT_FILE_TYPES]
        esf.sort()
        return esf

    out_file = tl.Any()  # only for .zarr cases
    @tl.default('out_file')
    def _out_file_default(self):
        return zarr.open(self.out_path, mode='a')

    def _i(self, name):
        ''' helper function for indexing self.index '''
        return ['left', 'bottom', 'right', 'top', 'dlon', 'dlat', 'nrows', 'ncols'].index(name)

    # Working attributes
    index = tt.Array()  # left, bottom, right, top, dlon, dlat, nrows, ncols
    @tl.default('index')
    def compute_index(self):
        index = np.zeros((len(self.elev_source_files), 8))
        for i, f in enumerate(self.elev_source_files):
            r = rasterio.open(f, 'r')
            index[i, :4] = np.array(r.bounds)
            index[i, 4] = r.transform.a
            index[i, 5] = r.transform.e
            index[i, 6:] = r.shape
        return index

    grid_id = tt.Array()
    grid_id2i = tt.Array()
    grid_slice = tl.List()
    grid_slice_unique = tl.List()
    grid_slice_noverlap = tl.List()
    grid_shape = tl.List()
    grid_lat_size = tt.Array()
    grid_lon_size = tt.Array()
    grid_lat_size_unique = tt.Array()
    grid_lon_size_unique = tt.Array()
    grid_size_tot = tl.List()
    grid_chunk = tl.List()
    edge_data = tl.List()


    # Properties
    @property
    def n_inputs(self):
        return len(self.elev_source_files)

    # Methods

    def compute_grid(self):
        # Get the unique lat/lons
        lats = np.round(self.index[:, self._i('top')], self.grid_round_decimals)
        lons = np.round(self.index[:, self._i('left')], self.grid_round_decimals)
        ulats = np.sort(np.unique(lats))[::-1].tolist()
        ulons = np.sort(np.unique(lons)).tolist()

        grid_shape = (len(ulats), len(ulons))
        grid_id = np.zeros((self.n_inputs, 3), dtype=int)
        grid_id2i = -np.ones(grid_shape, dtype=int)
        grid_lat_size = np.ones(len(ulats), dtype=int) * -1
        grid_lon_size = np.ones(len(ulons), dtype=int) * -1

        # Put the index of filename into the grid, and check that sizes are consistent
        for i in range(self.n_inputs):
            grid_id[i, :2] = [ulats.index(lats[i]), ulons.index(lons[i])]
            grid_id[i, 2] = grid_id[i, 1] + grid_id[i, 0] * grid_shape[1]
            grid_id2i[grid_id[i, 0], grid_id[i, 1]] = i

            # check sizes match
            if grid_lat_size[grid_id[i, 0]] < 0:
                grid_lat_size[grid_id[i, 0]] = self.index[i, self._i('nrows')]
            else:
                assert grid_lat_size[grid_id[i, 0]] == self.index[i, self._i('nrows')]

            if grid_lon_size[grid_id[i, 1]] < 0:
                grid_lon_size[grid_id[i, 1]] = self.index[i, self._i('ncols')]
            else:
                assert grid_lon_size[grid_id[i, 1]] == self.index[i, self._i('ncols')]

        # Figure out the slice indices in the zarr array for each elevation file
        grid_lat_size_cumulative = np.concatenate([[0], grid_lat_size.cumsum()])
        grid_lon_size_cumulative = np.concatenate([[0], grid_lon_size.cumsum()])
        grid_slice = []
        for i in range(self.n_inputs):
            ii = grid_id[i, 0]
            jj = grid_id[i, 1]
            slc = (slice(grid_lat_size_cumulative[ii], grid_lat_size_cumulative[ii + 1]),
                   slice(grid_lon_size_cumulative[jj], grid_lon_size_cumulative[jj + 1]))
            grid_slice.append(slc)

        self.grid_id = grid_id
        self.grid_id2i = grid_id2i
        self.grid_shape = grid_shape
        self.grid_lat_size = grid_lat_size
        self.grid_lon_size = grid_lon_size
        self.grid_size_tot = [int(grid_lat_size.sum()), int(grid_lon_size.sum())]
        self.grid_slice = grid_slice
        self.grid_chunk = [int(grid_lat_size.min()), int(grid_lon_size.min())]

    def _calc_overlap(self, a, da, b, db, s, tie):
        '''
        ...bbbb|bb       db = da/2
           a a |a . . .
        n_overlap_a = 1
        n_overlap_b = 4

        o
        o
        o
        ab
        ab
        ---
        ab
        o
        o
        o
        n_overlap_a = 1
        n_overlap_b = 2
        '''
        n_overlap_a = int(np.round(((b - a) / da + tie - 0.01) / 2))
        n_overlap_a = max(n_overlap_a, 1)  # max with 1 deals with the zero-overlap case
        n_overlap_b = int(np.round(((b - a) / db + 1 - tie - 0.01) / 2))
        n_overlap_b = max(n_overlap_b, 1)  # max with 1 deals with the zero-overlap case
        return n_overlap_a, n_overlap_b

    def compute_grid_overlaps(self):

        self.grid_slice_unique = []
        self.edge_data = []
        for i in range(self.n_inputs):
            id = self.grid_id[i]

            # Figure out the unique slices associated with each file
            slc = self.grid_slice[i]

            # do left-right overlaps
            if id[1] > 0:  # left
                left_id = self.grid_id2i[id[0], id[1] - 1]
                if left_id < 0:
                    lon_start = lon_start_e = 0
                else:
                    lon_start, lon_start_e = self._calc_overlap(
                        self.index[i, self._i('left')],
                        self.index[i, self._i('dlon')],
                        self.index[left_id, self._i('right')],
                        self.index[left_id, self._i('dlon')],
                        slc[1].start, tie=0)
            else:
                lon_start = lon_start_e = 0

            if id[1] < (self.grid_id2i.shape[1] - 1):  # right
                right_id = self.grid_id2i[id[0], id[1] + 1]
                if right_id < 0:
                    lon_end = lon_end_e = 0
                else:
                    lon_end, lon_end_e = self._calc_overlap(
                        self.index[right_id, self._i('left')],
                        self.index[i, self._i('dlon')],
                        self.index[i, self._i('right')],
                        self.index[right_id, self._i('dlon')],
                        slc[1].start, tie=1)
            else:
                lon_end = lon_end_e = 0

            # do top-bot overlaps
            if id[0] > 0:  # top
                top_id = self.grid_id2i[id[0] - 1, id[1]]
                if top_id < 0:
                    lat_start = lat_start_e = 0
                else:
                    lat_start, lat_start_e = self._calc_overlap(
                        self.index[i, self._i('top')],
                        self.index[i, self._i('dlat')],
                        self.index[top_id, self._i('bottom')],
                        self.index[top_id, self._i('dlat')],
                        slc[0].start, tie=0)
            else:
                lat_start = lat_start_e = 0
            if id[0] < (self.grid_id2i.shape[0] - 1):  # bottom
                bot_id = self.grid_id2i[id[0] + 1, id[1]]
                if bot_id < 0:
                    lat_end = lat_end_e = 0
                else:
                    lat_end, lat_end_e = self._calc_overlap(
                        self.index[bot_id, self._i('top')],
                        self.index[i, self._i('dlat')],
                        self.index[i, self._i('bottom')],
                        self.index[bot_id, self._i('dlat')],
                        slc[0].start, tie=1)
            else:
                lat_end = lat_end_e = 0

            self.grid_slice_unique.append((
                slice(slc[0].start + lat_start, slc[0].stop - lat_end),
                slice(slc[1].start + lon_start, slc[1].stop - lon_end)))

            # Figure out where to get edge data from
            edge_data = {
                    'left': (slc[0], slc[1].start - lon_start_e),
                    'right': (slc[0], slc[1].stop + lon_end_e - 1),
                    'top': (slc[0].start - lat_start_e, slc[1]),
                    'bottom': (slc[0].stop + lat_end_e - 1, slc[1]),
                        }
            self.edge_data.append(edge_data)

        # Figure out size of the non-overlapping arrays
        self.grid_lat_size_unique = np.array([
            self.grid_slice_unique[i][0].stop - self.grid_slice_unique[i][0].start
            for i in self.grid_id2i[:, 0]])
        self.grid_lon_size_unique = np.array([
            self.grid_slice_unique[i][1].stop - self.grid_slice_unique[i][1].start
            for i in self.grid_id2i[0, :]])

        # Figure out the slices for the non-overlapping arrays
        grid_lat_size_cumulative = np.concatenate([[0], self.grid_lat_size_unique.cumsum()])
        grid_lon_size_cumulative = np.concatenate([[0], self.grid_lon_size_unique.cumsum()])
        grid_slice = []
        for i in range(self.n_inputs):
            ii = self.grid_id[i, 0]
            jj = self.grid_id[i, 1]
            slc = (slice(grid_lat_size_cumulative[ii], grid_lat_size_cumulative[ii + 1]),
                   slice(grid_lon_size_cumulative[jj], grid_lon_size_cumulative[jj + 1]))
            grid_slice.append(slc)

        self.grid_slice_noverlap = grid_slice

        def compute_non_overlap_data(self, data, overlaps=[0, 0]):
            pass


    def process_elevation(self, indices=None):
        # TODO: Also find max, mean, and min elevation on edges
        # initialize zarr output
        out_file = os.path.join(self.out_path, 'elev')
        zf = zarr.open(out_file, shape=self.grid_size_tot, chunks=self.grid_chunk, mode='a', dtype=np.float32)
        out_file_success = os.path.join(self.out_path, 'success')
        success = zarr.open(out_file_success, shape=(self.n_inputs, 4), mode='a', dtype=bool)

        # populate kwds
        kwds = [dict(fn=self.elev_source_files[i],
                     out_fn=out_file, out_slice=self.grid_slice[i])
                for i in range(self.n_inputs)]

        success = self.queue_processes(calc_elev_cond, kwds, success[:, 0])
        self.out_file['success'][:, 0] = success
        return success

    def process_aspect_slope(self):
        # initialize zarr output
        out_aspect = os.path.join(self.out_path, 'aspect')
        zf = zarr.open(out_aspect, shape=self.grid_size_tot, chunks=self.grid_chunk, mode='a', dtype=np.float32)
        out_slope = os.path.join(self.out_path, 'slope')
        zf1 = zarr.open(out_slope, shape=self.grid_size_tot, chunks=self.grid_chunk, mode='a', dtype=np.float32)

        # populate kwds
        kwds = [dict(fn=self.elev_source_files[i],
                     out_fn_aspect=out_aspect,
                     out_fn_slope=out_slope,
                     out_fn=self.out_path,
                     out_slice=self.grid_slice[i])
                for i in range(self.n_inputs)]

        success = self.queue_processes(calc_aspect_slope, kwds, success=self.out_file['success'][:, 1])
        self.out_file['success'][:, 1] = success
        return success

    def process_uca(self):
        # initialize zarr output
        out_uca = os.path.join(self.out_path, 'uca')
        zf = zarr.open(out_uca, shape=self.grid_size_tot, chunks=self.grid_chunk, mode='a',
                dtype=np.float32)
        out_edge_done = os.path.join(self.out_path, 'edge_done')
        zf1 = zarr.open(out_edge_done, shape=self.grid_size_tot, chunks=self.grid_chunk, mode='a', dtype=np.bool)
        out_edge_todo = os.path.join(self.out_path, 'edge_todo')
        zf2 = zarr.open(out_edge_todo, shape=self.grid_size_tot, chunks=self.grid_chunk, mode='a', dtype=np.bool)

        # populate kwds
        kwds = [dict(fn=self.elev_source_files[i],
                     out_fn_uca=out_uca,
                     out_fn_todo=out_edge_todo,
                     out_fn_done=out_edge_done,
                     out_fn=self.out_path,
                     out_slice=self.grid_slice[i])
                for i in range(self.n_inputs)]

        success = self.queue_processes(calc_uca, kwds, success=self.out_file['success'][:, 2],
                intermediate_fun=self.compute_grid_overlaps)
        self.out_file['success'][:, 2] = success
        return success

    def update_uca_edge_metrics(self, out_uca=None, index=None):
        if index is None:
            index = range(self.n_inputs)
        if out_uca is None:
           out_uca = os.path.join(self.out_path, 'uca')
        else:
            out_uca = os.path.join(self.out_path, out_uca)
        out_edge_done = os.path.join(self.out_path, 'edge_done')
        out_edge_todo = os.path.join(self.out_path, 'edge_todo')
        out_metrics = os.path.join(self.out_path, 'uca_edge_metrics')
        zf = zarr.open(out_metrics, shape=(self.n_inputs, 2),
                mode='a', dtype=np.float32,
                fill_value=0)

        kwds = [dict(
                     fn_uca=out_uca,
                     fn_done=out_edge_done,
                     fn_todo=out_edge_todo,
                     slc=self.grid_slice[i],
                     edge_slc=self.edge_data[i])
                for i in index]

        metrics = self.queue_processes(calc_uca_ec_metrics, kwds)
        for i, ind in enumerate(index):
            zf[ind] = metrics[i]
        return zf[:]

    def process_uca_edges(self):
        out_uca = os.path.join(self.out_path, 'uca')
        out_edge_done = os.path.join(self.out_path, 'edge_done')
        out_edge_todo = os.path.join(self.out_path, 'edge_todo')

        # populate kwds
        kwds = [dict(fn=self.elev_source_files[i],
                     out_fn_uca=out_uca,
                     out_fn_todo=out_edge_todo,
                     out_fn_done=out_edge_done,
                     out_fn=self.out_path,
                     out_slice=self.grid_slice[i],
                     edge_slice=self.edge_data[i])
                for i in range(self.n_inputs)]
        # update metrics and decide who goes first
        mets = self.update_uca_edge_metrics('uca')
        if mets.shape[0] == 1:
            I = np.zeros(1, int)
        else:
            I = np.argpartition(-mets[:, 0], self.n_workers * 2)

        # Helper function for updating metrics
        def check_mets(finished):
            check_met_inds = []
            for f in finished:
                i, j, k = self.grid_id[f]
                check_met_inds.append(f)
                # left
                if j > 0: check_met_inds.append(self.grid_id2i[i, j-1])
                # right
                if j < self.grid_id2i.shape[1] - 1: check_met_inds.append(self.grid_id2i[i, j+1])
                # top
                if i > 0: check_met_inds.append(self.grid_id2i[i-1, j])
                # bottom
                if i < self.grid_id2i.shape[0] - 1: check_met_inds.append(self.grid_id2i[i+1, j])
            mets = self.update_uca_edge_metrics('uca', check_met_inds)
            if mets.shape[0] == 1:
                I = np.zeros(1, int)
            else:
                I = np.argpartition(-mets[:, 0], self.n_workers * 2)
            return mets, I

        # Non-parallel case (useful for debuggin)
        if self.n_workers == 1:
            I_old = np.zeros_like(I)
            while np.any(I_old != I):
                s = calc_uca_ec(**kwds[I[0]])
                I_old[:] = I[:]
                mets, I = check_mets([I[0]])
                if self._debug:
                    from matplotlib.pyplot import figure, subplot, pcolor, title, pause, axis, colorbar, clim, show
                    figure(figsize=(8, 4), dpi=200)
                    subplot(221)
                    pcolor(self.out_file['uca'][:])
                    title('uca [{}] --> [{}]'.format(I_old[0], I[0]))
                    axis('scaled')
                    subplot(222)
                    pcolor(self.out_file['edge_todo'][:] *2.0 + self.out_file['edge_done'][:] *1.0, cmap='jet')
                    title('edge_done (1) edge_todo (2)')
                    axis('scaled')
                    clim(0, 3)
                    colorbar()
                    subplot(223)
                    pcolor(self.out_file['edge_todo'][:] *2.0 + self.out_file['edge_done'][:] *1.0 + self.out_file['slope'][:], cmap='jet')
                    title('edge_done (1) edge_todo (2) + slope')
                    axis('scaled')
                    clim(0, 3)
                    colorbar()
                    subplot(224)
                    pcolor(self.out_file['aspect'][:]*180/np.pi, cmap='hsv')
                    title('aspect')
                    axis('scaled')
                    clim(0, 360)
                    colorbar()
                    show(True)
            return mets

        # create pool and queue
        pool = Pool(processes=self.n_workers)

        # submit workers
        active = I[:self.n_workers * 2].tolist()
        active = [a for a in active if mets[a, 0] > 0]
        res = [pool.apply_async(calc_uca_ec, kwds=kwds[i]) for i in active]
        print ("Starting with {}".format(active))
        while res:
            # monitor and submit new workers as needed
            finished = []
            finished_res = []
            for i, r in enumerate(res):
                try:
                    s = r.get(timeout=0.001)
                    finished.append(active[i])
                    finished_res.append(i)
                    #print(s)
                except (mpTimeoutError, TimeoutError) as e:
                    print(e)
                    pass
            if not finished: continue
            mets, I = check_mets(finished)
            active = [a for a in active if a not in finished]
            res = [r for i, r in enumerate(res) if i not in finished_res]
            candidates = I[:self.n_workers * 2].tolist()
            candidates = [c for c in candidates if c not in active and mets[c, 0] > 0][:len(finished)]
            res.extend([pool.apply_async(calc_uca_ec, kwds=kwds[i]) for i in candidates])
            active.extend(candidates)
            print ("Added {}, active {}".format(candidates, active))
            time.sleep(1)
        pool.close()
        pool.join()

        mets = self.update_uca_edge_metrics('uca')
        return mets

    def queue_processes(self, function, kwds, success=None, intermediate_fun=lambda:None):
        # initialize success if not created
        if success is None:
            success = [0] * len(kwds)

        # create pool and queue
        pool = Pool(processes=self.n_workers)

        # submit workers
        print ("Sumitting workers", end='...')
        #success = [function(**kwds[i])
        #        for i in range(self.n_inputs) if indices[i]]

        res = [pool.apply_async(function, kwds=kwd)
                for i, kwd in enumerate(kwds) if not success[i]]
        print(" waiting for computation")

        pool.close()  # prevent new tasks from being submitted

        intermediate_fun()

        pool.join()   # wait for tasks to finish

        for i, r in enumerate(res):
            s = r.get(timeout=0.0001)
            success[i] = s[0]
            print(s)
        return success

    def process_twi(self):
        print("Compute Grid")
        self.compute_grid()
        print("Compute Elevation")
        self.process_elevation()
        print("Compute Aspect and Slope")
        self.process_aspect_slope()
        print("Compute UCA")
        self.process_uca()
        print("Compute UCA Corrections")
        self.process_uca_edges()
