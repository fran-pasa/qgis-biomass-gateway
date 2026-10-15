"""
QGIS Processing Algorithm for mosaicking ESA BIOMASS rasters.
"""

from __future__ import annotations

from pathlib import Path

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterEnum,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterRasterDestination,
)

from ..mosaicker import build_vrt_mosaic


class ParameterVrtDestination(QgsProcessingParameterRasterDestination):
    """Destination parameter configured for VRT files
    """

    def clone(self) -> ParameterVrtDestination:
        c = ParameterVrtDestination(
            self.name(),
            self.description(),
            self.defaultValue(),
            optional=bool(self.flags() & self.FlagOptional),
        )
        c.setFlags(self.flags())
        return c

    def defaultFileExtension(self) -> str:
        return "vrt"

    def createFileFilter(self) -> str:
        return "VRT files (*.vrt *.VRT)"

    def supportedOutputRasterLayerExtensions(self) -> list[str]:
        return ["vrt"]


class MosaicBiomassAlgorithm(QgsProcessingAlgorithm):
    """QGIS Processing Algorithm to mosaic BIOMASS rasters into a VRT."""

    INPUT_LAYERS = "INPUT_LAYERS"
    RESAMPLING = "RESAMPLING"
    OUTPUT = "OUTPUT"

    def createInstance(self) -> MosaicBiomassAlgorithm:
        return MosaicBiomassAlgorithm()

    def name(self) -> str:
        return "mosaic_biomass"

    def displayName(self) -> str:
        return "Mosaic BIOMASS rasters"

    def group(self) -> str:
        return "Mosaicking"

    def groupId(self) -> str:
        return "mosaicking"

    def shortHelpString(self) -> str:
        return (
            "Builds a multi-band GDAL Virtual Raster (VRT) mosaic from already-georeferenced "
            "BIOMASS raster layers or files.\n\n"
            "Preserves all 4 radar polarization bands (HH, HV, VH, VV) "
            "and correctly masks out rotated nodata borders so overlapping swaths "
            "don't overwrite valid data."
        )

    def initAlgorithm(self, config=None) -> None:
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                "Input georeferenced raster layers (or files)",
                layerType=QgsProcessing.TypeRaster,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.RESAMPLING,
                "Resampling method",
                options=["Bilinear", "Nearest Neighbour", "Cubic"],
                defaultValue=0,
            )
        )

        self.addParameter(
            ParameterVrtDestination(
                self.OUTPUT,
                "Output Mosaic VRT",
            )
        )

    def processAlgorithm(self, parameters, context, feedback) -> dict[str, str]:
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        if not layers or len(layers) < 2:
            raise QgsProcessingException(
                "Please select at least 2 raster layers to build a mosaic."
            )

        raster_paths: list[str] = []
        for layer in layers:
            src = layer.source()
            if not src:
                raise QgsProcessingException(
                    f"Layer '{layer.name()}' does not have a valid file source."
                )
            clean_src = src.split("|")[0]
            raster_paths.append(clean_src)
            feedback.pushInfo(f"  ✓ Input layer: {layer.name()} -> {clean_src}")

        resampling_idx = self.parameterAsEnum(parameters, self.RESAMPLING, context)
        resampling_map = {0: "bilinear", 1: "nearest", 2: "cubic"}
        resample_alg = resampling_map.get(resampling_idx, "bilinear")

        output_path_str = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        output_path = Path(output_path_str).resolve()

        if output_path.suffix.lower() != ".vrt":
            raise QgsProcessingException(
                f"Invalid output extension '{output_path.suffix}'. "
                "The mosaicker generates GDAL Virtual Rasters and requires a '.vrt' extension."
            )

        feedback.pushInfo(f"Building VRT mosaic with {len(raster_paths)} layers...")

        def _gdal_progress(complete: float, msg: str, *args) -> int:
            if feedback.isCanceled():
                return 0
            feedback.setProgress(int(complete * 100))
            return 1

        try:
            build_vrt_mosaic(
                raster_paths=raster_paths,
                output_vrt_path=output_path,
                resample_alg=resample_alg,
                progress_callback=_gdal_progress,
            )
            feedback.pushInfo(f"Successfully generated mosaic VRT: {output_path}")
        except Exception as err:
            raise QgsProcessingException(f"Mosaicking failed: {err}") from err

        return {self.OUTPUT: str(output_path)}
