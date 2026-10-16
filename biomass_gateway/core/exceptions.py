"""
Custom exceptions for the plugin
"""

class BiomassGatewayError(Exception):
    """Base exception for all Biomass Gateway errors."""


class ProductReadError(BiomassGatewayError):
    """Raised when parsing or reading a Biomass product folder fails."""


class AnnotationParseError(ProductReadError):
    """Raised when XML annotation metadata cannot be read or is invalid."""


class WarpError(BiomassGatewayError):
    """Raised when GDAL georeferencing or VRT generation fails."""


class ProcessingError(BiomassGatewayError):
    """Raised when a QGIS Processing algorithm fails during execution."""


class MaapError(BiomassGatewayError):
    """Raised when MAAP STAC search or download operations fail."""


class BioPalError(BiomassGatewayError):
    """Raised when BioPAL runner or external conda environment fails."""

class MosaicError(BiomassGatewayError):
    """Raised when Mosaicking fails"""


class CalibrationError(BiomassGatewayError):
    """Raised when radiometric calibration to gamma nought fails."""


class RfiMitigationError(BiomassGatewayError):
    """Raised when RFI mitigation fails."""

