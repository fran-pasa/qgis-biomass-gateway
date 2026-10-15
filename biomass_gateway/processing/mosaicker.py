"""
Mosaicking module for georeferenced ESA BIOMASS products.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from osgeo import gdal

from ..core.exceptions import MosaicError
from ..core.logger import get_logger, log_qgis_info

logger = get_logger("mosaicker")


def build_vrt_mosaic(
    raster_paths: list[Path | str],
    output_vrt_path: Path | str,
    band_names: list[str] | None = None,
    nodata: float = -9999.0,
    resample_alg: str = "bilinear",
    progress_callback: Callable[[float, str], int] | None = None,
) -> Path:
    """Build a GDAL VRT mosaic from georeferenced BIOMASS products.

    Creates a multi-band GDAL Virtual Raster (VRT) that mosaics the input
    rasters on-the-fly, preserving polarizations, nodata masking across
    swath overlaps, and source overviews.

    Args:
        raster_paths: List of file paths to georeferenced raster products.
        output_vrt_path: Destination path for the output .vrt file.
        band_names: List of band descriptions (e.g. ["HH", "HV", "VH", "VV"]).
        nodata: Nodata value to mask in sources and configure on the VRT.
        resample_alg: Resampling algorithm name (e.g. 'bilinear', 'nearest', 'cubic').
        progress_callback: Optional callback for GDAL progress reporting.

    Returns:
        Path to the created VRT file.

    Raises:
        MosaicError: If fewer than 2 rasters are provided or GDAL BuildVRT fails.
    """
    if not raster_paths or len(raster_paths) < 2:
        raise MosaicError("At least 2 products are needed to build a mosaic")

    resolved_paths = [Path(p).resolve() for p in raster_paths]
    output_vrt_path = Path(output_vrt_path).resolve()
    output_vrt_path.parent.mkdir(parents=True, exist_ok=True)

    if output_vrt_path.is_file():
        try:
            output_vrt_path.unlink()
        except OSError as err:
            logger.warning("Could not remove existing VRT %s: %s", output_vrt_path, err)

    gdal.UseExceptions()
    try:
        input_file_strings = [str(p) for p in resolved_paths]

        options_kwargs: dict[str, object] = {
            "resolution": "highest",
            "resampleAlg": resample_alg,
            "srcNodata": nodata,
            "VRTNodata": nodata,
        }
        if progress_callback is not None:
            options_kwargs["callback"] = progress_callback

        vrt_options = gdal.BuildVRTOptions(**options_kwargs)

        vrt_ds = gdal.BuildVRT(
            str(output_vrt_path),
            input_file_strings,
            options=vrt_options,
        )

        if vrt_ds is None:
            raise MosaicError(f"GDAL BuildVRT returned None for {output_vrt_path}")

        # Preserve polarization names on each band
        pols = band_names or ["HH", "HV", "VH", "VV"]
        for i in range(1, vrt_ds.RasterCount + 1):
            band = vrt_ds.GetRasterBand(i)
            if band and i <= len(pols):
                band.SetDescription(pols[i - 1])
            # Explicitly release band proxy to prevent dangling C++ pointers upon dataset close
            band = None

        vrt_ds.FlushCache()
        vrt_ds = None

        log_qgis_info(f"VRT Mosaic built successfully: {output_vrt_path}")
        return output_vrt_path

    except Exception as err:
        if output_vrt_path.is_file():
            try:
                output_vrt_path.unlink()
            except OSError:
                pass
        raise MosaicError(f"Failed to build mosaic: {err}") from err