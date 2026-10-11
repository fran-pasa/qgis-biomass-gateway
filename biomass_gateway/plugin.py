"""
Plugin lifecycle manager.
"""

from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .ui.dock_widget import BiomassDockWidget


class BiomassGatewayPlugin:
    """
    Main QGIS plugin integration class
    """

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = Path(__file__).resolve().parent
        self.dock_widget: BiomassDockWidget | None = None
        self.action: QAction | None = None


    def initGui(self):
        """Called by QGIS when plugin is loaded into GUI
        """
        icon_path = self.plugin_dir / 'icon.png'
        icon = QIcon(str(icon_path)) if icon_path.is_file() else QIcon()

        self.action = QAction(icon, 'Biomass Gateway', self.iface.mainWindow())
        self.action.setStatusTip("Discover, load and process ESA BIOMASS data")
        self.action.triggered.connect(self.run)

        self.iface.addPluginToMenu("&Biomass Gateway", self.action)


    def unload(self):
        """Called by QGIS when plugin is disabled or reloaded
        """
        if self.action:
            self.iface.removePluginMenu("&Biomass Gateway", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

        if self.dock_widget:
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget.deleteLater()
            self.dock_widget = None


    def run(self):
        """Toggle or display the plugin dock widget panel
        """
        if not self.dock_widget:
            self.dock_widget = BiomassDockWidget(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock_widget)
        else:
            self.dock_widget.show()
            self.dock_widget.raise_()