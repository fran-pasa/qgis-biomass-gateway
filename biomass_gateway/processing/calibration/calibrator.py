"""Core radiometric calibration engine for BIOMASS L1 products.

Calculates Gamma Nought in Decibels (dB):
    beta0 = amplitude^2
    gamma0 = beta0 * gammaNought_LUT
    gamma0_dB = 10 * log10(gamma0)   (for gamma0 > 0; invalid / <= 0 mapped to NoData)

Overview generation is disabled by default, allowing QGIS to resample and render
the full-resolution raster dynamically on the fly.
"""

from pathlib import Path
import numpy as np
from osgeo import gdal

from ...core.exceptions import CalibrationError
from ...core.logger import get_logger
from ...core.models import BiomassProduct, CalibrationConfig
from ...core.memory import safe_close_dataset
from .geometry import read_sar_image_geometry
from .interpolator import interpolate_gamma_lut
from .lut_reader import read_lut_geometry

logger = get_logger("calibrator")


def calibrate_to_gamma0(
    input_raster_path: Path | str,
    lut_nc_path: Path | str,
    annot_xml_path: Path | str,
    output_path: Path | str,
    config: CalibrationConfig | None = None,
    progress_callback=None,
) -> Path:
    """Calibrate a BIOMASS measurement amplitude raster to Gamma Nought (dB).

    Generates a multi-band GeoTIFF in radar geometry preserving all GCPs,
    spatial references, and metadata from the source image.

    Args:
        input_raster_path: Path to the input amplitude raster (*_i_abs.tiff).
        lut_nc_path: Path to the NetCDF radiometric LUT file (*_lut.nc).
        annot_xml_path: Path to the annotation XML metadata file (*_annot.xml).
        output_path: Destination path for the output calibrated multi-band GeoTIFF.
        config: Calibration options (nodata, compression, overviews).
        progress_callback: Optional callable(float, str) reporting progress.

    Returns:
        Path to the generated GeoTIFF file.

    Raises:
        CalibrationError: If input files are missing or calibration fails.
    """
    input_raster_path = Path(input_raster_path).resolve()
    lut_nc_path = Path(lut_nc_path).resolve()
    annot_xml_path = Path(annot_xml_path).resolve()
    output_path = Path(output_path).resolve()
    config = config or CalibrationConfig()

    if not input_raster_path.is_file():
        raise CalibrationError(f"Measurement raster file not found: {input_raster_path}")
    if not lut_nc_path.is_file():
        raise CalibrationError(f"NetCDF LUT file not found: {lut_nc_path}")
    if not annot_xml_path.is_file():
        raise CalibrationError(f"Annotation XML file not found: {annot_xml_path}")

    gdal.UseExceptions()

    if progress_callback:
        progress_callback(0.05, "Reading NetCDF radiometric LUT...")
    logger.info("Reading radiometric LUT from %s", lut_nc_path.name)
    lut_data = read_lut_geometry(lut_nc_path)

    if progress_callback:
        progress_callback(0.15, "Reading SAR image geometry from annotation...")
    logger.info("Reading SAR image geometry from %s", annot_xml_path.name)
    geom = read_sar_image_geometry(annot_xml_path, lut_data.reference_azimuth_time)

    # Open source raster to verify geometry
    src_ds = gdal.Open(str(input_raster_path), gdal.GA_ReadOnly)
    if src_ds is None:
        raise CalibrationError(f"Cannot open measurement raster: {input_raster_path}")

    raster_width = src_ds.RasterXSize
    raster_height = src_ds.RasterYSize
    band_count = src_ds.RasterCount

    if (raster_height, raster_width) != (geom.n_lines, geom.n_samples):
        src_ds = None
        raise CalibrationError(
            f"Measurement raster size ({raster_height}, {raster_width}) does not match "
            f"annotation geometry ({geom.n_lines}, {geom.n_samples})."
        )

    # Resample LUT once onto the image grid
    if progress_callback:
        progress_callback(0.25, "Interpolating Gamma0 LUT onto image grid...")
    logger.info("Interpolating Gamma0 LUT...")
    gamma_full = interpolate_gamma_lut(lut_data, geom, progress_callback=progress_callback)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_file():
        try:
            output_path.unlink()
        except OSError:
            pass

    # Create destination multi-band GeoTIFF
    driver = gdal.GetDriverByName("GTiff")
    if driver is None:
        src_ds = None
        raise CalibrationError("GDAL GTiff driver not found.")

    creation_opts = [
        "TILED=YES",
        f"COMPRESS={config.compress}",
        "BIGTIFF=IF_SAFER",
        "NUM_THREADS=ALL_CPUS",
    ]

    out_ds = driver.Create(
        str(output_path),
        raster_width,
        raster_height,
        band_count,
        gdal.GDT_Float32,
        creation_opts,
    )
    if out_ds is None:
        src_ds = None
        raise CalibrationError(f"Failed to create output GeoTIFF at {output_path}")

    # Copy GCPs and spatial reference
    gcps = src_ds.GetGCPs()
    gcp_projection = src_ds.GetGCPProjection()
    if gcps:
        out_ds.SetGCPs(gcps, gcp_projection)

    projection = src_ds.GetProjection()
    if projection:
        out_ds.SetProjection(projection)

    geotransform = src_ds.GetGeoTransform(can_return_null=True)
    if geotransform:
        out_ds.SetGeoTransform(geotransform)

    # Copy root metadata and set calibration tags
    root_metadata = src_ds.GetMetadata() or {}
    out_ds.SetMetadata(root_metadata)
    quantity = "gamma0_dB" if config.to_db else "gamma0_linear"
    unit = "dB" if config.to_db else "power"
    out_ds.SetMetadataItem("QUANTITY", quantity)
    out_ds.SetMetadataItem("UNIT", unit)
    out_ds.SetMetadataItem("REFERENCE", "PFD v1.6.1 Sec.4.3.2 | ATBD v1.2.4 Sec.4.8")
    out_ds.SetMetadataItem("BIOMASS_LUT_FILE", str(lut_nc_path.resolve()))
    out_ds.SetMetadataItem("BIOMASS_ANNOTATION_FILE", str(annot_xml_path.resolve()))
    out_ds.SetMetadataItem("BIOMASS_SOURCE_RASTER", str(input_raster_path.resolve()))
    out_ds.SetMetadataItem("BIOMASS_PRODUCT_FOLDER", str(lut_nc_path.parent.resolve()))
    if config.build_overviews:
        out_ds.SetMetadataItem("OVERVIEW_RESAMPLING", config.overview_resampling.upper())

    # Stream processing band-by-band
    default_pols = ["HH", "HV", "VH", "VV"]
    ov_levels = list(config.overview_levels)

    if config.to_db and config.build_overviews and config.overview_resampling.upper() == "AVERAGE":
        # -------------------------------------------------------------
        # Phase 1: Compute and write linear power gamma0 band-by-band
        # -------------------------------------------------------------
        base_progress = 0.35
        progress_per_band = 0.30 / max(1, band_count)

        for band_idx in range(1, band_count + 1):
            src_band = src_ds.GetRasterBand(band_idx)
            out_band = out_ds.GetRasterBand(band_idx)

            desc = src_band.GetDescription() or (
                default_pols[band_idx - 1] if band_idx <= len(default_pols) else f"Band {band_idx}"
            )
            if progress_callback:
                progress_callback(
                    base_progress + (band_idx - 1) * progress_per_band,
                    f"Calibrating band {band_idx}/{band_count} ({desc}) in linear power...",
                )
            logger.info("Computing linear Gamma0 for band %d/%d (%s)...", band_idx, band_count, desc)

            amp = src_band.ReadAsArray().astype(np.float32)

            # Mask source nodata if specified
            src_nodata = src_band.GetNoDataValue()
            if src_nodata is not None:
                amp[amp == src_nodata] = np.nan

            # beta0 = amplitude^2
            beta0 = np.square(amp, dtype=np.float32)

            # gamma0 = beta0 * LUT
            gamma0 = beta0 * gamma_full

            # Map non-positive or non-finite linear power to NoData
            valid = np.isfinite(gamma0) & (gamma0 > 0.0)
            gamma0_lin = np.where(valid, gamma0, config.nodata).astype(np.float32)

            # Write linear band data
            out_band.WriteArray(gamma0_lin)
            out_band.SetNoDataValue(config.nodata)
            out_band.SetDescription(desc)

            # Copy and update band metadata
            band_meta = src_band.GetMetadata() or {}
            out_band.SetMetadata(band_meta)
            out_band.SetMetadataItem("POLARIMETRIC_INTERP", desc)
            out_band.SetMetadataItem("QUANTITY", quantity)
            out_band.SetMetadataItem("UNIT", unit)

            out_band.FlushCache()

            # free the band arrays and handles, delete gamma_full since phase 2 and 3 of this algorithm
            # don't need the LUT anymore
            src_band = None
            out_band = None
            del amp, beta0, gamma0, valid, gamma0_lin

        del gamma_full

        # -------------------------------------------------------------
        # Phase 2: Build pyramid overviews in linear power
        # -------------------------------------------------------------
        if progress_callback:
            progress_callback(0.68, "Building multi-scale overviews in linear power...")
        logger.info("Building overviews in linear power with AVERAGE resampling...")
        out_ds.FlushCache()
        try:
            out_ds.BuildOverviews("AVERAGE", ov_levels)
        except Exception as err:
            logger.warning("Failed to build overviews: %s", err)

        # -------------------------------------------------------------
        # Phase 3: Convert base bands and overviews to Decibels (dB)
        # -------------------------------------------------------------
        base_progress_db = 0.72
        progress_per_band_db = 0.25 / max(1, band_count)

        for band_idx in range(1, band_count + 1):
            out_band = out_ds.GetRasterBand(band_idx)
            desc = out_band.GetDescription() or f"Band {band_idx}"
            if progress_callback:
                progress_callback(
                    base_progress_db + (band_idx - 1) * progress_per_band_db,
                    f"Converting band {band_idx}/{band_count} ({desc}) and overviews to dB...",
                )
            logger.info("Converting band %d/%d (%s) and overviews to dB...", band_idx, band_count, desc)

            # Convert base band to dB
            lin_base = out_band.ReadAsArray()
            db_base = np.full(lin_base.shape, config.nodata, dtype=np.float32)
            valid_base = np.isfinite(lin_base) & (lin_base > 0.0) & (lin_base != config.nodata)
            db_base[valid_base] = (10.0 * np.log10(lin_base[valid_base])).astype(np.float32)

            out_band.WriteArray(db_base)
            out_band.FlushCache()

            # Convert each overview level to dB
            for ov_idx in range(out_band.GetOverviewCount()):
                ov_band = out_band.GetOverview(ov_idx)
                lin_ov = ov_band.ReadAsArray()
                db_ov = np.full(lin_ov.shape, config.nodata, dtype=np.float32)
                valid_ov = np.isfinite(lin_ov) & (lin_ov > 0.0) & (lin_ov != config.nodata)
                db_ov[valid_ov] = (10.0 * np.log10(lin_ov[valid_ov])).astype(np.float32)

                ov_band.WriteArray(db_ov)
                ov_band.SetNoDataValue(config.nodata)
                ov_band.FlushCache()
                ov_band = None
                del lin_ov, db_ov, valid_ov

            out_band.FlushCache()
            out_band = None
            del lin_base, db_base, valid_base

    else:
        # Direct single-pass processing (NEAREST, GAUSS, or direct linear)
        base_progress = 0.35
        progress_per_band = 0.60 / max(1, band_count)

        for band_idx in range(1, band_count + 1):
            src_band = src_ds.GetRasterBand(band_idx)
            out_band = out_ds.GetRasterBand(band_idx)

            desc = src_band.GetDescription() or (
                default_pols[band_idx - 1] if band_idx <= len(default_pols) else f"Band {band_idx}"
            )
            if progress_callback:
                progress_callback(
                    base_progress + (band_idx - 1) * progress_per_band,
                    f"Calibrating band {band_idx}/{band_count} ({desc})...",
                )
            logger.info("Processing band %d/%d (%s)...", band_idx, band_count, desc)

            amp = src_band.ReadAsArray().astype(np.float32)

            src_nodata = src_band.GetNoDataValue()
            if src_nodata is not None:
                amp[amp == src_nodata] = np.nan

            beta0 = np.square(amp, dtype=np.float32)
            gamma0 = beta0 * gamma_full

            valid = np.isfinite(gamma0) & (gamma0 > 0.0)
            if config.to_db:
                gamma0_out = np.full(gamma0.shape, config.nodata, dtype=np.float32)
                gamma0_out[valid] = (10.0 * np.log10(gamma0[valid])).astype(np.float32)
            else:
                gamma0_out = np.where(valid, gamma0, config.nodata).astype(np.float32)

            out_band.WriteArray(gamma0_out)
            out_band.SetNoDataValue(config.nodata)
            out_band.SetDescription(desc)

            band_meta = src_band.GetMetadata() or {}
            out_band.SetMetadata(band_meta)
            out_band.SetMetadataItem("POLARIMETRIC_INTERP", desc)
            out_band.SetMetadataItem("QUANTITY", quantity)
            out_band.SetMetadataItem("UNIT", unit)

            out_band.FlushCache()
            src_band =  None
            out_band = None
            del amp, beta0, gamma0, valid, gamma0_out

        del gamma_full

        if config.build_overviews:
            resampling = config.overview_resampling.upper()
            if progress_callback:
                progress_callback(0.92, f"Building raster overviews ({resampling})...")
            out_ds.FlushCache()
            try:
                out_ds.BuildOverviews(resampling, ov_levels)
            except Exception as err:
                logger.warning("Failed to build overviews: %s", err)

    safe_close_dataset(src_ds, flush_global_cache=False, run_gc=False)
    src_ds = None
    safe_close_dataset(out_ds, flush_global_cache=True, run_gc=True)
    out_ds = None

    if progress_callback:
        progress_callback(1.0, "Calibration completed.")
    logger.info("Gamma0 calibration completed: %s", output_path.name)
    return output_path


def calibrate_product_to_gamma0(
    product: BiomassProduct,
    output_path: Path | str,
    config: CalibrationConfig | None = None,
    progress_callback=None,
) -> Path:
    """Convenience helper to calibrate a parsed BiomassProduct instance to Gamma0 (dB).

    Args:
        product: BiomassProduct parsed metadata instance.
        output_path: Destination path for the output GeoTIFF.
        config: Calibration options.
        progress_callback: Optional progress reporter callback.

    Returns:
        Path to the calibrated GeoTIFF.

    Raises:
        CalibrationError: If required files are missing from the product.
    """
    src_file = product.abs_file or product.vrt_file
    if not src_file or not src_file.is_file():
        raise CalibrationError(f"Measurement raster file missing in product {product.product_id}")

    if not product.lut_file or not product.lut_file.is_file():
        raise CalibrationError(f"NetCDF radiometric LUT missing in product {product.product_id}")

    if not product.annotation_file or not product.annotation_file.is_file():
        raise CalibrationError(f"Annotation XML metadata missing in product {product.product_id}")

    return calibrate_to_gamma0(
        input_raster_path=src_file,
        lut_nc_path=product.lut_file,
        annot_xml_path=product.annotation_file,
        output_path=output_path,
        config=config,
        progress_callback=progress_callback,
    )
