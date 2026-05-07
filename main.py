"""
OWI Encar parser — CLI entry point.

Usage:
    python main.py <ENCAR_URL>
        Parse a single listing and print JSON to stdout.

    python main.py --test-all
        Parse the three OWI test URLs and write the JSON to results/.

    python main.py -h
        Show help.

Exit codes:
    0  success (also when --test-all writes all three results)
    1  parser returned an {"error": ...} payload (non-fatal)
    2  invalid CLI usage
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

# On Windows the default code page may be cp1251/cp866 which cannot encode
# Korean characters. Reconfigure stdout/stderr to UTF-8 so JSON containing
# Korean trim names (e.g. "M 스포츠") prints correctly.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

from parser.encar_parser import ParseError, parse_encar_url

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"

TEST_URLS = [
    ("41453346", "https://fem.encar.com/cars/detail/41453346?listAdvType=share"),
    ("41443212", "https://fem.encar.com/cars/detail/41443212?listAdvType=share"),
    ("41687040", "https://fem.encar.com/cars/detail/41687040?listAdvType=share"),
]


def _run_one(url: str) -> Dict[str, Any]:
    """Return either the parsed dict or ``{"error": "..."}``."""
    try:
        return parse_encar_url(url)
    except ParseError as e:
        return {"error": e.message}
    except Exception as e:  # noqa: BLE001 — final safety net
        return {"error": f"Unexpected error: {type(e).__name__}: {e}"}


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="encar-parser",
        description="OWI Encar listing parser. Returns canonical OWI JSON.",
    )
    p.add_argument(
        "url",
        nargs="?",
        help="Encar listing URL (https://fem.encar.com/cars/detail/<id>?...)",
    )
    p.add_argument(
        "--test-all",
        action="store_true",
        help="Parse the three OWI test URLs and write the result of "
        "each to results/result_<id>.json",
    )
    return p


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)

    if args.test_all:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        all_ok = True
        for vid, url in TEST_URLS:
            print(f"[{vid}] {url}", file=sys.stderr)
            payload = _run_one(url)
            out = RESULTS_DIR / f"result_{vid}.json"
            out.write_text(_dump_json(payload), encoding="utf-8")
            status = "ERROR" if "error" in payload else "OK"
            print(f"[{vid}] -> {out.name}  [{status}]", file=sys.stderr)
            if "error" in payload:
                all_ok = False
        return 0 if all_ok else 1

    if not args.url:
        _build_parser().print_help(sys.stderr)
        return 2

    payload = _run_one(args.url)
    print(_dump_json(payload))
    return 1 if "error" in payload else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
