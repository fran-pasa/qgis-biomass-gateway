"""
Module to manage GDAL cache memory, C++ dataset teardown,
overview guardrails and PyQGIS raster resampling guardrails
"""

from __future__ import annotations

import ctypes
import gc
import os
import sys

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from osgeo import gdal

from .logger import get_logger, log_qgis_info, log_qgis_warning

# QgsRasterLayer might not be loaded when running pytest runs, as well as Qt
# wrapping them in this if and try blocks, in combination with __future__, allows us to avoid
# runtime import issues
if TYPE_CHECKING:
    from qgis.core import QgsRasterLayer

try:
    from qgis.PyQt.QtWidgets import QApplication
except ImportError:
    QApplication = None


logger = get_logger("core.memory")


# pyramid levels (used later for overviews)
DEFAULT_OVERVIEW_LEVELS = (2, 4, 8, 16, 32, 64)

# memory thresholds
_EIGHT_GIB = 8 * 1024 * 1024 * 1024
_CACHE_LOW_RAM_MB = 256
_CACHE_HIGH_RAM_MB = 512


def get_system_ram() -> int | None:
    """Detect the system RAM in bytes
    """
    try:
        # Linux and MacOS: query POSIX kernel page size and page count
        if hasattr(os, "sysconf"):
            names = os.sysconf_names
            if "SC_PAGE_SIZE" in names and "SC_PHYS_PAGES" in names:
                page_size = int(os.sysconf("SC_PAGE_SIZE"))
                phys_pages = int(os.sysconf("SC_PHYS_PAGES"))
                if page_size > 0 and phys_pages > 0:
                    return page_size * phys_pages

        # for Windows we need to call Win32 GlobalMemoryStatusEx via ctypes
        if sys.platform == "win32":
            class _MemoryStatusEx(ctypes.Structure):
                __fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)): # type: ignore[attr-defined]
                return int(stat.ullTotalPhys)

    except (OSError, AttributeError) as e:
        logger.debug("Could not detect system RAM size: %s", e)

    return None


def configure_gdal_cache(max_mb: int | None = None) -> int:
    """Apply an adaptive ceiling to the global GDAL cache.

    Select 256 mb on machines with less than 8 gb RAM (or unknown RAM),
    and 512 mb on machines with more.
    """
    if max_mb is None:
        total_ram = get_system_ram()
        if total_ram is not None and total_ram > _EIGHT_GIB:
            target_mb = _CACHE_HIGH_RAM_MB
        else:
            target_mb = _CACHE_LOW_RAM_MB
    else:
        target_mb = max(64, int(max_mb))

    cache_bytes = target_mb * 1024 * 1024

    try:
        gdal.SetCacheMax(cache_bytes)
        logger.info("GDAL cache capped at %d MB (%d bytes)", target_mb, cache_bytes)
    except (RuntimeError, OverflowError) as e:
        logger.warning("Failed to set GDAL cache limit: %s", e)

    return cache_bytes


def safe_close_dataset(
        ds: gdal.Dataset | None,
        *,
        flush_global_cache: bool = True,
        run_gc: bool = True
) -> None:
    """Flush and tear down GDAL dataset, releasing C++ cache and Python GC
    """
    if ds is not None:
        try:
            # flush blocks belonging to
            ds.FlushCache()
        except RuntimeError as e:
            logger.debug("Error flushing dataset cache during close: %s", e)

    if flush_global_cache:
        try:
            prev_max = gdal.GetCacheMax()
            gdal.SetCacheMax(0)
            gdal.SetCacheMax(prev_max)
        except RuntimeError as e:
            logger.debug("Error flushing global GDAL cache: %s", e)

    if run_gc:
        gc.collect()


def layer_resampling_guard(layer: QgsRasterLayer, max_oversampling: float = 2.0) -> None:
    """Force nearest neighbour canvas resampling on a QgsRaster.
    """
    if layer is None or not layer.isValid():
        return

    resample_filter = layer.resampleFilter()
    if resample_filter is None:
        return

    # passing None disables the resampler and forces direct NN pixel lookup
    resample_filter.setZoomedInResampler(None)
    resample_filter.setZoomedOutResampler(None)
    resample_filter.setMaxOversampling(float(max_oversampling))


def has_raster_overviews(raster_path: Path | str) -> bool:
    """Check whether a raster file already contains internal
    or external (.ovr) pyramids.
    """
    path = Path(raster_path)
    if not path.is_file():
        return False

    ds: gdal.Dataset | None = None
    band: gdal.Band | None = None
    try:
        gdal.UseExceptions()
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        if ds is None or ds.RasterCount < 1:
            return False

        band = ds.GetRasterBand(1)
        if band is None:
            return False

        return int(band.GetOverviewCount()) > 0
    except RuntimeError as e:
        logger.debug("Could not inspect overviews for %s: %s", path.name, e)
        return False
    finally:
        # detach band reference before releasing dataset
        band = None
        ds = None


def ensure_raster_overviews(
        raster_path: Path | str,
        levels: Sequence[int] = DEFAULT_OVERVIEW_LEVELS,
        resampling: str = "NEAREST",
) -> bool:
    """Ensure a raster has pyramid overviews (.ovr), building them if missing
    """
    path = Path(raster_path).resolve()
    if not path.is_file():
        return False

    if has_raster_overviews(path):
        return True

    ds: gdal.Dataset | None = None
    try:
        gdal.UseExceptions()

        # opening in GA_ReadOnly tells GDAL to write an external .ovr sidecar file
        # instead of modifying the original source raster in-place.
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        if ds is None:
            return False

        level_list = [int(lvl) for lvl in levels]
        resample_method = resampling.upper()

        ds.BuildOverviews(resample_method, level_list)
        log_qgis_info(f"Built pyramids {level_list} ({resample_method}) for {path.name}")
        return True

    except RuntimeError as e:
        log_qgis_warning(f"Could not build pyramids for {path.name}: {e}")
        return False
    finally:
        safe_close_dataset(ds, flush_global_cache=True, run_gc=True)
        ds = None

