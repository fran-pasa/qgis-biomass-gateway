"""
Mosaicking module for BIOMASS products.
"""

from __future__ import annotations
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
        resample_alg: str = 'bilinear',
) -> Path:
    """Build a GDAL VRT mosaic from georeferenced Biomass products.
    """
    if not raster_paths or len(raster_paths) < 2:
        raise MosaicError("At least 2 products are needed to build a mosaic")

    resolved_paths = [Path(p).resolve() for p in raster_paths]
    output_vrt_path = Path(output_vrt_path).resolve()
    output_vrt_path.parent.mkdir(parents=True, exist_ok=True)

    gdal.UseExceptions()
    try:
        input_file_strings = [str(p) for p in resolved_paths]

        # config VRT options
        vrt_options = gdal.BuildVRTOptions(
            resolution='highest',
            resampleAlg=resample_alg,
            srcNodata=nodata,
            VRTNodata=nodata,
        )

        vrt_ds = gdal.BuildVRT(str(output_vrt_path),
                               input_file_strings,
                               options=vrt_options)

        if vrt_ds is None:
            raise MosaicError(f"GDAL BuildVRT returned None for {output_vrt_path}")

        # preserve polarization names
        pols = band_names or ["HH", "HV", "VH", "VV"]
        for i in range(1, vrt_ds.RasterCount+1):
            band = vrt_ds.GetRasterBand(i)
            if band and i <= len(pols):
                band.SetDescription(pols[i-1])

        vrt_ds.FlushCache()
        vrt_ds = None

        log_qgis_info(f"VRT Mosaic built successfully: {output_vrt_path}")
        return output_vrt_path

    except Exception as e:
        if output_vrt_path.is_file():
            try:
                output_vrt_path.unlink()
            except OSError:
                pass
        raise MosaicError(f"Failed to build mosaic: {e}")