"""
QGIS Processing Provider for BIOMASS Gateway algorithms
"""

from pathlib import Path

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms.calibrate_algorithm import CalibrateGamma0Algorithm
from .algorithms.warp_algorithm import WarpBiomassAlgorithm


class BiomassProcessingProvider(QgsProcessingProvider):
    """
    Exposes BIOMASS Gateway algorithms to the QGIS Processing Toolbox
    """

    def loadAlgorithms(self):
        """Register all algorithms under this provider."""
        self.addAlgorithm(WarpBiomassAlgorithm())
        self.addAlgorithm(CalibrateGamma0Algorithm())

        # contributors can register their algorithms here:
        # self.addAlgorithm(YourAlgorithm())

    def id(self) -> str:
        """
        Unique provider identifier used in scripts (e.g. biomass_gateway:warp_biomass)
        """
        return "biomass_gateway"

    def name(self) -> str:
        """
        Display name shown in the Processing Toolbox tree
        """
        return "BIOMASS Gateway"

    def longName(self) -> str:
        """
        Extended description for the provider
        """
        return "ESA BIOMASS Gateway"

    def icon(self) -> QIcon:
        """
        Toolbar and toolbox icon
        """
        icon_path = Path(__file__).resolve().parent.parent / "icon.png"
        if icon_path.is_file():
            return QIcon(str(icon_path))
        return super().icon()
