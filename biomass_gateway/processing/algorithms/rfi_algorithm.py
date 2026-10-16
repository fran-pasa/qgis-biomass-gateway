"""
QGIS Processing algorithm for Radio Frequency Interference (RFI) mitigation on BIOMASS SLC products.

Uses the Lambda-1 detector + Block Subspace Filter (BSF) (IEEE TGRS 2024).
Algorithm logic extracted from: https://github.com/huizhangyang/lambda1-bsf/tree/main
"""

from pathlib import Path

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
)

from ...core.exceptions import RfiMitigationError
from ...reader.folder_reader import is_biomass_folder, read_biomass_product
from ..rfi.detector import mitigate_rfi_slc


class RfiMitigationAlgorithm(QgsProcessingAlgorithm):
    """
    QGIS Processing algorithm for Radio Frequency Interference (RFI) mitigation on BIOMASS SLC products.
    """

    INPUT = "INPUT"
    AMPLITUDE_RASTER = "AMPLITUDE_RASTER"
    PHASE_RASTER = "PHASE_RASTER"
    ANNOTATION_XML = "ANNOTATION_XML"
    DOMAIN = "DOMAIN"

    OUTPUT_AMPLITUDE = "OUTPUT_AMPLITUDE"
    OUTPUT_MASK = "OUTPUT_MASK"
    OUTPUT_PHASE = "OUTPUT_PHASE"

    GAMMA = "GAMMA"
    NS_MAX = "NS_MAX"
    NS_MUL = "NS_MUL"
    NORM = "NORM"
    BLOCK_AZIMUTH = "BLOCK_AZIMUTH"
    BLOCK_RANGE = "BLOCK_RANGE"
    PAD_AZIMUTH = "PAD_AZIMUTH"
    PAD_RANGE = "PAD_RANGE"

    DOMAIN_CHOICES = ["image (Slant-range SLC blocks)", "freq (Range-frequency spectrum blocks)"]

    def createInstance(self):
        return RfiMitigationAlgorithm()

    def name(self) -> str:
        return "rfi_mitigation_lambda1_bsf"

    def displayName(self) -> str:
        return "RFI mitigation"

    def group(self) -> str:
        return "Preprocessing"

    def groupId(self) -> str:
        return "preprocessing"

    def shortHelpString(self) -> str:
        return (
            "Mitigates Radio Frequency Interference (RFI) on ESA BIOMASS Single-Look Complex (SLC/L1A) products "
            "using the Lambda-1 Detector and Block Subspace Filter (BSF) (IEEE TGRS 2024).\n\n"
            "Input Options:\n"
            "• BIOMASS L1A Product Folder: select the parent BIO_S1_SCS... product directory. Amplitude (*_i_abs.tiff), "
            "phase (*_i_phase.tiff), and annotation XML metadata are automatically discovered.\n"
            "• Manual overrides: explicitly select an open raster layer or browse for amplitude, phase, and annotation files.\n\n"
            "Processing Domains:\n"
            "• image: Slices patches of the complex SLC directly. Highly effective for broadband and pulsed RFI.\n"
            "• freq: Transforms range lines to the frequency domain and whitens by the inverse Hamming chirp window. "
            "Ideal for narrowband and persistent radio interference.\n\n"
            "Outputs:\n"
            "• Cleaned Amplitude: Multi-band GeoTIFF in radar geometry (ready for radiometric calibration or warping).\n"
            "• RFI Mask: Single-band Byte GeoTIFF flagging pixels where interference was detected and filtered (1=filtered, 0=clean).\n"
            "• Cleaned Phase (Optional): Multi-band Float32 GeoTIFF preserving cleaned complex phase in radians."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT,
                "Input BIOMASS L1A product folder",
                behavior=QgsProcessingParameterFile.Folder,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.AMPLITUDE_RASTER,
                "Amplitude raster layer (*_i_abs.tiff)",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.PHASE_RASTER,
                "Phase raster layer (*_i_phase.tiff)",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.ANNOTATION_XML,
                "Annotation XML metadata (*_annot.xml)",
                behavior=QgsProcessingParameterFile.File,
                extension="xml",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.DOMAIN,
                "Processing domain",
                options=self.DOMAIN_CHOICES,
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_AMPLITUDE,
                "Output cleaned amplitude GeoTIFF",
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_MASK,
                "Output RFI detection mask GeoTIFF (Byte)",
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_PHASE,
                "Output cleaned phase GeoTIFF (optional)",
                optional=True,
            )
        )

        # advanced parameters
        param_gamma = QgsProcessingParameterNumber(
            self.GAMMA,
            "Detection threshold factor (gamma)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1.0,
            minValue=0.1,
            maxValue=20.0,
        )
        param_gamma.setFlags(param_gamma.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param_gamma)

        param_ns_max = QgsProcessingParameterNumber(
            self.NS_MAX,
            "Maximum subspace dimension (ns_max)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=20,
            minValue=1,
            maxValue=100,
        )
        param_ns_max.setFlags(param_ns_max.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param_ns_max)

        param_ns_mul = QgsProcessingParameterNumber(
            self.NS_MUL,
            "Subspace dimension multiplier (ns_mul)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=2.0,
            minValue=0.1,
            maxValue=10.0,
        )
        param_ns_mul.setFlags(param_ns_mul.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param_ns_mul)

        param_norm = QgsProcessingParameterBoolean(
            self.NORM,
            "Normalize polarizations to unit RMS before joint SVD",
            defaultValue=True,
        )
        param_norm.setFlags(param_norm.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param_norm)

        param_blk_az = QgsProcessingParameterNumber(
            self.BLOCK_AZIMUTH,
            "Block size - Azimuth lines (0 = auto for domain)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=0,
            minValue=0,
        )
        param_blk_az.setFlags(param_blk_az.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param_blk_az)

        param_blk_rg = QgsProcessingParameterNumber(
            self.BLOCK_RANGE,
            "Block size - Range samples (0 = auto for domain)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=0,
            minValue=0,
        )
        param_blk_rg.setFlags(param_blk_rg.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param_blk_rg)

        param_pad_az = QgsProcessingParameterNumber(
            self.PAD_AZIMUTH,
            "Padding/overlap - Azimuth lines (-1 = auto for domain)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=-1,
            minValue=-1,
        )
        param_pad_az.setFlags(param_pad_az.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param_pad_az)

        param_pad_rg = QgsProcessingParameterNumber(
            self.PAD_RANGE,
            "Padding/overlap - Range samples (-1 = auto for domain)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=-1,
            minValue=-1,
        )
        param_pad_rg.setFlags(param_pad_rg.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param_pad_rg)

    def processAlgorithm(self, parameters, context, feedback):
        folder_str = self.parameterAsFile(parameters, self.INPUT, context)
        amp_layer = self.parameterAsRasterLayer(parameters, self.AMPLITUDE_RASTER, context)
        pha_layer = self.parameterAsRasterLayer(parameters, self.PHASE_RASTER, context)
        annot_str = self.parameterAsFile(parameters, self.ANNOTATION_XML, context)

        amp_path: Path | None = None
        pha_path: Path | None = None
        annot_path: Path | None = None

        if folder_str:
            folder_path = Path(folder_str).resolve()
            if folder_path.is_dir() and is_biomass_folder(folder_path):
                feedback.pushInfo(f"Parsing BIOMASS product folder: {folder_path.name}")
                product = read_biomass_product(folder_path)
                amp_path = product.abs_file
                pha_path = product.phase_file
                annot_path = product.annotation_file
            elif folder_path.is_dir():
                # directory fallback
                tiffs = sorted(folder_path.glob("*.tif*")) + sorted(folder_path.glob("*/*.tif*"))
                abs_cand = [f for f in tiffs if "_abs" in f.stem.lower()]
                pha_cand = [f for f in tiffs if "_phase" in f.stem.lower()]
                xmls = sorted(folder_path.glob("*.xml")) + sorted(folder_path.glob("*/*.xml"))
                if abs_cand:
                    amp_path = abs_cand[0]
                if pha_cand:
                    pha_path = pha_cand[0]
                if xmls:
                    annot_path = xmls[0]

        # manual overrides have higher priority
        if amp_layer:
            amp_path = Path(amp_layer.source()).resolve()
        if pha_layer:
            pha_path = Path(pha_layer.source()).resolve()
        if annot_str:
            annot_path = Path(annot_str).resolve()

        if not amp_path or not amp_path.is_file():
            raise QgsProcessingException(
                "Amplitude raster (*_i_abs.tiff) not found. Specify a BIOMASS product folder or an amplitude layer."
            )

        if not pha_path or not pha_path.is_file():
            raise QgsProcessingException(
                "Phase raster (*_i_phase.tiff) not found. "
                "Lambda-1 BSF strictly requires complex SLC data (both amplitude and phase)."
            )

        if not annot_path or not annot_path.is_file():
            raise QgsProcessingException(
                "Annotation XML file not found. "
                "Required to extract radar bandwidth parameters (ups1, ups2, bdr1, bdr2)."
            )

        output_amp = Path(self.parameterAsOutputLayer(parameters, self.OUTPUT_AMPLITUDE, context)).resolve()
        output_mask = Path(self.parameterAsOutputLayer(parameters, self.OUTPUT_MASK, context)).resolve()

        output_pha_str = self.parameterAsOutputLayer(parameters, self.OUTPUT_PHASE, context)
        output_pha = Path(output_pha_str).resolve() if output_pha_str else None

        domain_idx = self.parameterAsEnum(parameters, self.DOMAIN, context)
        domain = "freq" if domain_idx == 1 else "image"

        gamma = self.parameterAsDouble(parameters, self.GAMMA, context)
        ns_max = self.parameterAsInt(parameters, self.NS_MAX, context)
        ns_mul = self.parameterAsDouble(parameters, self.NS_MUL, context)
        norm = self.parameterAsBoolean(parameters, self.NORM, context)

        blk_az = self.parameterAsInt(parameters, self.BLOCK_AZIMUTH, context)
        blk_rg = self.parameterAsInt(parameters, self.BLOCK_RANGE, context)
        pad_az = self.parameterAsInt(parameters, self.PAD_AZIMUTH, context)
        pad_rg = self.parameterAsInt(parameters, self.PAD_RANGE, context)

        lblk = (blk_az, blk_rg) if (blk_az > 0 and blk_rg > 0) else None
        lpd = (pad_az, pad_rg) if (pad_az >= 0 and pad_rg >= 0) else None

        feedback.pushInfo(f"Input amplitude : {amp_path.name}")
        feedback.pushInfo(f"Input phase     : {pha_path.name}")
        feedback.pushInfo(f"Annotation XML  : {annot_path.name}")
        feedback.pushInfo(f"Domain          : {domain}")
        feedback.pushInfo(f"Output amplitude: {output_amp.name}")
        feedback.pushInfo(f"Output mask     : {output_mask.name}")
        if output_pha:
            feedback.pushInfo(f"Output phase    : {output_pha.name}")

        def _progress_cb(fraction: float, message: str):
            if feedback.isCanceled():
                raise QgsProcessingException("RFI mitigation canceled by user.")
            feedback.setProgress(int(fraction * 100))
            if message:
                feedback.pushInfo(message)

        try:
            stats = mitigate_rfi_slc(
                amplitude_raster_path=amp_path,
                phase_raster_path=pha_path,
                annot_xml_path=annot_path,
                output_amplitude_path=output_amp,
                output_mask_path=output_mask,
                output_phase_path=output_pha,
                domain=domain,
                gamma=gamma,
                ns_max=ns_max,
                ns_mul=ns_mul,
                norm=norm,
                lblk=lblk,
                lpd=lpd,
                progress_callback=_progress_cb,
            )
        except RfiMitigationError as err:
            raise QgsProcessingException(f"RFI mitigation failed: {err}") from err
        except Exception as err:
            raise QgsProcessingException(f"Unexpected error during RFI mitigation: {err}") from err

        results = {
            self.OUTPUT_AMPLITUDE: str(output_amp),
            self.OUTPUT_MASK: str(output_mask),
        }
        if output_pha:
            results[self.OUTPUT_PHASE] = str(output_pha)

        feedback.pushInfo(
            f"Execution finished: {stats['flagged_ratio']:.2%} of area flagged for RFI "
            f"({stats['num_rfi_blocks']} blocks filtered)."
        )
        return results
