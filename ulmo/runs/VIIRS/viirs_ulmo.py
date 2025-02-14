""" Module for Ulmo analysis on VIIRS"""
import os
import glob
import numpy as np
import subprocess

import pandas
import h5py
from skimage.restoration import inpaint

from sklearn.utils import shuffle

from ulmo import io as ulmo_io
from ulmo.preproc import io as pp_io
from ulmo.preproc import utils as pp_utils
from ulmo.viirs import extract as viirs_ext
from ulmo.modis import utils as modis_utils
from ulmo.analysis import evaluate as ulmo_evaluate
from ulmo.utils import catalog as cat_utils

import argparse


from functools import partial
from concurrent.futures import ProcessPoolExecutor
import subprocess
from tqdm import tqdm

from IPython import embed

#tbl_file_2014 = 's3://viirs/Tables/VIIRS_2014_std.parquet'
s3_bucket = 's3://viirs'


def parse_option():
    """
    This is a function used to parse the arguments in the training.

    Returns:
        args: (dict) dictionary of the arguments.
    """
    parser = argparse.ArgumentParser("VIIRS")
    parser.add_argument("--task", type=str,
                        help="task to execute: 'download', 'extract', 'preproc', 'eval'.")
    parser.add_argument("--year", type=int, help="Year to work on")
    parser.add_argument("--n_cores", type=int, help="Number of CPU to use")
    parser.add_argument("--day", type=int, default=1, help="Day to start extraction from")
    args = parser.parse_args()

    return args


def viirs_get_data_into_s3(year=2014, day1=1, 
                           debug=False):
    """Use wget to download data into s3

    Args:
        year (int, optional): year to download. Defaults to 2014.
        day1 (int, optional): day to start with (in case you restart). Defaults to 1.
    """
    # Check that the PODAAC password exists
    #assert os.getenv('PO_DAAC') is not None
    # Loop on days

    pushed_files = []
    nc_files = None

    # push to s3
    def push_to_s3(nc_files, sday, year):
        for nc_file in nc_files:
            s3_file = os.path.join(s3_bucket, 'data', str(year),
                                   sday, nc_file)
            ulmo_io.upload_file_to_s3(nc_file, s3_file)
            # Remove
            os.remove(nc_file)

    # for ss in range(365):
    ndays = 366
    for ss in range(day1-1, ndays):
        iday = ss + 1
        print("Working on day: {}".format(iday))
        sday = str(iday).zfill(3)
        # Popen
        pw = subprocess.Popen([
            'wget', '--no-check-certificate', '--user=mskelm',
            '--password=B3oNdVKkgWW0NmPoLyS',
            '-r', '-nc', '-np',  '-nH', '-nd', '-A',
            '{}*.nc'.format(str(year)),
            # '*.nc',
            'https://podaac-tools.jpl.nasa.gov/drive/files/allData/ghrsst/data/GDS2/L2P/VIIRS_NPP/OSPO/v2.61/{}/{}/'.format(
                year, sday)])
        if ss == day1-1:
            pass
        else:
            if len(nc_files) > 0:
                push_to_s3(nc_files, pvday, year)
        # Wait now
        pw.wait()
        # Files
        nc_files = glob.glob('{}*.nc'.format(year))
        nc_files.sort()
        pvday = sday
    # Last batch
    print("Pushing last batch")
    if len(nc_files) > 0:
        push_to_s3(nc_files, pvday, year)


def viirs_extract(debug=False, year=2014,
                  n_cores=20, nsub_files=5000,
                  ndebug_files=0):
    """Extract "cloud free" images for a given year

    Args:
        debug (bool, optional): [description]. Defaults to False.
        n_cores (int, optional): Number of cores to use. Defaults to 20.
        nsub_files (int, optional): Number of sub files to process at a time. Defaults to 5000.
        ndebug_files (int, optional): [description]. Defaults to 0.
    """
    # 10 cores took 6hrs
    # 20 cores took 3hrs
    tbl_file = f's3://viirs/Tables/VIIRS_{year}_std.parquet'

    # Pre-processing (and extraction) settings
    pdict = pp_io.load_options('viirs_std')

    #
    print("Grabbing the file list")
    all_viirs_files = ulmo_io.list_of_bucket_files('viirs', 
                                                   prefix=f'data/{year}')
    files = []
    bucket = 's3://viirs/'
    for ifile in all_viirs_files:
        if f'data/{year}' in ifile:
            files.append(bucket+ifile)

    # Output
    save_path = (f'VIIRS_{year}'
                 '_{}clear_{}x{}_inpaint.h5'.format(pdict['clear_threshold'],
                                                    pdict['field_size'],
                                                    pdict['field_size']))
    s3_filename = 's3://viirs/Extractions/{}'.format(save_path)

    # Setup for preproc
    map_fn = partial(viirs_ext.extract_file,
                     field_size=(pdict['field_size'], pdict['field_size']),
                     CC_max=1.-pdict['clear_threshold'] / 100.,
                     nadir_offset=pdict['nadir_offset'],
                     temp_bounds=tuple(pdict['temp_bounds']),
                     nrepeat=pdict['nrepeat'],
                     sub_grid_step=pdict['sub_grid_step'],
                     inpaint=True)

    # Local file for writing
    f_h5 = h5py.File(save_path, 'w')
    print("Opened local file: {}".format(save_path))

    nloop = len(files) // nsub_files + ((len(files) % nsub_files) > 0)
    metadata = None
    for kk in range(nloop):
        # Zero out
        fields, inpainted_masks = None, None
        #
        i0 = kk*nsub_files
        i1 = min((kk+1)*nsub_files, len(files))
        print('Files: {}:{} of {}'.format(i0, i1, len(files)))
        sub_files = files[i0:i1]

        with ProcessPoolExecutor(max_workers=n_cores) as executor:
            chunksize = len(
                sub_files) // n_cores if len(sub_files) // n_cores > 0 else 1
            answers = list(tqdm(executor.map(map_fn, sub_files,
                                             chunksize=chunksize), total=len(sub_files)))

        # Trim None's
        answers = [f for f in answers if f is not None]
        fields = np.concatenate([item[0] for item in answers])
        inpainted_masks = np.concatenate([item[1] for item in answers])
        if metadata is None:
            metadata = np.concatenate([item[2] for item in answers])
        else:
            metadata = np.concatenate([metadata]+[item[2]
                                                  for item in answers], axis=0)
        del answers

        # Write
        if kk == 0:
            f_h5.create_dataset('fields', data=fields,
                                compression="gzip", chunks=True,
                                maxshape=(None, fields.shape[1], fields.shape[2]))
            f_h5.create_dataset('inpainted_masks', data=inpainted_masks,
                                compression="gzip", chunks=True,
                                maxshape=(None, inpainted_masks.shape[1], inpainted_masks.shape[2]))
        else:
            # Resize
            for key in ['fields', 'inpainted_masks']:
                f_h5[key].resize(
                    (f_h5[key].shape[0] + fields.shape[0]), axis=0)
            # Fill
            f_h5['fields'][-fields.shape[0]:] = fields
            f_h5['inpainted_masks'][-fields.shape[0]:] = inpainted_masks

        del fields, inpainted_masks

    # Metadata
    columns = ['filename', 'row', 'column', 'latitude', 'longitude',
               'clear_fraction']
    dset = f_h5.create_dataset('metadata', data=metadata.astype('S'))
    dset.attrs['columns'] = columns
    # Close
    f_h5.close()

    # Table time
    viirs_table = pandas.DataFrame()
    viirs_table['filename'] = [item[0] for item in metadata]
    viirs_table['row'] = [int(item[1]) for item in metadata]
    viirs_table['col'] = [int(item[2]) for item in metadata]
    viirs_table['lat'] = [float(item[3]) for item in metadata]
    viirs_table['lon'] = [float(item[4]) for item in metadata]
    viirs_table['clear_fraction'] = [float(item[5]) for item in metadata]
    viirs_table['field_size'] = pdict['field_size']
    basefiles = [os.path.basename(ifile)
                 for ifile in viirs_table.filename.values]
    viirs_table['datetime'] = modis_utils.times_from_filenames(
        basefiles, ioff=-1, toff=0)
    viirs_table['ex_filename'] = s3_filename

    # Vet
    assert cat_utils.vet_main_table(viirs_table)

    # Final write
    ulmo_io.write_main_table(viirs_table, tbl_file)

    # Push to s3
    print("Pushing to s3")
    ulmo_io.upload_file_to_s3(save_path, s3_filename)
    # print("Run this:  s3 put {} s3://modis-l2/Extractions/{}".format(
    #    save_path, save_path))
    # process = subprocess.run(['s4cmd', '--force', '--endpoint-url',
    #    'https://s3.nautilus.optiputer.net', 'put', save_path,
    #    s3_filename])


def viirs_preproc(year=2014, debug=False, n_cores=20):
    """Pre-process the files

    Args:
        n_cores (int, optional): Number of cores to use
    """
    # Table
    tbl_file = f's3://viirs/Tables/VIIRS_{year}_std.parquet'
    viirs_tbl = ulmo_io.load_main_table(tbl_file)
    viirs_tbl = pp_utils.preproc_tbl(viirs_tbl, 1.,
                                     's3://viirs',
                                     preproc_root='viirs_std',
                                     inpainted_mask=True,
                                     use_mask=True,
                                     nsub_fields=10000,
                                     n_cores=n_cores)
    # Vet
    assert cat_utils.vet_main_table(viirs_tbl)

    # Final write
    ulmo_io.write_main_table(viirs_tbl, tbl_file)


def viirs_evaluate(year=2014, debug=False, model='modis-l2-std'):
    """Evaluate the VIIRS data using Ulmo

    Args:
        debug (bool, optional): [description]. Defaults to False.
        model (str, optional): [description]. Defaults to 'modis-l2-std'.
    """
    # Load table
    tbl_file = f's3://viirs/Tables/VIIRS_{year}_std.parquet'
    viirs_tbl = ulmo_io.load_main_table(tbl_file)

    # Evaluate
    print("Starting evaluating..")
    viirs_tbl = ulmo_evaluate.eval_from_main(viirs_tbl, 
                                             model=model)

    # Write
    assert cat_utils.vet_main_table(viirs_tbl)
    ulmo_io.write_main_table(viirs_tbl, tbl_file)
    print("Done evaluating..")


if __name__ == "__main__":
    # get the argument of training.
    pargs = parse_option()

    if pargs.task == 'download':
        print("Downloading Starts.")
        viirs_get_data_into_s3(year=pargs.year, 
                               day1=pargs.day)
        print("Downloading Ends.")
    elif pargs.task == 'extract':
        print("Extraction Starts.")
        viirs_extract(year=pargs.year,
                      n_cores=pargs.n_cores)
        print("Extraction Ends.")
    elif pargs.task == 'preproc':
        print("PreProc Starts.")
        viirs_preproc(year=pargs.year,
                      n_cores=pargs.n_cores)
        print("PreProc Ends.")
    elif pargs.task == 'eval':
        print("Evaluation Starts.")
        viirs_evaluate(year=pargs.year)
        print("Evaluation Ends.")
    else:
        raise IOError("Bad choice")
