"""
QGIS Processing algorithm for georeferencing BIOMASS products.
Supports Geolocation Array (NetCDF LUT), Thin-Plate Spline (TPS), and Polynomial warping.
"""

from pathlib import Path
from osgeo import gdal

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
)

from ...core.models import WarpConfig, WarpMethod
from ...georeferencing.warper import build_geolocation_vrt, get_default_cache_dir, warp_raster
from ...reader.folder_reader import is_biomass_folder, read_biomass_product


class WarpBiomassAlgorithm(QgsProcessingAlgorithm):
    """Processing algorithm to georeference ESA BIOMASS products using GDAL.

    Supports Geolocation Array (NetCDF LUT) and GCP-based interpolation (TPS / Polynomial).
    """

    INPUT = "INPUT"
    PRODUCT_FOLDER = "PRODUCT_FOLDER"
    METHOD = "METHOD"
    RESAMPLING = "RESAMPLING"
    TARGET_CRS = "TARGET_CRS"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return WarpBiomassAlgorithm()

    def name(self) -> str:
        return "warp_biomass"

    def displayName(self) -> str:
        return "Warp product"

    def group(self) -> str:
        return "Georeferencing"

    def groupId(self) -> str:
        return "georeferencing"

    def shortHelpString(self) -> str:
        return (
            "Georeferences an ESA BIOMASS product (calibrated GeoTIFF, measurement raster, "
            "or product folder) into map coordinates.\n\n"
            "Input Options:\n"
            "• Input raster layer: Select any raster layer currently open in QGIS (e.g. your "
            "calibrated Gamma0 raster), or click [...] to browse for a raster file on disk.\n"
            "• Original BIOMASS product folder: Select the parent BIOMASS folder (BIO_S1_...). "
            "Required when warping a standalone raster with the Geolocation Array method.\n\n"
            "Supported Methods:\n"
            "• Geolocation grid (NetCDF LUT): SAR geocoding using the orbit geometry "
            "grid in the NetCDF LUT. When warping a standalone raster (e.g. calibrated TIFF), "
            "select the original BIOMASS product folder.\n"
            "• Thin-Plate Spline (TPS): interpolation using embedded Ground Control "
            "Points (GCPs).\n"
            "• Polynomial (orders 1–3): polynomial fitting using embedded GCPs.\n\n"
            "Outputs a Warped VRT or GeoTIFF in the chosen target CRS."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT,
                "Input raster layer (choose from QGIS layers or browse file)",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.PRODUCT_FOLDER,
                "Original BIOMASS product folder (optional for TPS; required for Geolocation grid if input is a raster)",
                behavior=QgsProcessingParameterFile.Folder,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.METHOD,
                "Warping method",
                options=[
                    "Geolocation grid (from NetCDF LUT)",
                    "Thin-Plate Spline (TPS)",
                    "Polynomial Order 1",
                    "Polynomial Order 2",
                    "Polynomial Order 3",
                ],
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.RESAMPLING,
                "Resampling method",
                options=[
                    "Bilinear",
                    "Nearest Neighbour",
                    "Cubic",
                    "Cubic Spline",
                    "Lanczos",
                    "Average",
                ],
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterCrs(
                self.TARGET_CRS,
                "Target CRS",
                defaultValue="EPSG:4326",
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
                "Output warped raster (VRT or TIFF)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        folder_str = self.parameterAsFile(parameters, self.PRODUCT_FOLDER, context)
        method_idx = self.parameterAsEnum(parameters, self.METHOD, context)
        resample_idx = self.parameterAsEnum(parameters, self.RESAMPLING, context)
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context).authid()
        output_path = Path(self.parameterAsOutputLayer(parameters, self.OUTPUT, context)).resolve()

        folder_path = Path(folder_str).resolve() if folder_str else None

        method_map = [
            WarpMethod.GEOLOCATION_ARRAY,
            WarpMethod.TPS,
            WarpMethod.POLYNOMIAL_1,
            WarpMethod.POLYNOMIAL_2,
            WarpMethod.POLYNOMIAL_3,
        ]
        selected_method = method_map[method_idx]

        resample_map = ["bilinear", "nearest", "cubic", "cubicspline", "lanczos", "average"]
        selected_resample = resample_map[resample_idx]

        src_file = None
        product_obj = None
        band_names = None
        lut_file = None
        annotation_file = None
        lut_grid_shape = None

        # Resolve input raster from selected QGIS layer or path string
        if layer and layer.isValid():
            src_file = Path(layer.source().split("|")[0]).resolve()
            feedback.pushInfo(f"Using input raster from QGIS layer: {layer.name()} ({src_file.name})")
            layer_bands = [layer.bandName(i) for i in range(1, layer.bandCount() + 1)]
            if any(layer_bands) and not all(b.startswith("Band ") for b in layer_bands):
                band_names = layer_bands
        else:
            input_str = self.parameterAsString(parameters, self.INPUT, context)
            if input_str:
                p = Path(input_str.split("|")[0]).resolve()
                if p.is_file():
                    src_file = p
                elif p.is_dir() and not folder_path and is_biomass_folder(p):
                    folder_path = p

        if not src_file and not folder_path:
            raise QgsProcessingException(
                "Either an input raster layer/file or an original BIOMASS product folder must be specified."
            )

        # 1. Parse product folder if provided
        if folder_path and folder_path.is_dir() and is_biomass_folder(folder_path):
            feedback.pushInfo(f"Reading BIOMASS product folder: {folder_path.name}")
            product_obj = read_biomass_product(folder_path)
            lut_file = product_obj.lut_file
            annotation_file = product_obj.annotation_file
            if not band_names:
                band_names = product_obj.polarisations
            lut_grid_shape = product_obj.lut_grid_shape

        # 2. If src_file was not selected from layer or file, use product's primary measurement file
        if not src_file and product_obj:
            src_file = product_obj.abs_file or product_obj.vrt_file

        if not src_file or not src_file.is_file():
            raise QgsProcessingException(
                f"No valid raster measurement file found for input: {src_file or folder_path}"
            )

        # 3. If band_names not yet determined, extract from src_file if available
        if not band_names:
            src_ds_check = gdal.Open(str(src_file), gdal.GA_ReadOnly)
            if src_ds_check:
                extracted = [
                    src_ds_check.GetRasterBand(i).GetDescription()
                    for i in range(1, src_ds_check.RasterCount + 1)
                ]
                if any(extracted):
                    band_names = extracted
                src_ds_check = None

        warp_input = src_file

        # 4. Handle Geolocation Array method
        if selected_method == WarpMethod.GEOLOCATION_ARRAY:
            # Check embedded metadata if lut_file or annotation_file are not yet known
            if not lut_file or not annotation_file:
                ds_check = gdal.Open(str(src_file), gdal.GA_ReadOnly)
                if ds_check is not None:
                    meta_lut = ds_check.GetMetadataItem("BIOMASS_LUT_FILE")
                    meta_annot = ds_check.GetMetadataItem("BIOMASS_ANNOTATION_FILE")
                    ds_check = None
                    if meta_lut and Path(meta_lut).is_file():
                        lut_file = Path(meta_lut)
                    if meta_annot and Path(meta_annot).is_file():
                        annotation_file = Path(meta_annot)

            if not lut_file or not lut_file.is_file():
                raise QgsProcessingException(
                    "Geolocation Array warping requires the original BIOMASS product folder "
                    "containing the NetCDF LUT file (*_lut.nc).\n"
                    "Please specify the 'Original BIOMASS product folder' parameter."
                )
            if not annotation_file or not annotation_file.is_file():
                raise QgsProcessingException(
                    "Geolocation Array warping requires the annotation XML file (*_annot.xml) "
                    "from the BIOMASS product folder.\n"
                    "Please specify the 'Original BIOMASS product folder' parameter."
                )

            cache_dir = get_default_cache_dir()
            geoloc_vrt = cache_dir / f"{src_file.stem}_geoloc.vrt"
            feedback.pushInfo(f"Building Geolocation VRT referencing {lut_file.name}...")
            try:
                build_geolocation_vrt(
                    src_raster_path=src_file,
                    lut_nc_path=lut_file,
                    output_vrt_path=geoloc_vrt,
                    grid_shape=lut_grid_shape,
                    annotation_xml_path=annotation_file,
                    band_names=band_names,
                    srs="EPSG:4326",
                )
            except Exception as err:
                raise QgsProcessingException(f"Failed to build Geolocation grid VRT: {err}") from err

            warp_input = geoloc_vrt

        warp_config = WarpConfig(
            method=selected_method,
            dst_crs=target_crs,
            resample_alg=selected_resample,
        )

        feedback.pushInfo(
            f"Warping {src_file.name} -> {output_path.name} "
            f"using {selected_method.value} ({selected_resample}, {target_crs})..."
        )

        def _progress_cb(fraction: float, message: str = ""):
            if feedback.isCanceled():
                raise QgsProcessingException("Warping canceled by user.")
            feedback.setProgress(int(fraction * 100))
            if message:
                feedback.pushInfo(str(message))

        try:
            out = warp_raster(
                input_path=warp_input,
                output_path=output_path,
                config=warp_config,
                band_names=band_names,
                progress_callback=_progress_cb,
            )
            feedback.pushInfo(f"Successfully generated warped output: {out}")
        except Exception as err:
            raise QgsProcessingException(f"Warping failed: {err}") from err

        return {self.OUTPUT: str(output_path)}
