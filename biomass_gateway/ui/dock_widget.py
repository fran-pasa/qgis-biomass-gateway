"""
Main dockwidget panel for the ESA BIOMASS Gateway plugin.

Provides three tabs:
1. Local Product Browser - Inspect and georeference local BIOMASS directory products.
2. MAAP search - Discover remote BIOMASS collections via ESA MAAP STAC API and inspect
   product metadata and footprints.
3. Settings - Configure and test ESA MAAP Keycloak authentication credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsJsonUtils,
    QgsProject,
)
from qgis.gui import QgsDockWidget, QgsFileWidget, QgsRubberBand
from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.exceptions import ProductReadError
from ..core.logger import get_logger, log_qgis_info, log_qgis_warning
from ..core.models import BiomassProduct, WarpConfig, WarpMethod
from ..maap.auth import AuthValidationResult, MAAPAuthManager
from ..maap.client import (
    BIOMASS_COLLECTIONS,
    MAAPSTACClient,
    STACItem,
    STACSearchResult,
)
from ..reader.folder_reader import is_biomass_folder, read_biomass_product
from ..reader.loader import load_biomass_product

logger = get_logger("ui.dock_widget")


class BiomassDockWidget(QgsDockWidget):
    """Main dockwidget panel
    """
    def __init__(self, iface, parent=None):
        # set up main widget class
        super().__init__(parent)
        self.iface = iface

        # assign authentication manager
        self.auth_manager = MAAPAuthManager()

        # STAC API client and catalog search state
        self.stac_client = MAAPSTACClient()
        self.search_bbox: tuple[float, float, float, float] | None = None
        self.search_results: list[STACItem] = []
        self.selected_stac_item: STACItem | None = None
        self.current_start_record: int = 1
        self.next_start_record: int | None = None
        self.footprint_rubber_band: QgsRubberBand | None = None

        # Local product browser state
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
        self.tabs.addTab(self._create_search_tab(), "MAAP search")
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

        # Action buttons
        btn_layout = QHBoxLayout()
        self.btn_inspect = QPushButton("Inspect product")
        self.btn_inspect.clicked.connect(self._inspect_current_folder)
        btn_layout.addWidget(self.btn_inspect)

        # resampling method selector
        resample_layout = QHBoxLayout()
        resample_layout.addWidget(QLabel("<b>Resampling method:</b>"))
        self.cmb_resample = QComboBox()
        self.cmb_resample.addItems([
            "Nearest Neighbour",
            "Bilinear",
            "Cubic"
        ])
        self.cmb_resample.setCurrentIndex(0)
        resample_layout.addWidget(self.cmb_resample)
        layout.addLayout(resample_layout)

        self.btn_load_layer = QPushButton("Load layer on canvas")
        self.btn_load_layer.setEnabled(False)
        self.btn_load_layer.clicked.connect(self._load_layer_on_canvas)
        btn_layout.addWidget(self.btn_load_layer)
        layout.addLayout(btn_layout)

        # Metadata display
        layout.addWidget(QLabel("<b>Product details:</b>"))
        self.txt_details = QTextEdit()
        self.txt_details.setReadOnly(True)
        layout.addWidget(self.txt_details)

        return widget

    def _on_folder_selected(self, folder_path: str) -> None:
        """Handle folder selection change."""
        if not folder_path:
            return
        self._inspect_folder(Path(folder_path))

    def _inspect_current_folder(self) -> None:
        """Inspect product button click handler."""
        folder_path = self.file_widget.filePath()
        if not folder_path:
            QMessageBox.warning(
                self,
                "No folder selected",
                "Please select a BIOMASS product folder first.",
            )
            return
        self._inspect_folder(Path(folder_path))

    def _inspect_folder(self, folder_path: Path) -> None:
        """Parse folder metadata and update details view."""
        if not is_biomass_folder(folder_path):
            self.txt_details.setText("Warning: directory doesn't look like a BIOMASS product.")
            self.btn_load_layer.setEnabled(False)
            self.current_product = None
            return

        try:
            self.current_product = read_biomass_product(folder_path)
            p = self.current_product

            if p.is_geocoded:
                lut_status = "Not required (product is already map-projected)"
            elif p.has_lut and p.lut_grid_shape:
                lut_status = f"Found ({p.lut_grid_shape[0]} × {p.lut_grid_shape[1]} points)"
            else:
                lut_status = "Not found (using GCP/TPS fallback)"

            meas_label = "Amplitude" if not p.is_geocoded else "Measurement"

            summary = [
                f"<b>Product ID:</b> {p.product_id}",
                f"<b>Level:</b> {p.level.value} ({p.product_type})",
                f"<b>Swath:</b> {p.swath}",
                f"<b>Track:</b> {p.track or 'N/A'} | <b>Frame:</b> {p.frame or 'N/A'}",
                f"<b>Phase:</b> {p.mission_phase or 'N/A'}",
                f"<b>Sensing Start:</b> {p.sensing_start or 'N/A'}",
                f"<b>Sensing Stop:</b> {p.sensing_stop or 'N/A'}",
                f"<b>NetCDF Geolocation LUT:</b> {lut_status}",
                f"<b>Polarisations:</b> {', '.join(p.polarisations)}",
                f"<b>GCP Tie-Points:</b> {len(p.gcps)} points found",
                f"<b>{meas_label} File:</b> {'Found' if p.abs_file else 'Missing'}",
                f"<b>Phase File:</b> {'Found' if p.phase_file else 'Missing'}",
                f"<b>VRT File:</b> {'Found' if p.vrt_file else 'Missing'}",
            ]
            self.txt_details.setHtml("<br>".join(summary))
            self.btn_load_layer.setEnabled(p.has_measurements)

        except ProductReadError as e:
            QMessageBox.critical(self, "Read Error", str(e))
            self.txt_details.setText(f"Error reading product: {e}")
            self.btn_load_layer.setEnabled(False)
            self.current_product = None

    def _load_layer_on_canvas(self) -> None:
        """Load and georeference inspected local product onto QGIS canvas."""
        if not self.current_product:
            return

        self.btn_load_layer.setEnabled(False)
        self.btn_inspect.setEnabled(False)
        self.cmb_resample.setEnabled(False)
        if self.current_product.is_geocoded:
            self.btn_load_layer.setText("Loading...")
        else:
            self.btn_load_layer.setText("Warping (0%)...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        def update_progress(fraction: float, message: str = ""):
            try:
                pct = int(fraction * 100)
                if pct < 80:
                    self.btn_load_layer.setText(f"Warping ({pct}%)...")
                else:
                    self.btn_load_layer.setText(f"Building overviews ({pct}%)...")
                QApplication.processEvents()
            except Exception:
                pass

        try:
            resample_text = self.cmb_resample.currentText().lower()
            if "nearest" in resample_text:
                resample_alg = "nearest"
            elif "cubic" in resample_text:
                resample_alg = "cubic"
            else:
                resample_alg = "bilinear"

            warp_config = WarpConfig(
                method=WarpMethod.GEOLOCATION_ARRAY,
                resample_alg=resample_alg
            )
            layers = load_biomass_product(
                self.current_product,
                iface=self.iface,
                config=warp_config,
                progress_callback=update_progress,
            )
            if layers:
                self.txt_details.append(
                    f"<br><b style='color:green;'>✓ Successfully loaded on canvas ({len(layers)} layer(s)).</b>"
                )
        except Exception as err:
            QMessageBox.critical(self, "Load Error", f"Failed to load product:\n{err}")

        finally:
            QApplication.restoreOverrideCursor()
            self.btn_load_layer.setEnabled(True)
            self.btn_inspect.setEnabled(True)
            self.cmb_resample.setEnabled(True)
            self.btn_load_layer.setText("Load layer on canvas")


    def _create_search_tab(self) -> QWidget:
        """Build the MAAP STAC Catalog Search tab
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # search filters group
        filters_box = QGroupBox("Search filters")
        filters_layout = QVBoxLayout(filters_box)

        # collection selector
        filters_layout.addWidget(QLabel("<b>Collection:</b>"))
        self.cmb_collection = QComboBox()
        self.cmb_collection.addItem("All BIOMASS Collections", "ALL")
        for col in BIOMASS_COLLECTIONS:
            self.cmb_collection.addItem(col, col)
        filters_layout.addWidget(self.cmb_collection)

        # spatial extent filter
        filters_layout.addWidget(QLabel("<b>Spatial filter (Bounding box):</b>"))
        extent_btn_layout = QHBoxLayout()
        self.btn_canvas_extent = QPushButton("Use canvas extent")
        self.btn_canvas_extent.clicked.connect(self._use_canvas_extent)
        extent_btn_layout.addWidget(self.btn_canvas_extent)

        self.btn_clear_extent = QPushButton("Clear Extent")
        self.btn_clear_extent.clicked.connect(self._clear_canvas_extent)
        extent_btn_layout.addWidget(self.btn_clear_extent)
        filters_layout.addLayout(extent_btn_layout)

        self.lbl_extent_status = QLabel("<i>Extent: Global (no spatial filter)</i>")
        self.lbl_extent_status.setWordWrap(True)
        filters_layout.addWidget(self.lbl_extent_status)

        # filter by date
        self.chk_date_filter = QCheckBox("Filter by acquisition date")
        self.chk_date_filter.toggled.connect(self._toggle_date_filter)
        filters_layout.addWidget(self.chk_date_filter)

        date_layout = QHBoxLayout()
        date_layout.addWidget(QLabel("From:"))
        self.date_start = QDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_start.setDate(QDate(2026, 1, 1))
        self.date_start.setEnabled(False)
        date_layout.addWidget(self.date_start)

        date_layout.addWidget(QLabel("To:"))
        self.date_end = QDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_end.setDate(QDate.currentDate())
        self.date_end.setEnabled(False)
        date_layout.addWidget(self.date_end)
        filters_layout.addLayout(date_layout)

        # search button
        self.btn_search = QPushButton("Search MAAP catalog")
        self.btn_search.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_search.clicked.connect(self._on_search_clicked)
        filters_layout.addWidget(self.btn_search)

        layout.addWidget(filters_box)

        # results summary
        self.lbl_search_summary = QLabel("<i>No search executed yet.</i>")
        self.lbl_search_summary.setWordWrap(True)
        layout.addWidget(self.lbl_search_summary)

        # results table
        self.tbl_results = QTableWidget()
        self.tbl_results.setColumnCount(5)
        self.tbl_results.setHorizontalHeaderLabels([
            "Collection",
            "Product ID",
            "Acquisition Start",
            "Pols",
            "Orbit",
        ])
        self.tbl_results.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tbl_results.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.tbl_results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl_results.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tbl_results.itemSelectionChanged.connect(self._on_stac_item_selected)
        layout.addWidget(self.tbl_results)

        # product details
        layout.addWidget(QLabel("<b>Selected product details:</b>"))
        self.txt_stac_details = QTextEdit()
        self.txt_stac_details.setReadOnly(True)
        self.txt_stac_details.setMaximumHeight(90)
        layout.addWidget(self.txt_stac_details)

        # pagination & load buttons
        action_layout = QHBoxLayout()

        self.btn_prev_page = QPushButton("◀ Previous")
        self.btn_prev_page.setEnabled(False)
        self.btn_prev_page.clicked.connect(self._prev_page)
        action_layout.addWidget(self.btn_prev_page)

        self.btn_next_page = QPushButton("Next ▶")
        self.btn_next_page.setEnabled(False)
        self.btn_next_page.clicked.connect(self._next_page)
        action_layout.addWidget(self.btn_next_page)

        self.btn_load_stac_layer = QPushButton("Stream to canvas (soon available)")
        self.btn_load_stac_layer.setEnabled(False)
        self.btn_load_stac_layer.setStyleSheet("font-weight: bold;")
        action_layout.addWidget(self.btn_load_stac_layer)

        layout.addLayout(action_layout)

        return widget

    def _toggle_date_filter(self, enabled: bool) -> None:
        """Helper to enable or disable date edit pickers
        """
        self.date_start.setEnabled(enabled)
        self.date_end.setEnabled(enabled)

    def _use_canvas_extent(self) -> None:
        """Helper to extract current QGIS map canvas extent
        """
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        src_crs = canvas.mapSettings().destinationCrs()
        dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        try:
            transform = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
            rect_wgs84 = transform.transformBoundingBox(extent)

            # clip coordinates to geographic lat/lon boundaries [-180, 180, -90, 90]
            min_lon = max(-180.0, min(180.0, rect_wgs84.xMinimum()))
            max_lon = max(-180.0, min(180.0, rect_wgs84.xMaximum()))
            min_lat = max(-90.0, min(90.0, rect_wgs84.yMinimum()))
            max_lat = max(-90.0, min(90.0, rect_wgs84.yMaximum()))

            self.search_bbox = (
                round(min_lon, 4),
                round(min_lat, 4),
                round(max_lon, 4),
                round(max_lat, 4),
            )
            self.lbl_extent_status.setText(
                f"<b style='color: #2e7d32;'>✓ Extent:</b> [{self.search_bbox[0]}, {self.search_bbox[1]} to {self.search_bbox[2]}, {self.search_bbox[3]}]"
            )
        except Exception as err:
            QMessageBox.warning(
                self,
                "Extent transform error",
                f"Could not transform canvas extent to WGS84: {err}",
            )


    def _clear_canvas_extent(self) -> None:
        """Helper to clear spatial bounding box filter
        """
        self.search_bbox = None
        self.lbl_extent_status.setText("<i>Extent: global (no spatial filter)</i>")


    def _on_search_clicked(self) -> None:
        """Helper to run fresh starting at record 1
        """
        self._execute_stac_search(start_record=1)


    def _prev_page(self) -> None:
        """Helper to fetch previous page of STAC search results
        """
        prev_record = max(1, self.current_start_record - 20)
        self._execute_stac_search(start_record=prev_record)


    def _next_page(self) -> None:
        """Fetch next page of STAC search results."""
        if self.next_start_record:
            self._execute_stac_search(start_record=self.next_start_record)


    def _execute_stac_search(self, start_record: int = 1) -> None:
        """Helper to run STAC query on ESA MAAP and populate
        results table
        """
        # define collections to query
        selected_code = self.cmb_collection.currentData()
        collections = (
            BIOMASS_COLLECTIONS if selected_code == "ALL" else (selected_code,)
        )

        # define temporal range
        datetime_range: str | None = None
        if self.chk_date_filter.isChecked():
            start_str = self.date_start.date().toString("yyyy-MM-dd")
            end_str = self.date_end.date().toString("yyyy-MM-dd")
            datetime_range = f"{start_str}T00:00:00Z/{end_str}T23:59:59Z"

        # update UI state (wait cursor)
        self.btn_search.setEnabled(False)
        self.lbl_search_summary.setText("<i>Querying ESA MAAP STAC catalog...</i>")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        try:
            result: STACSearchResult = self.stac_client.search(
                collections=collections,
                bbox=self.search_bbox,
                datetime_range=datetime_range,
                limit=20,
                start_record=start_record,
            )

            self.search_results = list(result.items)
            self.current_start_record = start_record
            self.next_start_record = result.next_start_record

            # populate results table
            self.tbl_results.setRowCount(0)
            for row_idx, item in enumerate(self.search_results):
                self.tbl_results.insertRow(row_idx)
                self.tbl_results.setItem(row_idx, 0, QTableWidgetItem(item.collection))
                self.tbl_results.setItem(row_idx, 1, QTableWidgetItem(item.id))
                self.tbl_results.setItem(row_idx, 2, QTableWidgetItem(item.sensing_start))
                self.tbl_results.setItem(row_idx, 3, QTableWidgetItem(", ".join(item.polarizations)))
                self.tbl_results.setItem(row_idx, 4, QTableWidgetItem(str(item.orbit or "")))

            # update summary and pagination controls
            end_record = start_record + result.number_returned - 1
            self.lbl_search_summary.setText(
                f"<b>Matched:</b> {result.number_matched} products. "
                f"<b>Showing:</b> {result.number_returned} (records {start_record}–{end_record})"
            )
            self.btn_prev_page.setEnabled(start_record > 1)
            self.btn_next_page.setEnabled(result.next_start_record is not None)

            # reset selection
            self.selected_stac_item = None
            self.btn_load_stac_layer.setEnabled(False)
            self.txt_stac_details.clear()
            if self.footprint_rubber_band:
                self.footprint_rubber_band.reset()

        except Exception as err:
            self.lbl_search_summary.setText(f"<b style='color: #c62828;'>Search failed: {err}</b>")
            QMessageBox.critical(
                self,
                "Search Error",
                f"Failed to query ESA MAAP STAC Catalog:\n{err}",
            )

        finally:
            QApplication.restoreOverrideCursor()
            self.btn_search.setEnabled(True)

    def _on_stac_item_selected(self) -> None:
        """Helper to allow user to select a row in the results table
        """
        selected_rows = self.tbl_results.selectionModel().selectedRows()
        if not selected_rows:
            self.selected_stac_item = None
            self.btn_load_stac_layer.setEnabled(False)
            self.txt_stac_details.clear()
            if self.footprint_rubber_band:
                self.footprint_rubber_band.reset()
            return

        row = selected_rows[0].row()
        if row < 0 or row >= len(self.search_results):
            return

        item = self.search_results[row]
        self.selected_stac_item = item

        # streaming to canvas is disabled (soon available)
        self.btn_load_stac_layer.setEnabled(False)

        # show metadata summary
        # TODO: add more useful metadata
        summary = [
            f"<b>Product ID:</b> {item.id}",
            f"<b>Collection:</b> {item.collection} | <b>Type:</b> {item.product_type}",
            f"<b>Acquisition:</b> {item.sensing_start}",
            f"<b>Polarisations:</b> {', '.join(item.polarizations)} | <b>Orbit:</b> {item.orbit or 'N/A'}",
        ]
        self.txt_stac_details.setHtml("<br>".join(summary))

        # draw footprint on QGIS canvas
        self._draw_footprint(item.geometry)

    def _draw_footprint(self, geometry_dict: dict) -> None:
        """Helper to draw the STAC item footprint on the QGIS canvas
        """
        if not self.footprint_rubber_band:
            self.footprint_rubber_band = QgsRubberBand(self.iface.mapCanvas(), Qgis.GeometryType.Polygon)

            # use orange fill with some transparency & solid orange outline
            self.footprint_rubber_band.setColor(QColor(255, 140, 0, 70))
            self.footprint_rubber_band.setStrokeColor(QColor(255, 140, 0, 230))
            self.footprint_rubber_band.setWidth(2)

        self.footprint_rubber_band.reset()
        if not geometry_dict:
            return

        try:
            geom_json = json.dumps(geometry_dict)
            geom = QgsJsonUtils.geometryFromGeoJson(geom_json)
            if geom and not geom.isEmpty():
                # transform footprint if needed
                src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
                dest_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
                if src_crs != dest_crs:
                    transform = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
                    geom.transform(transform)

                self.footprint_rubber_band.setToGeometry(geom, None)
        except Exception as err:
            logger.warning(f"Could not render footprint on canvas: {err}")


    def _create_settings_tab(self) -> QWidget:
        """Build the Setting tab UI (for MAAP credential management)."""
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


    def closeEvent(self, event) -> None:
        """Clean up map canvas decorations when panel is closed
        """
        if self.footprint_rubber_band:
            self.footprint_rubber_band.reset()
            self.footprint_rubber_band = None
        super().closeEvent(event)
