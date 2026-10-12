"""
QGIS Processing algorithm for Thin-Plate Spline (TPS) warping of BIOMASS products
Created mainly to test how to provide processing algorithms through the plugin
"""

from pathlib import Path

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterRasterDestination,
)

from ...core.models import WarpConfig, WarpMethod
from ...georeferencing.warper import warp_raster
from ...reader.folder_reader import is_biomass_folder, read_biomass_product


class WarpBiomassAlgorithm(QgsProcessingAlgorithm):
    """
    Processing algorithm to georeference ESA BIOMASS products using GDAL TPS
    """

    INPUT = "INPUT"
    METHOD = "METHOD"
    TARGET_CRS = "TARGET_CRS"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return WarpBiomassAlgorithm()

    def name(self) -> str:
        return "warp_biomass"

    def displayName(self) -> str:
        return "Warp BIOMASS product"

    def group(self) -> str:
        return "Georeferencing"

    def groupId(self) -> str:
        return "georeferencing"

    def shortHelpString(self) -> str:
        return (
            "Georeferences an ESA BIOMASS L1A/L1B product using Ground Control Points (GCPs) "
            "and GDAL Thin-Plate Spline (TPS) interpolation.\n\n"
            "Outputs a Warped VRT or GeoTIFF referencing the source data."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT,
                "Input BIOMASS product folder (or Measurement file)",
                behavior=QgsProcessingParameterFile.Folder,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.METHOD,
                "Warping Method",
                options=[
                    "Thin-Plate Spline (TPS)",
                    "Polynomial Order 1",
                    "Polynomial Order 2",
                    "Polynomial Order 3",
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

    def processAlgorithm(self, parameters, context,
                         feedback):
        input_path = Path(self.parameterAsFile(parameters, self.INPUT, context)).resolve()
        method_idx = self.parameterAsEnum(parameters, self.METHOD, context)
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context).authid()
        output_path = Path(self.parameterAsOutputLayer(parameters, self.OUTPUT, context)).resolve()

        if not input_path.exists():
            raise QgsProcessingException(f"Input path does not exist: {input_path}")

        method_map = [
            WarpMethod.TPS,
            WarpMethod.POLYNOMIAL_1,
            WarpMethod.POLYNOMIAL_2,
            WarpMethod.POLYNOMIAL_3,
        ]
        selected_method = method_map[method_idx]

        warp_config = WarpConfig(
            method=selected_method,
            dst_crs=target_crs,
        )

        band_names = None
        if input_path.is_dir() and is_biomass_folder(input_path):
            feedback.pushInfo(f"Parsing BIOMASS product folder: {input_path.name}")
            product = read_biomass_product(input_path)
            src_file = product.abs_file or product.vrt_file
            band_names = product.polarisations
            if not src_file or not src_file.is_file():
                raise QgsProcessingException(f"No valid measurement raster found in {input_path}")
        elif input_path.is_file():
            src_file = input_path
        else:
            raise QgsProcessingException(f"Invalid input: {input_path}")

        feedback.pushInfo(f"Warping {src_file.name} -> {output_path.name} using {selected_method.value}...")

        try:
            out = warp_raster(
                input_path=src_file,
                output_path=output_path,
                config=warp_config,
                band_names=band_names,
            )
            feedback.pushInfo(f"Successfully generated warped output: {out}")
        except Exception as err:
            raise QgsProcessingException(f"Warping failed: {err}") from err

        return {self.OUTPUT: str(output_path)}
