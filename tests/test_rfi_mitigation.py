"""Unit tests for Lambda-1 BSF RFI mitigation engine and QGIS processing algorithm.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from osgeo import gdal, osr
import pytest

from biomass_gateway.core.exceptions import RfiMitigationError
from biomass_gateway.processing.algorithms.rfi_algorithm import RfiMitigationAlgorithm
from biomass_gateway.processing.rfi.detector import (
    get_eig_th,
    lambda1_bsf,
    mitigate_rfi_slc,
    ups_bdr,
)


def _create_mock_annotation_xml(xml_path: Path, fs_az=1500.0, b_az=1000.0, fs_rg=1500.0, b_rg=1000.0) -> Path:
    """Generate a minimal Biomass annotation XML with processing bandwidths."""
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
    <product>
        <azimuthProcessingParameters>
            <totalBandwidth>{fs_az}</totalBandwidth>
            <processingBandwidth>{b_az}</processingBandwidth>
            <windowCoefficient>0.75</windowCoefficient>
        </azimuthProcessingParameters>
        <rangeProcessingParameters>
            <totalBandwidth>{fs_rg}</totalBandwidth>
            <processingBandwidth>{b_rg}</processingBandwidth>
            <windowCoefficient>0.75</windowCoefficient>
        </rangeProcessingParameters>
    </product>
    """
    xml_path.write_text(content, encoding="utf-8")
    return xml_path


def _create_synthetic_slc_tiffs(
    amp_path: Path,
    phase_path: Path,
    shape: tuple[int, int] = (600, 700),
    num_bands: int = 4,
    inject_rfi: bool = True,
) -> tuple[Path, Path]:
    """Create synthetic multi-band amplitude and phase GeoTIFFs with optional RFI."""
    rows, cols = shape
    rng = np.random.default_rng(42)

    # Base noise SLC
    real = rng.standard_normal((num_bands, rows, cols)).astype(np.float32)
    imag = rng.standard_normal((num_bands, rows, cols)).astype(np.float32)

    if inject_rfi:
        # Inject persistent interference along columns 100..160
        rfi_real = 2.0 * np.cos(rng.uniform(0, 2 * np.pi, (num_bands, rows, 1))).astype(np.float32)
        rfi_imag = 2.0 * np.sin(rng.uniform(0, 2 * np.pi, (num_bands, rows, 1))).astype(np.float32)
        real[:, :, 100:160] += rfi_real
        imag[:, :, 100:160] += rfi_imag

    amp = np.hypot(real, imag)
    phase = np.arctan2(imag, real)

    driver = gdal.GetDriverByName("GTiff")
    amp_ds = driver.Create(str(amp_path), cols, rows, num_bands, gdal.GDT_Float32)
    phase_ds = driver.Create(str(phase_path), cols, rows, num_bands, gdal.GDT_Float32)

    # Add mock GCPs
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    gcps = [
        gdal.GCP(10.0, 45.0, 0.0, 0, 0, "1", "1"),
        gdal.GCP(11.0, 45.0, 0.0, cols, 0, "2", "2"),
        gdal.GCP(10.0, 44.0, 0.0, 0, rows, "3", "3"),
        gdal.GCP(11.0, 44.0, 0.0, cols, rows, "4", "4"),
    ]
    amp_ds.SetGCPs(gcps, srs.ExportToWkt())
    phase_ds.SetGCPs(gcps, srs.ExportToWkt())

    pols = ["HH", "HV", "VH", "VV"]
    for b in range(1, num_bands + 1):
        desc = pols[b - 1] if b <= len(pols) else f"Band {b}"
        ab = amp_ds.GetRasterBand(b)
        ab.SetDescription(desc)
        ab.WriteArray(amp[b - 1])

        pb = phase_ds.GetRasterBand(b)
        pb.SetDescription(desc)
        pb.WriteArray(phase[b - 1])

    amp_ds = None
    phase_ds = None
    return amp_path, phase_path


class TestLambda1BsEngine:
    """Tests for core Tracy-Widom thresholding and block subspace filter."""

    def test_ups_bdr_parsing(self, tmp_path: Path):
        xml_file = tmp_path / "test_annot.xml"
        _create_mock_annotation_xml(xml_file, fs_az=1500.0, b_az=1000.0, fs_rg=1800.0, b_rg=1200.0)

        p = ups_bdr(xml_file)
        assert np.isclose(p["ups1"], 1.5)
        assert np.isclose(p["ups2"], 1.5)
        # For a=0.75, bdr = a^2 + (1-a)^2 / 2 = 0.5625 + 0.03125 = 0.59375
        assert np.isclose(p["bdr1"], 0.75**2 + (1 - 0.75) ** 2 / 2)
        assert np.isclose(p["bdr2"], p["bdr1"])

    def test_ups_bdr_missing_file_raises(self, tmp_path: Path):
        non_existent = tmp_path / "non_existent.xml"
        with pytest.raises(RfiMitigationError, match="not found"):
            ups_bdr(non_existent)

    def test_ups_bdr_missing_tags_raises(self, tmp_path: Path):
        bad_xml = tmp_path / "bad.xml"
        bad_xml.write_text("<root><empty/></root>", encoding="utf-8")
        with pytest.raises(RfiMitigationError, match="Missing parameter"):
            ups_bdr(bad_xml)

    def test_get_eig_th(self):
        th = get_eig_th(var=1.0, n=500, p=500, gamma=1.5, ups1=1.5, ups2=1.5, bdr1=0.7, bdr2=0.7)
        assert isinstance(th, (float, np.floating))
        assert th > 0.0

    def test_lambda1_bsf_image_domain(self):
        rng = np.random.default_rng(0)
        # 4 pols, 600 x 700
        S = (rng.standard_normal((4, 600, 700)) + 1j * rng.standard_normal((4, 600, 700))).astype(np.complex64)
        # Inject broadband RFI in a block of columns
        S[:, :, 100:160] += 1.5 * np.exp(1j * rng.uniform(0, 6, (4, 600, 1)))

        kw = dict(gamma=1.0, ups1=1.5, ups2=1.5, bdr1=0.7, bdr2=0.7)
        q = []
        out, mask = lambda1_bsf(S, q_out=q, **kw)

        assert out.shape == S.shape
        assert mask.shape == S.shape[1:]
        assert mask.any()
        assert len(q) > 0
        # Check power reduction
        assert (np.abs(out) ** 2).mean() < (np.abs(S) ** 2).mean()

    def test_lambda1_bsf_single_channel(self):
        rng = np.random.default_rng(0)
        S = (rng.standard_normal((600, 700)) + 1j * rng.standard_normal((600, 700))).astype(np.complex64)
        kw = dict(gamma=1.0, ups1=1.5, ups2=1.5, bdr1=0.7, bdr2=0.7)
        out, mask = lambda1_bsf(S, **kw)
        assert out.shape == (600, 700)
        assert mask.shape == (600, 700)

    def test_lambda1_bsf_freq_domain(self):
        rng = np.random.default_rng(0)
        S = (rng.standard_normal((4, 600, 700)) + 1j * rng.standard_normal((4, 600, 700))).astype(np.complex64)
        # Inject narrowband CW interference at a fixed frequency
        S += 2.0 * np.exp(1j * 2 * np.pi * 0.1 * np.arange(700)[None, None, :])

        kw = dict(gamma=1.0, ups1=1.5, ups2=1.5, bdr1=0.7, bdr2=0.7)
        q = []
        out, mask = lambda1_bsf(S, lblk=(500, 100), lpd=(50, 0), domain="freq", q_out=q, **kw)

        assert out.shape == S.shape
        assert mask.shape == S.shape[1:]
        assert (np.abs(out) ** 2).mean() < (np.abs(S) ** 2).mean()


class TestMitigateRfiSlcDriver:
    """Tests for end-to-end GeoTIFF I/O and metadata preservation."""

    def test_mitigate_rfi_slc_execution(self, tmp_path: Path):
        amp_file = tmp_path / "BIO_S1_SCS__1S_i_abs.tiff"
        phase_file = tmp_path / "BIO_S1_SCS__1S_i_phase.tiff"
        xml_file = tmp_path / "BIO_S1_SCS__1S_annot.xml"

        _create_mock_annotation_xml(xml_file)
        _create_synthetic_slc_tiffs(amp_file, phase_file, shape=(600, 700), num_bands=4, inject_rfi=True)

        out_amp = tmp_path / "clean_abs.tif"
        out_mask = tmp_path / "rfi_mask.tif"
        out_phase = tmp_path / "clean_phase.tif"

        progress_calls = []

        def _cb(fraction: float, msg: str):
            progress_calls.append(fraction)

        stats = mitigate_rfi_slc(
            amplitude_raster_path=amp_file,
            phase_raster_path=phase_file,
            annot_xml_path=xml_file,
            output_amplitude_path=out_amp,
            output_mask_path=out_mask,
            output_phase_path=out_phase,
            gamma=1.0,
            progress_callback=_cb,
        )

        assert out_amp.is_file()
        assert out_mask.is_file()
        assert out_phase.is_file()
        assert len(progress_calls) > 0
        assert stats["flagged_ratio"] > 0.0

        # Check output amplitude dataset
        ds_amp = gdal.Open(str(out_amp), gdal.GA_ReadOnly)
        assert ds_amp.RasterXSize == 700
        assert ds_amp.RasterYSize == 600
        assert ds_amp.RasterCount == 4
        b1 = ds_amp.GetRasterBand(1)
        assert b1.GetDescription() == "HH"
        assert b1.GetOverviewCount() > 0
        assert b1.GetMetadataItem("STATISTICS_MAXIMUM") is not None
        assert len(ds_amp.GetGCPs()) == 4
        ds_amp = None

        # Check mask dataset
        ds_mask = gdal.Open(str(out_mask), gdal.GA_ReadOnly)
        assert ds_mask.RasterXSize == 700
        assert ds_mask.RasterYSize == 600
        assert ds_mask.RasterCount == 1
        mask_b = ds_mask.GetRasterBand(1)
        assert mask_b.GetOverviewCount() > 0
        assert mask_b.GetMetadataItem("STATISTICS_MAXIMUM") is not None
        mask_arr = mask_b.ReadAsArray()
        assert mask_arr.dtype == np.uint8
        assert set(np.unique(mask_arr)).issubset({0, 1})
        assert 1 in np.unique(mask_arr)  # RFI was flagged
        ds_mask = None

    def test_mitigate_rfi_slc_missing_phase_raises(self, tmp_path: Path):
        amp_file = tmp_path / "BIO_S1_SCS__1S_i_abs.tiff"
        xml_file = tmp_path / "BIO_S1_SCS__1S_annot.xml"
        _create_mock_annotation_xml(xml_file)
        _create_synthetic_slc_tiffs(amp_file, tmp_path / "dummy_phase.tiff", shape=(200, 200))

        non_existent_phase = tmp_path / "missing_phase.tiff"

        with pytest.raises(RfiMitigationError, match="Phase measurement raster not found"):
            mitigate_rfi_slc(
                amplitude_raster_path=amp_file,
                phase_raster_path=non_existent_phase,
                annot_xml_path=xml_file,
                output_amplitude_path=tmp_path / "out_amp.tif",
                output_mask_path=tmp_path / "out_mask.tif",
            )


class TestRfiProcessingAlgorithm:
    """Tests for QGIS Processing Algorithm registration, metadata, and parameter definitions."""

    def test_algorithm_metadata(self):
        alg = RfiMitigationAlgorithm()
        assert alg.name() == "rfi_mitigation_lambda1_bsf"
        assert alg.displayName() == "RFI Mitigation (Lambda-1 BSF)"
        assert alg.group() == "Preprocessing"
        assert alg.groupId() == "preprocessing"
        assert alg.createInstance() is not None

    def test_init_algorithm_parameters(self):
        alg = RfiMitigationAlgorithm()
        alg.initAlgorithm()

        param_names = [p.name() for p in alg.parameterDefinitions()]
        assert "INPUT" in param_names
        assert "AMPLITUDE_RASTER" in param_names
        assert "PHASE_RASTER" in param_names
        assert "ANNOTATION_XML" in param_names
        assert "DOMAIN" in param_names
        assert "OUTPUT_AMPLITUDE" in param_names
        assert "OUTPUT_MASK" in param_names
        assert "OUTPUT_PHASE" in param_names
        assert "GAMMA" in param_names
        assert "NS_MAX" in param_names
        assert "NS_MUL" in param_names
        assert "NORM" in param_names
        assert "BLOCK_AZIMUTH" in param_names
        assert "BLOCK_RANGE" in param_names
