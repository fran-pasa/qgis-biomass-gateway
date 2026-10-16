"""RFI mitigation package for BIOMASS SLC products.
"""

from .detector import (
    get_eig_th,
    lambda1_bsf,
    mitigate_rfi_slc,
    ups_bdr,
)

__all__ = [
    "get_eig_th",
    "lambda1_bsf",
    "mitigate_rfi_slc",
    "ups_bdr",
]
