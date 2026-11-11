"""
QGIS Processing algorithm for Pauli and HSV polarimetric decompositions on
BIOMASS SLC products.
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

from ...core.exceptions import DecompositionError
from ...core.models import (
    DecompositionConfig,
    DecompositionScale,
    DecompositionType,
    StretchMethod,
)
from ...reader.folder_reader import is_biomass_folder, read_biomass_product
from ..polarimetry.decompositions import run_polarimetric_decomposition


class PolarimetricDecompositionAlgorithm(QgsProcessingAlgorithm):
    """QGIS Processing algorithm for Pauli and HSV polarimetric
    decompositions.
    """

    INPUT = "INPUT"
    AMPLITUDE_RASTER = "AMPLITUDE_RASTER"
    PHASE_RASTER = "PHASE_RASTER"

    REPRESENTATION = "REPRESENTATION"
    SCALE = "SCALE"
    STRETCH_METHOD = "STRETCH_METHOD"
    QUANTILE_THRESHOLD = "QUANTILE_THRESHOLD"
    CSCALE = "CSCALE"
    SMOOTH_LINES = "SMOOTH_LINES"
    SMOOTH_SAMPLES = "SMOOTH_SAMPLES"
    ADD_ALPHA = "ADD_ALPHA"

    FIX_ICAL_PHASE = "FIX_ICAL_PHASE"
    PC_HH = "PC_HH"
    PC_HV = "PC_HV"
    PC_VH = "PC_VH"
    PC_VV = "PC_VV"

    OUTPUT_RGB = "OUTPUT_RGB"
    OUTPUT_FLOAT = "OUTPUT_FLOAT"

    REPRESENTATION_CHOICES = [
        "Pauli (R: |HH-VV|/√2, G: |HV+VH|/√2, B: |HH+VV|/√2)",
        "HSV (Hue: Alpha angle via Turbo colormap, Value: Span amplitude)",
    ]
    SCALE_CHOICES = [
        "Linear Amplitude",
        "Linear Power",
        "Decibels (dB)",
    ]
    STRETCH_CHOICES = [
        "Quantile / Percentile Clipping ([q, 1 - q])",
        "Mean Multiplier ([0, cscale * mean])",
    ]


    def createInstance(self):
        return PolarimetricDecompositionAlgorithm()


    def name(self) -> str:
        return "polarimetric_decomposition"


    def displayName(self) -> str:
        return "Polarimetric decomposition (Pauli / HSV)"


    def group(self) -> str:
        return "Polarimetry"


    def groupId(self) -> str:
        return "polarimetry"


    def shortHelpString(self) -> str:
        return (
            "Generates Pauli or HSV polarimetric color composite decompositions from ESA BIOMASS "
            "Single-Look Complex (SLC / L1A / L1C) products.\n\n"
            "Representations:\n"
            "• Pauli: Maps Double-bounce |HH-VV|/√2 to Red, Volume |HV+VH|/√2 to Green, "
            "and Surface |HH+VV|/√2 to Blue.\n"
            "• HSV: Maps the polarimetric scattering angle α = arccos(p1 / Span) / (π/2) onto the "
            "'turbo' colormap (Hue) modulated by total Span amplitude Value).\n\n"
            "Outputs:\n"
            "• 8-bit RGB/RGBA Composite: Stretched 0–255 GeoTIFF ready for visualization and warping.\n"
            "• Unscaled Float32 (Optional): 3-band Float32 GeoTIFF containing physical values before 8-bit stretching."
        )


    def initAlgorithm(self, config=None):
        # all input widgets are marked optional so the user can either pass a product
        # folder or override with explicit raster layers (e.g. after RFI mitigation)
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT,
                "Input BIOMASS SLC product folder (L1A / L1C)",
                behavior=QgsProcessingParameterFile.Folder,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.AMPLITUDE_RASTER,
                "Amplitude raster layer (*_i_abs.tiff) [optional override]",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.PHASE_RASTER,
                "Phase raster layer (*_i_phase.tiff) [optional override]",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.REPRESENTATION,
                "Decomposition representation",
                options=self.REPRESENTATION_CHOICES,
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.SCALE,
                "Radiometric scale (Pauli)",
                options=self.SCALE_CHOICES,
                defaultValue=0,
            )
        )

        # default 15x3 window matches bioqlk's slant-range aspect ratio (azimuth x range)
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SMOOTH_LINES,
                "Boxcar smoothing window — Azimuth lines (1 = no smoothing)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=15,
                minValue=1,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.SMOOTH_SAMPLES,
                "Boxcar smoothing window - Range samples (1 = no smoothing)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=3,
                minValue=1,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.STRETCH_METHOD,
                "8-bit contrast stretch method (Pauli)",
                options=self.STRETCH_CHOICES,
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.QUANTILE_THRESHOLD,
                "Quantile clipping threshold q (clips [q, 1 - q], e.g. 0.01 = 1%–99%)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.01,
                minValue=0.0,
                maxValue=0.49,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.CSCALE,
                "Mean multiplier (cscale / kscale for Mean stretch & HSV Span)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=2.5,
                minValue=0.1,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADD_ALPHA,
                "Add 4th Alpha transparency band (RGBA) for NoData borders",
                defaultValue=True,
            )
        )

        # hide iCal phase calibration offsets under the collapsible Advanced section
        adv_params = [
            QgsProcessingParameterBoolean(
                self.FIX_ICAL_PHASE,
                "Apply iCal phase offset corrections",
                defaultValue=False,
            ),
            QgsProcessingParameterNumber(
                self.PC_HH,
                "HH phase offset (radians)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=-0.4145157,
            ),
            QgsProcessingParameterNumber(
                self.PC_HV,
                "HV phase offset (radians)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.98025057,
            ),
            QgsProcessingParameterNumber(
                self.PC_VH,
                "VH phase offset (radians)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=-1.56556034,
            ),
            QgsProcessingParameterNumber(
                self.PC_VV,
                "VV phase offset (radians)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
            ),
        ]
        for p in adv_params:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
            self.addParameter(p)

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_RGB,
                "Polarimetric decomposition (8-bit RGB/RGBA)",
            )
        )

        # createByDefault=False prevents QGIS from generating a temp Float32 file unless requested
        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT_FLOAT,
                "Unscaled decomposition (32-bit Float)",
                optional=True,
                createByDefault=False,
            )
        )


    def processAlgorithm(self, parameters, context, feedback):
        folder_str = self.parameterAsFile(parameters, self.INPUT, context)
        amp_layer = self.parameterAsRasterLayer(parameters, self.AMPLITUDE_RASTER, context)
        pha_layer = self.parameterAsRasterLayer(parameters, self.PHASE_RASTER, context)

        amp_path: Path | None = None
        pha_path: Path | None = None
        lut_path: Path | None = None
        annot_path: Path | None = None
        product_folder: Path | None = None

        if folder_str:
            folder_path = Path(folder_str).resolve()
            if folder_path.is_dir() and is_biomass_folder(folder_path):
                feedback.pushInfo(f"Parsing BIOMASS product folder: {folder_path.name}")

                # extract measurement paths plus LUT/annotation for downstream warping
                product = read_biomass_product(folder_path)
                amp_path = product.abs_file
                pha_path = product.phase_file
                lut_path = product.lut_file
                annot_path = product.annotation_file
                product_folder = folder_path
            elif folder_path.is_dir():
                # fallback for non-standard directories containing *_abs and *_phase tiffs
                tiffs = sorted(folder_path.glob("*.tif*")) + sorted(folder_path.glob("*/*.tif*"))
                abs_cand = [f for f in tiffs if "_abs" in f.stem.lower()]
                pha_cand = [f for f in tiffs if "_phase" in f.stem.lower()]
                if abs_cand:
                    amp_path = abs_cand[0]
                if pha_cand:
                    pha_path = pha_cand[0]
                product_folder = folder_path

        # manual layer selections override folder discovery; strip any QGIS '|layername=' suffix
        if amp_layer:
            amp_path = Path(amp_layer.source().split("|")[0]).resolve()
        if pha_layer:
            pha_path = Path(pha_layer.source().split("|")[0]).resolve()

        if not amp_path or not amp_path.is_file():
            raise QgsProcessingException(
                "Amplitude raster (*_i_abs.tiff) not found. "
                "Specify a BIOMASS SLC product folder or an amplitude raster layer."
            )
        if not pha_path or not pha_path.is_file():
            raise QgsProcessingException(
                "Phase raster (*_i_phase.tiff) not found. "
                "Pauli and HSV decompositions require complex SLC data (both amplitude and phase)."
            )

        # map integer dropdown indices from QGIS widgets to domain enums
        rep_idx = self.parameterAsEnum(parameters, self.REPRESENTATION, context)
        representation = DecompositionType.HSV if rep_idx == 1 else DecompositionType.PAULI

        scale_idx = self.parameterAsEnum(parameters, self.SCALE, context)
        scale_map = {
            0: DecompositionScale.LINEAR_AMPLITUDE,
            1: DecompositionScale.LINEAR_POWER,
            2: DecompositionScale.DB,
        }
        scale = scale_map.get(scale_idx, DecompositionScale.LINEAR_AMPLITUDE)

        stretch_idx = self.parameterAsEnum(parameters, self.STRETCH_METHOD, context)
        stretch_method = StretchMethod.MEAN if stretch_idx == 1 else StretchMethod.QUANTILE

        quantile_th = self.parameterAsDouble(parameters, self.QUANTILE_THRESHOLD, context)
        cscale = self.parameterAsDouble(parameters, self.CSCALE, context)
        smooth_lines = self.parameterAsInt(parameters, self.SMOOTH_LINES, context)
        smooth_samples = self.parameterAsInt(parameters, self.SMOOTH_SAMPLES, context)
        add_alpha = self.parameterAsBoolean(parameters, self.ADD_ALPHA, context)

        fix_ical = self.parameterAsBoolean(parameters, self.FIX_ICAL_PHASE, context)
        pc_hh = self.parameterAsDouble(parameters, self.PC_HH, context)
        pc_hv = self.parameterAsDouble(parameters, self.PC_HV, context)
        pc_vh = self.parameterAsDouble(parameters, self.PC_VH, context)
        pc_vv = self.parameterAsDouble(parameters, self.PC_VV, context)

        output_rgb = Path(self.parameterAsOutputLayer(parameters, self.OUTPUT_RGB, context)).resolve()
        output_float_str = self.parameterAsOutputLayer(parameters, self.OUTPUT_FLOAT, context)
        output_float = Path(output_float_str).resolve() if output_float_str else None

        config = DecompositionConfig(
            representation=representation,
            scale=scale,
            stretch_method=stretch_method,
            quantile_threshold=quantile_th,
            cscale=cscale,
            smooth_window=(smooth_lines, smooth_samples),
            add_alpha=add_alpha,
            fix_ical_phase=fix_ical,
            pc_hh=pc_hh,
            pc_hv=pc_hv,
            pc_vh=pc_vh,
            pc_vv=pc_vv,
        )

        feedback.pushInfo(f"Input amplitude : {amp_path.name}")
        feedback.pushInfo(f"Input phase     : {pha_path.name}")
        feedback.pushInfo(f"Representation  : {representation.value.upper()}")
        feedback.pushInfo(f"Smoothing window: {smooth_lines} x {smooth_samples}")
        feedback.pushInfo(f"Output 8-bit RGB: {output_rgb.name}")
        if output_float:
            feedback.pushInfo(f"Output Float32  : {output_float.name}")

        # bridge internal progress (0.0..1.0) to QGIS feedback bar and honor user cancellation
        def _progress_cb(fraction: float, message: str) -> None:
            if feedback.isCanceled():
                raise QgsProcessingException("Polarimetric decomposition canceled by user.")
            feedback.setProgress(int(fraction * 100))
            if message:
                feedback.pushInfo(message)

        try:
            run_polarimetric_decomposition(
                abs_raster_path=amp_path,
                phase_raster_path=pha_path,
                output_rgb_path=output_rgb,
                output_float_path=output_float,
                config=config,
                lut_file=lut_path,
                annotation_file=annot_path,
                product_folder=product_folder,
                progress_callback=_progress_cb,
            )
        except DecompositionError as err:
            raise QgsProcessingException(f"Polarimetric decomposition failed: {err}") from err

        results = {self.OUTPUT_RGB: str(output_rgb)}
        if output_float:
            results[self.OUTPUT_FLOAT] = str(output_float)
        return results

