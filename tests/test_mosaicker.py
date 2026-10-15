"""
Unit tests for the mosaicker module and VRT processing parameter.
"""

from __future__ import annotations

from pathlib import Path
import pytest
from osgeo import gdal, osr

from biomass_gateway.core.exceptions import MosaicError
from biomass_gateway.processing.mosaicker import build_vrt_mosaic
from biomass_gateway.processing.algorithms.mosaic_algorithm import ParameterVrtDestination


def _create_synthetic_geotiff(
    path: Path,
    origin_x: float = 0.0,
    origin_y: float = 50.0,
    pixel_size: float = 0.01,
    width: int = 100,
    height: int = 100,
    band_count: int = 4,
    nodata: float = -9999.0,
) -> Path:
    """Helper to create a synthetic multi-band georeferenced GeoTIFF for testing."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)

    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(path), width, height, band_count, gdal.GDT_Float32)
    ds.SetGeoTransform([origin_x, pixel_size, 0.0, origin_y, 0.0, -pixel_size])
    ds.SetProjection(srs.ExportToWkt())

    for b in range(1, band_count + 1):
        band = ds.GetRasterBand(b)
        band.Fill(10.0 * b)
        band.SetNoDataValue(nodata)
        band = None

    ds.BuildOverviews("AVERAGE", [2, 4])
    ds.FlushCache()
    ds = None
    return path


def test_build_vrt_mosaic_insufficient_inputs(tmp_path: Path):
    """Verify MosaicError is raised if fewer than 2 input paths are provided."""
    vrt_out = tmp_path / "out.vrt"

    with pytest.raises(MosaicError, match="At least 2 products are needed"):
        build_vrt_mosaic([], vrt_out)

    with pytest.raises(MosaicError, match="At least 2 products are needed"):
        build_vrt_mosaic([tmp_path / "single.tif"], vrt_out)


def test_build_vrt_mosaic_success(tmp_path: Path):
    """Verify building a 4-band VRT mosaic preserves bands, descriptions, and extent."""
    img1 = _create_synthetic_geotiff(tmp_path / "scene1.tif", origin_x=0.0)
    img2 = _create_synthetic_geotiff(tmp_path / "scene2.tif", origin_x=0.5)  # overlapping swath
    vrt_out = tmp_path / "mosaic.vrt"

    pols = ["HH", "HV", "VH", "VV"]
    result = build_vrt_mosaic(
        raster_paths=[img1, img2],
        output_vrt_path=vrt_out,
        band_names=pols,
        nodata=-9999.0,
    )

    assert result.is_file()
    assert result == vrt_out

    # Open and inspect the generated VRT
    ds = gdal.Open(str(result), gdal.GA_ReadOnly)
    assert ds is not None
    assert ds.RasterCount == 4

    for i, expected_desc in enumerate(pols, start=1):
        band = ds.GetRasterBand(i)
        assert band.GetDescription() == expected_desc
        assert band.GetNoDataValue() == -9999.0
        band = None

    # Width should cover both swaths (union extent: 0.0 to 1.5 degrees = 150 pixels)
    assert ds.RasterXSize == 150
    assert ds.RasterYSize == 100
    ds = None


def test_build_vrt_mosaic_progress_callback(tmp_path: Path):
    """Verify progress callback is invoked during VRT building."""
    img1 = _create_synthetic_geotiff(tmp_path / "scene1.tif", origin_x=0.0)
    img2 = _create_synthetic_geotiff(tmp_path / "scene2.tif", origin_x=1.0)
    vrt_out = tmp_path / "mosaic.vrt"

    calls: list[float] = []

    def callback(complete: float, msg: str, *args) -> int:
        calls.append(complete)
        return 1

    build_vrt_mosaic(
        raster_paths=[img1, img2],
        output_vrt_path=vrt_out,
        progress_callback=callback,
    )

    assert len(calls) > 0
    assert calls[-1] == 1.0


def test_build_vrt_mosaic_overwrites_existing(tmp_path: Path):
    """Verify that build_vrt_mosaic safely overwrites a pre-existing VRT file."""
    img1 = _create_synthetic_geotiff(tmp_path / "scene1.tif", origin_x=0.0)
    img2 = _create_synthetic_geotiff(tmp_path / "scene2.tif", origin_x=1.0)
    vrt_out = tmp_path / "mosaic.vrt"
    vrt_out.write_text("old content")

    build_vrt_mosaic(raster_paths=[img1, img2], output_vrt_path=vrt_out)
    assert vrt_out.is_file()
    assert "<VRTDataset" in vrt_out.read_text()


def test_parameter_vrt_destination_clone():
    """Verify ParameterVrtDestination preserves VRT file extensions when cloned in QGIS."""
    param = ParameterVrtDestination("OUTPUT", "Output VRT")
    assert param.defaultFileExtension() == "vrt"
    assert "VRT" in param.createFileFilter()

    cloned = param.clone()
    assert isinstance(cloned, ParameterVrtDestination)
    assert cloned.name() == "OUTPUT"
    assert cloned.defaultFileExtension() == "vrt"
    assert cloned.createFileFilter() == param.createFileFilter()
