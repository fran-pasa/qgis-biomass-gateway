"""
Pytest configuration and shared fixtures for Biomass Gateway tests.
Mocks QGIS dependencies when running in standalone Python environments.
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock
import pytest

# ---------------------------------------------------------------------------
# Stub / Mock QGIS modules if not available in testing environment
# ---------------------------------------------------------------------------
if "qgis" not in sys.modules:
    try:
        import qgis  # noqa: F401
    except ImportError:
        qgis_mock = MagicMock()
        qgis_core_mock = MagicMock()

        class FakeQgsSettings:
            """In-memory stand-in for QgsSettings in non-QGIS environments."""
            def __init__(self) -> None:
                if not hasattr(FakeQgsSettings, "_store"):
                    FakeQgsSettings._store = {}

            def value(self, key: str, default: Any = None, type: Any = None) -> Any:
                val = FakeQgsSettings._store.get(key, default)
                if type is bool and isinstance(val, str):
                    return val.lower() in ("true", "1")
                return val

            def setValue(self, key: str, value: Any) -> None:
                FakeQgsSettings._store[key] = value

            def remove(self, key: str) -> None:
                FakeQgsSettings._store.pop(key, None)

            def contains(self, key: str) -> bool:
                return key in FakeQgsSettings._store

            def clear(self) -> None:
                FakeQgsSettings._store.clear()

        qgis_core_mock.QgsSettings = FakeQgsSettings
        qgis_mock.core = qgis_core_mock

        sys.modules["qgis"] = qgis_mock
        sys.modules["qgis.core"] = qgis_core_mock
        sys.modules["qgis.gui"] = MagicMock()
        sys.modules["qgis.PyQt"] = MagicMock()
        sys.modules["qgis.PyQt.QtCore"] = MagicMock()
        sys.modules["qgis.PyQt.QtGui"] = MagicMock()
        sys.modules["qgis.PyQt.QtWidgets"] = MagicMock()


@pytest.fixture(autouse=True)
def clean_qgs_settings():
    """Reset QgsSettings store before each test."""
    from qgis.core import QgsSettings
    settings = QgsSettings()
    if hasattr(settings, "clear"):
        settings.clear()
    yield
    if hasattr(settings, "clear"):
        settings.clear()


@pytest.fixture
def sample_stac_feature() -> dict[str, Any]:
    """Sample STAC item GeoJSON feature matching ESA MAAP STAC format."""
    return {
        "type": "Feature",
        "id": "BIO_S2_DGM__1S_20230101T120000_20230101T120100_T035_F016_01_TEST",
        "collection": "BiomassLevel1b",
        "bbox": [-60.5, -2.5, -59.5, -1.5],
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-60.5, -2.5],
                [-59.5, -2.5],
                [-59.5, -1.5],
                [-60.5, -1.5],
                [-60.5, -2.5],
            ]],
        },
        "properties": {
            "datetime": "2023-01-01T12:00:30Z",
            "start_datetime": "2023-01-01T12:00:00Z",
            "end_datetime": "2023-01-01T12:01:00Z",
            "product:type": "BIO_S2_DGM__1S",
            "sat:absolute_orbit": 12450,
            "sat:relative_orbit": 35,
            "sat:orbit_state": "descending",
            "sar:polarizations": ["HH", "HV", "VH", "VV"],
            "sar:instrument_mode": "S2",
        },
        "assets": {
            "enclosure_tiff": {
                "href": "https://data.maap.eo.esa.int/biomass/measurement/bio_s2_dgm.tiff",
                "type": "image/tiff; application=geotiff",
                "title": "Measurement TIFF",
                "roles": ["data"],
                "file:size": 104857600,
            },
            "quicklook_jpg": {
                "href": "https://data.maap.eo.esa.int/biomass/preview/bio_s2_dgm_ql.jpg",
                "type": "image/jpeg",
                "title": "Quicklook preview",
                "roles": ["overview"],
            },
            "metadata_xml": {
                "href": "https://data.maap.eo.esa.int/biomass/annotation/bio_s2_dgm_annot.xml",
                "type": "application/xml",
                "title": "Annotation XML",
                "roles": ["metadata"],
            },
        },
    }
