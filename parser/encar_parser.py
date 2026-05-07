"""
Encar listing parser.

Strategy
--------
1. Resolve the numeric ``vehicleId`` from any Encar URL.
2. Pull the canonical record from the official mobile-app readside API:
     ``https://api.encar.com/v1/readside/vehicle/<id>?include=...``
   Returns JSON, no JS challenge, no Cloudflare.
3. Pull damage details from ``/v1/readside/inspection/vehicle/<id>``
   (the body-diagram endpoint).  404 means there is no inspection
   (e.g. on lease listings) -> ``damages = []``.
4. Pull insurance / pledge counters from the open record endpoint.
5. Normalize fields into the OWI schema.

If ANY step that returns the BASE record fails the parser raises
``ParseError`` and the caller emits ``{"error": "..."}``.
The damage / insurance endpoints are best-effort: their absence is
not a failure (they are simply missing for some listing types).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

from parser.engine_specs import (
    horsepower_from_engine_code,
    horsepower_from_grade,
    normalize_drive,
)
from parser.utils import (
    DAMAGE_TYPE_BY_CODE,
    extract_vehicle_id,
    map_part,
    normalize_body,
    normalize_fuel,
    normalize_year_month,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_HOST = "https://api.encar.com"

# All sub-records we want from the readside endpoint, in one round-trip.
READSIDE_INCLUDE = ",".join(
    [
        "ADVERTISEMENT",
        "CATEGORY",
        "CONDITION",
        "CONTACT",
        "MANAGE",
        "OPTIONS",
        "PHOTOS",
        "SPEC",
        "PARTNERSHIP",
        "CENTER",
        "VIEW",
    ]
)

# Browser-like headers. The official Encar mobile webview sends these
# values when calling api.encar.com — matching them avoids 403.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://fem.encar.com",
    "Referer": "https://fem.encar.com/",
    "Cache-Control": "no-cache",
}

DEFAULT_TIMEOUT_S = 20
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ParseError(Exception):
    """Single error class that always carries a human-readable message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def _get_json(
    session: requests.Session,
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_S,
    allow_404: bool = False,
) -> Optional[dict]:
    """GET ``url`` and return the decoded JSON.

    * ``None`` is returned only when ``allow_404=True`` AND the server
      replied with 404 (genuinely absent sub-record).
    * Transient errors (429 / 5xx) are retried with linear backoff.
    * Anything else -> ``ParseError``.
    """
    last_err: Optional[str] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"network error: {e}"
            log.warning("attempt %s failed for %s: %s", attempt, url, e)
            time.sleep(0.7 * attempt)
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as e:
                raise ParseError(
                    f"Encar API returned 200 but body is not JSON ({e}). "
                    f"URL: {url}"
                ) from e

        if r.status_code == 404 and allow_404:
            return None

        if r.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
            log.warning(
                "attempt %s: HTTP %s on %s, retrying", attempt, r.status_code, url
            )
            time.sleep(0.7 * attempt)
            continue

        # non-retryable or out of retries
        if r.status_code == 403:
            raise ParseError(
                "Encar returned HTTP 403 (Forbidden). "
                "The IP may be rate-limited or the readside API requires updated headers."
            )
        if r.status_code == 404:
            raise ParseError(
                "Encar returned HTTP 404 - listing does not exist or has been removed."
            )
        if r.status_code == 429:
            raise ParseError(
                "Encar returned HTTP 429 (Too Many Requests). Reduce request rate or rotate IP."
            )
        raise ParseError(
            f"Encar API returned HTTP {r.status_code}. URL: {url}. "
            f"Body preview: {r.text[:200]!r}"
        )

    raise ParseError(f"Failed to reach Encar API after {MAX_RETRIES} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Endpoint shortcuts
# ---------------------------------------------------------------------------

def _fetch_base(session: requests.Session, vid: int) -> dict:
    url = f"{API_HOST}/v1/readside/vehicle/{vid}?include={READSIDE_INCLUDE}"
    payload = _get_json(session, url, allow_404=False)
    if not isinstance(payload, dict) or not payload:
        raise ParseError(f"Empty payload from readside API for vehicle {vid}.")
    return payload


def _fetch_inspection(session: requests.Session, vid: int) -> Optional[dict]:
    """Body-diagram inspection. Absent for lease/rent listings."""
    url = f"{API_HOST}/v1/readside/inspection/vehicle/{vid}"
    return _get_json(session, url, allow_404=True)


def _fetch_record(session: requests.Session, vid: int) -> Optional[dict]:
    """Insurance / accident counters. Absent for some listings."""
    url = f"{API_HOST}/v1/readside/record/vehicle/{vid}/open"
    return _get_json(session, url, allow_404=True)


def _fetch_view(session: requests.Session, vid: int) -> Optional[dict]:
    """Compact `vehicles/view` shape. Has spec.horsePower (when populated)."""
    url = f"{API_HOST}/v1/readside/vehicles/view?vehicleIds={vid}"
    payload = _get_json(session, url, allow_404=True)
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first
    return None


# ---------------------------------------------------------------------------
# Helpers for safe field access
# ---------------------------------------------------------------------------

def _require(d: dict, key: str, ctx: str) -> Any:
    """Raise a clear ParseError if ``key`` is missing or None."""
    if not isinstance(d, dict):
        raise ParseError(f"{ctx}: expected dict, got {type(d).__name__}")
    if key not in d or d[key] is None:
        raise ParseError(f"{ctx}: missing required field '{key}'")
    return d[key]


def _coalesce_str(*vals: object) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


_HANGUL_RE = re.compile(r"[\uAC00-\uD7A3]+")


def _strip_hangul(text: str) -> str:
    """Remove Hangul syllables from text, keep latin/numbers/punct."""
    if not text:
        return ""
    s = _HANGUL_RE.sub("", text)
    # normalize whitespace left after removal
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Damages
# ---------------------------------------------------------------------------

def _build_damages(inspection: Optional[dict]) -> List[Dict[str, str]]:
    """Build the OWI ``damages`` list from inspection.outers.

    Encar's inspection JSON ``outers`` array contains every panel that
    the inspector marked on the body diagram.  Each entry has a
    ``statusTypes`` array with codes:

        X -> 교환/교체   (replace, RED marker)
        W -> 판금        (sheet metal, BLUE marker)
        T -> 도장        (paint, BLUE marker)

    Per OWI spec, only blue/red markers (paint or replace) are reported.
    """
    if not isinstance(inspection, dict):
        return []

    outers = inspection.get("outers") or []
    if not isinstance(outers, list):
        return []

    out: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in outers:
        if not isinstance(entry, dict):
            continue

        type_obj = entry.get("type") or {}
        code = str(type_obj.get("code") or "").strip().upper()
        kr_title = str(type_obj.get("title") or "").strip() or None

        # statusTypes is a LIST — Encar can stack multiple markers on one
        # part (e.g., painted AND replaced). We pick the strongest one
        # (replace > paint).
        status_codes = []
        for st in entry.get("statusTypes") or []:
            if isinstance(st, dict) and st.get("code"):
                status_codes.append(str(st["code"]).strip().upper())

        damage_type: Optional[str] = None
        if any(c == "X" for c in status_codes):
            damage_type = "replace"
        elif any(c in ("W", "T") for c in status_codes):
            damage_type = "paint"
        else:
            # fall back to single-code lookup table (covers any future code)
            for c in status_codes:
                if c in DAMAGE_TYPE_BY_CODE:
                    damage_type = DAMAGE_TYPE_BY_CODE[c]
                    break

        if not damage_type:
            continue

        part_label = map_part(code, kr_title)
        key = (part_label, damage_type)
        if key in seen:
            continue
        seen.add(key)
        out.append({"part": part_label, "type": damage_type})

    return out


# ---------------------------------------------------------------------------
# Pledge / sold / leasing flags
# ---------------------------------------------------------------------------

# Encar advertisementType strings observed:
#   NORMAL              — regular sale
#   OPERATING_LEASE     — operating lease (assignment)
#   FINANCIAL_LEASE     — financial lease
#   RENT / LONG_RENT    — long-term rent
_LEASING_TYPES = {"OPERATING_LEASE", "FINANCIAL_LEASE", "LEASE", "RENT", "LONG_RENT"}


def _is_leasing(advertisement: dict, partnership: Optional[dict]) -> bool:
    if not isinstance(advertisement, dict):
        return False

    ad_type = str(advertisement.get("advertisementType") or "").upper()
    if any(token in ad_type for token in ("LEASE", "RENT")):
        return True
    if ad_type in _LEASING_TYPES:
        return True

    lri = advertisement.get("leaseRentInfo")
    if isinstance(lri, dict) and lri:
        return True

    if isinstance(partnership, dict):
        if partnership.get("lease") or partnership.get("rent"):
            return True
    return False


def _is_sold_or_pledge(advertisement: dict, condition: dict, price_10k: int) -> bool:
    # 9999 만원 (= 99,990,000 KRW) is a placeholder Encar uses for
    # reserved/under-deposit listings — see OWI spec.
    if price_10k == 9999:
        return True

    if isinstance(condition, dict):
        seizing = condition.get("seizing") or {}
        if isinstance(seizing, dict):
            for k in ("pledgeCount", "seizingCount"):
                v = seizing.get(k)
                if isinstance(v, int) and v > 0:
                    return True

    if isinstance(advertisement, dict):
        status = str(advertisement.get("status") or "").upper()
        if status in ("SOLD", "RESERVED", "DEAL_DONE"):
            return True

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_encar_url(
    url_or_id: str,
    *,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Parse an Encar listing URL and return the OWI JSON dict.

    On any input/network/data failure ``ParseError`` is raised.
    The CLI layer turns that into ``{"error": message}`` — never a traceback.
    """
    try:
        vid = extract_vehicle_id(url_or_id)
    except ValueError as e:
        raise ParseError(str(e)) from e

    own_session = session is None
    sess = session or _make_session()

    try:
        base = _fetch_base(sess, vid)

        category = _require(base, "category", "readside.category")
        spec = _require(base, "spec", "readside.spec")
        advertisement = _require(base, "advertisement", "readside.advertisement")
        condition = base.get("condition") or {}
        partnership = base.get("partnership") or {}

        # ---- price -----------------------------------------------------
        price_10k_raw = _require(advertisement, "price", "advertisement.price")
        try:
            price_10k = int(price_10k_raw)
        except (TypeError, ValueError) as e:
            raise ParseError(f"Bad price value: {price_10k_raw!r}") from e
        price_krw = price_10k * 10_000

        # ---- brand / model --------------------------------------------
        brand = _coalesce_str(category.get("manufacturerName"))
        if not brand:
            raise ParseError("category.manufacturerName is empty")

        model_name = _coalesce_str(category.get("modelName"))
        grade_name = _coalesce_str(category.get("gradeName"))
        grade_detail = _coalesce_str(category.get("gradeDetailName"))

        # Prefer English grade names when available to avoid Korean text in `model`.
        grade_en = _coalesce_str(category.get("gradeEnglishName"))
        grade_detail_en = _coalesce_str(category.get("gradeDetailEnglishName"))
        grade_for_model = grade_en or grade_name
        grade_detail_for_model = grade_detail_en or grade_detail

        model_full = " ".join(p for p in (model_name, grade_for_model, grade_detail_for_model) if p)
        # Final safety: if Encar returned Korean text, strip Hangul so the output stays ASCII/Latin.
        model_full = _strip_hangul(model_full)
        if not model_full:
            raise ParseError("Cannot derive model name (modelName/gradeName missing)")

        # ---- engine ----------------------------------------------------
        try:
            engine_cc = int(_require(spec, "displacement", "spec.displacement"))
        except (TypeError, ValueError) as e:
            raise ParseError(
                f"Bad engine displacement: {spec.get('displacement')!r}"
            ) from e

        # Inspection (body diagram + master.detail.motorType). Best-effort:
        # 404 for lease listings is normal; we use this for both the
        # horsepower resolver and the damages array below.
        inspection = _fetch_inspection(sess, vid)
        motor_type = (
            ((inspection or {}).get("master") or {}).get("detail") or {}
        ).get("motorType")

        # ---- horsepower (3-layer resolver, see docs/method.md) ---------
        # Layer 1: any structured horsePower-like field on the readside spec.
        horsepower: Optional[int] = None
        for k in ("horsepower", "horsePower", "maxOutput", "enginePower", "outputPs", "powerPs"):
            v = spec.get(k)
            if isinstance(v, int) and v > 0:
                horsepower = v
                break

        # Layer 2: dedicated /vehicles/view endpoint (separate Encar mobile shape).
        if horsepower is None:
            view = _fetch_view(sess, vid)
            if isinstance(view, dict):
                v = (view.get("spec") or {}).get("horsePower")
                if isinstance(v, int) and v > 0:
                    horsepower = v

        # Layer 3a: OEM engine code from inspection (e.g. BMW B58B30S -> 489).
        if horsepower is None:
            horsepower = horsepower_from_engine_code(motor_type)

        # Layer 3b: marketing trim badge (e.g. "xDrive 50e" -> 489).
        if horsepower is None:
            full_grade = " ".join(p for p in (grade_name, grade_detail) if p)
            horsepower = horsepower_from_grade(
                brand, full_grade, year_month=str(category.get("yearMonth") or "")
            )

        # ---- fuel ------------------------------------------------------
        try:
            fuel_type = normalize_fuel(spec.get("fuelName") or spec.get("fuelCd"))
        except ValueError as e:
            raise ParseError(str(e)) from e

        # ---- drive (heuristic - see normalize_drive docstring) ---------
        drive = normalize_drive(
            grade_name + " " + grade_detail,
            str(spec.get("bodyName") or ""),
            brand=brand,
        )

        # ---- body ------------------------------------------------------
        try:
            body_type = normalize_body(spec.get("bodyName"))
        except ValueError as e:
            raise ParseError(str(e)) from e

        # ---- registration ---------------------------------------------
        try:
            first_registration = normalize_year_month(
                _require(category, "yearMonth", "category.yearMonth")
            )
        except ValueError as e:
            raise ParseError(str(e)) from e

        # ---- mileage ---------------------------------------------------
        try:
            mileage_km = int(_require(spec, "mileage", "spec.mileage"))
        except (TypeError, ValueError) as e:
            raise ParseError(f"Bad mileage value: {spec.get('mileage')!r}") from e

        # ---- flags -----------------------------------------------------
        is_leasing = _is_leasing(advertisement, partnership)
        is_sold_or_pledge = _is_sold_or_pledge(advertisement, condition, price_10k)

        # ---- damages (uses already-fetched inspection) -----------------
        damages = _build_damages(inspection)

        # Best-effort: surface insurance flag if the open-record endpoint
        # tells us this car had a major accident. We DO NOT add it to the
        # ``damages`` array (per OWI spec, that array is only the body
        # diagram), but we keep the call here so a future maintainer can
        # easily expose it as a separate field.
        _ = _fetch_record(sess, vid)

        return {
            "price_krw": price_krw,
            "brand": brand,
            "model": model_full,
            "engine_cc": engine_cc,
            "horsepower": horsepower,
            "fuel_type": fuel_type,
            "drive": drive,
            "body_type": body_type,
            "first_registration": first_registration,
            "mileage_km": mileage_km,
            "is_leasing": is_leasing,
            "is_sold_or_pledge": is_sold_or_pledge,
            "damages": damages,
        }
    finally:
        if own_session:
            sess.close()
