"""
Unit tests for GDAL/PyQGIS memory management, pyramid overviews, and canvas
resampling guardrails (biomass_gateway.core.memory).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from biomass_gateway.core.memory import (
    DEFAULT_OVERVIEW_LEVELS,
    configure_gdal_cache,
    ensure_raster_overviews,
    get_system_ram,
    has_raster_overviews,
    layer_resampling_guard,
    safe_close_dataset,
)


class TestSystemRamAndGdalCache:
    """Tests for RAM detection and adaptive GDAL block cache ceiling."""

    def test_default_overview_levels(self) -> None:
        assert DEFAULT_OVERVIEW_LEVELS == (2, 4, 8, 16, 32, 64)

    def test_get_system_ram_returns_positive_int_or_none(self) -> None:
        ram = get_system_ram()
        assert ram is None or (isinstance(ram, int) and ram > 0)

    def test_get_system_ram_handles_os_error(self) -> None:
        with patch("biomass_gateway.core.memory.os.sysconf", side_effect=OSError("unavailable")):
            assert get_system_ram() is None

    def test_configure_gdal_cache_low_ram(self) -> None:
        four_gib = 4 * 1024 * 1024 * 1024
        with (
            patch("biomass_gateway.core.memory.get_system_ram", return_value=four_gib),
            patch("biomass_gateway.core.memory.gdal.SetCacheMax") as mock_set_cache,
        ):
            limit = configure_gdal_cache()
            expected = 256 * 1024 * 1024
            assert limit == expected
            mock_set_cache.assert_called_once_with(expected)

    def test_configure_gdal_cache_high_ram(self) -> None:
        sixteen_gib = 16 * 1024 * 1024 * 1024
        with (
            patch("biomass_gateway.core.memory.get_system_ram", return_value=sixteen_gib),
            patch("biomass_gateway.core.memory.gdal.SetCacheMax") as mock_set_cache,
        ):
            limit = configure_gdal_cache()
            expected = 512 * 1024 * 1024
            assert limit == expected
            mock_set_cache.assert_called_once_with(expected)

    def test_configure_gdal_cache_explicit_mb_and_floor(self) -> None:
        with patch("biomass_gateway.core.memory.gdal.SetCacheMax") as mock_set_cache:
            limit = configure_gdal_cache(max_mb=16)
            expected = 64 * 1024 * 1024
            assert limit == expected
            mock_set_cache.assert_called_once_with(expected)


class TestSafeCloseDataset:
    """Tests for deterministic C++ dataset teardown and global cache eviction."""

    def test_safe_close_dataset_flushes_and_collects(self) -> None:
        mock_ds = MagicMock()
        with (
            patch("biomass_gateway.core.memory.gdal.GetCacheMax", return_value=268435456),
            patch("biomass_gateway.core.memory.gdal.SetCacheMax") as mock_set_cache,
            patch("biomass_gateway.core.memory.gc.collect") as mock_gc,
        ):
            safe_close_dataset(mock_ds, flush_global_cache=True, run_gc=True)

            mock_ds.FlushCache.assert_called_once()
            assert mock_set_cache.call_args_list == [((0,),), ((268435456,),)]
            mock_gc.assert_called_once()

    def test_safe_close_dataset_none_handle(self) -> None:
        with (
            patch("biomass_gateway.core.memory.gdal.GetCacheMax", return_value=268435456),
            patch("biomass_gateway.core.memory.gdal.SetCacheMax") as mock_set_cache,
            patch("biomass_gateway.core.memory.gc.collect") as mock_gc,
        ):
            safe_close_dataset(None, flush_global_cache=False, run_gc=False)
            mock_set_cache.assert_not_called()
            mock_gc.assert_not_called()


class TestLayerResamplingGuard:
    """Tests for PyQGIS raster layer Nearest Neighbour resampling enforcement."""

    def test_layer_resampling_guard_valid_layer(self) -> None:
        mock_layer = MagicMock()
        mock_layer.isValid.return_value = True
        mock_filter = MagicMock()
        mock_layer.resampleFilter.return_value = mock_filter

        layer_resampling_guard(mock_layer, max_oversampling=2.0)

        mock_filter.setZoomedInResampler.assert_called_once_with(None)
        mock_filter.setZoomedOutResampler.assert_called_once_with(None)
        mock_filter.setMaxOversampling.assert_called_once_with(2.0)

    def test_layer_resampling_guard_invalid_or_none_layer(self) -> None:
        layer_resampling_guard(None)  # type: ignore[arg-type]

        mock_invalid = MagicMock()
        mock_invalid.isValid.return_value = False
        layer_resampling_guard(mock_invalid)
        mock_invalid.resampleFilter.assert_not_called()


class TestRasterOverviews:
    """Tests for checking and building external GDAL .ovr pyramids."""

    def test_has_raster_overviews_missing_file(self, tmp_path: Path) -> None:
        assert has_raster_overviews(tmp_path / "nonexistent.tif") is False

    def test_has_raster_overviews_detects_overviews(self, tmp_path: Path) -> None:
        tif_path = tmp_path / "sample.tif"
        tif_path.write_bytes(b"fake")

        mock_band = MagicMock()
        mock_band.GetOverviewCount.return_value = 6
        mock_ds = MagicMock()
        mock_ds.RasterCount = 1
        mock_ds.GetRasterBand.return_value = mock_band

        with patch("biomass_gateway.core.memory.gdal.Open", return_value=mock_ds):
            assert has_raster_overviews(tif_path) is True

    def test_ensure_raster_overviews_builds_when_missing(self, tmp_path: Path) -> None:
        tif_path = tmp_path / "raw_product.tif"
        tif_path.write_bytes(b"fake")

        mock_ds = MagicMock()
        with (
            patch("biomass_gateway.core.memory.has_raster_overviews", return_value=False),
            patch("biomass_gateway.core.memory.gdal.Open", return_value=mock_ds) as mock_open,
            patch("biomass_gateway.core.memory.safe_close_dataset") as mock_close,
        ):
            result = ensure_raster_overviews(tif_path)

            assert result is True
            mock_open.assert_called_once()
            mock_ds.BuildOverviews.assert_called_once_with(
                "NEAREST", list(DEFAULT_OVERVIEW_LEVELS)
            )
            mock_close.assert_called_once_with(
                mock_ds, flush_global_cache=True, run_gc=True
            )

    def test_ensure_raster_overviews_skips_when_present(self, tmp_path: Path) -> None:
        tif_path = tmp_path / "already_has_ovr.tif"
        tif_path.write_bytes(b"fake")

        with (
            patch("biomass_gateway.core.memory.has_raster_overviews", return_value=True),
            patch("biomass_gateway.core.memory.gdal.Open") as mock_open,
        ):
            assert ensure_raster_overviews(tif_path) is True
            mock_open.assert_not_called()
