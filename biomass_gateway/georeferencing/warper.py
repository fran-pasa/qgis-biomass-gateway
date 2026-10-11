"""
GDAL-based georeferencing and Warped VRT generation for BIOMASS products
"""

from pathlib import Path
import tempfile

from osgeo import gdal

from ..core.exceptions import WarpError
from ..core.logger import get_logger, log_qgis_info
from ..core.models import BiomassProduct, WarpConfig, WarpMethod

logger = get_logger("warper")

# translation dict to allow the code to use friendly stringnames
# instead of GDAL specific constants
RESAMPLE_MAP: dict[str, int] = {
    "near": gdal.GRA_NearestNeighbour,
    "nearest": gdal.GRA_NearestNeighbour,
    "bilinear": gdal.GRA_Bilinear,
    "cubic": gdal.GRA_Cubic,
    "cubicspline": gdal.GRA_CubicSpline,
    "lanczos": gdal.GRA_Lanczos,
    "average": gdal.GRA_Average,
}


def _resolve_resample_alg(resample_alg: str | int) -> int:
    """Resolve resampling algorithm name to a GDAL constant
    """
    if isinstance(resample_alg, int):
        return resample_alg
    return RESAMPLE_MAP.get(str(resample_alg).lower(), gdal.GRA_Bilinear)


def _build_warp_options(config: WarpConfig) -> gdal.WarpOptions:
    """Build gdal.WarpOptions from a WarpConfig instance
    """
    resample_alg = _resolve_resample_alg(config.resample_alg)

    options_kwargs: dict = {
        "format": "VRT",
        "srcSRS": config.src_crs,
        "dstSRS": config.dst_crs,
        "dstNodata": config.nodata,
        "resampleAlg": resample_alg,
    }

    # default to TPS if method is unrecognized
    if config.method == WarpMethod.TPS:
        options_kwargs["tps"] = True
    elif config.method == WarpMethod.POLYNOMIAL_1:
        options_kwargs["polynomialOrder"] = 1
    elif config.method == WarpMethod.POLYNOMIAL_2:
        options_kwargs["polynomialOrder"] = 2
    elif config.method == WarpMethod.POLYNOMIAL_3:
        options_kwargs["polynomialOrder"] = 3
    else:
        options_kwargs["tps"] = True

    return gdal.WarpOptions(**options_kwargs)


def get_default_cache_dir() -> Path:
    """Return the temporary cache dir for warped VRTs
    """
    # create a biomass_gateway dedicated dir into OS temp dir
    cache_dir = Path(tempfile.gettempdir()) / "biomass_gateway"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def warp_raster(
        input_path: Path | str,
        output_path: Path | str,
        config: WarpConfig | None = None,
        band_names: list[str] | None = None,
) -> Path:
    """Core warp logic - warp a raster with GCPs into a Warped VRT using GDAL.

    Args:
        input_path: path to the input raster file (TIFF or VRT with GCPs).
        output_path: path where the output VRT will be written.
        config: warp config options. Defaults to TPS with EPSG:4979 -> EPSG:4326.
        band_names: optional list of band descriptions (e.g. ['HH', 'HV', 'VH', 'VV']).

    Returns:
        Path to the created warped VRT.

    Raises:
        WarpError: If warping fails or input file does not exist.
    """
    # verify that the input file actually exists first
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()

    if not input_path.is_file():
        raise WarpError(f"Input raster file does not exist: {input_path}")

    # ensure config and output folder exist
    config = config or WarpConfig()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # tell GDAL to throw Python exceptions if something goes wrong
    # this makes debugging easier cause GDAL fails silently by default
    gdal.UseExceptions()

    try:
        # build warp options and execute warp
        warp_opts = _build_warp_options(config)
        ds = gdal.Warp(str(output_path), str(input_path), options=warp_opts)
        if ds is None:
            # process failed
            raise WarpError(f"GDAL Warp returned None for {input_path}")

        # set band names if they are provided
        if band_names:
            for idx, name in enumerate(band_names, start=1):
                if idx <= ds.RasterCount:
                    band = ds.GetRasterBand(idx)
                    band.SetDescription(name)

        # save and close the file
        ds = None

        log_qgis_info(f"Warped VRT created: {output_path.name}")
        return output_path

    except Exception as err:
        if output_path.is_file():
            try:
                output_path.unlink()
            except OSError:
                pass
        raise WarpError(f"Failed to warp {input_path.name}: {err}") from err


def create_warped_vrt(
        product: BiomassProduct,
        config: WarpConfig | None = None,
        measurement_type: str = "abs",
        output_path: Path | str | None = None,
) -> Path:
    """Create a Warped VRT for a Biomass product.

    Select the source measurement file (amplitude, phase, or complex VRT),
    applies Thin-Plate Spline (TPS) georeferencing against product GCPs, and
    write a VRT.

    Args:
        product: BiomassProduct model instance.
        config: warp config options.
        measurement_type: type of measurement ('abs', 'phase', or 'complex').
        output_path: optional explicit output path. Defaults to system temp cache.

    Returns:
        Path to the generated Warped VRT file.

    Raises:
        WarpError: if required measurement file is missing or warping fails.
    """
    if measurement_type == "phase":
        src_file = product.phase_file
    elif measurement_type in ("complex", "vrt"):
        src_file = product.vrt_file
    else:
        src_file = product.abs_file or product.vrt_file

    if not src_file or not src_file.is_file():
        raise WarpError(
            f"Measurement file for '{measurement_type}' not found in product {product.product_id}"
        )

    if output_path is None:
        cache_dir = get_default_cache_dir()
        output_name = f"{product.product_id}_{measurement_type}_warped.vrt"
        output_path = cache_dir / output_name

    return warp_raster(
        input_path=src_file,
        output_path=output_path,
        config=config,
        band_names=product.polarisations,
    )
