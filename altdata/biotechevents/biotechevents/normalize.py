"""Normalization helpers — sponsor name → ticker, status canonicalization.

Pure functions, no I/O. The sponsor → ticker map is the workhorse;
extend `_SPONSOR_TO_TICKER` as you encounter new biotech names that
matter for your watchlist.
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Phase canonicalization
# ---------------------------------------------------------------------------

_PHASE_MAP = {
    "PHASE1":          "PHASE1",
    "PHASE 1":         "PHASE1",
    "PHASE I":         "PHASE1",
    "EARLY_PHASE1":    "EARLY_PHASE1",
    "PHASE1_PHASE2":   "PHASE1_PHASE2",
    "PHASE 1/PHASE 2": "PHASE1_PHASE2",
    "PHASE2":          "PHASE2",
    "PHASE 2":         "PHASE2",
    "PHASE II":        "PHASE2",
    "PHASE2_PHASE3":   "PHASE2_PHASE3",
    "PHASE3":          "PHASE3",
    "PHASE 3":         "PHASE3",
    "PHASE III":       "PHASE3",
    "PHASE4":          "PHASE4",
    "PHASE 4":         "PHASE4",
    "PHASE IV":        "PHASE4",
    "NA":              "NA",
    "NOT APPLICABLE":  "NA",
}


def normalize_phase(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().upper()
    return _PHASE_MAP.get(s) or s


# ---------------------------------------------------------------------------
# Status canonicalization
# ---------------------------------------------------------------------------

_STATUS_NORMALIZE = {
    "RECRUITING": "RECRUITING",
    "ACTIVE_NOT_RECRUITING": "ACTIVE_NOT_RECRUITING",
    "ACTIVE, NOT RECRUITING": "ACTIVE_NOT_RECRUITING",
    "COMPLETED": "COMPLETED",
    "TERMINATED": "TERMINATED",
    "WITHDRAWN": "WITHDRAWN",
    "SUSPENDED": "SUSPENDED",
    "NOT_YET_RECRUITING": "NOT_YET_RECRUITING",
    "NOT YET RECRUITING": "NOT_YET_RECRUITING",
    "ENROLLING_BY_INVITATION": "ENROLLING_BY_INVITATION",
    "UNKNOWN": "UNKNOWN",
}


def normalize_status(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().upper().replace(", ", "_").replace(" ", "_")
    return _STATUS_NORMALIZE.get(s) or s


# Status changes that move the stock — used by the trade-signal layer
# downstream (not part of this scraper's responsibility, but we expose
# the categorization here so consumers don't re-derive it).
NEGATIVE_STATUS = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}
POSITIVE_STATUS = {"COMPLETED"}    # for primary endpoint readouts


# ---------------------------------------------------------------------------
# Date parsing — ClinicalTrials returns 'YYYY-MM' or 'YYYY-MM-DD' or 'YYYY'
# ---------------------------------------------------------------------------

def normalize_date(raw: Optional[str]) -> Optional[str]:
    """Coerce a date-ish string into YYYY-MM-DD when possible.

    ClinicalTrials.gov uses several formats. We always preserve the
    original in the conditions_json blob; this function only normalizes
    for query convenience. Returns None for unparseable inputs.
    """
    if not raw:
        return None
    s = str(raw).strip()
    # Already ISO date
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    # YYYY-MM → assume first of month
    if re.match(r"^\d{4}-\d{2}$", s):
        return s + "-01"
    # YYYY → assume Jan 1
    if re.match(r"^\d{4}$", s):
        return s + "-01-01"
    return None


# ---------------------------------------------------------------------------
# Sponsor → ticker mapping
# ---------------------------------------------------------------------------

# Hand-curated. Extends as we encounter sponsors whose tickers matter.
# All keys are lowercased for case-insensitive lookup.
_SPONSOR_TO_TICKER = {
    # Big pharma
    "pfizer":                              "PFE",
    "pfizer inc":                          "PFE",
    "moderna":                             "MRNA",
    "moderna inc":                         "MRNA",
    "moderna therapeutics":                "MRNA",
    "biontech":                            "BNTX",
    "biontech se":                         "BNTX",
    "merck sharp & dohme":                 "MRK",
    "merck sharp & dohme llc":             "MRK",
    "merck sharp & dohme corp":            "MRK",
    "merck & co":                          "MRK",
    "johnson & johnson":                   "JNJ",
    "janssen pharmaceuticals":             "JNJ",
    "janssen research & development":      "JNJ",
    "johnson and johnson":                 "JNJ",
    "novartis":                            "NVS",
    "novartis pharmaceuticals":            "NVS",
    "roche":                               "ROG.SW",
    "f hoffmann-la roche":                 "ROG.SW",
    "genentech":                           "ROG.SW",
    "abbvie":                              "ABBV",
    "abbvie inc":                          "ABBV",
    "eli lilly":                           "LLY",
    "eli lilly and company":               "LLY",
    "bristol-myers squibb":                "BMY",
    "astrazeneca":                         "AZN",
    "gsk":                                 "GSK",
    "glaxosmithkline":                     "GSK",
    "sanofi":                              "SNY",
    "amgen":                               "AMGN",
    "regeneron":                           "REGN",
    "regeneron pharmaceuticals":           "REGN",
    "vertex":                              "VRTX",
    "vertex pharmaceuticals":              "VRTX",
    "gilead":                              "GILD",
    "gilead sciences":                     "GILD",
    "biogen":                              "BIIB",
    "biogen inc":                          "BIIB",
    "novo nordisk":                        "NVO",
    "novo nordisk a/s":                    "NVO",
    "takeda":                              "TAK",
    "bayer":                               "BAYRY",
    # Small/mid-cap biotech with frequent catalysts
    "intellia":                            "NTLA",
    "intellia therapeutics":               "NTLA",
    "crispr therapeutics":                 "CRSP",
    "editas":                              "EDIT",
    "editas medicine":                     "EDIT",
    "beam":                                "BEAM",
    "beam therapeutics":                   "BEAM",
    "verve therapeutics":                  "VERV",
    "exact sciences":                      "EXAS",
    "alnylam":                             "ALNY",
    "alnylam pharmaceuticals":             "ALNY",
    "ionis":                               "IONS",
    "ionis pharmaceuticals":               "IONS",
    "viking therapeutics":                 "VKTX",
    "iovance":                             "IOVA",
    "iovance biotherapeutics":             "IOVA",
    "ardelyx":                             "ARDX",
    "incyte":                              "INCY",
    "incyte corp":                         "INCY",
    "blueprint medicines":                 "BPMC",
    "neurocrine":                          "NBIX",
    "neurocrine biosciences":              "NBIX",
    "argenx":                              "ARGX",
    "ionis pharmaceuticals":               "IONS",
    "summit therapeutics":                 "SMMT",
    "tg therapeutics":                     "TGTX",
    "rxrx":                                "RXRX",
    "recursion pharmaceuticals":           "RXRX",
    "axsome therapeutics":                 "AXSM",
    "halozyme":                            "HALO",
    "applied genetic technologies":        "AGTC",
}


_GENERIC_SUFFIXES = re.compile(
    r"\s+(inc\.?|corp\.?|corporation|llc|ltd\.?|limited|"
    r"plc|sa|s\.a\.|n\.v\.|nv|ag|gmbh|co\.?|company)$",
    re.IGNORECASE,
)


def sponsor_to_ticker(sponsor_name: Optional[str]) -> Optional[str]:
    """Best-effort sponsor → ticker mapping. Returns None if unknown.

    Tries exact match first, then strips common corporate suffixes
    and tries again.
    """
    if not sponsor_name:
        return None
    lowered = sponsor_name.strip().lower()
    if lowered in _SPONSOR_TO_TICKER:
        return _SPONSOR_TO_TICKER[lowered]
    # Try with corporate suffix stripped
    stripped = _GENERIC_SUFFIXES.sub("", lowered).strip()
    if stripped in _SPONSOR_TO_TICKER:
        return _SPONSOR_TO_TICKER[stripped]
    # Substring match — fragile but useful for "Eli Lilly and Company Limited"
    for key, ticker in _SPONSOR_TO_TICKER.items():
        if len(key) > 8 and key in lowered:
            return ticker
    return None
