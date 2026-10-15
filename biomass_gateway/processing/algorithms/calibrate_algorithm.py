"""
QGIS Processing algorithm for radiometric calibration of BIOMASS products to Gamma Nought (dB).
"""

from pathlib import Path

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFile,
    QgsProcessingParameterRasterDestination,
)

from ...core.exceptions import CalibrationError
from ...core.models import CalibrationConfig
from ...reader.folder_reader import is_biomass_folder, read_biomass_product
from ..calibration.calibrator import calibrate_to_gamma0


class CalibrateGamma0Algorithm(QgsProcessingAlgorithm):
    """Processing algorithm to radiometrically calibrate ESA BIOMASS products to Gamma0 (dB).
    """

    INPUT = "INPUT"
    ANNOTATION_XML = "ANNOTATION_XML"
    LUT_NC = "LUT_NC"
    OUTPUT = "OUTPUT"

    def createInstance(self):
        return CalibrateGamma0Algorithm()

    def name(self) -> str:
        return "calibrate_gamma0"

    def displayName(self) -> str:
        return "Calibrate to Gamma0 (dB)"

    def group(self) -> str:
        return "Calibration"

    def groupId(self) -> str:
        return "calibration"

    def shortHelpString(self) -> str:
        return (
            "Radiometrically calibrates ESA BIOMASS L1 products (L1A/SCS, L1B/DGM) "
            "to Gamma Nought in Decibels (dB).\n\n"
            "Calculates beta0 = amplitude^2, then evaluates the NetCDF gammaNought LUT "
            "using bilinear spline interpolation in radar geometry. Values are converted "
            "to dB via 10*log10(gamma0), with non-positive / invalid pixels mapped to NoData.\n\n"
            "Outputs a multi-band GeoTIFF in radar geometry"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT,
                "Input BIOMASS product folder (or measurement TIFF)",
                behavior=QgsProcessingParameterFile.Folder,
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.ANNOTATION_XML,
                "Annotation XML file",
                behavior=QgsProcessingParameterFile.File,
                extension="xml",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.LUT_NC,
                "Radiometric LUT NetCDF file",
                behavior=QgsProcessingParameterFile.File,
                extension="nc",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
                "Output calibrated Gamma0 GeoTIFF (dB)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        input_str = self.parameterAsFile(parameters, self.INPUT, context)
        if not input_str:
            raise QgsProcessingException("Input path must be specified.")

        input_path = Path(input_str).resolve()
        output_path = Path(self.parameterAsOutputLayer(parameters, self.OUTPUT, context)).resolve()

        if not input_path.exists():
            raise QgsProcessingException(f"Input path does not exist: {input_path}")

        annot_override = self.parameterAsFile(parameters, self.ANNOTATION_XML, context)
        lut_override = self.parameterAsFile(parameters, self.LUT_NC, context)

        annot_path = Path(annot_override).resolve() if annot_override else None
        lut_path = Path(lut_override).resolve() if lut_override else None
        src_file = None

        if input_path.is_dir() and is_biomass_folder(input_path):
            feedback.pushInfo(f"Parsing BIOMASS product folder: {input_path.name}")
            product = read_biomass_product(input_path)
            src_file = product.abs_file or product.vrt_file
            if not annot_path:
                annot_path = product.annotation_file
            if not lut_path:
                lut_path = product.lut_file
        elif input_path.is_file():
            src_file = input_path
        else:
            raise QgsProcessingException(f"Invalid input: {input_path}")

        if not src_file or not src_file.is_file():
            raise QgsProcessingException(f"Measurement amplitude raster not found for {input_path}")
        if not annot_path or not annot_path.is_file():
            raise QgsProcessingException("Annotation XML file not found or specified.")
        if not lut_path or not lut_path.is_file():
            raise QgsProcessingException("Radiometric LUT NetCDF file not found or specified.")

        feedback.pushInfo(f"Input Measurement : {src_file.name}")
        feedback.pushInfo(f"LUT NetCDF        : {lut_path.name}")
        feedback.pushInfo(f"Annotation XML    : {annot_path.name}")
        feedback.pushInfo(f"Output GeoTIFF    : {output_path.name}")


        def _progress_cb(fraction: float, message: str):
            if feedback.isCanceled():
                raise QgsProcessingException("Calibration canceled by user.")
            feedback.setProgress(int(fraction * 100))
            if message:
                feedback.pushInfo(message)

        try:
            calibrate_to_gamma0(
                input_raster_path=src_file,
                lut_nc_path=lut_path,
                annot_xml_path=annot_path,
                output_path=output_path,
                config=CalibrationConfig(),
                progress_callback=_progress_cb,
            )
        except CalibrationError as err:
            raise QgsProcessingException(f"Calibration failed: {err}") from err
        except Exception as err:
            raise QgsProcessingException(f"Unexpected error during calibration: {err}") from err

        return {self.OUTPUT: str(output_path)}
