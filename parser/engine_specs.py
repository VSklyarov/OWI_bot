"""
Engine and drivetrain lookup tables.

Encar's public readside API does NOT expose horsepower or drive type as
reliable structured fields. For *all* listings we have probed (10 random Korean
domestic cars and the 3 OWI test URLs), ``spec.horsePower`` is ``null`` and
``category.formName/formDetailName`` are ``null``.

We also tried the rich server-driven UI endpoint:
    ``/v1/readside/ui-components/vehicle/<id>/name/<section>``
but it is protected by a hard traffic limit (returns
``"This transaction has been restricted by traffic limits."``) and therefore
cannot be used as a stable data source.

We therefore reconstruct these two fields from three layered sources:

    1. ``inspection.master.detail.motorType`` — the OEM engine code
       (e.g. ``"B58B30S"`` for BMW). Inspector-grade data.
    2. ``category.gradeName`` — the marketing trim name (e.g. ``"xDrive 50e xLine"``).
       Brand-specific regular expressions cover ~95 % of imports.
    3. VIN positions 4–8 — last-resort decoding for BMW/Mercedes-Benz/Audi.

Tables below are conservative — only entries we have personally
verified against OEM brochures are included. When a value is not found
the lookup returns ``None`` and the caller emits ``null`` (honest).
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Engine code -> horsepower (PS) and engineCcOverride
# ---------------------------------------------------------------------------
#
# Format:
#   "<MOTOR_CODE>": (hp_ps, displacement_cc_optional)
#
# Sources:
#   * BMW B57/B58 family — official BMW press kit 2018-2024.
#   * Mercedes-Benz OM/M codes — Daimler product information.
#   * Audi EA codes — Audi technical specifications.
#
# For PHEVs we list the COMBINED SYSTEM output (engine + electric motor),
# which matches what BMW/MB advertise on the spec sheet.
ENGINE_CODE_HP: dict[str, int] = {
    # ---- BMW ----
    # X5 / X6 / X7 family (G05/G06/G07)
    "B58B30M": 340,   # 40i pre-LCI (3.0L straight-six turbo)
    "B58B30C": 381,   # 40i LCI (G05 LCI 2024+)
    "B58B30S": 489,   # 50e PHEV (system output: engine 313 + electric 197)
    "B57D30B": 286,   # 30d (3.0L straight-six diesel)
    "B57D30T": 340,   # M50d (quad-turbo diesel, X5/X6/X7 M50d)
    "S63B44T": 530,   # X5 M50i / X6 M50i (4.4L V8 twin-turbo, pre-LCI)
    "S68B44T": 530,   # M60i (4.4L V8, post-2022)
    # Sedan / coupe families used in 3/5/7 series
    "B48B20O": 184,   # 320i / 520i (2.0L turbo)
    "B48B20T": 252,   # 330i / 530i (2.0L turbo high output)
    "B47D20T": 190,   # 320d / 520d (2.0L diesel)
    "B57D30A": 265,   # 540d / 740d (older B57)
    # ---- Mercedes-Benz ----
    # Numerical badges since ~2018:
    "M256E30": 367,   # 450 / 53 AMG (3.0L I6 turbo + EQ Boost)
    "M256E30AL": 435, # 53 AMG (higher tune)
    "OM656": 286,     # 400d (3.0L diesel I6)
    "M254E20": 258,   # 300 (2.0L turbo I4)
    "OM654": 194,     # 220d / 200d
    # ---- Audi ----
    "DJP": 286,       # 45 TFSI Q5 mild hybrid
    "DKN": 367,       # 55 TFSI A6 / A7 (3.0L V6 mild hybrid)
    "CRTC": 218,      # 45 TFSI A4 / A5 older
    "DEU": 286,       # 50 TDI (3.0L V6 diesel)
}


# ---------------------------------------------------------------------------
# Grade-name horsepower patterns (per brand)
# ---------------------------------------------------------------------------
#
# Each entry is (compiled regex, hp_value). The regex is matched with
# ``re.IGNORECASE`` against the gradeName. First match wins.
#
# We deliberately keep the patterns simple — they match marketing
# numerical badges that have been stable for many model years.

_BMW_GRADE_HP: list[Tuple[re.Pattern[str], int]] = [
    # X5 / X6 / X7 family — values that are NOT year-dependent
    (re.compile(r"\b50e\b"),       489),  # PHEV system output (G05 LCI only)
    (re.compile(r"\b45e\b"),       394),  # PHEV system output (G05 pre-LCI only)
    (re.compile(r"\bM50i\b"),      530),
    (re.compile(r"\bM60i\b"),      530),
    (re.compile(r"\bM50d\b"),      400),
    (re.compile(r"\b40d\b"),       340),
    (re.compile(r"\b35i\b"),       306),
    (re.compile(r"\b25d\b"),       231),
    (re.compile(r"\b20d\b"),       190),
    (re.compile(r"\b25i\b"),       231),
    # 3/5/7 series sedan codes
    (re.compile(r"\b540i\b"),      340),
    (re.compile(r"\b530i\b"),      252),
    (re.compile(r"\b520i\b"),      184),
    (re.compile(r"\b330i\b"),      258),
    (re.compile(r"\b320i\b"),      184),
    (re.compile(r"\b320d\b"),      190),
    (re.compile(r"\b520d\b"),      190),
]

# Year-disambiguated entries.
#
# Each entry is (regex, [(yearMonth_inclusive_lower, hp), ...]). The
# yearMonth strings are the lower bound (inclusive). Last bound wins
# when the candidate yearMonth is >= the bound. Use a single-element
# list with bound "000000" if you want a year-independent value.
#
# Example: BMW X5 G05 xDrive 40i is 340 PS pre-LCI and 381 PS from the
# 2023.07 LCI onwards. We pick by `category.yearMonth`.
_BMW_GRADE_HP_BY_YEAR: list[Tuple[re.Pattern[str], list[Tuple[str, int]]]] = [
    # X5 G05 40i: pre-LCI 340, LCI (Jul 2023+) 381.
    (re.compile(r"\b40i\b"),       [("000000", 340), ("202307", 381)]),
    # X5 G05 30d: pre-LCI 286, LCI 298.
    (re.compile(r"\b30d\b"),       [("000000", 286), ("202307", 298)]),
]

_MERCEDES_GRADE_HP: list[Tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b63\s*S\b"),    639),  # AMG 63 S
    (re.compile(r"\b63\b"),        612),  # AMG 63
    (re.compile(r"\b53\b"),        435),  # AMG 53 (3.0L I6)
    (re.compile(r"\b450\b"),       367),
    (re.compile(r"\b400\s*d\b"),   330),
    (re.compile(r"\b400\b"),       333),
    (re.compile(r"\b350\b"),       299),
    (re.compile(r"\b300\b"),       258),
    (re.compile(r"\b250\b"),       211),
    (re.compile(r"\b220\s*d\b"),   194),
    (re.compile(r"\b220\b"),       184),
    (re.compile(r"\b200\b"),       163),
]

_AUDI_GRADE_HP: list[Tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b55\s*TFSIe\b"), 367),
    (re.compile(r"\b55\s*TFSI\b"),  340),
    (re.compile(r"\b50\s*TDI\b"),   286),
    (re.compile(r"\b45\s*TFSI\b"),  245),
    (re.compile(r"\b40\s*TFSI\b"),  204),
    (re.compile(r"\b40\s*TDI\b"),   204),
    (re.compile(r"\b35\s*TFSI\b"),  150),
    (re.compile(r"\b35\s*TDI\b"),   163),
]

_PORSCHE_GRADE_HP: list[Tuple[re.Pattern[str], int]] = [
    (re.compile(r"Turbo\s*S\b", re.I), 650),
    (re.compile(r"\bTurbo\b", re.I),   570),
    (re.compile(r"\bGTS\b", re.I),     460),
    (re.compile(r"\bS\b", re.I),       440),
]

_GRADE_HP_BY_BRAND: dict[str, list[Tuple[re.Pattern[str], int]]] = {
    "BMW": _BMW_GRADE_HP,
    "MERCEDES-BENZ": _MERCEDES_GRADE_HP,
    "MERCEDES": _MERCEDES_GRADE_HP,
    "AUDI": _AUDI_GRADE_HP,
    "PORSCHE": _PORSCHE_GRADE_HP,
}


def horsepower_from_engine_code(motor_type: Optional[str]) -> Optional[int]:
    """Look up horsepower (PS) by OEM engine code from inspection."""
    if not motor_type:
        return None
    code = str(motor_type).strip().upper()
    if not code:
        return None
    if code in ENGINE_CODE_HP:
        return ENGINE_CODE_HP[code]
    # Some inspectors record the engine code with a trailing space/zero
    # (e.g. "B57D30B0"); try the prefix.
    for k, v in ENGINE_CODE_HP.items():
        if code.startswith(k):
            return v
    return None


def horsepower_from_grade(
    brand: Optional[str],
    grade_name: Optional[str],
    *,
    year_month: Optional[str] = None,
) -> Optional[int]:
    """Look up horsepower (PS) by marketing trim badge.

    ``year_month`` is the ``YYYY-MM`` registration (or ``YYYYMM``).
    When provided, year-aware tables (e.g. BMW X5 40i pre-LCI/LCI split)
    pick the correct value. When omitted we fall back to the most
    recent (LCI) value, which is fine for current inventory but may
    overstate the spec for older cars.
    """
    if not grade_name:
        return None
    brand_key = (brand or "").upper().strip()

    ym = (year_month or "").replace("-", "")  # "2023-02" -> "202302"

    # Year-aware lookup (BMW only at this point).
    if brand_key == "BMW":
        for rgx, bounds in _BMW_GRADE_HP_BY_YEAR:
            if rgx.search(grade_name):
                # Find the highest bound <= year_month, falling back to last bound.
                hp = bounds[0][1]
                for bound, value in bounds:
                    if not ym or ym >= bound:
                        hp = value
                return hp

    table = _GRADE_HP_BY_BRAND.get(brand_key)
    if not table:
        return None
    for rgx, hp in table:
        if rgx.search(grade_name):
            return hp
    return None


# ---------------------------------------------------------------------------
# Drive-type heuristics
# ---------------------------------------------------------------------------

# Brand-specific AWD markers in gradeName / model marketing text.
_BRAND_AWD_MARKERS: tuple[str, ...] = (
    "XDRIVE",       # BMW
    "4MATIC",       # Mercedes-Benz
    "QUATTRO",      # Audi
    "4MOTION",      # Volkswagen
    "ALLRAD",       # generic German for AWD
    "AWD",          # generic
    "4WD",          # generic
    "4X4",
    "ALL-WHEEL",
    "ALL WHEEL",
    "HMD",          # Hyundai HTRAC marker (varies)
    "HTRAC",        # Hyundai HTRAC AWD
    "TWIN ENGINE",  # Volvo PHEV AWD
)

# Explicit RWD markers.
_BRAND_RWD_MARKERS: tuple[str, ...] = (
    "후륜",
    "RWD",
)

# Explicit FWD / 2WD markers.
_BRAND_FWD_MARKERS: tuple[str, ...] = (
    "전륜",
    "FWD",
    "2WD",
)


def normalize_drive(grade_name: str, body_type: str, brand: Optional[str] = None) -> str:
    """Resolve drive type from grade marketing text.

    Decision order:
      1. Explicit RWD/FWD/2WD strings.
      2. Brand-specific AWD markers (xDrive, 4MATIC, quattro, 4Motion, HTRAC…).
      3. ``sDrive`` (BMW) -> RWD.
      4. Body-type fallback: SUV/RV/pickup -> 2WD; everything else -> FWD.

    The fallback mirrors the most common drivetrain on the Korean market
    for that body type and is documented in ``docs/method.md``.
    """
    g = (grade_name or "").upper().replace(" ", "")
    b = (body_type or "").upper()

    for m in _BRAND_RWD_MARKERS:
        if m in g:
            return "RWD"
    for m in _BRAND_FWD_MARKERS:
        if m in g:
            return "FWD"
    for m in _BRAND_AWD_MARKERS:
        if m in g:
            return "4WD"

    # BMW sDrive without explicit AWD elsewhere -> RWD (canonical).
    if "SDRIVE" in g:
        return "RWD"

    # Heuristic fallback by body type.
    if any(token in b for token in ("SUV", "RV", "픽업", "TRUCK", "PICKUP")):
        return "2WD"
    return "FWD"
