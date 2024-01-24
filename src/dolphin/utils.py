from __future__ import annotations

import datetime
import math
import resource
import sys
import warnings
from itertools import chain
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

import numpy as np
from numpy.typing import ArrayLike, DTypeLike
from osgeo import gdal, gdal_array, gdalconst
from rich.progress import MofNCompleteColumn, Progress, SpinnerColumn, TimeElapsedColumn

from dolphin._log import get_log
from dolphin._types import Bbox, Filename

DateOrDatetime = Union[datetime.date, datetime.datetime]

gdal.UseExceptions()
logger = get_log(__name__)


def progress(dummy=False):
    """Create a Progress bar context manager.

    Parameters
    ----------
    dummy : bool, default = False
        If True, skips showing and calls `contextlib.nullcontext`

    Usage
    -----
    >>> with progress() as p:
    ...     for i in p.track(range(10)):
    ...         pass
    10/10 Working... ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:00
    """

    class DummyProgress:
        """Context manager that does no additional processing.

        Needs a `track` method to match rich.Progress.
        """

        def track(self, iterable, **kwargs):
            yield from iterable

        def __enter__(self):
            return self

        def __exit__(self, *excinfo):
            pass

    if dummy:
        return DummyProgress()

    return Progress(
        SpinnerColumn(),
        MofNCompleteColumn(),
        *Progress.get_default_columns()[:-1],  # Skip the ETA column
        TimeElapsedColumn(),
    )


def numpy_to_gdal_type(np_dtype: DTypeLike) -> int:
    """Convert numpy dtype to gdal type.

    Parameters
    ----------
    np_dtype : DTypeLike
        Numpy dtype to convert.

    Returns
    -------
    int
        GDAL type code corresponding to `np_dtype`.

    Raises
    ------
    TypeError
        If `np_dtype` is not a numpy dtype, or if the provided dtype is not
        supported by GDAL (for example, `np.dtype('>i4')`)
    """
    np_dtype = np.dtype(np_dtype)

    if np.issubdtype(bool, np_dtype):
        return gdalconst.GDT_Byte
    gdal_code = gdal_array.NumericTypeCodeToGDALTypeCode(np_dtype)
    if gdal_code is None:
        msg = f"dtype {np_dtype} not supported by GDAL."
        raise TypeError(msg)
    return gdal_code


def gdal_to_numpy_type(gdal_type: Union[str, int]) -> np.dtype:
    """Convert gdal type to numpy type."""
    if isinstance(gdal_type, str):
        gdal_type = gdal.GetDataTypeByName(gdal_type)
    return np.dtype(gdal_array.GDALTypeCodeToNumericTypeCode(gdal_type))


def _get_path_from_gdal_str(name: Filename) -> Path:
    s = str(name)
    if s.upper().startswith("DERIVED_SUBDATASET"):
        # like DERIVED_SUBDATASET:AMPLITUDE:slc_filepath.tif
        p = s.split(":")[-1].strip('"').strip("'")
    elif ":" in s and (s.upper().startswith("NETCDF") or s.upper().startswith("HDF")):
        # like NETCDF:"slc_filepath.nc":subdataset
        p = s.split(":")[1].strip('"').strip("'")
    else:
        # Whole thing is the path
        p = str(name)
    return Path(p)


def _resolve_gdal_path(gdal_str: Filename) -> Filename:
    """Resolve the file portion of a gdal-openable string to an absolute path."""
    s_clean = str(gdal_str).lstrip('"').lstrip("'").rstrip('"').rstrip("'")
    prefixes = ["DERIVED_SUBDATASET", "NETCDF", "HDF"]
    is_gdal_str = any(s_clean.upper().startswith(pre) for pre in prefixes)
    file_part = str(_get_path_from_gdal_str(s_clean))

    # strip quotes to add back in after
    file_part = file_part.strip('"').strip("'")
    file_part_resolved = Path(file_part).resolve()
    resolved = s_clean.replace(file_part, str(file_part_resolved))
    return Path(resolved) if not is_gdal_str else resolved


def _get_slices(half_r: int, half_c: int, r: int, c: int, rows: int, cols: int):
    """Get the slices for the given pixel and half window size."""
    # Clamp min indexes to 0
    r_start = max(r - half_r, 0)
    c_start = max(c - half_c, 0)
    # Clamp max indexes to the array size
    r_end = min(r + half_r + 1, rows)
    c_end = min(c + half_c + 1, cols)
    return (r_start, r_end), (c_start, c_end)


def full_suffix(filename: Filename):
    """Get the full suffix of a filename, including multiple dots.

    Parameters
    ----------
    filename : str or Path
        path to file

    Returns
    -------
    str
        The full suffix, including multiple dots.

    Examples
    --------
        >>> full_suffix('test.tif')
        '.tif'
        >>> full_suffix('test.tar.gz')
        '.tar.gz'
    """
    fpath = Path(filename)
    return "".join(fpath.suffixes)


def gpu_is_available() -> bool:
    """Check if a GPU is available."""
    try:
        # cupy not available on Mac m1
        import cupy as cp  # noqa: F401
        from numba import cuda

    except ImportError:
        logger.debug("numba/cupy installed, but GPU not available")
        return False
    try:
        cuda_version = cuda.runtime.get_version()
        logger.debug(f"CUDA version: {cuda_version}")
    except OSError as e:
        logger.debug(f"CUDA runtime version error: {e}")
        return False
    try:
        n_gpu = len(cuda.gpus)
    except cuda.CudaSupportError as e:
        logger.debug(f"CUDA support error {e}")
        return False
    if n_gpu < 1:
        logger.debug("No available GPUs found")
        return False
    return True


def get_array_module(arr):
    """Get the array module (numpy or cupy) for the given array.

    References
    ----------
    https://docs.cupy.dev/en/stable/user_guide/basic.html#how-to-write-cpu-gpu-agnostic-code
    """
    try:
        import cupy as cp

        xp = cp.get_array_module(arr)
    except ImportError:
        logger.debug("cupy not installed, falling back to numpy")
        xp = np
    return xp


def take_looks(arr, row_looks, col_looks, func_type="nansum", edge_strategy="cutoff"):
    """Downsample a numpy matrix by summing blocks of (row_looks, col_looks).

    Parameters
    ----------
    arr : np.array
        2D array of an image
    row_looks : int
        the reduction rate in row direction
    col_looks : int
        the reduction rate in col direction
    func_type : str, optional
        the numpy function to use for downsampling, by default "nansum"
    edge_strategy : str, optional
        how to handle edges, by default "cutoff"
        Choices are "cutoff", "pad"

    Returns
    -------
    ndarray
        The downsampled array, shape = ceil(rows / row_looks, cols / col_looks)

    Notes
    -----
    Cuts off values if the size isn't divisible by num looks.
    Will use cupy if available and if `arr` is a cupy array on the GPU.
    """
    xp = get_array_module(arr)

    if row_looks == 1 and col_looks == 1:
        return arr

    if arr.ndim >= 3:
        return xp.stack(
            [
                take_looks(
                    a,
                    row_looks,
                    col_looks,
                    func_type=func_type,
                    edge_strategy=edge_strategy,
                )
                for a in arr
            ]
        )

    arr = _make_dims_multiples(arr, row_looks, col_looks, how=edge_strategy)

    rows, cols = arr.shape
    new_rows = rows // row_looks
    new_cols = cols // col_looks

    func = getattr(xp, func_type)
    with warnings.catch_warnings():
        # ignore the warning about nansum of empty slice
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return func(
            xp.reshape(arr, (new_rows, row_looks, new_cols, col_looks)), axis=(3, 1)
        )


def _make_dims_multiples(arr, row_looks, col_looks, how="cutoff"):
    """Pad or cutoff an array to make the dimensions multiples of the looks."""
    rows, cols = arr.shape
    row_cutoff = rows % row_looks
    col_cutoff = cols % col_looks
    if how == "cutoff":
        if row_cutoff != 0:
            arr = arr[:-row_cutoff, :]
        if col_cutoff != 0:
            arr = arr[:, :-col_cutoff]
        return arr
    elif how == "pad":
        pad_rows = (row_looks - row_cutoff) % row_looks
        pad_cols = (col_looks - col_cutoff) % col_looks
        pad_val = False if arr.dtype == bool else np.nan
        if pad_rows > 0 or pad_cols > 0:
            arr = np.pad(
                arr,
                ((0, pad_rows), (0, pad_cols)),
                mode="constant",
                constant_values=pad_val,
            )
        return arr
    else:
        msg = f"Invalid edge strategy: {how}"
        raise ValueError(msg)


def upsample_nearest(
    arr: np.ndarray,
    output_shape: tuple[int, int],
    looks: Optional[tuple[int, int]] = None,
) -> np.ndarray:
    """Upsample a numpy matrix by repeating blocks of (row_looks, col_looks).

    Parameters
    ----------
    arr : np.array
        2D or 3D downsampled array.
    output_shape : tuple[int, int]
        The desired output shape.
    looks : tuple[int, int]
        The number of looks in the row and column directions.
        If not provided, will be calculated from `output_shape`.

    Returns
    -------
    ndarray
        The upsampled array, shape = `output_shape`.

    Notes
    -----
    Will use cupy if available and if `arr` is a cupy array on the GPU.
    """
    xp = get_array_module(arr)
    in_rows, in_cols = arr.shape[-2:]
    out_rows, out_cols = output_shape[-2:]
    if (in_rows, in_cols) == (out_rows, out_cols):
        return arr

    if looks is None:
        row_looks = out_rows // in_rows
        col_looks = out_cols // in_cols
    else:
        row_looks, col_looks = looks

    arr_up = xp.repeat(xp.repeat(arr, row_looks, axis=-2), col_looks, axis=-1)
    # This may be larger than the original array, or it may be smaller, depending
    # on whether it was padded or cutoff
    out_r = min(out_rows, arr_up.shape[-2])
    out_c = min(out_cols, arr_up.shape[-1])

    shape = (len(arr), out_rows, out_cols) if arr.ndim == 3 else (out_rows, out_cols)
    arr_out = xp.zeros(shape=shape, dtype=arr.dtype)
    arr_out[..., :out_r, :out_c] = arr_up[..., :out_r, :out_c]
    return arr_out


def decimate(arr: ArrayLike, strides: dict[str, int]) -> ArrayLike:
    """Decimate an array by strides in the x and y directions.

    Output will match [`io.compute_out_shape`][dolphin.io.compute_out_shape]

    Parameters
    ----------
    arr : ArrayLike
        2D or 3D array to decimate.
    strides : dict[str, int]
        The strides in the x and y directions.

    Returns
    -------
    ArrayLike
        The decimated array.

    """
    xs, ys = strides["x"], strides["y"]
    rows, cols = arr.shape[-2:]
    start_r = ys // 2
    start_c = xs // 2
    end_r = (rows // ys) * ys + 1
    end_c = (cols // xs) * xs + 1
    return arr[..., start_r:end_r:ys, start_c:end_c:xs]


def get_max_memory_usage(units: str = "GB", children: bool = True) -> float:
    """Get the maximum memory usage of the current process.

    Parameters
    ----------
    units : str, optional, choices=["GB", "MB", "KB", "byte"]
        The units to return, by default "GB".
    children : bool, optional
        Whether to include the memory usage of child processes, by default True

    Returns
    -------
    float
        The maximum memory usage in the specified units.

    Raises
    ------
    ValueError
        If the units are not recognized.

    References
    ----------
    1. https://stackoverflow.com/a/7669279/4174466
    2. https://unix.stackexchange.com/a/30941/295194
    3. https://manpages.debian.org/bullseye/manpages-dev/getrusage.2.en.html

    """
    max_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if children:
        max_mem += resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if units.lower().startswith("g"):
        factor = 1e9
    elif units.lower().startswith("m"):
        factor = 1e6
    elif units.lower().startswith("k"):
        factor = 1e3
    elif units.lower().startswith("byte"):
        factor = 1.0
    else:
        msg = f"Unknown units: {units}"
        raise ValueError(msg)
    if sys.platform.startswith("linux"):
        # on linux, ru_maxrss is in kilobytes, while on mac, ru_maxrss is in bytes
        factor /= 1e3

    return max_mem / factor


def get_gpu_memory(pid: Optional[int] = None, gpu_id: int = 0) -> float:
    """Get the memory usage (in GiB) of the GPU for the current pid."""
    try:
        from pynvml.smi import nvidia_smi
    except ImportError as e:
        msg = "Please install pynvml through pip or conda"
        raise ImportError(msg) from e

    def get_mem(process):
        used_mem = process["used_memory"] if process else 0
        if process["unit"] == "MiB":
            multiplier = 1 / 1024
        else:
            logger.warning(f"Unknown unit: {process['unit']}")
        return used_mem * multiplier

    nvsmi = nvidia_smi.getInstance()
    processes = nvsmi.DeviceQuery()["gpu"][gpu_id]["processes"]
    if not processes:
        return 0.0

    if pid is None:
        # Return sum of all processes
        return sum(get_mem(p) for p in processes)
    else:
        procs = [p for p in processes if p["pid"] == pid]
        return get_mem(procs[0]) if procs else 0.0


def moving_window_mean(
    image: ArrayLike, size: Union[int, tuple[int, int]]
) -> np.ndarray:
    """Calculate the mean of a moving window of size `size`.

    Parameters
    ----------
    image : ndarray
        input image
    size : int or tuple of int
        Window size. If a single int, the window is square.
        If a tuple of (row_size, col_size), the window can be rectangular.

    Returns
    -------
    ndarray
        image the same size as `image`, where each pixel is the mean
        of the corresponding window.
    """
    if isinstance(size, int):
        size = (size, size)
    if len(size) != 2:
        msg = "size must be a single int or a tuple of 2 ints"
        raise ValueError(msg)
    if size[0] % 2 == 0 or size[1] % 2 == 0:
        msg = "size must be odd in both dimensions"
        raise ValueError(msg)

    row_size, col_size = size
    row_pad = row_size // 2
    col_pad = col_size // 2

    # Pad the image with zeros
    image_padded = np.pad(
        image, ((row_pad + 1, row_pad), (col_pad + 1, col_pad)), mode="constant"
    )

    # Calculate the cumulative sum of the image
    integral_img = np.cumsum(np.cumsum(image_padded, axis=0), axis=1)
    if not np.iscomplexobj(integral_img):
        integral_img = integral_img.astype(float)

    # Calculate the mean of the moving window
    # Uses the algorithm from https://en.wikipedia.org/wiki/Summed-area_table
    window_mean = (
        integral_img[row_size:, col_size:]
        - integral_img[:-row_size, col_size:]
        - integral_img[row_size:, :-col_size]
        + integral_img[:-row_size, :-col_size]
    )
    window_mean /= row_size * col_size
    return window_mean


def set_num_threads(num_threads: int):
    """Set the cap on threads spawned by numpy and numba.

    Uses https://github.com/joblib/threadpoolctl for numpy.
    """
    import numba
    from threadpoolctl import ThreadpoolController

    # Set the environment variables for the workers
    controller = ThreadpoolController()
    controller.limit(limits=num_threads)
    # https://numba.readthedocs.io/en/stable/user/threading-layer.html#example-of-limiting-the-number-of-threads
    numba.set_num_threads(num_threads)


def get_cpu_count():
    """Get the number of CPUs available to the current process.

    This function accounts for the possibility of a Docker container with
    limited CPU resources on a larger machine (which is ignored by
    `multiprocessing.cpu_count()`).

    Returns
    -------
    int
        The number of CPUs available to the current process.

    References
    ----------
    1. https://github.com/joblib/loky/issues/111
    2. https://github.com/conan-io/conan/blob/982a97041e1ece715d157523e27a14318408b925/conans/client/tools/oss.py#L27 # noqa
    """  # noqa: E501

    def get_cpu_quota():
        return int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())

    def get_cpu_period():
        return int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())

    try:
        cfs_quota_us = get_cpu_quota()
        cfs_period_us = get_cpu_period()
        if cfs_quota_us > 0 and cfs_period_us > 0:
            return int(math.ceil(cfs_quota_us / cfs_period_us))
    except Exception:
        pass
    return cpu_count()


def flatten(list_of_lists: Iterable[Iterable[Any]]) -> chain[Any]:
    """Flatten one level of a nested iterable."""
    return chain.from_iterable(list_of_lists)


def _format_date_pair(start: DateOrDatetime, end: DateOrDatetime, fmt="%Y%m%d") -> str:
    return f"{start.strftime(fmt)}_{end.strftime(fmt)}"


def prepare_geometry(
    geometry_dir: Path,
    geo_files: Sequence[Path],
    matching_file: Path,
    dem_file: Optional[Path],
    epsg: int,
    out_bounds: Bbox,
    strides: Optional[dict[str, int]] = None,
) -> dict[str, Path]:
    """Prepare geometry files.

    Parameters
    ----------
    geometry_dir : Path
        Output directory for geometry files.
    geo_files : list[Path]
        list of geometry files.
    matching_file : Path
        Matching file.
    dem_file : Optional[Path]
        DEM file.
    epsg : int
        EPSG code.
    out_bounds : Bbox
        Output bounds.
    strides : Dict[str, int], optional
        Strides for resampling, by default {"x": 1, "y": 1}.

    Returns
    -------
    Dict[str, Path]
        Dictionary of prepared geometry files.
    """
    from dolphin import stitching
    from dolphin.io import format_nc_filename

    if strides is None:
        strides = {"x": 1, "y": 1}
    geometry_dir.mkdir(exist_ok=True)

    stitched_geo_list = {}

    if geo_files[0].name.endswith(".h5"):
        # ISCE3 geocoded SLCs
        datasets = ["los_east", "los_north"]

        for ds_name in datasets:
            outfile = geometry_dir / f"{ds_name}.tif"
            logger.info(f"Creating {outfile}")
            stitched_geo_list[ds_name] = outfile
            ds_path = f"/data/{ds_name}"
            cur_files = [format_nc_filename(f, ds_name=ds_path) for f in geo_files]

            no_data = 0

            stitching.merge_images(
                cur_files,
                outfile=outfile,
                driver="GTiff",
                out_bounds=out_bounds,
                out_bounds_epsg=epsg,
                in_nodata=no_data,
                out_nodata=no_data,
                target_aligned_pixels=True,
                strides=strides,
                overwrite=False,
            )

        if dem_file:
            height_file = geometry_dir / "height.tif"
            stitched_geo_list["height"] = height_file
            if not height_file.exists():
                logger.info(f"Creating {height_file}")
                stitching.warp_to_match(
                    input_file=dem_file,
                    match_file=matching_file,
                    output_file=height_file,
                    resample_alg="cubic",
                )
    else:
        # ISCE2 radar coordinates
        dsets = {
            "hgt.rdr": "height",
            "incLocal.rdr": "incidence_angle",
            "lat.rdr": "latitude",
            "lon.rdr": "longitude",
        }

        for geo_file in geo_files:
            if geo_file.stem in dsets:
                out_name = dsets[geo_file.stem]
            elif geo_file.name in dsets:
                out_name = dsets[geo_file.name]
                continue

            out_file = geometry_dir / (out_name + ".tif")
            stitched_geo_list[out_name] = out_file
            logger.info(f"Creating {out_file}")

            stitching.warp_to_match(
                input_file=geo_file,
                match_file=matching_file,
                output_file=out_file,
                resample_alg="cubic",
            )

    return stitched_geo_list
