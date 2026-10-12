"""
Layer loader and SAR raster rendering for BIOMASS products.
"""

from pathlib import Path

from qgis.core import (
    QgsContrastEnhancement,
    QgsMultiBandColorRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSingleBandGrayRenderer,
)

from ..core.exceptions import ProductReadError, WarpError
from ..core.logger import get_logger, log_qgis_info, log_qgis_warning
from ..core.models import BiomassProduct, WarpConfig
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

    for band_num, setter in [
        (r, renderer.setRedContrastEnhancement),
        (g, renderer.setGreenContrastEnhancement),
        (b, renderer.setBlueContrastEnhancement),
    ]:
        ce = QgsContrastEnhancement(provider.dataType(band_num))
        ce.setContrastEnhancementAlgorithm(
            QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
            True,
        )
        try:
            min_val, max_val = provider.cumulativeCut(band_num, 0.02, 0.98,
                                                      QgsRectangle(), 250000)
            if min_val < max_val:
                ce.setMinimumValue(min_val)
                ce.setMaximumValue(max_val)
        except Exception as err:
            logger.debug("Could not compute cumulative cut for band %d: %s", band_num, err)

        setter(ce)

    layer.setRenderer(renderer)
    layer.triggerRepaint()


def apply_sar_gray_styling(layer: QgsRasterLayer, gray_band: int = 1) -> None:
    """Apply grayscale rendering with cumulative 2%-98% stretch
    for single-band SAR rasters.
    """
    provider = layer.dataProvider()
    if not provider:
        return

    renderer = QgsSingleBandGrayRenderer(provider, gray_band)
    ce = QgsContrastEnhancement(provider.dataType(gray_band))
    ce.setContrastEnhancementAlgorithm(
        QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
        True,
    )
    try:
        min_val, max_val = provider.cumulativeCut(gray_band, 0.02, 0.98,
                                                  QgsRectangle(), 250000)
        if min_val < max_val:
            ce.setMinimumValue(min_val)
            ce.setMaximumValue(max_val)
    except Exception as err:
        logger.debug("Could not compute cumulative cut for gray band %d: %s", gray_band, err)

    renderer.setContrastEnhancement(ce)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def load_biomass_product(
        product: BiomassProduct,
        iface=None,
        warp: bool = True,
        load_phase: bool = False,
        config: WarpConfig | None = None,
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

    # resolve and load amplitude raster
    if warp and (product.is_l1a or product.is_l1b or len(product.gcps) > 0):
        try:
            amp_path = create_warped_vrt(product, config=config, measurement_type="abs")
            layer_title = f"{product.product_id} [Amplitude - TPS]"
        except WarpError as err:
            logger.error("TPS Warping failed for %s: %s", product.product_id, err)
            raise ProductReadError(f"Georeferencing failed for {product.product_id}: {err}") from err
    else:
        amp_path = product.abs_file or product.vrt_file
        layer_title = f"{product.product_id} [Amplitude]"

    if not amp_path or not Path(amp_path).is_file():
        raise ProductReadError(f"No valid amplitude file found for product {product.product_id}")

    amp_layer = QgsRasterLayer(str(amp_path), layer_title, "gdal")
    if not amp_layer.isValid():
        err_msg = amp_layer.error().summary() if amp_layer.error() else "Unknown GDAL error"
        raise ProductReadError(f"Failed to load raster layer {amp_path}: {err_msg}")

    # apply false color if 3 layers, otherwise grayscale
    if amp_layer.bandCount() >= 3:
        apply_sar_false_color_styling(amp_layer, red_band=1, green_band=2, blue_band=4)
    else:
        apply_sar_gray_styling(amp_layer, gray_band=1)

    project.addMapLayer(amp_layer, False)
    group.addLayer(amp_layer)
    loaded_layers.append(amp_layer)

    # (optional) for L1A data, resolve and load phase raster
    if load_phase and product.phase_file and product.phase_file.is_file():
        try:
            if warp and len(product.gcps) > 0:
                phase_path = create_warped_vrt(product, config=config, measurement_type="phase")
                phase_title = f"{product.product_id} [Phase - TPS]"
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
        canvas.setExtent(amp_layer.extent())
        canvas.refresh()

    log_qgis_info(f"Successfully loaded {product.product_id} into canvas group '{group_name}'.")
    return loaded_layers
