"""
Lambda-1 RFI detector + block subspace filter (BSF) for SAR single-look-complex data.

Algorithm: Huizhang Yang et al., "Lambda-1 Detector for Interference Detection in Synthetic Aperture Radar
Images" and "BSF: Block Subspace Filter for Removing Narrowband and Wideband Radio Interference Artifacts in
Single-Look Complex SAR Images", IEEE TGRS 2024 (reference code: github.com/huizhangyang/lambda1-bsf).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np
from osgeo import gdal
from scipy import fft as sfft
from scipy.sparse.linalg import svds

from ...core.exceptions import RfiMitigationError
from ...core.logger import get_logger
from ...core.memory import DEFAULT_OVERVIEW_LEVELS, safe_close_dataset
from ...core.memory import DEFAULT_OVERVIEW_LEVELS, safe_close_dataset

logger = get_logger("rfi.detector")


def get_eig_th(var, n, p, gamma=2, ups1=1.5,
               ups2=1.5, bdr1=0.7, bdr2=0.7):
    """Lambda-1 threshold (Tracy-Widom, pfa = 1e-5) for an n x p block of noise with variance var
    """
    ups1 = ups1 / bdr1
    ups2 = ups2 / bdr2
    n = np.trunc(n / ups1)
    p = np.trunc(p / ups2)
    var = var * ups1 * ups2 * gamma
    TW = 2.6672
    u = np.square(np.sqrt(n) + np.sqrt(p))
    sigma = np.sqrt(u) * np.power(1 / np.sqrt(n) + 1 / np.sqrt(p), 1 / 3)
    return (TW * sigma + u) * var


def ups_bdr(annot_xml: Path | str) -> dict[str, float]:
    """ups/bdr for lambda1 from a Biomass annotation XML: axis 1 = azimuth, axis 2 = range."""
    path = Path(annot_xml).resolve()
    if not path.is_file():
        raise RfiMitigationError(f"Annotation XML not found: {path}")

    try:
        root = ET.parse(path).getroot()
    except Exception as err:
        raise RfiMitigationError(f"Failed to parse annotation XML {path}: {err}") from err

    def get(xpath: str) -> float:
        el = root.find(xpath)
        if el is None or el.text is None:
            raise KeyError(f"tag not found: {xpath}")
        return float(el.text)

    az, rg = "azimuthProcessingParameters", "rangeProcessingParameters"
    try:
        def bdr(blk: str) -> float:
            a = get(f".//{blk}/windowCoefficient")
            return a**2 + (1 - a) ** 2 / 2  # Hamming: B_eq/B

        return {
            "ups1": get(f".//{az}/totalBandwidth") / get(f".//{az}/processingBandwidth"),  # fs_az / B_az
            "ups2": get(f".//{rg}/totalBandwidth") / get(f".//{rg}/processingBandwidth"),  # fs_rg / B_rg
            "bdr1": bdr(az),
            "bdr2": bdr(rg),
        }
    except KeyError as err:
        raise RfiMitigationError(f"Missing parameter in annotation XML {path}: {err}") from err


def _block_filter(
    slc: np.ndarray,
    lblk: tuple[int, int],
    lpd: tuple[int, int],
    gamma: float,
    ns_max: int,
    ns_mul: float,
    axis: int,
    q_out: list[int] | None,
    min_size: int,
    outlier: float | None,
    kw: dict[str, Any],
    progress_cb: Callable[[float, str], None] | None = None,
    in_place: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Block loop of lambda1_bsf on a (n_pol, rows, cols) stack. Returns (filtered stack, mask (rows, cols)).

    outlier: zero pixels above mean + outlier*std of |patch| before the SVD (None = off).
    """
    n, R, C = slc.shape
    out = slc if in_place else slc.copy()
    mask = np.zeros((R, C), np.int8)

    y_steps = list(range(0, R, lblk[0]))
    x_steps = list(range(0, C, lblk[1]))
    total_blocks = len(y_steps) * len(x_steps)
    block_idx = 0

    for y in y_steps:
        for x in x_steps:
            block_idx += 1
            if progress_cb and (block_idx % 10 == 0 or block_idx == total_blocks):
                progress_cb(
                    block_idx / total_blocks,
                    f"Filtering block {block_idx}/{total_blocks} ({(block_idx / total_blocks):.0%})...",
                )

            y0, y1 = max(y - lpd[0], 0), min(y + lblk[0] + lpd[0], R)
            x0, x1 = max(x - lpd[1], 0), min(x + lblk[1] + lpd[1], C)
            P = slc[:, y0:y1, x0:x1].copy()
            if outlier:
                a = np.abs(P)
                P[a > a.mean() + outlier * a.std()] = 0  # same outlier rule as reference code
            X = np.concatenate(list(P), axis=axis)  # spliced matrix
            if min(X.shape) <= min_size:  # too small for svds / edge sliver
                continue
            th = get_eig_th(np.var(X), *X.shape, gamma=gamma, **kw)
            if svds(X, k=1, maxiter=20)[1][0] ** 2 <= th:
                continue
            k_svd = min(ns_max, min(X.shape) - 1)
            if k_svd < 1:
                continue
            u, s, vh = svds(X, k=k_svd)
            q = int(np.sum(s**2 > th) * ns_mul)
            q = max(1, min(q, len(s)))
            rem = np.stack(np.split((u[:, -q:] * s[-q:]) @ vh[-q:], n, axis=axis))  # (n, h, w)
            hy, hx = min(lblk[0], R - y), min(lblk[1], C - x)
            out[:, y : y + hy, x : x + hx] -= rem[:, y - y0 : y - y0 + hy, x - x0 : x - x0 + hx]
            mask[y : y + hy, x : x + hx] = 1
            if q_out is not None:
                q_out.append(q)
    return out, mask


def lambda1_bsf(
    slc: np.ndarray,
    lblk: tuple[int, int] = (500, 500),
    lpd: tuple[int, int] = (50, 50),
    gamma: float = 1.5,
    ns_max: int = 15,
    ns_mul: float = 2,
    axis: int = 1,
    q_out: list[int] | None = None,
    norm: bool = False,
    domain: str = "image",
    progress_cb: Callable[[float, str], None] | None = None,
    in_place: bool = False,
    **kw,
) -> tuple[np.ndarray, np.ndarray]:
    """Lambda-1 detector + block subspace filter on slc (n_pol, rows, cols); a 2D array is one channel.

    The n_pol patches of each block are spliced along `axis` into one matrix, so n_pol=1 is per-channel
    filtering and n_pol=4 the joint (polarimetric) one: same code, only the input stack changes.
    kw: ups1, ups2, bdr1, bdr2 (from ups_bdr). q_out: list that receives the eigenvalues removed per block.
    norm: scale each pol to unit rms before splicing (so strong co-pols do not dominate the joint SVD and
    threshold) and scale the result back; no effect for a single channel.
    domain: "image" = blocks of the SLC (lblk x lpd, as before). "freq" = FFT along range, keep the in-band bins
    (B/fs = 1/ups2), whiten by the Hamming window (coefficient inverted from bdr2), and cut the same lblk/lpd
    blocks on (azimuth line, frequency bin), e.g. lblk=(500, 100), lpd=(50, 0), with ups2 = bdr2 = 1; needs ups2
    and bdr2 in kw. No outlier
    zeroing in this mode (it would zero the narrowband RFI itself). mask is then "azimuth block flagged in
    at least one band", repeated along range, so it keeps the (rows, cols) shape.
    Returns (cleaned, mask (rows, cols)) with the shape of slc.
    """
    squeeze = slc.ndim == 2
    slc = slc[None] if squeeze else slc
    n, R, C = slc.shape
    if norm:
        scale = np.sqrt((np.abs(slc) ** 2).mean(axis=(1, 2), keepdims=True))  # rms per pol
        scale = np.where(scale == 0, 1.0, scale)
        if in_place:
            slc /= scale
        else:
            slc = slc / scale
    if domain == "image":
        out, mask = _block_filter(
            slc,
            lblk,
            lpd,
            gamma,
            ns_max,
            ns_mul,
            axis,
            q_out,
            max(ns_max, 100),
            8,
            kw,
            progress_cb=progress_cb,
            in_place=in_place,
        )
    elif domain == "freq":
        bw = 1 / kw["ups2"]  # B / fs, normalized range bandwidth
        alpha = (1 + np.sqrt(6 * kw["bdr2"] - 2)) / 3  # Hamming coefficient, inverse of bdr2
        f = np.fft.fftshift(np.fft.fftfreq(C))
        inb = np.abs(f) < bw / 2
        w = (alpha + (1 - alpha) * np.cos(2 * np.pi * f[inb] / bw)).astype(np.float32)  # window over the band
        F = sfft.fftshift(sfft.fft(slc, axis=-1, norm="ortho"), axes=-1)  # (n, R, C) range spectrum
        G, mask_f = _block_filter(
            F[..., inb] / w,
            lblk,
            lpd,
            gamma,
            ns_max,
            ns_mul,
            axis,
            q_out,
            max(ns_max, 50),
            None,
            {**kw, "ups2": 1, "bdr2": 1},
            progress_cb=progress_cb,
            in_place=True,
        )
        F[..., inb] = G * w  # out-of-band bins stay as they were
        del G
        out = sfft.ifft(sfft.ifftshift(F, axes=-1), axis=-1, norm="ortho")
        del F
        mask = np.repeat(mask_f.any(axis=1, keepdims=True), C, axis=1).astype(np.int8)
    else:
        raise ValueError(f"domain must be 'image' or 'freq', got {domain!r}")
    if norm:
        if in_place:
            out *= scale
        else:
            out = out * scale
    return (out[0] if squeeze else out), mask


def mitigate_rfi_slc(
    amplitude_raster_path: Path | str,
    phase_raster_path: Path | str,
    annot_xml_path: Path | str,
    output_amplitude_path: Path | str,
    output_mask_path: Path | str,
    output_phase_path: Path | str | None = None,
    domain: str = "image",
    gamma: float = 1.5,
    ns_max: int = 15,
    ns_mul: float = 2.0,
    norm: bool = True,
    lblk: tuple[int, int] | None = None,
    lpd: tuple[int, int] | None = None,
    axis: int = 1,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Execute Lambda-1 BSF RFI mitigation on complex SLC rasters and save outputs as GeoTIFFs.

    Args:
        amplitude_raster_path: Path to source amplitude GeoTIFF (*_i_abs.tiff).
        phase_raster_path: Path to source phase GeoTIFF (*_i_phase.tiff).
        annot_xml_path: Path to product annotation XML (*_annot.xml).
        output_amplitude_path: Destination path for cleaned amplitude GeoTIFF.
        output_mask_path: Destination path for RFI mask GeoTIFF (Byte).
        output_phase_path: Optional destination path for cleaned phase GeoTIFF.
        domain: 'image' or 'freq'.
        gamma: Threshold factor for Lambda-1 detector.
        ns_max: Maximum subspace dimensions.
        ns_mul: Subspace dimension multiplier.
        norm: Whether to normalize polarizations to unit RMS before joint SVD.
        lblk: Block dimensions (azimuth, range). Defaults: (500, 500) for image, (500, 100) for freq.
        lpd: Overlap/padding dimensions (azimuth, range). Defaults: (50, 50) for image, (50, 0) for freq.
        axis: Splicing axis across polarizations.
        progress_callback: Optional callable(float, str) reporting progress.

    Returns:
        dict containing execution summary metrics.
    """
    amp_path = Path(amplitude_raster_path).resolve()
    pha_path = Path(phase_raster_path).resolve()
    xml_path = Path(annot_xml_path).resolve()
    out_amp_path = Path(output_amplitude_path).resolve()
    out_mask_path = Path(output_mask_path).resolve()
    out_pha_path = Path(output_phase_path).resolve() if output_phase_path else None

    if not amp_path.is_file():
        raise RfiMitigationError(f"Amplitude measurement raster not found: {amp_path}")
    if not pha_path.is_file():
        raise RfiMitigationError(f"Phase measurement raster not found: {pha_path}")
    if not xml_path.is_file():
        raise RfiMitigationError(f"Annotation XML metadata not found: {xml_path}")

    # Set default block and padding based on domain
    if domain == "freq":
        lblk = lblk or (500, 100)
        lpd = lpd or (50, 0)
    else:
        domain = "image"
        lblk = lblk or (500, 500)
        lpd = lpd or (50, 50)

    if progress_callback:
        progress_callback(0.02, "Reading annotation parameters...")
    p_params = ups_bdr(xml_path)

    if progress_callback:
        progress_callback(0.05, "Opening measurement datasets...")

    gdal.UseExceptions()
    src_amp_ds = gdal.Open(str(amp_path), gdal.GA_ReadOnly)
    if src_amp_ds is None:
        raise RfiMitigationError(f"Failed to open amplitude raster: {amp_path}")

    src_pha_ds = gdal.Open(str(pha_path), gdal.GA_ReadOnly)
    if src_pha_ds is None:
        src_amp_ds = None
        raise RfiMitigationError(f"Failed to open phase raster: {pha_path}")

    width = src_amp_ds.RasterXSize
    height = src_amp_ds.RasterYSize
    band_count = src_amp_ds.RasterCount

    if (src_pha_ds.RasterXSize, src_pha_ds.RasterYSize) != (width, height):
        raise RfiMitigationError(
            f"Amplitude ({width}x{height}) and Phase ({src_pha_ds.RasterXSize}x{src_pha_ds.RasterYSize}) "
            "dimensions do not match."
        )

    if src_pha_ds.RasterCount != band_count:
        raise RfiMitigationError(
            f"Amplitude ({band_count}) and Phase ({src_pha_ds.RasterCount}) band counts do not match."
        )

    if progress_callback:
        progress_callback(0.10, f"Loading {band_count} polarimetric SLC bands into memory...")

    band_descriptions = []
    slc = np.empty((band_count, height, width), dtype=np.complex64)

    for b in range(1, band_count + 1):
        amp_band = src_amp_ds.GetRasterBand(b)
        pha_band = src_pha_ds.GetRasterBand(b)

        desc = amp_band.GetDescription() or f"Band {b}"
        band_descriptions.append(desc)

        amp_arr = amp_band.ReadAsArray().astype(np.float32)
        pha_arr = pha_band.ReadAsArray().astype(np.float32)

        # Complex SLC reconstruction: abs * e^(j * phase)
        slc[b - 1] = amp_arr * np.exp(1j * pha_arr)
        amp_band = None
        pha_band = None
        del amp_arr, pha_arr

    q_records: list[int] = []

    def _sub_cb(fraction: float, message: str = ""):
        if progress_callback:
            # Map block filtering progress from 0.15 to 0.85
            overall = 0.15 + fraction * 0.70
            progress_callback(overall, message)

    if progress_callback:
        progress_callback(0.15, f"Running Lambda-1 BSF ({domain} domain, gamma={gamma})...")

    clean, mask = lambda1_bsf(
        slc,
        lblk=lblk,
        lpd=lpd,
        gamma=gamma,
        ns_max=ns_max,
        ns_mul=ns_mul,
        axis=axis,
        q_out=q_records,
        norm=norm,
        domain=domain,
        progress_cb=_sub_cb,
        in_place=True,
        **p_params,
    )

    del slc
    import gc

    gc.collect()

    if clean.ndim == 2:
        clean = clean[None]

    if progress_callback:
        progress_callback(0.85, "Writing cleaned amplitude GeoTIFF...")

    driver = gdal.GetDriverByName("GTiff")
    if driver is None:
        raise RfiMitigationError("GDAL GTiff driver not found.")

    creation_opts = [
        "TILED=YES",
        "BLOCKXSIZE=512",
        "BLOCKYSIZE=512",
        "COMPRESS=DEFLATE",
        "BIGTIFF=IF_SAFER",
        "NUM_THREADS=ALL_CPUS",
    ]

    # 1. Output Cleaned Amplitude
    out_amp_path.parent.mkdir(parents=True, exist_ok=True)
    if out_amp_path.is_file():
        try:
            out_amp_path.unlink()
        except OSError:
            pass

    out_amp_ds = driver.Create(str(out_amp_path), width, height, band_count, gdal.GDT_Float32, creation_opts)
    if out_amp_ds is None:
        raise RfiMitigationError(f"Failed to create output amplitude raster: {out_amp_path}")

    _copy_georef(src_amp_ds, out_amp_ds)
    out_amp_ds.SetMetadataItem("PROCESSING", "RFI_MITIGATION")
    out_amp_ds.SetMetadataItem("RFI_ALGORITHM", "LAMBDA1_BSF")
    out_amp_ds.SetMetadataItem("RFI_DOMAIN", domain)
    out_amp_ds.SetMetadataItem("RFI_GAMMA", str(gamma))

    for b in range(1, band_count + 1):
        out_band = out_amp_ds.GetRasterBand(b)
        out_band.SetDescription(band_descriptions[b - 1])
        amp_data = np.abs(clean[b - 1]).astype(np.float32)
        out_band.WriteArray(amp_data)
        try:
            out_band.SetStatistics(
                float(np.nanmin(amp_data)),
                float(np.nanmax(amp_data)),
                float(np.nanmean(amp_data)),
                float(np.nanstd(amp_data)),
            )
        except Exception as stat_err:
            logger.debug("Failed to set statistics for amplitude band %d: %s", b, stat_err)
        out_band = None
        del amp_data

    out_amp_ds.FlushCache()
    if progress_callback:
        progress_callback(0.88, "Building pyramid overviews for cleaned amplitude GeoTIFF...")
    try:
        out_amp_ds.BuildOverviews("NEAREST", list(DEFAULT_OVERVIEW_LEVELS))
    except Exception as err:
        logger.warning("Failed to build overviews for amplitude: %s", err)
    safe_close_dataset(out_amp_ds, flush_global_cache=True, run_gc=False)
    out_amp_ds = None

    # 2. Output RFI Mask
    if progress_callback:
        progress_callback(0.92, "Writing RFI detection mask...")

    out_mask_path.parent.mkdir(parents=True, exist_ok=True)
    if out_mask_path.is_file():
        try:
            out_mask_path.unlink()
        except OSError:
            pass

    out_mask_ds = driver.Create(str(out_mask_path), width, height, 1, gdal.GDT_Byte, creation_opts)
    if out_mask_ds is None:
        raise RfiMitigationError(f"Failed to create output mask raster: {out_mask_path}")

    _copy_georef(src_amp_ds, out_mask_ds)
    out_mask_ds.SetMetadataItem("PROCESSING", "RFI_MASK")
    out_mask_ds.SetMetadataItem("RFI_ALGORITHM", "LAMBDA1_BSF")

    mask_band = out_mask_ds.GetRasterBand(1)
    mask_band.SetDescription("RFI Mitigation Flag Mask (1=Mitigated, 0=Clean)")
    mask_u8 = mask.astype(np.uint8)
    mask_band.WriteArray(mask_u8)
    try:
        mask_band.SetStatistics(
            float(mask_u8.min()),
            float(mask_u8.max()),
            float(mask_u8.mean()),
            float(mask_u8.std()),
        )
    except Exception as stat_err:
        logger.debug("Failed to set statistics for mask: %s", stat_err)
    mask_band = None
    del mask_u8

    out_mask_ds.FlushCache()
    try:
        out_mask_ds.BuildOverviews("NEAREST", list(DEFAULT_OVERVIEW_LEVELS))
    except Exception as err:
        logger.warning("Failed to build overviews for mask: %s", err)
    safe_close_dataset(out_mask_ds, flush_global_cache=True, run_gc=False)
    out_mask_ds = None

    # 3. Optional Output Cleaned Phase
    if out_pha_path:
        if progress_callback:
            progress_callback(0.96, "Writing cleaned phase GeoTIFF...")
        out_pha_path.parent.mkdir(parents=True, exist_ok=True)
        if out_pha_path.is_file():
            try:
                out_pha_path.unlink()
            except OSError:
                pass

        out_pha_ds = driver.Create(str(out_pha_path), width, height, band_count, gdal.GDT_Float32, creation_opts)
        if out_pha_ds is not None:
            _copy_georef(src_pha_ds, out_pha_ds)
            out_pha_ds.SetMetadataItem("PROCESSING", "RFI_MITIGATION")
            out_pha_ds.SetMetadataItem("RFI_ALGORITHM", "LAMBDA1_BSF")
            for b in range(1, band_count + 1):
                p_band = out_pha_ds.GetRasterBand(b)
                p_band.SetDescription(band_descriptions[b - 1])
                pha_data = np.angle(clean[b - 1]).astype(np.float32)
                p_band.WriteArray(pha_data)
                try:
                    p_band.SetStatistics(
                        float(np.nanmin(pha_data)),
                        float(np.nanmax(pha_data)),
                        float(np.nanmean(pha_data)),
                        float(np.nanstd(pha_data)),
                    )
                except Exception as stat_err:
                    logger.debug("Failed to set statistics for phase band %d: %s", b, stat_err)
                p_band = None
                del pha_data
            out_pha_ds.FlushCache()
            try:
                out_pha_ds.BuildOverviews("NEAREST", list(DEFAULT_OVERVIEW_LEVELS))
            except Exception as err:
                logger.warning("Failed to build overviews for phase: %s", err)
            safe_close_dataset(out_pha_ds, flush_global_cache=True, run_gc=False)
            out_pha_ds = None

    del clean
    safe_close_dataset(src_amp_ds, flush_global_cache=False, run_gc=False)
    src_amp_ds = None
    safe_close_dataset(src_pha_ds, flush_global_cache=True, run_gc=True)
    src_pha_ds = None

    flagged_ratio = float(mask.mean())
    flagged_count = int(np.sum(mask > 0))
    mean_q = float(np.mean(q_records)) if q_records else 0.0

    if progress_callback:
        progress_callback(
            1.0, f"RFI mitigation complete: {flagged_ratio:.1%} flagged area, mean subspace rank removed: {mean_q:.1f}"
        )

    return {
        "flagged_ratio": flagged_ratio,
        "flagged_pixels": flagged_count,
        "total_pixels": int(mask.size),
        "mean_q": mean_q,
        "num_rfi_blocks": len(q_records),
    }


def _copy_georef(src_ds: gdal.Dataset, dst_ds: gdal.Dataset) -> None:
    """Copy GCPs, projection, GeoTransform and metadata from source to destination dataset
    """
    gcps = src_ds.GetGCPs()
    gcp_proj = src_ds.GetGCPProjection()
    if gcps:
        dst_ds.SetGCPs(gcps, gcp_proj)

    proj = src_ds.GetProjection()
    if proj:
        dst_ds.SetProjection(proj)

    gt = src_ds.GetGeoTransform(can_return_null=True)
    if gt:
        dst_ds.SetGeoTransform(gt)

    meta = src_ds.GetMetadata()
    if meta:
        dst_ds.SetMetadata(meta)
