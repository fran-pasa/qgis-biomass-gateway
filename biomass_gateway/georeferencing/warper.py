"""
GDAL-based georeferencing and Warped VRT generation for BIOMASS products
"""

from pathlib import Path
import tempfile

from osgeo import gdal

from ..core.exceptions import WarpError
from ..core.logger import get_logger, log_qgis_info
from ..core.models import BiomassProduct, WarpConfig, WarpMethod

from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET
from osgeo import gdal, osr

try:
    from qgis.PyQt.QtWidgets import QApplication
except ImportError:
    QApplication = None

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


def _build_warp_options(
    config: WarpConfig,
    output_format: str = "VRT",
    callback=None,
) -> gdal.WarpOptions:
    """Build gdal.WarpOptions from a WarpConfig instance
    """
    resample_alg = _resolve_resample_alg(config.resample_alg)

    options_kwargs: dict = {
        "format": output_format,
        "dstSRS": config.dst_crs,
        "srcNodata": config.nodata,
        "dstNodata": config.nodata,
        "resampleAlg": resample_alg,
        "multithread": True,
        "warpOptions": ["NUM_THREADS=ALL_CPUS"],
    }

    if callback is not None:
        options_kwargs["callback"] = callback

    if output_format == "GTiff":
        options_kwargs["creationOptions"] = [
            "TILED=YES",
            "COMPRESS=DEFLATE",
            "BIGTIFF=IF_SAFER",
            "NUM_THREADS=ALL_CPUS",
        ]

    # default to TPS if method is unrecognized
    if config.method == WarpMethod.GEOLOCATION_ARRAY:
        options_kwargs["geoloc"] = True
    elif config.method == WarpMethod.TPS:
        options_kwargs["srcSRS"] = config.src_crs
        options_kwargs["tps"] = True
    elif config.method == WarpMethod.POLYNOMIAL_1:
        options_kwargs["srcSRS"] = config.src_crs
        options_kwargs["polynomialOrder"] = 1
    elif config.method == WarpMethod.POLYNOMIAL_2:
        options_kwargs["srcSRS"] = config.src_crs
        options_kwargs["polynomialOrder"] = 2
    elif config.method == WarpMethod.POLYNOMIAL_3:
        options_kwargs["srcSRS"] = config.src_crs
        options_kwargs["polynomialOrder"] = 3
    else:
        options_kwargs["tps"] = True

    return gdal.WarpOptions(**options_kwargs)


def build_geolocation_vrt(
    src_raster_path: Path,
    lut_nc_path: Path,
    output_vrt_path: Path,
    grid_shape: tuple[int, int] | None = None,
    annotation_xml_path: Path | None = None,
    band_names: list[str] | None = None,
    srs: str = "EPSG:4326",
) -> Path:
    """Create an intermediate VRT file containing the GDAL GEOLOCATION
    metadata domain.
    """
    gdal.UseExceptions()
    ds = gdal.Open(str(src_raster_path), gdal.GA_ReadOnly)
    if ds is None:
        raise WarpError(f"Cannot open source raster: {src_raster_path}")

    # convert srs identifier into WKT as required by GDAL GEOLOCATION
    spatial_ref = osr.SpatialReference()
    spatial_ref.SetFromUserInput(srs)
    srs_wkt = escape(spatial_ref.ExportToWkt())

    raster_width = ds.RasterXSize
    raster_height = ds.RasterYSize
    band_count = ds.RasterCount
    first_band = ds.GetRasterBand(1)
    data_type_name = gdal.GetDataTypeName(first_band.DataType)

    lut_str = str(lut_nc_path.resolve())
    x_dataset = f'NETCDF:"{lut_str}":/geometry/longitude'
    y_dataset = f'NETCDF:"{lut_str}":/geometry/latitude'

    # compute exact PIXEL / LINE offsets and steps from annotation & LUT time axes
    ds_az = gdal.Open(f'NETCDF:"{lut_str}":relativeAzimuthTimeRGC')
    ds_rg = gdal.Open(f'NETCDF:"{lut_str}":slantRangeTimeRGC')
    az_lut = ds_az.ReadAsArray().flatten() if ds_az else None
    rg_lut = ds_rg.ReadAsArray().flatten() if ds_rg else None
    ds_az = None
    ds_rg = None

    if annotation_xml_path and annotation_xml_path.is_file() and az_lut is not None and rg_lut is not None:
        try:
            tree = ET.parse(annotation_xml_path)
            sar_elem = tree.getroot().find("sarImage")
            if sar_elem is not None:
                r_start_txt = sar_elem.findtext("firstSampleSlantRangeTime")
                r_dt_txt = sar_elem.findtext("rangeTimeInterval")
                az_dt_txt = sar_elem.findtext("azimuthTimeInterval")
                if r_start_txt and r_dt_txt and az_dt_txt:
                    r_start = float(r_start_txt)
                    r_dt = float(r_dt_txt)
                    az_dt = float(az_dt_txt)

                    pixel_offset = (rg_lut[0] - r_start) / r_dt
                    pixel_step = (rg_lut[1] - rg_lut[0]) / r_dt

                    # Negative LINE_STEP and reversed offset to counter GDAL's NetCDF bottom-up reading
                    line_offset = az_lut[-1] / az_dt
                    line_step = - (az_lut[1] - az_lut[0]) / az_dt
                else:
                    raise WarpError(
                        f"Missing timing tags (firstSampleSlantRangeTime, rangeTimeInterval, azimuthTimeInterval) in {annotation_xml_path}"
                    )
            else:
                raise WarpError(f"<sarImage> element not found in {annotation_xml_path}")
        except Exception as err:
            raise WarpError(f"Failed to calculate timing parameters for Geolocation grid: {err}") from err
    else:
        raise WarpError("Cannot build Geolocation grid: annotation XML or NetCDF time axes missing")

    # build XML structure
    bands_xml = []
    for b in range(1, band_count + 1):
        band_obj = ds.GetRasterBand(b)
        nodata_val = band_obj.GetNoDataValue()
        color_interp_id = band_obj.GetColorInterpretation()
        color_interp_name = gdal.GetColorInterpretationName(color_interp_id)
        desc = band_names[b - 1] if band_names and b <= len(band_names) else f"Band {b}"

        nodata_tag = f"\n            <NoDataValue>{nodata_val}</NoDataValue>" if nodata_val is not None else ""
        color_tag = f"\n            <ColorInterp>{color_interp_name}</ColorInterp>" if color_interp_name and color_interp_name != "Undefined" else ""
        src_nodata_tag = f"\n              <SrcNoData>{nodata_val}</SrcNoData>\n              <DstNoData>{nodata_val}</DstNoData>" if nodata_val is not None else ""

        band_xml = f"""  <VRTRasterBand dataType="{data_type_name}" band="{b}">{nodata_tag}{color_tag}
            <Description>{desc}</Description>
            <SimpleSource>
              <SourceFilename relativeToVRT="0">{src_raster_path.resolve()}</SourceFilename>
              <SourceBand>{b}</SourceBand>
              <SourceProperties RasterXSize="{raster_width}" RasterYSize="{raster_height}" DataType="{data_type_name}"/>{src_nodata_tag}
            </SimpleSource>
          </VRTRasterBand>"""
        bands_xml.append(band_xml)

    vrt_content = f"""<VRTDataset rasterXSize="{raster_width}" rasterYSize="{raster_height}">
          <Metadata domain="GEOLOCATION">
            <MDI key="SRS">{srs_wkt}</MDI>
            <MDI key="X_DATASET">{x_dataset}</MDI>
            <MDI key="X_BAND">1</MDI>
            <MDI key="Y_DATASET">{y_dataset}</MDI>
            <MDI key="Y_BAND">1</MDI>
            <MDI key="PIXEL_OFFSET">{pixel_offset}</MDI>
            <MDI key="LINE_OFFSET">{line_offset}</MDI>
            <MDI key="PIXEL_STEP">{pixel_step}</MDI>
            <MDI key="LINE_STEP">{line_step}</MDI>
          </Metadata>
        {chr(10).join(bands_xml)}
        </VRTDataset>
        """

    output_vrt_path.parent.mkdir(parents=True, exist_ok=True)
    output_vrt_path.write_text(vrt_content, encoding="utf-8")
    ds = None
    return output_vrt_path


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
        progress_callback=None,
) -> Path:
    """Core warp logic - warp a raster with GCPs into a Warped VRT using GDAL.

    Args:
        input_path: path to the input raster file (TIFF or VRT with GCPs).
        output_path: path where the output VRT will be written.
        config: warp config options. Defaults to TPS with EPSG:4979 -> EPSG:4326.
        band_names: optional list of band descriptions (e.g. ['HH', 'HV', 'VH', 'VV']).
        progress_callback: optional callback receiving (float, str) for progress reporting.

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
    if output_path.is_file():
        try:
            output_path.unlink()
        except OSError:
            pass

    # tell GDAL to throw Python exceptions if something goes wrong
    # this makes debugging easier cause GDAL fails silently by default
    gdal.UseExceptions()

    try:
        output_format = "GTiff" if output_path.suffix.lower() in (".tif", ".tiff") else "VRT"

        def _warp_cb(complete, *args, **kwargs):
            try:
                if QApplication and QApplication.instance():
                    QApplication.processEvents()
                if progress_callback:
                    msg = args[0] if args else ""
                    progress_callback(complete * 0.8, msg)
            except Exception:
                pass
            return 1

        # build warp options and execute warp
        warp_opts = _build_warp_options(config, output_format=output_format, callback=_warp_cb)
        ds = gdal.Warp(str(output_path), str(input_path), options=warp_opts)
        if ds is None:
            # process failed
            raise WarpError(f"GDAL Warp returned None for {input_path}")

        # set band names and nodata if they are provided
        for idx in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(idx)
            if config.nodata is not None:
                band.SetNoDataValue(config.nodata)
            if band_names and idx <= len(band_names):
                band.SetDescription(band_names[idx - 1])

        if output_format == "GTiff":
            try:
                ds.FlushCache()
                if QApplication and QApplication.instance():
                    QApplication.processEvents()

                def _ov_cb(complete, *args, **kwargs):
                    try:
                        if QApplication and QApplication.instance():
                            QApplication.processEvents()
                        if progress_callback:
                            msg = args[0] if args else ""
                            progress_callback(0.8 + complete * 0.2, msg)
                    except Exception:
                        pass
                    return 1

                try:
                    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16], _ov_cb)
                except TypeError:
                    try:
                        ds.BuildOverviews("AVERAGE", [2, 4, 8, 16], callback=_ov_cb)
                    except TypeError:
                        ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
            except Exception as e:
                logger.debug("Failed to build overviews: %s", e)

        # save and close the file
        ds = None

        log_qgis_info(f"Warped raster created: {output_path.name}")
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
        progress_callback=None,
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
        progress_callback: optional callback receiving (float, str) for progress reporting.

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

    cache_dir = get_default_cache_dir()
    config = config or WarpConfig()

    # determine whether to use the geolocation LUT or fallback to TPS
    use_lut = (
        config.method == WarpMethod.GEOLOCATION_ARRAY
        and product.has_lut
        and product.lut_grid_shape is not None
    )

    if use_lut and product.lut_file:
        try:
            log_qgis_info(f"Using Geolocation grid for {product.product_id}")
            # create geolocation VRT
            geoloc_vrt = cache_dir / f"{product.product_id}_{measurement_type}.vrt"
            build_geolocation_vrt(
                src_raster_path=src_file,
                lut_nc_path=product.lut_file,
                output_vrt_path=geoloc_vrt,
                grid_shape=product.lut_grid_shape,
                annotation_xml_path=product.annotation_file,
                band_names=product.polarisations,
            )
            warp_input = geoloc_vrt # just an alias
            warp_method_used = WarpMethod.GEOLOCATION_ARRAY
        except Exception as err:
            log_qgis_warning(
                f"Geolocation grid unavailable for {product.product_id} ({err}); falling back to TPS on GCPs"
            )
            warp_input = src_file
            warp_method_used = WarpMethod.TPS
    else:
        log_qgis_info(
            f"NetCDF LUT not available for {product.product_id}; falling back to TPS on GCPs"
        )
        warp_input = src_file
        warp_method_used = WarpMethod.TPS

    active_config = WarpConfig(
        method=warp_method_used,
        src_crs=config.src_crs,
        dst_crs=config.dst_crs,
        nodata=config.nodata,
        resample_alg=config.resample_alg
    )

    if output_path is None:
        suffix = "geoloc_warped.tif" if warp_method_used == WarpMethod.GEOLOCATION_ARRAY else "tps_warped.tif"
        output_path = cache_dir / f"{product.product_id}_{measurement_type}_{suffix}"

    return warp_raster(
        input_path=warp_input,
        output_path=output_path,
        config=active_config,
        band_names=product.polarisations,
        progress_callback=progress_callback,
    )
