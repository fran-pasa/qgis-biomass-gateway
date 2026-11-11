"""
Polarimetric decompositions for ESA BIOMASS SLC products.

Decompositions math mirrors what was done in the BioPal library
(https://github.com/BioPAL/data_readers/blob/main/bioqlk.py)
"""

from __future__ import annotations

import numpy as np

from pathlib import Path
from typing import Callable
from osgeo import gdal
from scipy.ndimage import uniform_filter

from ...core.exceptions import DecompositionError
from ...core.logger import get_logger
from ...core.memory import safe_close_dataset
from ...core.models import (
    DecompositionConfig,
    DecompositionScale,
    DecompositionType,
    StretchMethod
    )

logger = get_logger("polarimetry.decompositions")


def compute_pauli(
        hh: np.ndarray,
        hv: np.ndarray,
        vh: np.ndarray,
        vv: np.ndarray,
        scale: DecompositionScale = DecompositionScale.LINEAR_AMPLITUDE,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute pauli decomposition channels (R, G, B).
    """
    inv_sqrt2 = np.float32(1.0 / np.sqrt(2.0))

    # red = double-bounce scattering
    # green = volume scattering
    # blue = surface scattering
    red = inv_sqrt2 * np.abs(hh - vv).astype(np.float32)
    green = inv_sqrt2 * np.abs(hv + vh).astype(np.float32)
    blue = inv_sqrt2 * np.abs(hh + vv).astype(np.float32)


    if scale == DecompositionScale.LINEAR_AMPLITUDE:
        return red, green, blue
    elif scale == DecompositionScale.LINEAR_POWER:
        return red**2, green**2, blue**2
    elif scale == DecompositionScale.DB:
        with np.errstate(divide='ignore', invalid='ignore'):
            red_db = 20.0 * np.log10(np.where(red > 0, red, np.nan))
            green_db = 20.0 * np.log10(np.where(green > 0, green, np.nan))
            blue_db = 20.0 * np.log10(np.where(blue > 0, blue, np.nan))

            return (
                red_db.astype(np.float32),
                green_db.astype(np.float32),
                blue_db.astype(np.float32)
            )
    raise DecompositionError(f"Unsupported decomposition scale {scale}")


def apply_ical_phase_correction(
        hh: np.ndarray,
        hv: np.ndarray,
        vh: np.ndarray,
        vv: np.ndarray,
        pc_hh: float = -0.4145157,
        pc_hv: float = 1.98025057,
        pc_vh: float = -1.56556034,
        pc_vv: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Apply per-channel phase offset correction (in radians) to complex
    SLC channels.
    The default offsets are derived from the BIOMASS iCal phase factors in the
    BioPal library (https://github.com/BioPAL/data_readers/blob/main/bioqlk.py)
    """
    # 1j in Python is the standard way of representign imaginary units
    return (
        hh * np.exp(1j * np.float32(pc_hh)),
        hv * np.exp(1j * np.float32(pc_hv)),
        vh * np.exp(1j * np.float32(pc_vh)),
        vv * np.exp(1j * np.float32(pc_vv))
    )


def compute_hsv(
        hh: np.ndarray,
        hv: np.ndarray,
        vh: np.ndarray,
        vv: np.ndarray,
        kscale: float = 2.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute HSV decomposition components (hue, saturation, value) from
    Pauli basis.
    """
    # unpack scattering mechanisms from Pauli
    p2, p3, p1 = compute_pauli(hh, hv, vh, vv, scale=DecompositionScale.LINEAR_AMPLITUDE)

    # compute total polarimetric amplitude
    amplitude = np.sqrt(p1**2 + p2**2 + p3**2).astype(np.float32)

    with np.errstate(divide='ignore', invalid='ignore'):
        ratio = np.where(amplitude > 0, np.clip(p1 / amplitude, -1.0, 1.0), np.nan)

        # normalize the scattering angle (0 = surface scatt, 1 = double-bounce/volume scatt)
        alpha = (np.arccos(ratio) / (np.pi / 2.0)).astype(np.float32)

    # set saturation to 1 on valid pixels and NaN on invalid pixels
    saturation = np.where(np.isfinite(alpha), 1.0, np.nan).astype(np.float32)

    mean_amp = float(np.nanmean(amplitude))
    if not np.isfinite(mean_amp) or mean_amp <= 0.0:
        value = np.zeros_like(amplitude, dtype=np.float32)
    else:
        # normalize amplitude between 0 and 1
        vmax = np.float32(kscale * mean_amp)
        value = (np.clip(amplitude, 0.0, vmax) / vmax).astype(np.float32)

    return alpha, saturation, value


def smooth_nan(arr: np.ndarray, window_size: tuple[int, int] | None) -> np.ndarray:
    """Apply a 2D boxcar filter while ignoring NaN pixels.
    Ported from https://github.com/BioPAL/data_readers/blob/main/bioqlk.py
    """
    if window_size is None or (window_size[0] <= 1 and window_size[1] <= 1):
        # fast exit if smoothing is disabled (e.g. with a 1,1 window)
        return arr

    # record where invalid/NoData pixels are located, otherwise uniform_filter will propagate even a single NaN
    nan_mask = np.isnan(arr)
    if np.all(nan_mask):
        return arr.copy()

    # replace NaN values with 0.0 so they contribute nothing to the moving window sum
    arr_filled = np.where(nan_mask, 0.0, arr).astype(np.float32)

    # create a 1.0 / 0.0 weight array (1.0 = valid pixels, 0.0 = pixels are NaN)
    valid_mask = (~nan_mask).astype(np.float32)

    # run boxcar moving average
    smoothed_data = uniform_filter(arr_filled, size=window_size, mode='nearest')
    smoothed_mask = uniform_filter(valid_mask, size=window_size, mode='nearest')

    # normalize boxcar sum by the fraction of valid pixels in the window
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.where(smoothed_mask > 0, smoothed_data / smoothed_mask, np.nan)

    # restore NaN at originally invalid pixel locations
    result[nan_mask] = np.nan

    return result.astype(np.float32)

def _turbo_colormap(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Helper to evaluate Google's 'turbo' colormap on normalized array x in [0, 1].
    This helper is useful to avoid using matplotlib turbo, as matplotlib may not be installed in all
    QGIS setups.
    """
    xc = np.clip(x, 0.0, 1.0).astype(np.float32)

    r = (
        0.13572138
        + xc * (
            4.61539260
            + xc * (
                -42.66032258
                + xc * (132.13108234 + xc * (-152.94239396 + xc * 59.28637943))
            )
        )
    )
    g = (
        0.09140261
        + xc * (
            2.19418839
            + xc * (
                4.84296658
                + xc * (-14.18503333 + xc * (4.27729857 + xc * 2.82956604))
            )
        )
    )
    b = (
        0.10667330
        + xc * (
            12.64194608
            + xc * (
                -60.58204836
                + xc * (110.36276771 + xc * (-89.90310912 + xc * 27.34824973))
            )
        )
    )

    return (
        np.clip(r, 0.0, 1.0).astype(np.float32),
        np.clip(g, 0.0, 1.0).astype(np.float32),
        np.clip(b, 0.0, 1.0).astype(np.float32),
    )


def hsv_to_rgb_uint8(
        hue: np.ndarray,
        saturation: np.ndarray,
        value: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert HSV channels to 8-bit RGB [0, 255]
    """
    valid = np.isfinite(hue) & np.isfinite(saturation) & np.isfinite(value)
    hue_safe = np.where(valid, hue, 0.0)
    val_safe = np.where(valid, np.clip(value, 0.0, 1.0), 0.0)

    # map scattering angle (hue) onto the turbo color ramp and modulate brightness by span (value)
    r_norm, g_norm, b_norm = _turbo_colormap(hue_safe)

    r_u8 = np.where(valid, np.clip(r_norm * val_safe * 255.0, 0.0, 255.0), 0.0).astype(np.uint8)
    g_u8 = np.where(valid, np.clip(g_norm * val_safe * 255.0, 0.0, 255.0), 0.0).astype(np.uint8)
    b_u8 = np.where(valid, np.clip(b_norm * val_safe * 255.0, 0.0, 255.0), 0.0).astype(np.uint8)

    return r_u8, g_u8, b_u8


def scale_channel_to_uint8(
        channel: np.ndarray,
        vmin: float,
        vmax: float,
) -> np.ndarray:
    """Linearly stretch a 2D float channel from [vmin, vmax] to uint8 [0, 255].
    """
    valid = np.isfinite(channel)
    if not np.any(valid) or vmax <= vmin:
        return np.zeros(channel.shape, dtype=np.uint8)

    clipped = np.clip(np.where(valid, channel, vmin), vmin, vmax)
    scaled = 1.0 + (clipped - vmin) / (vmax - vmin) * 254.0
    return np.where(valid, np.clip(scaled, 1.0, 255.0), 0.0).astype(np.uint8)


def stretch_pauli_to_rgb_uint8(
        red: np.ndarray,
        green: np.ndarray,
        blue: np.ndarray,
        stretch_method: StretchMethod = StretchMethod.QUANTILE,
        scale: DecompositionScale = DecompositionScale.LINEAR_AMPLITUDE,
        quantile_threshold: float = 0.01,
        cscale: float = 2.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stretch 3 float Pauli channels into 8-bit RGB uint8 arrays.
    """
    if stretch_method == StretchMethod.MEAN and scale == DecompositionScale.DB:
        raise DecompositionError(
            "Mean-based contrast stretching (MEAN) is not compatible with dB scale. "
            "Use Quantile stretching or Linear Amplitude/Power scale."
        )

    out_channels: list[np.ndarray] = []
    for ch in (red, green, blue):
        valid = ch[np.isfinite(ch)]
        if valid.size == 0:
            out_channels.append(np.zeros(ch.shape, dtype=np.uint8))
            continue

        if stretch_method == StretchMethod.QUANTILE:
            q = float(np.clip(quantile_threshold, 0.0, 0.49))
            vmin = float(np.quantile(valid, q)) if scale == DecompositionScale.DB else 0.0
            vmax = float(np.quantile(valid, 1.0 - q))
        elif stretch_method == StretchMethod.MEAN:
            vmin = 0.0
            vmax = float(cscale * np.mean(valid))
        else:
            raise DecompositionError(f"Unsupported stretch method: {stretch_method}")

        out_channels.append(scale_channel_to_uint8(ch, vmin, vmax))

    return out_channels[0], out_channels[1], out_channels[2]


def _copy_metadata(src_ds: gdal.Dataset, dst_ds: gdal.Dataset,
                   lut_file: Path | None = None, annotation_file: Path | None = None,
                   product_folder: Path | None = None) -> None:
    """Copy GCPs, projection, geotransform and other BIOMASS LUT metadata
    from src_ds to dst_ds
    """
    # copy the GCPs and their CRS from the slant-range input to the output
    # this way TPS / polynomial warping wrorks on the decomposition output as well.
    gcps = src_ds.GetGCPs()
    if gcps:
        dst_ds.SetGCPs(gcps, src_ds.GetGCPProjection())

    # copy any existing CRS projection and affine GeoTransform
    proj = src_ds.GetProjection()
    if proj:
        dst_ds.SetProjection(proj)

    gt = src_ds.GetGeoTransform(can_return_null=True)
    if gt:
        dst_ds.SetGeoTransform(gt)

    # forwards these tags to the new output even when input already has them already
    src_meta = src_ds.GetMetadata() or {}
    for key in ("BIOMASS_LUT_FILE", "BIOMASS_ANNOTATION_FILE", "BIOMASS_PRODUCT_FOLDER"):
        if key in src_meta:
            dst_ds.SetMetadataItem(key, src_meta[key])

    if lut_file is not None:
        dst_ds.SetMetadataItem("BIOMASS_LUT_FILE", str(Path(lut_file).resolve()))
    if annotation_file is not None:
        dst_ds.SetMetadataItem("BIOMASS_ANNOTATION_FILE", str(Path(annotation_file).resolve()))
    if product_folder is not None:
        dst_ds.SetMetadataItem("BIOMASS_PRODUCT_FOLDER", str(Path(product_folder).resolve()))


def _write_uint8_rgb(
        output_path: Path,
        src_ds: gdal.Dataset,
        r_u8: np.ndarray,
        g_u8: np.ndarray,
        b_u8: np.ndarray,
        valid_mask: np.ndarray,
        config: DecompositionConfig,
        lut_file: Path | None = None,
        annotation_file: Path | None = None,
        product_folder: Path | None = None,
) -> Path:
    """Helper to write 8bit RGB or RGBA geotiff
    """
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    row, cols = r_u8.shape

    # create 4 band (RGBA) when transparency is enabled, RBG otherwise
    band_count = 4 if config.add_alpha else 3

    driver = gdal.GetDriverByName("GTiff")
    if driver is None:
        raise DecompositionError("GDAL GTiff driver not found.")

    # tell qgis to render the file as a true-color RGB/RGBA image
    # rather than a single band raster
    creation_options = [
        "TILED=YES",
        f"COMPRESS={config.compress}",
        "BIGTIFF=IF_SAFER",
        "NUM_THREADS=ALL_CPUS",
        "PHOTOMETRIC=RGB",
    ]
    if config.add_alpha:
        creation_options.append("ALPHA=YES")

    out_ds = driver.Create(
        str(output_path), cols, row, band_count, gdal.GDT_Byte, creation_options
    )
    if out_ds is None:
        raise DecompositionError(f"Failed to create 8bit RGB at {output_path}.")

    # copy GCPs, CRS and LUT file / Annotation file tags
    # this way the output of this module can be passed to 'Warp product' if needed.
    _copy_metadata(src_ds, out_ds, lut_file, annotation_file, product_folder)
    out_ds.SetMetadataItem("DECOMPOSITION", config.representation.value.upper())

    # write bands and assign GDAL color interpretation
    rgb_spec = [
        (1, r_u8, gdal.GCI_RedBand, "Red"),
        (2, g_u8, gdal.GCI_GreenBand, "Green"),
        (3, b_u8, gdal.GCI_BlueBand, "Blue"),
    ]
    for b_idx, arr, color_interp, desc in rgb_spec:
        band = out_ds.GetRasterBand(b_idx)
        band.SetColorInterpretation(color_interp)
        band.SetDescription(desc)
        if not config.add_alpha:
            band.SetNoDataValue(0)
        band.WriteArray(arr)
        band = None

    # compute the Alpha band: 255 (opaque) where the pixel was valid,
    # and 0 (transparent) on NoData borders
    if config.add_alpha:
        alpha = np.where(valid_mask, 255, 0).astype(np.uint8)
        alpha_band = out_ds.GetRasterBand(4)
        alpha_band.SetColorInterpretation(gdal.GCI_AlphaBand)
        alpha_band.SetDescription("Alpha")
        alpha_band.WriteArray(alpha)
        alpha_band = None

    # build pyramids
    if config.build_overviews:
        out_ds.FlushCache()
        out_ds.BuildOverviews(config.overview_resampling, list(config.overview_levels))

    safe_close_dataset(out_ds, flush_global_cache=True, run_gc=False)
    out_ds = None

    return output_path


def _write_float32(
        output_path: Path,
        src_ds: gdal.Dataset,
        ch1: np.ndarray,
        ch2: np.ndarray,
        ch3: np.ndarray,
        band_descriptions: tuple[str, str, str],
        config: DecompositionConfig,
        lut_file: Path | None = None,
        annotation_file: Path | None = None,
        product_folder: Path | None = None,
) -> Path:
    """Write 3-band Float32 geotiff
    """
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows, cols = ch1.shape
    driver = gdal.GetDriverByName("GTiff")
    if driver is None:
        raise DecompositionError("GDAL GTiff driver not found.")

    creation_options = [
        "TILED=YES",
        f"COMPRESS={config.compress}",
        "BIGTIFF=IF_SAFER",
        "NUM_THREADS=ALL_CPUS",
    ]
    out_ds = driver.Create(
        str(output_path), cols, rows, 3, gdal.GDT_Float32, creation_options
    )
    if out_ds is None:
        raise DecompositionError(f"Failed to create float32 geotiff at {output_path}.")

    _copy_metadata(src_ds, out_ds, lut_file, annotation_file, product_folder)
    out_ds.SetMetadataItem("DECOMPOSITION", config.representation.value.upper())
    out_ds.SetMetadataItem("SCALE", config.scale.value)

    for b_idx, (arr, desc) in enumerate(zip((ch1, ch2, ch3),
                                            band_descriptions), start=1):
        band = out_ds.GetRasterBand(b_idx)
        band.SetDescription(desc)
        band.SetNoDataValue(config.nodata)
        out_arr = np.where(np.isfinite(arr), arr, config.nodata).astype(np.float32)
        band.WriteArray(out_arr)
        band = None

    if config.build_overviews:
        out_ds.FlushCache()
        out_ds.BuildOverviews(config.overview_resampling, list(config.overview_levels))

    safe_close_dataset(out_ds, flush_global_cache=True, run_gc=False)
    out_ds = None

    return output_path


def run_polarimetric_decomposition(
        abs_raster_path: Path | str,
        phase_raster_path: Path | str,
        output_rgb_path: Path | None,
        output_float_path: Path | None = None,
        config: DecompositionConfig | None = None,
        lut_file: Path | None = None,
        annotation_file: Path | None = None,
        product_folder: Path | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[Path, Path | None]:
    """Run Pauli or HSV decomposition on BIOMASS complex SLC
    (amp + phase) rasters
    """
    # path & config setup
    config = config or DecompositionConfig()
    abs_path = Path(abs_raster_path).resolve()
    phase_path = Path(phase_raster_path).resolve()
    out_rgb = Path(output_rgb_path).resolve()
    out_float = Path(output_float_path).resolve() if output_float_path else None

    if not abs_path.is_file():
        raise DecompositionError(f"Amplitude raster not found: {abs_path}.")
    if not phase_path.is_file():
        raise DecompositionError(f"Phase raster not found: {phase_path}.")

    gdal.UseExceptions() #tells GDAL to raise Python errors instead of throwing silent error codes

    # open both files in read only mode
    abs_ds = gdal.Open(str(abs_path), gdal.GA_ReadOnly)
    phase_ds = gdal.Open(str(phase_path), gdal.GA_ReadOnly)
    if abs_ds is None or phase_ds is None:
        raise DecompositionError("Failed to open input amplitude or phase raster with GDAL.")

    try:
        # verify number of bands and the raster size is the same across amp and phase files
        n_bands = abs_ds.RasterCount
        if n_bands != phase_ds.RasterCount:
            raise DecompositionError(f"Band count mismatch between amplitude ({n_bands} and phase ({phase_ds.RasterCount})).")
        if n_bands != 4:
            raise DecompositionError(
                f"Polarimetric decomposition requires 4 bands, got {n_bands}."
            )
        if (abs_ds.RasterXSize != phase_ds.RasterXSize) or (abs_ds.RasterYSize != phase_ds.RasterYSize):
            raise DecompositionError("Dimension mismatch between amplitude and phase raster")

        if progress_callback:
            progress_callback(0.10, f"Reading {n_bands} complex SLC channels...")

        channels: list[np.ndarray] = []
        valid_mask = np.ones((abs_ds.RasterYSize, abs_ds.RasterXSize), dtype=bool)

        # reconstruct complex SLC channels S = A * exp(j * phi) from amplitude and phase bands
        for b_idx in range(1, n_bands + 1):
            ab = abs_ds.GetRasterBand(b_idx)
            pb = phase_ds.GetRasterBand(b_idx)
            amp = ab.ReadAsArray().astype(np.float32)
            pha = pb.ReadAsArray().astype(np.float32)

            # mask out non-finite pixels, zero-amplitude borders, and explicit GDAL nodata values
            amp_nd = ab.GetNoDataValue()
            pha_nd = pb.GetNoDataValue()
            band_valid = np.isfinite(amp) & np.isfinite(pha) & (amp > 0.0)
            if amp_nd is not None:
                band_valid &= amp != amp_nd
            if pha_nd is not None:
                band_valid &= pha != pha_nd

            # a pixel is valid only if it is valid across all 4 polarizations
            valid_mask &= band_valid
            ch = np.where(band_valid, amp * np.exp(1j * pha), np.nan + 1j * np.nan).astype(np.complex64)
            channels.append(ch)
            ab = None
            pb = None

        # unpack in BIOMASS band order (1=HH, 2=HV, 3=VH, 4=VV)
        hh, hv, vh, vv = channels[0], channels[1], channels[2], channels[3]

        if config.fix_ical_phase:
            if progress_callback:
                progress_callback(0.30, "Applying iCal phase offset corrections...")
            hh, hv, vh, vv = apply_ical_phase_correction(hh, hv, vh, vv,
                                                         pc_hh=config.pc_hh,
                                                         pc_hv=config.pc_hv,
                                                         pc_vh=config.pc_vh,
                                                         pc_vv=config.pc_vv)

        if progress_callback:
            progress_callback(0.45, f"Computing {config.representation.value.upper()} decomposition...")

        if config.representation == DecompositionType.PAULI:
            ch1, ch2, ch3 = compute_pauli(hh, hv, vh, vv,
                                          scale=config.scale)
            float_desc = (
                "Pauli_Red (|HH-VV|/sqrt(2))",
                "Pauli_Green (|HV+VH|/sqrt(2))",
                "Pauli_Blue (|HH+VV|/sqrt(2))",
            )
        elif config.representation == DecompositionType.HSV:
            ch1, ch2, ch3 = compute_hsv(hh, hv, vh, vv,
                                        kscale=config.cscale)
            float_desc = ("HSV_Hue (Alpha)", "HSV_Saturation", "HSV_Value (Span)")
        else:
            raise DecompositionError(f"Unsupported representation: {config.representation}")

        # free complex64 channels immediately to halve peak RAM before boxcar smoothing
        del channels, hh, hv, vh, vv

        # smooth float channels prior to both Float32 export and 8-bit stretching (matches bioqlk.make_rgb)
        if config.smooth_window and (config.smooth_window[0] > 1 or config.smooth_window[1] > 1):
            if progress_callback:
                progress_callback(0.65, f"Applying {config.smooth_window[0]}x{config.smooth_window[1]} boxcar filter...")
            ch1 = smooth_nan(ch1, config.smooth_window)
            ch2 = smooth_nan(ch2, config.smooth_window)
            ch3 = smooth_nan(ch3, config.smooth_window)

        if out_float is not None:
            if progress_callback:
                progress_callback(0.75, f"Writing unscaled Float32 GeoTiff: {out_float.name}...")
            _write_float32(
                out_float, abs_ds, ch1, ch2, ch3, float_desc, config,
                lut_file, annotation_file, product_folder
            )

        if progress_callback:
            progress_callback(0.85, f"Writing 8bit RGB: {out_rgb.name}...")

        if config.representation == DecompositionType.PAULI:
            r_u8, g_u8, b_u8 = stretch_pauli_to_rgb_uint8(
                ch1, ch2, ch3,
                stretch_method=config.stretch_method,
                scale=config.scale,
                quantile_threshold=config.quantile_threshold,
                cscale=config.cscale
            )
        else:
            r_u8, g_u8, b_u8 = hsv_to_rgb_uint8(ch1, ch2, ch3)

        _write_uint8_rgb(
            out_rgb, abs_ds, r_u8, g_u8, b_u8, valid_mask, config,
            lut_file, annotation_file, product_folder
        )

        if progress_callback:
            progress_callback(1.0, "Polarimetric decomposition complete.")

        return out_rgb, out_float

    finally:
        # flush GDAL global cache, run python garbace collection so files are never locked on disk
        safe_close_dataset(abs_ds, flush_global_cache=False, run_gc=False)
        safe_close_dataset(phase_ds, flush_global_cache=True, run_gc=True)
        abs_ds = None
        phase_ds = None
