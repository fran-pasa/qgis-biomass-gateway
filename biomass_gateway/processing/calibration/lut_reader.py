"""NetCDF radiometric LUT reader using GDAL.
"""

from pathlib import Path
import numpy as np
from osgeo import gdal

from ...core.exceptions import CalibrationError
from ...core.models import LutData


def _extract_reference_azimuth_time(ds: gdal.Dataset, lut_nc_path: Path) -> str | None:
    """Extract referenceAzimuthTime string from NetCDF global metadata or fallback to netCDF4.
    """
    ref_time = ds.GetMetadataItem("NC_GLOBAL#referenceAzimuthTime")
    if ref_time:
        return ref_time.strip()

    ref_time = ds.GetMetadataItem("referenceAzimuthTime")
    if ref_time:
        return ref_time.strip()

    meta = ds.GetMetadata() or {}
    for key, val in meta.items():
        if "referenceAzimuthTime" in key and val:
            return str(val).strip()

    # fallback to netCDF4 if available
    try:
        import netCDF4 as nc

        with nc.Dataset(str(lut_nc_path)) as nc_ds:
            if hasattr(nc_ds, "referenceAzimuthTime"):
                return str(nc_ds.getncattr("referenceAzimuthTime")).strip()
    except Exception:
        pass

    return None


def _find_subdataset_uri(subdatasets: list[tuple[str, str]], target_name: str, lut_nc_path: Path) -> str:
    """Find matching subdataset URI from GDAL subdataset list.
    """
    lut_str = str(lut_nc_path.resolve())
    for sds_name, sds_desc in subdatasets:
        if target_name in sds_name or target_name in sds_desc:
            return sds_name

    # fallback direct NetCDF specifier
    return f'NETCDF:"{lut_str}":{target_name}'


def _validate_lut_axes(gamma_lut: np.ndarray, az_axis: np.ndarray, rg_axis: np.ndarray) -> None:
    """Validate that LUT shape matches axes and axes are strictly monotonic.
    """
    expected_shape = (len(az_axis), len(rg_axis))
    if gamma_lut.shape != expected_shape:
        raise CalibrationError(
            f"LUT shape {gamma_lut.shape} does not match coordinate axes {expected_shape}."
        )

    if not np.all(np.diff(az_axis) > 0):
        raise CalibrationError("relativeAzimuthTimeRGC axis is not strictly increasing.")

    if not np.all(np.diff(rg_axis) > 0):
        raise CalibrationError("slantRangeTimeRGC axis is not strictly increasing.")


def read_lut_geometry(lut_nc_path: Path | str) -> LutData:
    """Read the Gamma Nought LUT, time axes, and reference azimuth time from NetCDF.

    Args:
        lut_nc_path: Path to the *_lut.nc NetCDF file.

    Returns:
        LutData containing gamma_lut array, az_axis, rg_axis, and reference_azimuth_time.

    Raises:
        CalibrationError: If required variables, axes, or attributes are missing or invalid.
    """
    path = Path(lut_nc_path).resolve()
    if not path.is_file():
        raise CalibrationError(f"NetCDF LUT file not found: {path}")

    gdal.UseExceptions()

    # Try netCDF4 first if available for high-fidelity attribute & group extraction,
    # otherwise use GDAL driver.
    try:
        import netCDF4 as nc

        with nc.Dataset(str(path)) as ds:
            gamma_lut = None
            if "radiometry" in ds.groups and "gammaNought" in ds.groups["radiometry"].variables:
                gamma_var = ds.groups["radiometry"].variables["gammaNought"]
                gamma_lut = np.ma.filled(gamma_var[:].astype(np.float64), np.nan)
            elif "gammaNought" in ds.variables:
                gamma_var = ds.variables["gammaNought"]
                gamma_lut = np.ma.filled(gamma_var[:].astype(np.float64), np.nan)

            if gamma_lut is not None:
                az_axis = np.asarray(ds.variables["relativeAzimuthTimeRGC"][:], dtype=np.float64)
                rg_axis = np.asarray(ds.variables["slantRangeTimeRGC"][:], dtype=np.float64)
                ref_time = str(ds.getncattr("referenceAzimuthTime")).strip()

                _validate_lut_axes(gamma_lut, az_axis, rg_axis)
                return LutData(
                    gamma_lut=gamma_lut,
                    azimuth_axis=az_axis,
                    range_axis=rg_axis,
                    reference_azimuth_time=ref_time,
                )
    except ImportError:
        pass
    except Exception:
        # Fall through to GDAL native reading
        pass

    # GDAL native reading
    root_ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if root_ds is None:
        raise CalibrationError(f"GDAL failed to open NetCDF file: {path}")

    subdatasets = root_ds.GetSubDatasets() or []
    ref_time = _extract_reference_azimuth_time(root_ds, path)
    if not ref_time:
        root_ds = None
        raise CalibrationError(f"Missing 'referenceAzimuthTime' attribute in NetCDF LUT: {path}")

    lut_str = str(path)

    # 1. Open Gamma Nought variable
    gamma_sds_name = _find_subdataset_uri(subdatasets, "gammaNought", path)
    ds_gamma = gdal.Open(gamma_sds_name, gdal.GA_ReadOnly)
    if ds_gamma is None:
        ds_gamma = gdal.Open(f'NETCDF:"{lut_str}":/radiometry/gammaNought', gdal.GA_ReadOnly)
    if ds_gamma is None:
        root_ds = None
        raise CalibrationError(f"Variable 'gammaNought' not found in NetCDF LUT: {path}")

    band = ds_gamma.GetRasterBand(1)
    nodata_val = band.GetNoDataValue()
    gamma_lut = ds_gamma.ReadAsArray().astype(np.float64)
    if nodata_val is not None:
        gamma_lut[gamma_lut == nodata_val] = np.nan
    band = None
    ds_gamma = None

    # 2. Open relativeAzimuthTimeRGC axis
    az_sds_name = _find_subdataset_uri(subdatasets, "relativeAzimuthTimeRGC", path)
    ds_az = gdal.Open(az_sds_name, gdal.GA_ReadOnly)
    if ds_az is None:
        ds_az = gdal.Open(f'NETCDF:"{lut_str}":relativeAzimuthTimeRGC', gdal.GA_ReadOnly)
    if ds_az is None:
        root_ds = None
        raise CalibrationError(f"Variable 'relativeAzimuthTimeRGC' not found in NetCDF LUT: {path}")
    az_axis = ds_az.ReadAsArray().flatten().astype(np.float64)
    ds_az = None

    # 3. Open slantRangeTimeRGC axis
    rg_sds_name = _find_subdataset_uri(subdatasets, "slantRangeTimeRGC", path)
    ds_rg = gdal.Open(rg_sds_name, gdal.GA_ReadOnly)
    if ds_rg is None:
        ds_rg = gdal.Open(f'NETCDF:"{lut_str}":slantRangeTimeRGC', gdal.GA_ReadOnly)
    if ds_rg is None:
        root_ds = None
        raise CalibrationError(f"Variable 'slantRangeTimeRGC' not found in NetCDF LUT: {path}")
    rg_axis = ds_rg.ReadAsArray().flatten().astype(np.float64)
    ds_rg = None
    root_ds = None

    # Check dimension orientation (handle transposed shape if returned by GDAL)
    if gamma_lut.shape == (len(rg_axis), len(az_axis)):
        gamma_lut = gamma_lut.T

    _validate_lut_axes(gamma_lut, az_axis, rg_axis)

    return LutData(
        gamma_lut=gamma_lut,
        azimuth_axis=az_axis,
        range_axis=rg_axis,
        reference_azimuth_time=ref_time,
    )
