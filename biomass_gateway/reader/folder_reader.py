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


def _extract_track_frame_from_name(folder_name: str) -> tuple[str | None,  str | None]:
    """Extract track and frame rom product folder name
    """
    track_match = re.search(r"_(T\d{3})_", folder_name)
    frame_match = re.search(r"_(F\d{3})_", folder_name)
    track = track_match.group(1) if track_match else None
    frame = frame_match.group(1) if frame_match else None
    return track, frame


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

    swath = "S2"
    product_type = "SCS"
    mission_phase = None
    start_time: datetime | None = None
    stop_time: datetime | None = None
    polarisations: list[str] = ["HH", "HV", "VH", "VV"]
    track, frame = _extract_track_frame_from_name(product_id)

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

    # find measurement files
    abs_files = (
            list(path.glob("measurement/*_abs.tiff"))
            or list(path.glob("measurement/*_abs.tif"))
            or list(path.glob("*_abs.tiff"))
    )
    abs_file = abs_files[0] if abs_files else None

    phase_files = (
            list(path.glob("measurement/*_phase.tiff"))
            or list(path.glob("measurement/*_phase.tif"))
            or list(path.glob("*_phase.tiff"))
    )
    phase_file = phase_files[0] if phase_files else None

    vrt_files = list(path.glob("measurement/*_i.vrt")) or list(path.glob("*_i.vrt"))
    vrt_file = vrt_files[0] if vrt_files else None

    level = _parse_product_level(product_type, product_id)

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
