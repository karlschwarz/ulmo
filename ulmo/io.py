""" Basic I/O methods"""

import os
import numpy as np
import xarray as xr

import json
import pandas
import h5py 
from urllib.parse import urlparse
from io import BytesIO

# DO NOT IMOPRT ANY ULMO!

# s3
import smart_open
import boto3
import functools

endpoint_url = (os.getenv('ENDPOINT_URL') 
                if os.getenv('ENDPOINT_URL') is not None else 
                    'http://rook-ceph-rgw-nautiluss3.rook')

s3 = boto3.resource('s3', endpoint_url=endpoint_url)
client = boto3.client('s3', endpoint_url=endpoint_url)
tparams = {'client': client}
open = functools.partial(smart_open.open, 
                         transport_params=tparams)

import boto3
class Params():
    """Class that loads hyperparameters from a json file.
    Example:
    ```
    params = Params(json_path)
    print(params.learning_rate)
    params.learning_rate = 0.5  # change the value of learning_rate in params
    ```
    This module comes from:
    https://github.com/cs230-stanford/cs230-code-examples/blob/master/pytorch/vision/utils.py
    """

    def __init__(self, json_path):
        with open(json_path) as f:
            params = json.load(f)
            self.__dict__.update(params)

    def save(self, json_path):
        with open(json_path, 'w') as f:
            json.dump(self.__dict__, f, indent=4)
            
    def update(self, json_path):
        """Loads parameters from json file"""
        with open(json_path) as f:
            params = json.load(f)
            self.__dict__.update(params)

    @property
    def dict(self):
        """Gives dict-like access to Params instance by `params.dict['learning_rate']"""
        return self.__dict__

def grab_cutout(cutout:pandas.core.series.Series, 
               close=True, pp_hf=None):                
    """Grab the pre-processed image of a cutout

    Args:
        cutout (pandas.core.series.Series): cutout
        close (bool, optional): If True, close the file afterwards. Defaults to True.
        pp_hf ([type], optional): [description]. Defaults to None.

    Returns:
        np.ndarray: Image of the cutout
    """
    # Open?
    if pp_hf is None:
        with open(cutout.pp_file, 'rb') as f:
            pp_hf = h5py.File(f, 'r')
    img = pp_hf['valid'][cutout.pp_idx, 0, ...]

    # Close?
    if close:
        pp_hf.close()
        return img
    else:
        return img, pp_hf


def list_of_bucket_files(inp:str, prefix='/', delimiter='/'):
    """Generate a list of files in the bucket

    Args:
        inp (str): name of bucket or full s3 path
        prefix (str, optional): Folder(s) path. Defaults to '/'.
        delimiter (str, optional): [description]. Defaults to '/'.

    Returns:
        list: List of files without s3 bucket prefix
    """
    if inp[0:2] == 's3':
        parsed_s3 = urlparse(inp)
        bucket_name = parsed_s3.netloc
        prefix = parsed_s3.path
    else:
        bucket_name = inp
    # Do it        
    prefix = prefix[1:] if prefix.startswith(delimiter) else prefix
    bucket = s3.Bucket(bucket_name)
    return list(_.key for _ in bucket.objects.filter(Prefix=prefix))                                

def load_nc(filename, field='SST', verbose=True):
    """
    Load a MODIS or equivalent .nc file
    Does not work for VIIRS
    Does not work for s3

    Parameters
    ----------
    filename : str
    field : str, optional
    verbose : bool, optional

    Returns
    -------
    field, qual, latitude, longitude : np.ndarray, np.ndarray, np.ndarray np.ndarray
        Temperture map
        Quality
        Latitutides
        Longitudes

    or None's if the data is corrupt!

    """
    raise DeprecationWarning("Use ulmo.modis.io.load_nc instead")
    geo = xr.open_dataset(
        filename_or_obj=filename,
        group='geophysical_data',
        engine='h5netcdf',
        mask_and_scale=True)
    nav = xr.open_dataset(
        filename_or_obj=filename,
        group='navigation_data',
        engine='h5netcdf',
        mask_and_scale=True)

    # Translate user field to MODIS
    mfields = dict(SST='sst', aph_443='aph_443_giop')

    # Flags
    mflags = dict(SST='qual_sst', aph_443='l2_flags')

    # Go for it
    try:
        # Fails if data is corrupt
        dfield = np.array(geo[mfields[field]])
        qual = np.array(geo[mflags[field]])
        latitude = np.array(nav['latitude'])
        longitude = np.array(nav['longitude'])
    except:
        if verbose:
            print("Data is corrupt!")
        return None, None, None, None

    geo.close()
    nav.close()

    # Return
    return dfield, qual, latitude, longitude

def load_main_table(tbl_file:str, verbose=True):
    """Load the table of cutouts 

    Args:
        tbl_file (str): Path to table of cutouts. Local or s3
        verbose (bool, optional): [description]. Defaults to True.

    Raises:
        IOError: [description]

    Returns:
        pandas.DataFrame: table of cutouts
    """
    _, file_extension = os.path.splitext(tbl_file)

    # s3?
    if tbl_file[0:5] == 's3://':
        inp = load_to_bytes(tbl_file)
    else:
        inp = tbl_file
        
    # Allow for various formats
    if file_extension == '.csv':
        main_table = pandas.read_csv(inp, index_col=0)
        # Set time
        if 'datetime' in main_table.keys():
            main_table.datetime = pandas.to_datetime(main_table.datetime)
    elif file_extension == '.feather':
        # Allow for s3
        main_table = pandas.read_feather(inp)
    elif file_extension == '.parquet':
        # Allow for s3
        main_table = pandas.read_parquet(inp)
    else:
        raise IOError("Bad table extension: ")
    # Report
    if verbose:
        print("Read main table: {}".format(tbl_file))
    return main_table

def load_to_bytes(s3_uri:str):
    """Load s3 file into memory as a Bytes object

    Args:
        s3_uri (str): Full s3 path

    Returns:
        BytesIO: object in memory
    """
    parsed_s3 = urlparse(s3_uri)
    f = BytesIO()
    s3.meta.client.download_fileobj(parsed_s3.netloc, 
                                    parsed_s3.path[1:], f)
    f.seek(0)
    return f


def download_file_from_s3(local_file:str, s3_uri:str, 
                          clobber_local=True, verbose=True):
    """ Grab an s3 file

    Args:
        local_file (str): 
            Name of file to be dropped on local drive
        s3_uri (str): 
            Full s3 path
        clobber_local (bool, optional): [description]. Defaults to True.
    """
    parsed_s3 = urlparse(s3_uri)
    # Download preproc file for speed
    if not os.path.isfile(local_file) or clobber_local:
        if verbose:
            print("Downloading from s3: {}".format(local_file))
        s3.Bucket(parsed_s3.netloc).download_file(
            parsed_s3.path[1:], local_file)
        if verbose:
            print("Done!")
    
def upload_file_to_s3(local_file:str, s3_uri:str):
    """Upload a single file to s3 storage

    Args:
        local_file (str): path to local file
        s3_uri (str): URL for s3 file 
    """
    # https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3-uploading-files.html
    parsed_s3 = urlparse(s3_uri)
    s3.meta.client.upload_file(local_file,
                             parsed_s3.netloc, 
                             parsed_s3.path[1:])
    print("Uploaded {} to {}".format(local_file, s3_uri))
    
def write_bytes_to_local(bytes_:BytesIO, outfile:str):
    """Write a binary object to disk

    Args:
        bytes_ (BytesIO): contains the binary object
        outfile (str): [description]
    """
    bytes_.seek(0)
    with open(outfile, 'wb') as f:
        f.write(bytes_.getvalue())


def write_bytes_to_s3(bytes_:BytesIO, s3_uri:str):
    """Write bytes to s3 

    Args:
        bytes_ (BytesIO): contains the binary object
        s3_uri (str): Path to s3 bucket including filename
    """
    bytes_.seek(0)
    # Do it
    parsed_s3 = urlparse(s3_uri)
    s3.meta.client.upload_fileobj(Fileobj=bytes_, 
                             Bucket=parsed_s3.netloc, 
                             Key=parsed_s3.path[1:])

def write_main_table(main_table:pandas.DataFrame, outfile:str, to_s3=True):
    """Write Main table for ULMO analysis
    Format is determined from the outfile extension.
        Options are ".csv", ".feather", ".parquet"

    Args:
        main_table (pandas.DataFrame): Main table for ULMO analysis
        outfile (str): Output filename.  Its extension sets the format
        to_s3 (bool, optional): If True, write to s3

    Raises:
        IOError: [description]
    """
    _, file_extension = os.path.splitext(outfile)
    if file_extension == '.csv':
        main_table.to_csv(outfile, date_format='%Y-%m-%d %H:%M:%S')
    elif file_extension == '.feather':
        bytes_ = BytesIO()
        main_table.to_feather(path=bytes_)
        if to_s3:
            write_bytes_to_s3(bytes_, outfile)
        else:
            write_bytes_to_local(bytes_, outfile)
    elif file_extension == '.parquet':
        bytes_ = BytesIO()
        main_table.to_parquet(path=bytes_)
        if to_s3:
            write_bytes_to_s3(bytes_, outfile)
        else:
            write_bytes_to_local(bytes_, outfile)
    else:
        raise IOError("Not ready for this")
    print("Wrote Analysis Table: {}".format(outfile))