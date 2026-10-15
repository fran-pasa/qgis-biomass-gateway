"""
Layer loader and SAR raster rendering for BIOMASS products.
"""

from pathlib import Path

from qgis.core import (
    QgsContrastEnhancement,
    QgsCoordinateTransform,
    QgsMultiBandColorRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSingleBandGrayRenderer,
)

from ..core.exceptions import ProductReadError, WarpError
from ..core.logger import get_logger, log_qgis_info, log_qgis_warning
from ..core.models import BiomassProduct, ProductLevel, WarpConfig, WarpMethod
from ..georeferencing.warper import create_warped_vrt

logger = get_logger("reader.loader")


def apply_sar_false_color_styling(
        layer: QgsRasterLayer,
        red_band: int = 1,
        green_band: int = 2,
        blue_band: int = 4,
) -> None:
    """Apply False Color RGB symbology with cumulative 2%-98% stretch.
    By default: Red = band 1 (HH), Green = Band 2 (HV), Blue = Band 4 (VV)
    """
    provider = layer.dataProvider()
    if not provider:
        return

    band_count = layer.bandCount()
    r = min(red_band, band_count)
    g = min(green_band, band_count)
    b = min(blue_band, band_count)

    renderer = QgsMultiBandColorRenderer(provider, r, g, b)
    applied_all = True

    for band_num, setter in [
        (r, renderer.setRedContrastEnhancement),
        (g, renderer.setGreenContrastEnhancement),
        (b, renderer.setBlueContrastEnhancement),
    ]:
        band_applied = False
        try:
            min_val, max_val = provider.cumulativeCut(band_num, 0.02, 0.98,
                                                      layer.extent(), 250000)
            if min_val is not None and max_val is not None:
                min_val = max(0.0, float(min_val))
                max_val = float(max_val)
                if min_val < max_val:
                    ce = QgsContrastEnhancement(provider.dataType(band_num))
                    ce.setContrastEnhancementAlgorithm(
                        QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                        True,
                    )
                    ce.setMinimumValue(min_val)
                    ce.setMaximumValue(max_val)
                    setter(ce)
                    band_applied = True
        except Exception as err:
            logger.debug("Could not compute cumulative cut for band %d: %s", band_num, err)

        if not band_applied:
            applied_all = False

    layer.setRenderer(renderer)
    if not applied_all:
        layer.setDefaultContrastEnhancement()
    layer.triggerRepaint()


def apply_sar_gray_styling(layer: QgsRasterLayer, gray_band: int = 1) -> None:
    """Apply grayscale rendering with cumulative 2%-98% stretch
    for single-band SAR rasters.
    """
    provider = layer.dataProvider()
    if not provider:
        return

    renderer = QgsSingleBandGrayRenderer(provider, gray_band)
    applied_ce = False

    try:
        min_val, max_val = provider.cumulativeCut(gray_band, 0.02, 0.98,
                                                  layer.extent(), 250000)
        if min_val is not None and max_val is not None and min_val < max_val:
            min_val = max(0.0, float(min_val))
            if min_val < float(max_val):
                ce = QgsContrastEnhancement(provider.dataType(gray_band))
                ce.setContrastEnhancementAlgorithm(
                    QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                    True,
                )
                ce.setMinimumValue(min_val)
                ce.setMaximumValue(float(max_val))
                renderer.setContrastEnhancement(ce)
                applied_ce = True
    except Exception as err:
        logger.debug("Could not compute cumulative cut for gray band %d: %s", gray_band, err)

    layer.setRenderer(renderer)
    if not applied_ce:
        layer.setDefaultContrastEnhancement()
    layer.triggerRepaint()


def load_biomass_product(
        product: BiomassProduct,
        iface=None,
        warp: bool = True,
        load_phase: bool = False,
        config: WarpConfig | None = None,
        progress_callback=None,
) -> list[QgsRasterLayer]:
    """Load and style a BiomassProduct into the QGIS project canvas.

    Create a dedicated layer group named after the product ID, georeference
    the amplitude raster via TPS, apply standard false-color symbology (HH-HV-VV),
    and center the map canvas.

    Args:
        product: The parsed BiomassProduct model instance.
        iface: Optional QgisInterface instance for map canvas control.
        warp: If True, generates a TPS Warped VRT; if False, loads raw file.
        load_phase: If True and product has phase data, also loads the phase layer.
        config: Optional custom WarpConfig.
        progress_callback: Optional progress reporting callback (fraction, message).

    Returns:
        List of loaded QgsRasterLayer instances.

    Raises:
        ProductReadError: If raster layer creation fails or measurement file is missing.
    """
    if not product.has_measurements:
        raise ProductReadError(f"Product {product.product_id} has no measurement files to load.")

    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group_name = product.product_id
    group = root.findGroup(group_name)
    if group is None:
        group = root.insertGroup(0, group_name)

    loaded_layers: list[QgsRasterLayer] = []

    # determine whether georeferencing/warping is required.
    # geocoded products (L2A, L2B) are already map-projected and
    # should not be warped.
    needs_warp = (
        warp
        and not product.is_geocoded
        and (product.is_l1a or product.is_l1b or product.is_l1c or product.has_lut or len(product.gcps) > 0)
    )

    # Determine measurement label
    if product.is_l1a or product.is_l1b or product.is_l1c or product.level == ProductLevel.L1M:
        meas_label = "Amplitude"
    elif product.product_type != "UNKNOWN":
        meas_label = product.product_type
    else:
        meas_label = "Measurement"

    # check if geolocation grid or TPS will be used
    is_geoloc = (
        needs_warp
        and product.has_lut
        and product.lut_grid_shape is not None
        and (config is None or config.method == WarpMethod.GEOLOCATION_ARRAY)
    )
    geoloc_tag = "Geolocation grid" if is_geoloc else "TPS"

    # resolve and load primary measurement raster
    if needs_warp:
        try:
            amp_path = create_warped_vrt(
                product,
                config=config,
                measurement_type="abs",
                progress_callback=progress_callback,
            )
            layer_title = f"{product.product_id} [{meas_label} - {geoloc_tag}]"
        except WarpError as err:
            logger.error("Georeferencing failed for %s: %s", product.product_id, err)
            raise ProductReadError(f"Georeferencing failed for {product.product_id}: {err}") from err
    else:
        amp_path = product.abs_file or product.vrt_file
        layer_title = f"{product.product_id} [{meas_label}]"

    if not amp_path or not Path(amp_path).is_file():
        raise ProductReadError(f"No valid measurement file found for product {product.product_id}")

    amp_layer = QgsRasterLayer(str(amp_path), layer_title, "gdal")
    if not amp_layer.isValid():
        err_msg = amp_layer.error().summary() if amp_layer.error() else "Unknown GDAL error"
        raise ProductReadError(f"Failed to load raster layer {amp_path}: {err_msg}")

    # make polarimetric SAR composite (HH, HV, VV) if 3+ bands,
    # else single-band grayscale
    if amp_layer.bandCount() >= 3:
        blue_b = 4 if amp_layer.bandCount() >= 4 else 3
        apply_sar_false_color_styling(amp_layer, red_band=1, green_band=2, blue_band=blue_b)
    else:
        apply_sar_gray_styling(amp_layer, gray_band=1)

    project.addMapLayer(amp_layer, False)
    group.addLayer(amp_layer)
    loaded_layers.append(amp_layer)

    # (optional) for complex products, resolve and load phase raster
    if load_phase and product.phase_file and product.phase_file.is_file():
        try:
            if needs_warp:
                phase_path = create_warped_vrt(
                    product,
                    config=config,
                    measurement_type="phase",
                    progress_callback=progress_callback,
                )
                phase_title = f"{product.product_id} [Phase - {geoloc_tag}]"
            else:
                phase_path = product.phase_file
                phase_title = f"{product.product_id} [Phase]"

            phase_layer = QgsRasterLayer(str(phase_path), phase_title, "gdal")
            if phase_layer.isValid():
                apply_sar_gray_styling(phase_layer, gray_band=1)
                project.addMapLayer(phase_layer, False)
                group.addLayer(phase_layer)

                # keep phase unchecked by default so canvas doesn't get messy
                tree_layer = group.findLayer(phase_layer.id())
                if tree_layer:
                    tree_layer.setItemVisibilityChecked(False)
                loaded_layers.append(phase_layer)
        except Exception as err:
            log_qgis_warning(f"Could not load phase layer for {product.product_id}: {err}")

    # center and zoom map canvas if iface is available
    if iface and amp_layer.isValid():
        canvas = iface.mapCanvas()
        layer_crs = amp_layer.crs()
        canvas_crs = canvas.mapSettings().destinationCrs()
        extent = amp_layer.extent()

        if layer_crs.isValid() and canvas_crs.isValid() and layer_crs != canvas_crs:
            transform = QgsCoordinateTransform(layer_crs, canvas_crs, project)
            try:
                extent = transform.transformBoundingBox(extent)
            except Exception as err:
                logger.debug("Could not transform extent to canvas CRS: %s", err)

        if not extent.isEmpty():
            extent.scale(1.05)
            canvas.setExtent(extent)
            canvas.refresh()
        iface.setActiveLayer(amp_layer)

    log_qgis_info(f"Successfully loaded {product.product_id} into canvas group '{group_name}'.")
    return loaded_layers
