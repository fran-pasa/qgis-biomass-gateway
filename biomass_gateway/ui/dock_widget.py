"""
Main dockwidget panel for the plugin
"""

from pathlib import Path

from qgis.gui import QgsDockWidget, QgsFileWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import  (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QApplication,
    QLineEdit
)

from ..core.exceptions import ProductReadError
from ..core.models import BiomassProduct
from ..reader.folder_reader import is_biomass_folder, read_biomass_product
from ..reader.loader import  load_biomass_product
from ..maap.auth import MAAPAuthManager, AuthValidationResult


class BiomassDockWidget(QgsDockWidget):
    """Main dockwidget panel
    """
    def __init__(self, iface, parent=None):
        # set up main widget class
        super().__init__(parent)
        self.iface = iface

        # assign authentication manager
        self.auth_manager = MAAPAuthManager()

        # variable useful to track currently loaded BiomassProduct
        self.current_product: BiomassProduct | None = None

        # set visible title, and internal object name, build UI
        self.setWindowTitle("BIOMASS Gateway")
        self.setObjectName("BiomassGatewayDockWidget")
        self.init_ui()


    def init_ui(self):
        """Build interface layout
        """
        # create widget and give it vertical box layout
        # items will be stacked top to bottom.
        main_widget = QWidget()
        layout = QVBoxLayout(main_widget)

        # create container with tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._create_browser_tab(), "Local Product Browser")
        self.tabs.addTab(self._create_placeholder_tab("MAAP STAC search"), "MAAP search")
        self.tabs.addTab(self._create_settings_tab(), "Settings")

        # add container to the layout, main_widget will be the central widget for this panel
        layout.addWidget(self.tabs)
        self.setWidget(main_widget)


    def _create_browser_tab(self) -> QWidget:
        """
        Create the Local Product Browser tab
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # folder picker
        layout.addWidget(QLabel("<b>Select BIOMASS product folder:</b>"))
        self.file_widget = QgsFileWidget()
        self.file_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.file_widget.fileChanged.connect(self._on_folder_selected)
        layout.addWidget(self.file_widget)

        # action buttons
        btn_layout = QHBoxLayout()
        self.btn_inspect = QPushButton("Inspect product")
        self.btn_inspect.clicked.connect(self._inspect_current_folder)
        btn_layout.addWidget(self.btn_inspect)

        self.btn_load_layer = QPushButton("Load layer on canvas")
        self.btn_load_layer.setEnabled(False)
        self.btn_load_layer.clicked.connect(self._load_layer_on_canvas)
        btn_layout.addWidget(self.btn_load_layer)
        layout.addLayout(btn_layout)

        # metadata display
        layout.addWidget(QLabel("<b>Product details:</b>"))
        self.txt_details = QTextEdit()
        self.txt_details.setReadOnly(True)
        layout.addWidget(self.txt_details)

        return widget


    def _create_settings_tab(self) -> QTabWidget:
        """Build the Setting tab UI (for MAAP credential mgmt)
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # section header
        layout.addWidget(QLabel("<b>ESA MAAP authentication:</b>"))
        desc = QLabel("Configure your MAAP access")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # token input (configured as a password field)
        self.edit_token = QLineEdit()
        self.edit_token.setEchoMode(QLineEdit.Password)
        self.edit_token.setPlaceholderText("Paste Bearer token here")

        # pre-populate fields if a token already exists in QgsSettings
        existing_token = self.auth_manager.get_offline_token()
        if existing_token:
            self.edit_token.setText(existing_token)
        layout.addWidget(self.edit_token)

        # action buttons layout
        btn_layout = QHBoxLayout()

        # toggle buttons to hide token chars
        self.btn_toggle_token = QPushButton("Show token")
        self.btn_toggle_token.setCheckable(True)
        self.btn_toggle_token.clicked.connect(self._toggle_token_visibility)
        btn_layout.addWidget(self.btn_toggle_token)

        # save button to commit to QgsSettings and update gdal configs
        self.btn_save_token = QPushButton("Save token")
        self.btn_save_token.clicked.connect(self._save_token)
        btn_layout.addWidget(self.btn_save_token)

        # clear button (delete from QgsSettings and resets GDAL
        self.btn_clear_token = QPushButton("Clear token")
        self.btn_clear_token.clicked.connect(self._clear_token)
        btn_layout.addWidget(self.btn_clear_token)

        layout.addLayout(btn_layout)

        # test authentication button
        self.btn_test_auth = QPushButton("Test authentication")
        self.btn_test_auth.clicked.connect(self._test_auth)
        layout.addWidget(self.btn_test_auth)

        # display feedback of the authentication test
        self.lbl_auth_status = QLabel()
        self.lbl_auth_status.setWordWrap(True)
        if existing_token:
            self.lbl_auth_status.setText(
                "<i>Token loaded from QGIS settings. Click 'Test authentication' to verify.</i>"
            )
        else:
            self.lbl_auth_status.setText(
                "<i style='color: gray;'>No token configured.</i>"
            )
        layout.addWidget(self.lbl_auth_status)

        # move all controls to the top of the tab
        layout.addStretch()

        return widget


    def _toggle_token_visibility(self):
        """Helper to toggle QLineEdit between Password (masked)
        and Normal (plain text) modes
        """
        if self.btn_toggle_token.isChecked():
            self.edit_token.setEchoMode(QLineEdit.Normal)
            self.btn_toggle_token.setText("Hide token")
        else:
            self.edit_token.setEchoMode(QLineEdit.Password)
            self.btn_toggle_token.setText("Show token")


    def _save_token(self):
        """Helper to save the token (if a valid one is provided)
        """
        token = self.edit_token.text().strip()
        if not token:
            QMessageBox.warning(
                self,
                "Empty token",
                "Please enter or paste a valid Bearer token before saving."
            )
            return

        self.auth_manager.save_offline_token(token)
        self.lbl_auth_status.setText("<b style='color: #2e7d32;'>✓ Token saved.</b>")


    def _clear_token(self):
        """Helper to clear the token from the settings and
        reset the input field
        """
        self.auth_manager.clear_token()
        self.edit_token.clear()
        self.lbl_auth_status.setText("<b style='color: gray;'>✓ Token cleared.</b>")
        return


    def _test_auth(self):
        token = self.edit_token.text().strip()
        if not token:
            QMessageBox.warning(
                self,
                "No token provided",
                "Please enter a token to test."
            )
            return

        # disable button and display wait cursor (to prevent repeated clicks)
        self.btn_test_auth.setEnabled(False)
        self.lbl_auth_status.setText("<i>Validating token with ESA MAAP Keycloak...</i>")

        # run validation
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        try:
            result: AuthValidationResult = self.auth_manager.validate_token(token)
            if result.is_valid:
                self.lbl_auth_status.setText(f"<b style='color: #2e7d32;'>{result.message}</b>")
                self.auth_manager.save_offline_token(token)
            else:
                self.lbl_auth_status.setText(f"<b style='color: #c62828;'>{result.message}</b>")

        except Exception as e:
            self.lbl_auth_status.setText(f"<b style='color: #c62828;'>Error: {e}</b>")

        finally:
            QApplication.restoreOverrideCursor()
            self.btn_test_auth.setEnabled(True)


    def  _create_placeholder_tab(self, label_text: str) -> QWidget:
        """Helper to create placeholder tabs
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)

        lbl = QLabel(f"<i>{label_text}</i>")
        lbl.setWordWrap(True)

        layout.addWidget(lbl)
        layout.addStretch()

        return widget


    def _on_folder_selected(self, folder_path: str):
        """Helper to pass the folder path to the inspection logic,
        if it exists
        """
        if not folder_path:
            return
        self._inspect_folder(Path(folder_path))


    def _inspect_current_folder(self):
        """Helper that runs when the 'Inspect product' button is clicked
        """
        folder_path = self.file_widget.filePath()
        if not folder_path:
            QMessageBox.warning(self,  "No folder selected",
                                "Please select a BIOMASS product folder first.")
            return
        self._inspect_folder(Path(folder_path))


    def _inspect_folder(self, folder_path: Path):
        """Parse folder metadata and update details view.
        """
        if not is_biomass_folder(folder_path):
            self.txt_details.setText("Warning: directory doesn't look like a BIOMASS product.")
            self.btn_load_layer.setEnabled(False)
            self.current_product = None
            return

        try:
            # attempt to parse the folder data into BiomassProduct obj
            self.current_product = read_biomass_product(folder_path)
            p = self.current_product

            # if successful, extract the attributes from the object, format them and
            # display them, enabling the load on canvas button if measurement files
            # exist.
            summary = [
                f"<b>Product ID:</b> {p.product_id}",
                f"<b>Level:</b> {p.level.value} ({p.product_type})",
                f"<b>Swath:</b> {p.swath}",
                f"<b>Track:</b> {p.track or 'N/A'} | <b>Frame:</b> {p.frame or 'N/A'}",
                f"<b>Phase:</b> {p.mission_phase or 'N/A'}",
                f"<b>Sensing Start:</b> {p.sensing_start or 'N/A'}",
                f"<b>Sensing Stop:</b> {p.sensing_stop or 'N/A'}",
                f"<b>Polarisations:</b> {', '.join(p.polarisations)}",
                f"<b>GCP Tie-Points:</b> {len(p.gcps)} points found",
                f"<b>Amplitude File:</b> {'Found' if p.abs_file else 'Missing'}",
                f"<b>Phase File:</b> {'Found' if p.phase_file else 'Missing'}",
                f"<b>VRT File:</b> {'Found' if p.vrt_file else 'Missing'}",
            ]
            self.txt_details.setHtml("<br>".join(summary))
            self.btn_load_layer.setEnabled(p.has_measurements)

        except ProductReadError as e:
            # log the error and reset state
            QMessageBox.critical(self, "Read Error", str(e))
            self.txt_details.setText(f"Error reading product: {e}")
            self.btn_load_layer.setEnabled(False)
            self.current_product = None


    def _load_layer_on_canvas(self):
        """Load the inspected Biomass product onto the QGIS canvas
        """
        if not self.current_product:
            return

        self.btn_load_layer.setEnabled(False)
        self.btn_load_layer.setText("Warping & Loading...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        try:
            layers = load_biomass_product(self.current_product, iface=self.iface)
            if layers:
                self.txt_details.append(
                    f"<br><b style='color:green;'>✓ Successfully loaded on canvas ({len(layers)} layer(s)).< / b > "
                )
        except Exception as err:
            QMessageBox.critical(self, "Load Error", f"Failed to load product:\n{err}")

        finally:
            QApplication.restoreOverrideCursor()
            self.btn_load_layer.setEnabled(True)
            self.btn_load_layer.setText("Load & Warp on canvas")
