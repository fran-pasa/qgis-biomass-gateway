"""
ESA MAAP STAC API client for BIOMASS mission catalog discovery.
Handles queries to the STAC catalog.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.parse

from typing import Any
from dataclasses import dataclass, field

from ..core.logger import get_logger

logger = get_logger("stac_client")

# base endpoint for ESA MAAP STAC Search
MAAP_STAC_SEARCH_URL = "https://catalog.maap.eo.esa.int/catalogue/search"

# define BIOMASS data collections
BIOMASS_COLLECTIONS = (
    "BiomassLevel1a",
    "BiomassLevel1b",
    "BiomassLevel1c",
    "BiomassLevel2a",
    "BiomassLevel2b",
)


# Specification-driven measurement raster keys mapped by BIOMASS collection
COLLECTION_PRIMARY_MEASUREMENT_KEYS: dict[str, tuple[str, ...]] = {
    "BiomassLevel1a": ("enclosure_i_abs_tiff", "enclosure_i_phase_tiff"),
    "BiomassLevel1b": ("enclosure_tiff",),
    "BiomassLevel1c": ("enclosure_i_abs_tiff", "enclosure_i_phase_tiff"),
    "BiomassLevel2a": ("enclosure_tiff", "enclosure_i_fh_tiff", "enclosure_i_fd_tiff"),
    "BiomassLevel2b": ("enclosure_tiff", "enclosure_i_agb_tiff"),
}


@dataclass(frozen=True)
class STACAsset:
    name: str
    href: str
    media_type: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)
    title: str = ""
    file_size: int | None = None


    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> STACAsset:
        """Parse STAC asset JSON into a typed STACAsset dataclass
        """
        return cls(
            name=name,
            href=data.get("href", ""),
            media_type=data.get("type", ""),
            roles=tuple(data.get("roles", [])),
            title=data.get("title", ""),
            file_size=data.get("file:size"),
        )

@dataclass(frozen=True)
class STACItem:
    id: str
    collection: str
    bbox: tuple[float, float, float, float]
    geometry: dict[str, Any]
    properties: dict[str, Any]
    assets: dict[str, STACAsset]


    @classmethod
    def from_geojson_feature(cls, feat: dict[str, Any]) -> STACItem:
        """Construct a STACItem instance from a GeoJSON dict
        """
        # Parse assets dictionary
        raw_assets = feat.get("assets", {})
        parsed_assets = {
            key: STACAsset.from_dict(key, val)
            for key, val in raw_assets.items()
            if isinstance(val, dict)
        }

        raw_bbox = feat.get("bbox", [-180.0, -90.0, 180.0, 90.0])
        bbox_tuple = (
            float(raw_bbox[0]),
            float(raw_bbox[1]),
            float(raw_bbox[2]),
            float(raw_bbox[3]),
        )

        return cls(
            id=feat.get("id", ""),
            collection=feat.get("collection", ""),
            bbox=bbox_tuple,
            geometry=feat.get("geometry", {}),
            properties=feat.get("properties", {}),
            assets=parsed_assets,
        )


    @property
    def sensing_start(self) -> str:
        """Acquisition start datetime
        """
        return (
                self.properties.get("start_datetime")
                or self.properties.get("datetime")
                or "Unknown"
        )


    @property
    def product_type(self) -> str:
        """Product type code (e.g. 'BIO_S2_DGM__1S')
        """
        return self.properties.get("product:type") or self.collection


    @property
    def polarizations(self) -> list[str]:
        """Biomass polarization channels
        """
        pols = self.properties.get("sar:polarizations", [])
        if isinstance(pols, list):
            return pols
        return [str(pols)] if pols else []


    @property
    def orbit(self) -> int | None:
        """Satellite orbit number
        """
        orbit = self.properties.get("sat:absolute_orbit")
        return int(orbit) if orbit is not None else None


    @property
    def quicklook_url(self) -> str | None:
        """Public browse JPEG overview URL, or None if unavailable."""
        asset = self.assets.get("quicklook_jpg") or self.assets.get("thumbnail")
        return asset.href if asset else None


    @property
    def measurement_tiff_url(self) -> str | None:
        """Resolve primary measurement GeoTIFF URL for GDAL /vsicurl/ streaming.
        Uses deterministic mapping based on BIOMASS mission product specification.
        """
        # 1. Deterministic lookup via collection-specific keys
        keys = COLLECTION_PRIMARY_MEASUREMENT_KEYS.get(self.collection, ())
        for key in keys:
            asset = self.assets.get(key)
            if asset and asset.href:
                return asset.href

        # 2. General fallback for any other BIOMASS measurement TIFF
        for asset in self.assets.values():
            is_measurement = "/measurement/" in asset.href.lower() or "data" in asset.roles
            is_tiff = "tiff" in asset.media_type.lower() or asset.href.lower().endswith((".tif", ".tiff"))
            if is_measurement and is_tiff:
                return asset.href

        return None

@dataclass(frozen=True)
class STACSearchResult:
    """Encapsulate a paginated STAC search response
    """

    items: tuple[STACItem, ...]
    number_matched: int
    number_returned: int
    next_start_record: int | None = None


class MAAPSTACClient:
    """STAC client for querying ESA MAAP catalog
    """

    def __init__(self, base_url: str = MAAP_STAC_SEARCH_URL, timeout: int = 20) -> None:
        self.base_url = base_url
        self.timeout = timeout


    def search(
            self,
            collections: tuple[str, ...] | list[str] = BIOMASS_COLLECTIONS,
            bbox: tuple[float, float, float, float] | list[float] | None = None,
            datetime_range: str | None = None,
            limit: int = 20,
            start_record: int = 1,
    ) -> STACSearchResult:
        """Query the ESA MAAP STAC search endpoint.
        """
        # build query parameters dict
        params: dict[str, str | int] = {
            "collections": ",".join(collections),
            "limit": limit,
            "startRecord": start_record,
        }

        if bbox is not None and len(bbox) == 4:
            params["bbox"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"

        if datetime_range:
            params["datetime"] = datetime_range.strip()

        # encode URL query parameters
        query_string = urllib.parse.urlencode(params)
        full_url = f"{self.base_url}?{query_string}"
        logger.debug(f"Executing STAC query: {full_url}")

        # create HTTP Request with standard headers
        req = urllib.request.Request(
            full_url,
            headers={
                "Accept": "application/geo+json, application/json",
                "User-Agent": "QGIS-Biomass-Gateway",
            },
            method="GET",
        )

        # do network request and parse GeoJSON
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            payload_bytes = response.read()
            data = json.loads(payload_bytes.decode("utf-8"))

        # convert GeoJSON features into STACItem instances
        raw_features = data.get("features", [])
        items = tuple(
            STACItem.from_geojson_feature(feat)
            for feat in raw_features
            if isinstance(feat, dict)
        )

        # Extract next_start_record from pagination links (if available)
        next_start_record: int | None = None
        for link in data.get("links", []):
            if link.get("rel") == "next" and "href" in link:
                parsed_href = urllib.parse.urlparse(link["href"])
                query_dict = urllib.parse.parse_qs(parsed_href.query)
                if "startRecord" in query_dict:
                    try:
                        next_start_record = int(query_dict["startRecord"][0])
                    except (ValueError, IndexError):
                        pass
                break

        return STACSearchResult(
            items=items,
            number_matched=int(data.get("numberMatched", len(items))),
            number_returned=int(data.get("numberReturned", len(items))),
            next_start_record=next_start_record,
        )