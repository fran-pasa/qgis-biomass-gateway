"""SAR image pixel timing coordinates and geometry parser.
"""

from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np

from ...core.exceptions import CalibrationError
from ...core.models import SarImageGeometry


def parse_utc(value: str) -> datetime:
    """Convert an ISO-8601 UTC string into a timezone-aware datetime object.
    """
    cleaned = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)


def read_sar_image_geometry(
    annot_xml_path: Path | str,
    reference_azimuth_time: str,
) -> SarImageGeometry:
    """Build the temporal coordinates of image pixels in the LUT reference frame.

    Supports both Slant Range (L1A/SCS, L1C) and Ground Range (L1B/DGM).

    Args:
        annot_xml_path: Path to the annotation XML file.
        reference_azimuth_time: ISO-8601 string of the LUT reference azimuth time.

    Returns:
        SarImageGeometry containing target azimuth axis, range time (1D for slant, 2D for ground),
        image dimensions, and projection type.

    Raises:
        CalibrationError: If annotation XML is missing or geometry calculations fail consistency checks.
    """
    path = Path(annot_xml_path).resolve()
    if not path.is_file():
        raise CalibrationError(f"Annotation XML not found: {path}")

    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as err:
        raise CalibrationError(f"Failed to parse annotation XML {path}: {err}") from err

    sar_image = root.find(".//sarImage")
    if sar_image is None:
        raise CalibrationError(f"<sarImage> element not found in {path}")

    n_samples_str = sar_image.findtext("numberOfSamples")
    n_lines_str = sar_image.findtext("numberOfLines")
    if not n_samples_str or not n_lines_str:
        raise CalibrationError("Missing numberOfSamples or numberOfLines in annotation.")

    n_samples = int(n_samples_str)
    n_lines = int(n_lines_str)

    # -------------------------------------------------------------------------
    # Azimuth timing
    # -------------------------------------------------------------------------
    first_az_str = sar_image.findtext("firstLineAzimuthTime")
    az_interval_str = sar_image.findtext("azimuthTimeInterval")
    last_az_str = sar_image.findtext("lastLineAzimuthTime")

    if not first_az_str or not az_interval_str:
        raise CalibrationError("Missing azimuth timing elements in annotation.")

    first_az = parse_utc(first_az_str)
    az_interval = float(az_interval_str)
    reference_az = parse_utc(reference_azimuth_time)

    first_az_relative = (first_az - reference_az).total_seconds()
    target_az_axis = first_az_relative + np.arange(n_lines, dtype=np.float64) * az_interval

    if last_az_str:
        last_az = parse_utc(last_az_str)
        last_az_relative = (last_az - reference_az).total_seconds()
        if not np.isclose(target_az_axis[-1], last_az_relative, atol=1e-6, rtol=0.0):
            raise CalibrationError("Inconsistent azimuth axis: computed last line does not match annotation.")

    projection_str = (sar_image.findtext("projection") or "").strip()

    # -------------------------------------------------------------------------
    # Slant Range projection (L1A / SCS, L1C)
    # -------------------------------------------------------------------------
    if projection_str == "Slant Range":
        first_rg_str = sar_image.findtext("firstSampleSlantRangeTime")
        rg_interval_str = sar_image.findtext("rangeTimeInterval")
        last_rg_str = sar_image.findtext("lastSampleSlantRangeTime")

        if not first_rg_str or not rg_interval_str:
            raise CalibrationError("Missing slant range timing parameters in annotation.")

        first_rg = float(first_rg_str)
        rg_interval = float(rg_interval_str)
        target_rg_axis = first_rg + np.arange(n_samples, dtype=np.float64) * rg_interval

        if last_rg_str:
            last_rg = float(last_rg_str)
            if not np.isclose(target_rg_axis[-1], last_rg, atol=1e-12, rtol=0.0):
                raise CalibrationError(
                    "Inconsistent slant-range axis: computed last sample does not match annotation."
                )

        return SarImageGeometry(
            azimuth_axis=target_az_axis,
            range_time=target_rg_axis,
            n_lines=n_lines,
            n_samples=n_samples,
            projection=projection_str,
        )

    # -------------------------------------------------------------------------
    # Ground Range projection (L1B / DGM)
    # -------------------------------------------------------------------------
    if projection_str == "Ground Range":
        spacing_str = sar_image.findtext("rangePixelSpacing")
        if not spacing_str:
            raise CalibrationError("Missing rangePixelSpacing in Ground Range annotation.")
        range_pixel_spacing = float(spacing_str)

        conversions = sar_image.findall("rangeCoordinateConversion/coordinateConversion")
        if not conversions:
            raise CalibrationError("No rangeCoordinateConversion records found for Ground Range product.")

        conv_times_list: list[float] = []
        conv_times_list: list[float] = []
        conv_coeffs_list: list[np.ndarray] = []

        for conv in conversions:
            az_str = conv.findtext("azimuthTime")
            coeffs_str = conv.findtext("groundToSlantCoefficients")
            if not az_str or not coeffs_str:
                raise CalibrationError("Malformed coordinateConversion record in annotation XML.")

            conv_az = parse_utc(az_str)
            conv_times_list.append((conv_az - reference_az).total_seconds())
            coeffs = np.asarray(coeffs_str.split(), dtype=np.float64)
            conv_coeffs_list.append(coeffs)

        conv_times = np.asarray(conv_times_list, dtype=np.float64)

        n_coeff = len(conv_coeffs_list[0])
        if any(len(c) != n_coeff for c in conv_coeffs_list):
            raise CalibrationError("groundToSlant polynomials have inconsistent coefficient lengths.")

        conv_coeffs = np.vstack(conv_coeffs_list)

        # Interpolate polynomial coefficients along azimuth lines
        line_coeffs = np.empty((n_lines, n_coeff), dtype=np.float64)
        for k in range(n_coeff):
            line_coeffs[:, k] = np.interp(target_az_axis, conv_times, conv_coeffs[:, k])

        # Evaluate 2D slant-range time for each line
        target_rg_time = np.empty((n_lines, n_samples), dtype=np.float64)
        delta_gr = np.arange(n_samples, dtype=np.float64) * range_pixel_spacing

        for line in range(n_lines):
            target_rg_time[line, :] = np.polynomial.polynomial.polyval(delta_gr, line_coeffs[line])

        first_rg_str = sar_image.findtext("firstSampleSlantRangeTime")
        last_rg_str = sar_image.findtext("lastSampleSlantRangeTime")

        if first_rg_str:
            first_rg = float(first_rg_str)
            if not np.allclose(target_rg_time[:, 0], first_rg, atol=1e-12, rtol=0.0):
                raise CalibrationError("Inconsistent slant-range time for first sample.")

        if last_rg_str:
            last_rg = float(last_rg_str)
            if not np.allclose(target_rg_time[:, -1], last_rg, atol=1e-12, rtol=0.0):
                raise CalibrationError("Inconsistent slant-range time for last sample.")

        return SarImageGeometry(
            azimuth_axis=target_az_axis,
            range_time=target_rg_time,
            n_lines=n_lines,
            n_samples=n_samples,
            projection=projection_str,
        )

    raise CalibrationError(f"Unsupported SAR projection: '{projection_str}'")
