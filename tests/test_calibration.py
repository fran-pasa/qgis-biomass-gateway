"""Unit tests for radiometric calibration module and processing algorithm.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import numpy as np
import pytest

from biomass_gateway.core.exceptions import CalibrationError
from biomass_gateway.core.models import LutData, SarImageGeometry
from biomass_gateway.processing.algorithms.calibrate_algorithm import CalibrateGamma0Algorithm
from biomass_gateway.processing.calibration.geometry import parse_utc, read_sar_image_geometry
from biomass_gateway.processing.calibration.interpolator import interpolate_gamma_lut


class TestSarGeometry:
    """Tests for SAR image geometry parsing."""

    def test_parse_utc(self) -> None:
        dt = parse_utc("2026-08-28T21:28:39.000000Z")
        assert dt.year == 2026
        assert dt.month == 8
        assert dt.day == 28
        assert dt.hour == 21
        assert dt.minute == 28
        assert dt.second == 39
        assert dt.tzinfo == UTC

    def test_read_slant_range_geometry(self, tmp_path: Path) -> None:
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <product>
            <sarImage>
                <numberOfSamples>5</numberOfSamples>
                <numberOfLines>4</numberOfLines>
                <firstLineAzimuthTime>2026-08-28T21:28:39.000000Z</firstLineAzimuthTime>
                <lastLineAzimuthTime>2026-08-28T21:28:42.000000Z</lastLineAzimuthTime>
                <azimuthTimeInterval>1.0</azimuthTimeInterval>
                <projection>Slant Range</projection>
                <firstSampleSlantRangeTime>0.003</firstSampleSlantRangeTime>
                <lastSampleSlantRangeTime>0.007</lastSampleSlantRangeTime>
                <rangeTimeInterval>0.001</rangeTimeInterval>
            </sarImage>
        </product>
        """
        xml_file = tmp_path / "test_annot.xml"
        xml_file.write_text(xml_content, encoding="utf-8")

        ref_time = "2026-08-28T21:28:39.000000Z"
        geom = read_sar_image_geometry(xml_file, ref_time)

        assert geom.n_lines == 4
        assert geom.n_samples == 5
        assert geom.projection == "Slant Range"
        assert np.allclose(geom.azimuth_axis, [0.0, 1.0, 2.0, 3.0])
        assert geom.range_time.ndim == 1
        assert np.allclose(geom.range_time, [0.003, 0.004, 0.005, 0.006, 0.007])

    def test_read_ground_range_geometry(self, tmp_path: Path) -> None:
        # Polynomial: delta_gr * 0.00001 + 0.003
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
        <product>
            <sarImage>
                <numberOfSamples>3</numberOfSamples>
                <numberOfLines>2</numberOfLines>
                <firstLineAzimuthTime>2026-08-28T21:28:39.000000Z</firstLineAzimuthTime>
                <lastLineAzimuthTime>2026-08-28T21:28:40.000000Z</lastLineAzimuthTime>
                <azimuthTimeInterval>1.0</azimuthTimeInterval>
                <projection>Ground Range</projection>
                <rangePixelSpacing>10.0</rangePixelSpacing>
                <firstSampleSlantRangeTime>0.003</firstSampleSlantRangeTime>
                <lastSampleSlantRangeTime>0.0032</lastSampleSlantRangeTime>
                <rangeCoordinateConversion>
                    <coordinateConversion>
                        <azimuthTime>2026-08-28T21:28:39.000000Z</azimuthTime>
                        <gr0>0.0</gr0>
                        <groundToSlantCoefficients>0.003 0.00001</groundToSlantCoefficients>
                    </coordinateConversion>
                </rangeCoordinateConversion>
            </sarImage>
        </product>
        """
        xml_file = tmp_path / "test_dgm_annot.xml"
        xml_file.write_text(xml_content, encoding="utf-8")

        ref_time = "2026-08-28T21:28:39.000000Z"
        geom = read_sar_image_geometry(xml_file, ref_time)

        assert geom.n_lines == 2
        assert geom.n_samples == 3
        assert geom.projection == "Ground Range"
        assert geom.range_time.ndim == 2
        assert geom.range_time.shape == (2, 3)
        assert np.isclose(geom.range_time[0, 0], 0.003)
        assert np.isclose(geom.range_time[0, -1], 0.0032)

    def test_missing_xml_raises_calibration_error(self, tmp_path: Path) -> None:
        non_existent = tmp_path / "does_not_exist.xml"
        with pytest.raises(CalibrationError, match="not found"):
            read_sar_image_geometry(non_existent, "2026-08-28T21:28:39Z")


class TestLutInterpolation:
    """Tests for LUT spline interpolation."""

    def test_slant_range_interpolation(self) -> None:
        # Simple 3x3 LUT
        az_axis = np.array([0.0, 5.0, 10.0])
        rg_axis = np.array([0.001, 0.005, 0.010])
        gamma_lut = np.array([
            [1.0, 2.0, 3.0],
            [2.0, 4.0, 6.0],
            [3.0, 6.0, 9.0],
        ], dtype=np.float64)

        lut = LutData(
            gamma_lut=gamma_lut,
            azimuth_axis=az_axis,
            range_axis=rg_axis,
            reference_azimuth_time="2026-08-28T21:28:39Z",
        )

        geom = SarImageGeometry(
            azimuth_axis=np.array([2.5, 7.5]),
            range_time=np.array([0.003, 0.007]),
            n_lines=2,
            n_samples=2,
            projection="Slant Range",
        )

        interpolated = interpolate_gamma_lut(lut, geom)
        assert interpolated.shape == (2, 2)
        assert interpolated.dtype == np.float32
        # Check that interpolated values are bounded between 1.0 and 9.0
        assert np.all(interpolated >= 1.0)
        assert np.all(interpolated <= 9.0)

    def test_lut_out_of_bounds_raises(self) -> None:
        az_axis = np.array([0.0, 1.0])
        rg_axis = np.array([0.001, 0.002])
        gamma_lut = np.ones((2, 2), dtype=np.float64)

        lut = LutData(
            gamma_lut=gamma_lut,
            azimuth_axis=az_axis,
            range_axis=rg_axis,
            reference_azimuth_time="2026-08-28T21:28:39Z",
        )

        # Azimuth exceeds LUT
        geom = SarImageGeometry(
            azimuth_axis=np.array([0.0, 5.0]),
            range_time=np.array([0.001, 0.002]),
            n_lines=2,
            n_samples=2,
            projection="Slant Range",
        )

        with pytest.raises(CalibrationError, match="not fully covered"):
            interpolate_gamma_lut(lut, geom)


class TestCalibrationAlgorithm:
    """Tests for CalibrateGamma0Algorithm registration and properties."""

    def test_algorithm_metadata(self) -> None:
        alg = CalibrateGamma0Algorithm()
        assert alg.name() == "calibrate_gamma0"
        assert alg.displayName() == "Calibrate to Gamma0 (dB)"
        assert alg.group() == "Calibration"
        assert alg.groupId() == "calibration"
        assert alg.createInstance() is not None

    def test_init_algorithm_parameters(self) -> None:
        alg = CalibrateGamma0Algorithm()
        alg.initAlgorithm()

        param_names = [p.name() for p in alg.parameterDefinitions()]
        assert "INPUT" in param_names
        assert "ANNOTATION_XML" in param_names
        assert "LUT_NC" in param_names
        assert "OUTPUT" in param_names
