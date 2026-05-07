"""
Helpers and lookup tables for Encar field normalization.

All mappings here are kept as plain dictionaries so that:
  * tests can import them and exercise edge cases,
  * a future maintainer can add a new code in one place
    without touching the parsing pipeline.
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

# Encar runs two front-ends:
#   * https://fem.encar.com/cars/detail/<id>            (current React SPA)
#   * https://www.encar.com/dc/dc_cardetailview.do?...  (legacy, query string)
# Vehicle id is the only piece we actually need to hit api.encar.com.
_FEM_RE = re.compile(r"https?://fem\.encar\.com/cars/detail/(\d+)", re.IGNORECASE)
_LEGACY_RE = re.compile(r"[?&]carid=(\d+)", re.IGNORECASE)
_BARE_ID_RE = re.compile(r"^\s*(\d{6,12})\s*$")


def extract_vehicle_id(url_or_id: str) -> int:
    """Return numeric vehicle id from any Encar URL form (or bare id)."""
    if not url_or_id:
        raise ValueError("URL is empty")

    s = str(url_or_id).strip()
    for rgx in (_FEM_RE, _LEGACY_RE, _BARE_ID_RE):
        m = rgx.search(s)
        if m:
            return int(m.group(1))
    raise ValueError(
        f"Cannot find vehicle id in URL: {s!r}. "
        "Expected https://fem.encar.com/cars/detail/<ID>?... "
        "or https://www.encar.com/dc/dc_cardetailview.do?carid=<ID>"
    )


# ---------------------------------------------------------------------------
# Year / month
# ---------------------------------------------------------------------------

def normalize_year_month(value: object) -> str:
    """Encar exposes registration period as YYYYMM (e.g. ``"202506"``).

    We return ``"YYYY-MM"``. Raises ``ValueError`` on malformed input.
    """
    s = str(value).strip()
    m = re.fullmatch(r"(\d{4})(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    # tolerate already-normalized "2025-06"
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        return s
    raise ValueError(f"Unexpected year/month value: {value!r}")


# ---------------------------------------------------------------------------
# Fuel
# ---------------------------------------------------------------------------

# Order matters: compound types ("X+전기") are checked BEFORE single-fuel
# matches because "가솔린+전기" both contains "가솔" and "전기" — by spec
# such a combination is a (plug-in) hybrid.
def normalize_fuel(value: object) -> str:
    """Map Korean fuel name from ``spec.fuelName`` to one of the four
    canonical strings expected by the OWI schema.

    Encar values seen in the wild:
        가솔린         -> gasoline
        디젤           -> diesel
        하이브리드     -> hybrid
        전기           -> electric
        가솔린+전기    -> hybrid   (plug-in hybrid, e.g. BMW xDrive 50e)
        디젤+전기      -> hybrid
        LPG            -> gasoline (closest match; documented as best-effort)
    """
    s = str(value or "").strip()
    if not s:
        raise ValueError("fuelName is empty")

    # explicit hybrid first — covers PHEV listed by Encar as "가솔린+전기" etc.
    if "하이브" in s or "+" in s or "hybrid" in s.lower():
        return "hybrid"
    if s == "전기" or "전기" in s and "가솔" not in s and "디젤" not in s:
        return "electric"
    if "가솔" in s or "gasoline" in s.lower():
        return "gasoline"
    if "디젤" in s or "diesel" in s.lower():
        return "diesel"
    # LPG and other niche fuels are not in the OWI taxonomy. We pick the
    # closest neighbour explicitly so the mismatch is obvious in the docs.
    if "LPG" in s.upper() or "가스" in s:
        return "gasoline"

    raise ValueError(f"Unknown fuelName: {value!r}")


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------
# `normalize_drive` is implemented in `parser.engine_specs` together with
# the engine-code horsepower lookup, since both share the brand-trim
# vocabulary. Re-exported here so existing imports keep working.
from parser.engine_specs import normalize_drive  # noqa: F401, E402  (public re-export)


# ---------------------------------------------------------------------------
# Body type
# ---------------------------------------------------------------------------

# Encar exposes Korean body labels for some imports and English for others.
# We canonicalize to the most widely used English short forms so the JSON
# is comparable across listings.
_BODY_MAP = {
    "세단": "Sedan",
    "쿠페": "Coupe",
    "해치백": "Hatchback",
    "왜건": "Wagon",
    "컨버터블": "Convertible",
    "SUV": "SUV",
    "RV": "RV",
    "미니밴": "Minivan",
    "벤": "Van",
    "밴": "Van",
    "픽업": "Pickup",
    "트럭": "Truck",
    "스포츠카": "Sportscar",
}


def normalize_body(value: object) -> str:
    s = str(value or "").strip()
    if not s:
        raise ValueError("bodyName is empty")
    if s in _BODY_MAP:
        return _BODY_MAP[s]
    # Many Encar listings already store English body names ("SUV", "Sedan"…)
    return s


# ---------------------------------------------------------------------------
# Damages — inspection diagram
# ---------------------------------------------------------------------------

# Encar inspection codes for the body diagram.
# Reference (from Encar inspection schema V200922):
#   X — 교환 / 교체  (replace)         RED on the diagram
#   W — 판금          (sheet metal)    BLUE on the diagram
#   T — 도장          (paint)          BLUE on the diagram
#   C — 부식          (corrosion)      not blue/red — ignored per OWI spec
#   A — 흠집          (scratch)        ignored
#   U — 요철          (dent)           ignored
DAMAGE_TYPE_BY_CODE = {
    "X": "replace",
    "W": "paint",
    "T": "paint",
}


# Map of Encar body-diagram part codes (P0xx) to short English labels.
# Sourced from the public Encar inspection diagram. We keep both English
# and the original Korean title in the output (English in ``part``, original
# Korean in ``part_kr`` when available).
PART_CODE_MAP = {
    "P001": "Hood",
    "P002": "Front fender (right)",
    "P003": "Front door (right)",
    "P004": "Rear door (right)",
    "P005": "Rear fender (right)",
    "P006": "Trunk lid",
    "P007": "Roof panel",
    "P008": "Rear fender (left)",
    "P009": "Rear door (left)",
    "P010": "Front door (left)",
    "P011": "Front fender (left)",
    "P012": "Radiator support",
    "P013": "Cowl panel",
    "P014": "Front panel",
    "P015": "Cross member",
    "P016": "Inside panel (left)",
    "P017": "Inside panel (right)",
    "P018": "Rear panel",
    "P019": "Trunk floor",
    "P020": "Package tray",
    "P021": "Front fender (left)",
    "P022": "Front fender (right)",
    "P023": "Side sill panel (left)",
    "P024": "Side sill panel (right)",
    "P025": "Wheel house (left)",
    "P026": "Wheel house (right)",
    "P027": "Pillar A (left)",
    "P028": "Pillar A (right)",
    "P029": "Pillar B (left)",
    "P030": "Pillar B (right)",
    "P031": "Pillar C (left)",
    "P032": "Pillar C (right)",
    "P033": "Side member (left)",
    "P034": "Side member (right)",
    "P035": "Floor panel",
    "P036": "Dash panel",
}


def map_part(code: str, fallback_kr_title: Optional[str] = None) -> str:
    """Return human-readable part name for a body-diagram code.

    Falls back to the Korean title from the inspection JSON if the code
    is not in our mapping (Encar occasionally adds new codes).
    """
    code = (code or "").strip().upper()
    if code in PART_CODE_MAP:
        return PART_CODE_MAP[code]
    if fallback_kr_title:
        return fallback_kr_title
    return code or "unknown"
