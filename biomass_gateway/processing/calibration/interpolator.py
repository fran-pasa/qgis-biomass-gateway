"""
Bilinear interpolation of radiometric LUT onto the SAR image grid.
"""

import numpy as np
from scipy.interpolate import RectBivariateSpline

from ...core.exceptions import CalibrationError
from ...core.models import LutData, SarImageGeometry


def interpolate_gamma_lut(
    lut: LutData,
    geometry: SarImageGeometry,
    progress_callback=None,
) -> np.ndarray:
    """Interpolate Gamma Nought LUT values for each pixel in the SAR image grid.

    Args:
        lut: Extracted LutData containing gamma_lut and LUT coordinate axes.
        geometry: Target SarImageGeometry containing image pixel time coordinates.
        progress_callback: Optional callable(float, str) for progress reporting.

    Returns:
        2D float32 array of shape (n_lines, n_samples) with interpolated Gamma Nought values.

    Raises:
        CalibrationError: If the image is not covered by the LUT or interpolation fails.
    """
    invalid_count = int(np.count_nonzero(~np.isfinite(lut.gamma_lut)))
    if invalid_count > 0:
        raise CalibrationError(f"LUT contains {invalid_count} invalid (non-finite) values.")

    # Verify coverage
    min_az = geometry.azimuth_axis[0]
    max_az = geometry.azimuth_axis[-1]
    if min_az < lut.azimuth_axis[0] or max_az > lut.azimuth_axis[-1]:
        raise CalibrationError(
            f"SAR image azimuth range [{min_az:.4f}, {max_az:.4f}] s is not fully covered by "
            f"LUT azimuth range [{lut.azimuth_axis[0]:.4f}, {lut.azimuth_axis[-1]:.4f}] s."
        )

    min_rg = float(np.nanmin(geometry.range_time))
    max_rg = float(np.nanmax(geometry.range_time))
    if min_rg < lut.range_axis[0] or max_rg > lut.range_axis[-1]:
        raise CalibrationError(
            f"SAR image range [{min_rg:.6e}, {max_rg:.6e}] s is not fully covered by "
            f"LUT range [{lut.range_axis[0]:.6e}, {lut.range_axis[-1]:.6e}] s."
        )

    spline = RectBivariateSpline(lut.azimuth_axis, lut.range_axis, lut.gamma_lut, kx=1, ky=1, s=0.0)

    # Slant Range (1D range axis common to all lines)
    if geometry.range_time.ndim == 1:
        if progress_callback:
            progress_callback(0.3, "Interpolating LUT on slant-range grid...")
        out = spline(geometry.azimuth_axis, geometry.range_time, grid=True)
        return out.astype(np.float32)

    # Ground Range (2D range time, line-by-line evaluation to conserve memory)
    if geometry.range_time.ndim == 2:
        n_lines, n_samples = geometry.n_lines, geometry.n_samples
        out = np.empty((n_lines, n_samples), dtype=np.float32)

        report_step = max(1, n_lines // 10)
        for line in range(n_lines):
            az_line = np.full(n_samples, geometry.azimuth_axis[line], dtype=np.float64)
            out[line, :] = spline.ev(az_line, geometry.range_time[line, :]).astype(np.float32)

            if progress_callback and (line % report_step == 0 or line == n_lines - 1):
                fraction = 0.1 + 0.3 * (line / n_lines)
                progress_callback(fraction, f"Interpolating LUT lines ({line}/{n_lines})...")

        return out

    raise CalibrationError(f"Unexpected range_time dimensions: {geometry.range_time.ndim}")
