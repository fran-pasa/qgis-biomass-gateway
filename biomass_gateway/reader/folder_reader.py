"""Biomass product folder and annotation XML reader.
"""

import re
import xml.etree.ElementTree as ET

from datetime import datetime
from pathlib import Path

from ..core.exceptions import AnnotationParseError, ProductReadError
from ..core.models import BiomassProduct,  GcpPoint, ProductLevel


def is_biomass_folder(folder_path: Path) -> bool:
    """Check if a directory resembles an ESA BIOMASS product.
    """
    path = Path(folder_path)
    if not path.is_dir():
        return False

    name = path.name.upper()
    if name.startswith("BIO"):
        return True

    # check for expected subdirs
    return (path / "annotation").is_dir() or (path / "measurement").is_dir()


def _parse_product_level(product_type: str, folder_name: str) -> ProductLevel:
    """Determine ProductLevel enum from product type string or
    folder name.
    """
    target = f"{product_type} {folder_name}".upper()
    if "SCS" in target:
        return ProductLevel.L1A
    if "DGM" in target:
        return ProductLevel.L1B
    if "_1M" in target:
        return ProductLevel.L1M
    if "L1C" in target:
        return ProductLevel.L1C
    if "L2A" in target:
        return ProductLevel.L2A
    if "L2B" in target:
        return ProductLevel.L2B
    return ProductLevel.UNKNOWN


def _extract_name_metadata(folder_name: str) -> dict[str, str | None]:
    """Extract swath, product type, track and frame from BIOMASS folder name.
    Example: BIO_S2_SCS__1S_20261007T093919_..._T035_F306_...
    """
    parts = folder_name.split("_")
    swath = None
    product_type = None
    if len(parts) >= 3 and parts[0].upper() == "BIO":
        swath = parts[1].upper()
        product_type = parts[2].upper()

    track_match = re.search(r"_(T\d{3})_", folder_name)
    frame_match = re.search(r"_(F\d{3})_", folder_name)
    return {
        "swath": swath,
        "product_type": product_type,
        "track": track_match.group(1) if track_match else None,
        "frame": frame_match.group(1) if frame_match else None,
    }


def _extract_track_frame_from_name(folder_name: str) -> tuple[str | None,  str | None]:
    """Extract track and frame from product folder name
    """
    meta = _extract_name_metadata(folder_name)
    return meta["track"], meta["frame"]


def _parse_gcps_from_vrt(vrt_path: Path) -> list[GcpPoint]:
    """Extract GCPs from VRT XML structure
    """
    if not vrt_path.is_file():
        return []

    try:
        tree = ET.parse(vrt_path)
        root = tree.getroot()
        gcp_elements = root.findall(".//GCPList/GCP")
        gcps: list[GcpPoint] = []
        for elem in gcp_elements:
            gcp_id  = elem.attrib.get("Id", "")
            pixel = float(elem.attrib.get("Pixel", 0.0))
            line = float(elem.attrib.get("Line", 0.0))
            x = float(elem.attrib.get("X", 0.0))
            y = float(elem.attrib.get("Y", 0.0))
            z = float(elem.attrib.get("Z", 0.0))
            gcps.append(GcpPoint(id=gcp_id, pixel=pixel, line=line, x=x, y=y, z=z))
        return gcps
    except Exception as err:
        # fallback if VRT XML parsing fails
        return []


def _extract_lut_grid_shape(lut_path: Path) -> tuple[int, int] | None:
    if not lut_path or not lut_path.is_file():
        return None

    try:
        from osgeo import gdal

        # open the latitude subdataset from the netcdf
        subds_name = f'NETCDF:"{lut_path}":/geometry/latitude'
        ds = gdal.Open(subds_name, gdal.GA_ReadOnly)
        if ds is not None:
            shape = (ds.RasterYSize, ds.RasterXSize)
            ds = None
            return shape
    except Exception:
        pass


def read_biomass_product(folder_path: Path | str) -> BiomassProduct:
    """Parse a Biomass product folder and its annotation metadata.

    Raises:
        ProductReadError: if the folder does not exist or is invalid
        AnnotationParseError: if the annotation XML file is invalid
    """
    path = Path(folder_path).resolve()
    if not path.is_dir():
        raise ProductReadError(f"Directory does not exist: {path}")

    product_id = path.name

    # locate annotation XML metadata file
    annotation_files = list(path.glob("annotation/*_annot.xml")) or list(path.glob("*_annot.xml"))
    annotation_file = annotation_files[0] if annotation_files else None

    # locate NetCDF LUT file
    lut_files = (
        list(path.glob("annotation/*_lut.nc"))
        or list(path.glob("*_lut.nc"))
        or list(path.glob("annotation/*.nc"))
    )
    lut_file = lut_files[0] if lut_files else None
    lut_grid_shape = _extract_lut_grid_shape(lut_file) if lut_file else None

    name_meta = _extract_name_metadata(product_id)
    swath = name_meta["swath"] or "N/A"
    product_type = name_meta["product_type"] or "UNKNOWN"
    track = name_meta["track"]
    frame = name_meta["frame"]
    mission_phase = None
    start_time: datetime | None = None
    stop_time: datetime | None = None
    polarisations: list[str] = []

    if annotation_file and annotation_file.is_file():
        try:
            tree = ET.parse(annotation_file)
            root = tree.getroot()

            acq = root.find("acquisitionInformation")
            if acq is not None:
                swath = acq.findtext("swath") or swath
                product_type = acq.findtext("productType") or product_type
                mission_phase = acq.findtext("missionPhaseID") or mission_phase

                raw_start = acq.findtext("startTime")
                if raw_start:
                    start_time = datetime.fromisoformat(raw_start)

                raw_stop = acq.findtext("stopTime")
                if raw_stop:
                    stop_time = datetime.fromisoformat(raw_stop)

                # extract track and frame if not already extracted
                rel_orbit = acq.findtext("relativeOrbitNumber")
                if rel_orbit and not track:
                    track = f"T{int(rel_orbit):03d}"

                frame_num = acq.findtext("frame")
                if frame_num and not frame:
                    frame = f"F{int(frame_num):03d}"

                # extract pol
                pol_elems = acq.findall(".//polarisationList/polarisation")
                if pol_elems:
                    polarisations = [p.text for p in pol_elems if p.text]

        except ET.ParseError as err:
            raise AnnotationParseError(f"Failed to parse XML annotation {annotation_file}: {err}") from err

    # find measurement files: search measurement/ folder, then product root
    meas_candidates = (
        sorted(path.glob("measurement/*.tiff"))
        + sorted(path.glob("measurement/*.tif"))
        + sorted(path.glob("*.tiff"))
        + sorted(path.glob("*.tif"))
    )
    unique_tiffs: list[Path] = []
    seen = set()
    for f in meas_candidates:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            unique_tiffs.append(f)

    # identify amp and phase files
    abs_files = [f for f in unique_tiffs if "_abs" in f.stem.lower()]
    phase_files = [f for f in unique_tiffs if "_phase" in f.stem.lower()]

    abs_file = abs_files[0] if abs_files else None
    phase_file = phase_files[0] if phase_files else None

    # if no explicit _abs file found, pick first non-phase TIFF as primary measurement
    # (e.g. L1B _dgm, L2A _agb, _fh, _fd, or generic single tiff)
    if not abs_file:
        non_phase_tiffs = [f for f in unique_tiffs if "_phase" not in f.stem.lower()]
        if non_phase_tiffs:
            abs_file = non_phase_tiffs[0]

    vrt_files = (
        list(path.glob("measurement/*_i.vrt"))
        or list(path.glob("measurement/*.vrt"))
        or list(path.glob("*_i.vrt"))
        or list(path.glob("*.vrt"))
    )
    vrt_file = vrt_files[0] if vrt_files else None

    level = _parse_product_level(product_type, product_id)

    # set appropriate default polarisations/bands based on product level
    if not polarisations:
        if level in (ProductLevel.L1A, ProductLevel.L1B, ProductLevel.L1C, ProductLevel.L1M):
            polarisations = ["HH", "HV", "VH", "VV"]
        else:
            polarisations = [product_type] if product_type != "UNKNOWN" else ["Band 1"]

    # extract GCPs from VRT file
    gcps: list[GcpPoint] = []
    if vrt_file:
        gcps = _parse_gcps_from_vrt(vrt_file)

    return BiomassProduct(
        product_id=product_id,
        folder_path=path,
        level=level,
        swath=swath,
        product_type=product_type,
        track=track,
        frame=frame,
        sensing_start=start_time,
        sensing_stop=stop_time,
        mission_phase=mission_phase,
        abs_file=abs_file,
        phase_file=phase_file,
        vrt_file=vrt_file,
        annotation_file=annotation_file,
        polarisations=polarisations,
        gcps=gcps,
        lut_file=lut_file,
        lut_grid_shape=lut_grid_shape,
    )
