"""
Domain models for the plugin
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class WarpMethod(str, Enum):
    """Supported GDAL georeferencing methods.
    """
    GEOLOCATION_ARRAY = 'geoloc'
    TPS = "tps"
    POLYNOMIAL_1 = "order_1"
    POLYNOMIAL_2 = "order_2"
    POLYNOMIAL_3 = "order_3"


class ProductLevel(str, Enum):
    """Supported BIOMASS processing levels.
    """
    L1A = "L1A"
    L1B = "L1B"
    L1C = "L1C"
    L1M = "L1M"
    L2A = "L2A"
    L2B = "L2B"
    UNKNOWN = "UNKNOWN"


class Polarization(str, Enum):
    """Radar polarization channels
    """
    HH = "HH"
    HV = "HV"
    VV = "VV"
    VH = "VH"


class MeasurementType(str, Enum):
    """Measurement file type
    """
    AMPLITUDE = "abs"
    PHASE = "phase"
    COMPLEX = "complex"


@dataclass(frozen=True)
class GcpPoint:
    """Ground Control Point (GCP) extracted from the product
    """
    id: str
    pixel: float
    line: float
    x: float # longitude
    y: float #  latitude
    z: float = 0.0 # ellipsoidal height in meters


@dataclass(frozen=True)
class BiomassBand:
    """Individual raster band info
    """
    index: int
    polarization: str
    measurement_type: MeasurementType


@dataclass
class WarpConfig:
    """Configuration options for GDAL warping
    """
    method: WarpMethod = WarpMethod.GEOLOCATION_ARRAY
    src_crs: str = "EPSG:4979"
    dst_crs: str = "EPSG:4326"
    nodata: float = -9999.0
    resample_alg: str = "bilinear"


@dataclass
class BiomassProduct:
    """Parsed metadata representation of a Biomass product folder
    """

    product_id: str
    folder_path: Path
    level: ProductLevel
    swath: str = "N/A"
    product_type: str = "UNKNOWN"
    track: str | None = None  # e.g. "T035"
    frame: str | None = None  # e.g. "F306"
    sensing_start: datetime | None = None
    sensing_stop: datetime | None = None
    mission_phase: str | None = None  # e.g. "TOM" (Tomographic), "INT" (Interferometric)
    abs_file: Path | None = None
    phase_file: Path | None = None
    vrt_file: Path | None = None
    annotation_file: Path | None = None
    polarisations: list[str] = field(default_factory=lambda: ["HH", "HV", "VH", "VV"])
    gcps: list[GcpPoint] = field(default_factory=list)
    lut_file: Path | None = None
    lut_grid_shape: tuple[int, int] | None = None


    @property
    def has_measurements(self) -> bool:
        """Check if product has raster measurement files
        """
        return self.abs_file is not None or self.phase_file is not None or self.vrt_file is not None


    @property
    def is_l1a(self) -> bool:
        """True if Single-look Complex Slant product (has both abs and phase)
        """
        return self.level == ProductLevel.L1A


    @property
    def is_l1b(self) -> bool:
        """True if Detected Ground Multi-look product (amplitude only)
        """
        return self.level == ProductLevel.L1B


    @property
    def is_l1c(self) -> bool:
        """True if co-registered slant-range Complex product
        """
        return self.level == ProductLevel.L1C


    @property
    def is_geocoded(self) -> bool:
        """True if Level 2A or Level 2B product (already map-projected)
        """
        return self.level in (ProductLevel.L2A, ProductLevel.L2B)


    @property
    def has_lut(self) -> bool:
        """True if a valid NetCDF LUT file is available
        """
        return self.lut_file is not None and self.lut_file.is_file()


@dataclass(frozen=True)
class CalibrationConfig:
    """Configuration options for radiometric calibration to Gamma Nought.
    """
    nodata: float = -9999.0
    to_db: bool = True
    build_overviews: bool = False
    overview_resampling: str = "NEAREST"
    overview_levels: tuple[int, ...] = (2, 5, 10, 20)
    compress: str = "DEFLATE"


@dataclass(frozen=True)
class LutData:
    """Radiometric LUT and coordinate axes extracted from NetCDF.
    """
    gamma_lut: Any  # np.ndarray
    azimuth_axis: Any  # np.ndarray
    range_axis: Any  # np.ndarray
    reference_azimuth_time: str


@dataclass(frozen=True)
class SarImageGeometry:
    """SAR image pixel timing coordinates in the LUT reference frame.
    """
    azimuth_axis: Any  # np.ndarray
    range_time: Any  # np.ndarray (1D for slant range, 2D for ground range)
    n_lines: int
    n_samples: int
    projection: str