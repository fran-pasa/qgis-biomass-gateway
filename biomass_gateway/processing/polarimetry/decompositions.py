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
    # green = surface scattering
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
    QGIS setups. """
    xc = np.clip(x, 0.0, 1.0).astype(np.float32)
    r = (
            0.13572138
            + xc * (4.61539260 + xc * (-42.66032258 + xc * (132.13108234 + xc * (-152.94239396 + xc *
                                                                                 59.28637943))))
    )
    g = (
            0.09140261
            + xc * (2.19418839 + xc * (4.84296658 + xc * (-14.18503333 + xc * (4.27729857 + xc * 2.82956604))))
    )
    b = (
            0.10667330
            + xc * (12.64194608 + xc * (-60.58204836 + xc * (110.36276771 + xc * (-89.90310912 + xc *
                                                                                  27.34824973))))
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
    scaled = (clipped - vmin) / (vmax - vmin) * 255.0
    return np.where(valid, np.clip(scaled, 0.0, 255.0), 0.0).astype(np.uint8)


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
            vmin = float(np.quantile(valid, q))
            vmax = float(np.quantile(valid, 1.0 - q))
        elif stretch_method == StretchMethod.MEAN:
            vmin = 0.0
            vmax = float(cscale * np.mean(valid))
        else:
            raise DecompositionError(f"Unsupported stretch method: {stretch_method}")

        out_channels.append(scale_channel_to_uint8(ch, vmin, vmax))

    return out_channels[0], out_channels[1], out_channels[2]
