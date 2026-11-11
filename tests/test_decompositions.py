"""Unit tests for Pauli and HSV polarimetric decompositions and QGIS processing algorithm.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from biomass_gateway.core.exceptions import DecompositionError
from biomass_gateway.core.models import (
    DecompositionConfig,
    DecompositionScale,
    DecompositionType,
    StretchMethod,
)
from biomass_gateway.processing.algorithms.decomposition_algorithm import (
    PolarimetricDecompositionAlgorithm,
)
from biomass_gateway.processing.polarimetry.decompositions import (
    apply_ical_phase_correction,
    compute_hsv,
    compute_pauli,
    hsv_to_rgb_uint8,
    run_polarimetric_decomposition,
    scale_channel_to_uint8,
    smooth_nan,
    stretch_pauli_to_rgb_uint8,
)


class TestPauliMath:
    """Tests for Pauli decomposition math across scales."""

    def test_pauli_surface_scattering(self) -> None:
        """Pure surface scatterer (HH == VV, HV == VH == 0) puts all energy in Blue."""
        shape = (4, 4)
        hh = np.full(shape, 2.0 + 0.0j, dtype=np.complex64)
        vv = np.full(shape, 2.0 + 0.0j, dtype=np.complex64)
        hv = np.zeros(shape, dtype=np.complex64)
        vh = np.zeros(shape, dtype=np.complex64)

        red, green, blue = compute_pauli(hh, hv, vh, vv, scale=DecompositionScale.LINEAR_AMPLITUDE)

        assert np.allclose(red, 0.0)
        assert np.allclose(green, 0.0)
        assert np.allclose(blue, 4.0 / np.sqrt(2.0))

    def test_pauli_double_bounce_scattering(self) -> None:
        """Pure dihedral scatterer (HH == -VV, HV == VH == 0) puts all energy in Red."""
        shape = (4, 4)
        hh = np.full(shape, 2.0 + 0.0j, dtype=np.complex64)
        vv = np.full(shape, -2.0 + 0.0j, dtype=np.complex64)
        hv = np.zeros(shape, dtype=np.complex64)
        vh = np.zeros(shape, dtype=np.complex64)

        red, green, blue = compute_pauli(hh, hv, vh, vv, scale=DecompositionScale.LINEAR_AMPLITUDE)

        assert np.allclose(red, 4.0 / np.sqrt(2.0))
        assert np.allclose(green, 0.0)
        assert np.allclose(blue, 0.0)

    def test_pauli_power_and_db_scales(self) -> None:
        """Verify power (squared) and dB (20*log10) scaling."""
        shape = (2, 2)
        hh = np.full(shape, np.sqrt(2.0) + 0.0j, dtype=np.complex64)
        vv = np.zeros(shape, dtype=np.complex64)
        hv = np.full(shape, np.sqrt(2.0) + 0.0j, dtype=np.complex64)
        vh = np.zeros(shape, dtype=np.complex64)

        r_amp, g_amp, b_amp = compute_pauli(hh, hv, vh, vv, scale=DecompositionScale.LINEAR_AMPLITUDE)
        r_pow, g_pow, b_pow = compute_pauli(hh, hv, vh, vv, scale=DecompositionScale.LINEAR_POWER)
        r_db, g_db, b_db = compute_pauli(hh, hv, vh, vv, scale=DecompositionScale.DB)

        # |HH - VV| / sqrt(2) = sqrt(2) / sqrt(2) = 1.0 -> Power = 1.0 -> dB = 0.0
        assert np.allclose(r_amp, 1.0)
        assert np.allclose(r_pow, 1.0)
        assert np.allclose(r_db, 0.0, atol=1e-5)
        assert np.allclose(g_db, 0.0, atol=1e-5)
        assert np.allclose(b_db, 0.0, atol=1e-5)


class TestHsvAndIcalMath:
    """Tests for HSV decomposition, iCal phase offsets, smoothing, and 8-bit conversion."""

    def test_apply_ical_phase_correction_preserves_amplitude(self) -> None:
        rng = np.random.default_rng(7)
        ch = (rng.standard_normal((8, 8)) + 1j * rng.standard_normal((8, 8))).astype(np.complex64)
        hh_c, hv_c, vh_c, vv_c = apply_ical_phase_correction(ch, ch, ch, ch, pc_hh=0.5, pc_hv=-1.2, pc_vh=0.8, pc_vv=0.0)

        assert np.allclose(np.abs(hh_c), np.abs(ch), atol=1e-5)
        assert np.allclose(np.abs(hv_c), np.abs(ch), atol=1e-5)
        assert np.allclose(vv_c, ch)

    def test_hsv_bounds_and_surface_alpha_zero(self) -> None:
        """Pure surface scatterer (p1 == Span) yields alpha (Hue) == 0."""
        shape = (6, 6)
        hh = np.full(shape, 1.5 + 0.0j, dtype=np.complex64)
        vv = np.full(shape, 1.5 + 0.0j, dtype=np.complex64)
        hv = np.zeros(shape, dtype=np.complex64)
        vh = np.zeros(shape, dtype=np.complex64)

        hue, sat, val = compute_hsv(hh, hv, vh, vv, kscale=2.5)

        assert np.allclose(hue, 0.0, atol=1e-6)
        assert np.allclose(sat, 1.0)
        assert np.all((val >= 0.0) & (val <= 1.0))

        r_u8, g_u8, b_u8 = hsv_to_rgb_uint8(hue, sat, val)
        assert r_u8.dtype == np.uint8
        assert r_u8.shape == shape
        assert np.all(b_u8 > 0)

    def test_smooth_nan_ignores_nans_and_preserves_nan_mask(self) -> None:
        arr = np.ones((6, 6), dtype=np.float32) * 10.0
        arr[0, 0] = np.nan
        arr[5, 5] = np.nan

        smoothed = smooth_nan(arr, (3, 3))
        # Valid interior pixels remain 10.0 without being dragged down by NaN neighbors
        assert np.isclose(smoothed[1, 1], 10.0)
        assert np.isnan(smoothed[0, 0])
        assert np.isnan(smoothed[5, 5])

    def test_stretch_pauli_erves_nonzero_for_valid_dark_pixels(self) -> None:
        """Valid dark pixels (e.g. water) map to >= 1 while NaN maps to 0."""
        ch = np.array([[0.001, 1.0], [2.0, np.nan]], dtype=np.float32)
        u8 = scale_channel_to_uint8(ch, vmin=0.0, vmax=2.0)

        assert u8[0, 0] >= 1  # Valid dark pixel is not 0
        assert u8[1, 0] == 255
        assert u8[1, 1] == 0  # NaN pixel is 0

    def test_stretch_pauli_mean_and_db_incompatible_raises(self) -> None:
        ch = np.ones((4, 4), dtype=np.float32)
        with pytest.raises(DecompositionError, match="not compatible with dB"):
            stretch_pauli_to_rgb_uint8(
                ch, ch, ch,
                stretch_method=StretchMethod.MEAN,
                scale=DecompositionScale.DB,
            )


class TestDecompositionDriverAndAlgorithm:
    """Tests for GDAL driver and PolarimetricDecompositionAlgorithm."""

    def test_missing_files_raise_decomposition_error(self, tmp_path: Path) -> None:
        with pytest.raises(DecompositionError, match="not found"):
            run_polarimetric_decomposition(
                abs_raster_path=tmp_path / "missing_abs.tiff",
                phase_raster_path=tmp_path / "missing_phase.tiff",
                output_rgb_path=tmp_path / "out.tiff",
            )

    def test_driver_execution_pauli_and_hsv(self, tmp_path: Path) -> None:
        """Test run_polarimetric_decomposition end-to-end with mocked GDAL datasets."""
        abs_file = tmp_path / "test_i_abs.tiff"
        pha_file = tmp_path / "test_i_phase.tiff"
        abs_file.write_bytes(b"fake")
        pha_file.write_bytes(b"fake")
        out_rgb = tmp_path / "pauli_rgb.tiff"
        out_flt = tmp_path / "pauli_flt.tiff"

        rng = np.random.default_rng(42)
        amp_bands = [rng.uniform(0.5, 5.0, (16, 16)).astype(np.float32) for _ in range(4)]
        pha_bands = [rng.uniform(-np.pi, np.pi, (16, 16)).astype(np.float32) for _ in range(4)]

        def _make_src_ds(bands_data: list[np.ndarray]) -> MagicMock:
            ds = MagicMock()
            ds.RasterCount = 4
            ds.RasterXSize = 16
            ds.RasterYSize = 16
            ds.GetGCPs.return_value = []
            ds.GetProjection.return_value = ""
            ds.GetGeoTransform.return_value = None
            ds.GetMetadata.return_value = {}

            def _get_band(idx: int) -> MagicMock:
                b = MagicMock()
                b.ReadAsArray.return_value = bands_data[idx - 1]
                b.GetNoDataValue.return_value = -9999.0
                return b

            ds.GetRasterBand.side_effect = _get_band
            return ds

        written_arrays: list[np.ndarray] = []
        out_ds_mock = MagicMock()
        out_band_mock = MagicMock()
        out_band_mock.WriteArray.side_effect = lambda arr: written_arrays.append(arr.copy())
        out_ds_mock.GetRasterBand.return_value = out_band_mock

        driver_mock = MagicMock()
        driver_mock.Create.return_value = out_ds_mock

        with (
            patch("biomass_gateway.processing.polarimetry.decompositions.gdal.Open") as mock_open,
            patch("biomass_gateway.processing.polarimetry.decompositions.gdal.GetDriverByName", return_value=driver_mock),
        ):
            mock_open.side_effect = [_make_src_ds(amp_bands), _make_src_ds(pha_bands)]
            rgb_res, flt_res = run_polarimetric_decomposition(
                abs_raster_path=abs_file,
                phase_raster_path=pha_file,
                output_rgb_path=out_rgb,
                output_float_path=out_flt,
                config=DecompositionConfig(
                    representation=DecompositionType.PAULI,
                    smooth_window=(3, 3),
                    add_alpha=True,
                    fix_ical_phase=True,
                ),
            )

        assert rgb_res == out_rgb.resolve()
        assert flt_res == out_flt.resolve()
        # 3 float bands + 4 RGBA uint8 bands = 7 bands written
        assert len(written_arrays) == 7
        assert written_arrays[-1].dtype == np.uint8
        assert np.all(written_arrays[-1] == 255)  # All valid pixels have Alpha == 255

        # Also verify 3-band symmetrized L1C (HH, XX, VV) works cleanly
        written_arrays.clear()
        amp_3bands = amp_bands[:3]
        pha_3bands = pha_bands[:3]

        def _make_3band_ds(bands_data: list[np.ndarray]) -> MagicMock:
            ds = _make_src_ds(bands_data)
            ds.RasterCount = 3
            return ds

        with (
            patch("biomass_gateway.processing.polarimetry.decompositions.gdal.Open") as mock_open,
            patch("biomass_gateway.processing.polarimetry.decompositions.gdal.GetDriverByName", return_value=driver_mock),
        ):
            mock_open.side_effect = [_make_3band_ds(amp_3bands), _make_3band_ds(pha_3bands)]
            rgb_3b, _ = run_polarimetric_decomposition(
                abs_raster_path=abs_file,
                phase_raster_path=pha_file,
                output_rgb_path=out_rgb,
                config=DecompositionConfig(representation=DecompositionType.PAULI, smooth_window=None),
            )
        assert rgb_3b == out_rgb.resolve()
        assert len(written_arrays) == 4

    def test_algorithm_metadata_and_parameters(self) -> None:
        alg = PolarimetricDecompositionAlgorithm()
        assert alg.name() == "polarimetric_decomposition"
        assert alg.displayName() == "Polarimetric decomposition (Pauli / HSV)"
        assert alg.group() == "Polarimetry"
        assert alg.groupId() == "polarimetry"

        alg.initAlgorithm()
        param_names = [p.name() for p in alg.parameterDefinitions()]
        for expected in (
            "INPUT",
            "AMPLITUDE_RASTER",
            "PHASE_RASTER",
            "REPRESENTATION",
            "SCALE",
            "STRETCH_METHOD",
            "SMOOTH_LINES",
            "SMOOTH_SAMPLES",
            "ADD_ALPHA",
            "FIX_ICAL_PHASE",
            "OUTPUT_RGB",
            "OUTPUT_FLOAT",
        ):
            assert expected in param_names
