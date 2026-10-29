"""
Polarimetric decompositions for ESA BIOMASS SLC products.
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
        scale: DecompositionScale = DecompositionScale.Linear,
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