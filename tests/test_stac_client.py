"""
Unit tests for ESA MAAP STAC API client and data models.
"""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from biomass_gateway.core.exceptions import MaapError
from biomass_gateway.maap.client import (
    BIOMASS_COLLECTIONS,
    COLLECTION_PRIMARY_MEASUREMENT_KEYS,
    MAAPSTACClient,
    STACAsset,
    STACItem,
    STACSearchResult,
)


class TestSTACDataModels:
    """Tests for STACAsset and STACItem parsing and properties."""

    def test_stac_asset_from_dict(self) -> None:
        raw_asset = {
            "href": "https://example.com/raster.tif",
            "type": "image/tiff",
            "roles": ["data"],
            "title": "Primary GeoTIFF",
            "file:size": 2048,
        }
        asset = STACAsset.from_dict("data_key", raw_asset)

        assert asset.name == "data_key"
        assert asset.href == "https://example.com/raster.tif"
        assert asset.media_type == "image/tiff"
        assert asset.roles == ("data",)
        assert asset.title == "Primary GeoTIFF"
        assert asset.file_size == 2048

    def test_stac_asset_from_empty_dict(self) -> None:
        asset = STACAsset.from_dict("empty", {})
        assert asset.name == "empty"
        assert asset.href == ""
        assert asset.media_type == ""
        assert asset.roles == ()
        assert asset.file_size is None

    def test_stac_item_from_geojson(self, sample_stac_feature: dict[str, Any]) -> None:
        item = STACItem.from_geojson_feature(sample_stac_feature)

        assert item.id == "BIO_S2_DGM__1S_20230101T120000_20230101T120100_T035_F016_01_TEST"
        assert item.collection == "BiomassLevel1b"
        assert item.bbox == (-60.5, -2.5, -59.5, -1.5)
        assert item.sensing_start == "2023-01-01T12:00:00Z"
        assert item.product_type == "BIO_S2_DGM__1S"
        assert item.polarizations == ["HH", "HV", "VH", "VV"]
        assert item.orbit == 12450
        assert item.quicklook_url == "https://data.maap.eo.esa.int/biomass/preview/bio_s2_dgm_ql.jpg"
        assert item.measurement_tiff_url == "https://data.maap.eo.esa.int/biomass/measurement/bio_s2_dgm.tiff"

    def test_stac_item_sensing_start_fallbacks(self) -> None:
        # Fallback to datetime if start_datetime is absent
        item1 = STACItem.from_geojson_feature({
            "id": "item1",
            "properties": {"datetime": "2023-05-10T10:00:00Z"},
        })
        assert item1.sensing_start == "2023-05-10T10:00:00Z"

        # Fallback to Unknown if neither is present
        item2 = STACItem.from_geojson_feature({"id": "item2", "properties": {}})
        assert item2.sensing_start == "Unknown"

    def test_stac_item_measurement_tiff_collection_mappings(self) -> None:
        # Test L1A prefers enclosure_i_abs_tiff
        l1a_feat = {
            "id": "item_l1a",
            "collection": "BiomassLevel1a",
            "assets": {
                "enclosure_i_abs_tiff": {"href": "https://data.esa.int/abs.tif", "type": "image/tiff"},
                "enclosure_i_phase_tiff": {"href": "https://data.esa.int/phase.tif", "type": "image/tiff"},
            },
        }
        item_l1a = STACItem.from_geojson_feature(l1a_feat)
        assert item_l1a.measurement_tiff_url == "https://data.esa.int/abs.tif"

        # Test L2A prefers enclosure_tiff or enclosure_i_fh_tiff
        l2a_feat = {
            "id": "item_l2a",
            "collection": "BiomassLevel2a",
            "assets": {
                "enclosure_i_fh_tiff": {"href": "https://data.esa.int/fh.tif", "type": "image/tiff"},
            },
        }
        item_l2a = STACItem.from_geojson_feature(l2a_feat)
        assert item_l2a.measurement_tiff_url == "https://data.esa.int/fh.tif"

        # Test general fallback to any measurement TIFF
        generic_feat = {
            "id": "item_custom",
            "collection": "CustomCollection",
            "assets": {
                "other_asset": {
                    "href": "https://data.esa.int/measurement/data_custom.tif",
                    "type": "image/tiff",
                    "roles": ["data"],
                },
            },
        }
        item_generic = STACItem.from_geojson_feature(generic_feat)
        assert item_generic.measurement_tiff_url == "https://data.esa.int/measurement/data_custom.tif"

        # No measurement tiff available
        no_tiff_feat = {
            "id": "item_notiff",
            "collection": "CustomCollection",
            "assets": {
                "metadata": {"href": "https://data.esa.int/meta.xml", "type": "application/xml"},
            },
        }
        item_notiff = STACItem.from_geojson_feature(no_tiff_feat)
        assert item_notiff.measurement_tiff_url is None


class TestMAAPSTACClient:
    """Tests for MAAPSTACClient search requests and error handling."""

    def test_default_initialization(self) -> None:
        client = MAAPSTACClient()
        assert "catalogue/search" in client.base_url
        assert client.timeout == 20

    @patch("urllib.request.urlopen")
    def test_search_successful_response(
        self,
        mock_urlopen: MagicMock,
        sample_stac_feature: dict[str, Any],
    ) -> None:
        mock_payload = {
            "type": "FeatureCollection",
            "features": [sample_stac_feature],
            "numberMatched": 42,
            "numberReturned": 1,
            "links": [
                {
                    "rel": "next",
                    "href": "https://catalog.maap.eo.esa.int/catalogue/search?startRecord=11&limit=10",
                }
            ],
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_payload).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = MAAPSTACClient()
        result = client.search(
            collections=["BiomassLevel1b"],
            bbox=(-65.0, -5.0, -55.0, 5.0),
            datetime_range="2023-01-01T00:00:00Z/2023-02-01T23:59:59Z",
            limit=10,
            start_record=1,
        )

        assert isinstance(result, STACSearchResult)
        assert len(result.items) == 1
        assert result.number_matched == 42
        assert result.number_returned == 1
        assert result.next_start_record == 11
        assert result.items[0].id == sample_stac_feature["id"]

        # Verify request parameters
        called_req = mock_urlopen.call_args[0][0]
        url = called_req.full_url
        assert "startRecord=1" in url
        assert "limit=10" in url
        assert "collections=BiomassLevel1b" in url
        assert "bbox=-65.0%2C-5.0%2C-55.0%2C5.0" in url or "bbox=-65.0,-5.0,-55.0,5.0" in url

    @patch("urllib.request.urlopen")
    def test_search_empty_features(self, mock_urlopen: MagicMock) -> None:
        mock_payload = {
            "type": "FeatureCollection",
            "features": [],
            "numberMatched": 0,
            "numberReturned": 0,
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_payload).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = MAAPSTACClient()
        result = client.search(collections=["BiomassLevel2a"])

        assert len(result.items) == 0
        assert result.number_matched == 0
        assert result.number_returned == 0
        assert result.next_start_record is None

    @patch("urllib.request.urlopen")
    def test_search_network_errors_propagate(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://catalog.maap.eo.esa.int",
            code=500,
            msg="Internal Server Error",
            hdrs=MagicMock(),
            fp=io.BytesIO(b"Catalog backend error"),
        )

        client = MAAPSTACClient()
        with pytest.raises(urllib.error.HTTPError):
            client.search()
