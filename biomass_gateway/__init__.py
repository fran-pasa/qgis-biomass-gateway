"""
Biomass Gateway plugin entrypoint (as required by QGIS)
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.gui import QgisInterface


def classFactory(iface: "QgisInterface"):
    from .plugin import BiomassGatewayPlugin

    return BiomassGatewayPlugin(iface)