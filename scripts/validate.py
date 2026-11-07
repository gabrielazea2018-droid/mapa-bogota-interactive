#!/usr/bin/env python3
"""Validate the generated Bogota interactive map (contract checks 1-9).

The script inspects ``<site>/index.html`` plus ``<site>/data/`` and runs the
nine acceptance checks of the pipeline contract.  It prints one
``[PASS]``/``[FAIL]`` line per check, a ``N passed / M failed`` summary and
exits 0 only when every check passed, so CI can gate on it.

Usage:
    python scripts/validate.py [--site DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

DEFAULT_SITE = Path(__file__).resolve().parents[1] / "site"

# Must match BOGOTA_BBOX in scripts/geo_utils.py: the pipeline only publishes
# features inside this box, so validate.py applies exactly the same box.
# (min_lon, min_lat, max_lon, max_lat)
BOGOTA_BBOX = (-75.0, 3.5, -73.5, 5.5)

DATA_FILES = (
    "localidades.geojson",
    "barrios.geojson",
    "ips.geojson",
    "colegios.geojson",
    "parques.geojson",
    "indicators.json",
    "provenance.json",
)

GEOJSON_FILES = tuple(name for name in DATA_FILES if name.endswith(".geojson"))

# Minimum number of records each layer must carry in the real dataset.
MIN_COUNTS = {"ips": 1000, "colegios": 1000, "parques": 100, "barrios": 100}
LOCALIDADES_EXPECTED = 20

MAX_HTML_BYTES = 500 * 1024          # check 1: 500 KB
MAX_DATA_BYTES = 8 * 1024 * 1024     # check 8: 8 MB
INLINE_FEATURECOLLECTION = '"type":"FeatureCollection"'  # check 2
SIN_DATO_LIMIT = 1                   # check 7
REQUIRED_MARKERS = ("fetch(", "L.control.layers", "services.arcgisonline.com", "es-CO")
TEMPLATE_MARKERS = ("{{ ", "{%")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def load_json(path: Path):
    """Read a JSON file, raising FileNotFoundError/JSONDecodeError on trouble."""
    return json.loads(path.read_text(encoding="utf-8"))


def feature_count(path: Path) -> int:
    """Number of features in a GeoJSON file (0 for anything else)."""
    obj = load_json(path)
    if obj.get("type") == "FeatureCollection":
        return len(obj.get("features") or [])
    return 0


def iter_lon_lat(node):
    """Yield every (lon, lat) pair found anywhere in a coordinates array."""
    if not isinstance(node, (list, tuple)):
        return
    if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
        yield float(node[0]), float(node[1])
        return
    for child in node:
        yield from iter_lon_lat(child)


def geojson_bbox(path: Path):
    """Return (min_lon, min_lat, max_lon, max_lat) of a GeoJSON file."""
    obj = load_json(path)
    coords = []
    if obj.get("type") == "FeatureCollection":
        for feat in obj.get("features") or []:
            geom = feat.get("geometry") or {}
            coords.extend(iter_lon_lat(geom.get("coordinates")))
    else:
        coords.extend(iter_lon_lat(obj.get("coordinates")))
    if not coords:
        return None
    lons = [lon for lon, _ in coords]
    lats = [lat for _, lat in coords]
    return min(lons), min(lats), max(lons), max(lats)


def bbox_inside(bbox) -> bool:
    """True when a bbox lies completely inside the Bogota reference bbox."""
    min_lon, min_lat, max_lon, max_lat = bbox
    ref_min_lon, ref_min_lat, ref_max_lon, ref_max_lat = BOGOTA_BBOX
    return (
        min_lon >= ref_min_lon
        and max_lon <= ref_max_lon
        and min_lat >= ref_min_lat
        and max_lat <= ref_max_lat
    )


def dir_size(path: Path) -> int:
    """Total bytes of every file below ``path`` (0 when it does not exist)."""
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Checks 1-9
# ---------------------------------------------------------------------------


def check_html_size(html_path: Path):
    """Check 1: index.html exists and is smaller than 500 KB."""
    if not html_path.is_file():
        return False, f"{html_path} does not exist"
    size = html_path.stat().st_size
    if size >= MAX_HTML_BYTES:
        return False, f"index.html is {size:,} bytes (limit {MAX_HTML_BYTES:,})"
    return True, f"index.html is {size:,} bytes (< {MAX_HTML_BYTES:,})"


def check_no_inline_geojson(html_path: Path, data_dir: Path):
    """Check 2: no FeatureCollection inlined, HTML smaller than the payload."""
    if not html_path.is_file():
        return False, "index.html does not exist"
    text = html_path.read_text(encoding="utf-8", errors="replace")
    if INLINE_FEATURECOLLECTION in text:
        return False, "index.html embeds a FeatureCollection inline"
    geojsons = sorted(data_dir.glob("*.geojson"))
    if not geojsons:
        return False, f"no .geojson files found under {data_dir}"
    payload = sum(p.stat().st_size for p in geojsons)
    size = html_path.stat().st_size
    if size >= payload:
        return (
            False,
            f"index.html ({size:,} B) is not smaller than the GeoJSON payload "
            f"({payload:,} B)",
        )
    return True, (
        f"index.html ({size:,} B) < GeoJSON payload ({payload:,} B); "
        "no inline FeatureCollection"
    )


def check_markers(html_path: Path):
    """Check 3: runtime loader and Spanish locale markers are present."""
    if not html_path.is_file():
        return False, "index.html does not exist"
    text = html_path.read_text(encoding="utf-8", errors="replace")
    missing = [m for m in REQUIRED_MARKERS if m not in text]
    if missing:
        return False, "missing marker(s): " + ", ".join(repr(m) for m in missing)
    return True, "found " + ", ".join(REQUIRED_MARKERS)


def check_bbox(data_dir: Path):
    """Check 4: the 7 data files exist, parse, and the geolayers sit in Bogota."""
    missing = [name for name in DATA_FILES if not (data_dir / name).is_file()]
    if missing:
        return False, "missing data file(s): " + ", ".join(missing)
    problems = []
    for name in DATA_FILES:
        if not name.endswith(".json"):
            continue
        try:
            load_json(data_dir / name)
        except (OSError, ValueError) as exc:
            problems.append(f"{name}: unreadable JSON ({exc})")
    for name in GEOJSON_FILES:
        bbox = geojson_bbox(data_dir / name)
        if bbox is None:
            problems.append(f"{name}: no coordinates found")
        elif not bbox_inside(bbox):
            problems.append(
                "{}: lon {:.3f}..{:.3f} lat {:.3f}..{:.3f} outside Bogota".format(
                    name, bbox[0], bbox[2], bbox[1], bbox[3]
                )
            )
    if problems:
        return False, "; ".join(problems)
    return True, (
        f"all {len(DATA_FILES)} data files present; "
        f"{len(GEOJSON_FILES)} geolayers inside the Bogota bbox"
    )


def check_localidades(data_dir: Path):
    """Check 5: exactly 20 localidades."""
    path = data_dir / "localidades.geojson"
    if not path.is_file():
        return False, "localidades.geojson does not exist"
    count = feature_count(path)
    if count != LOCALIDADES_EXPECTED:
        return False, f"found {count} localidades (expected {LOCALIDADES_EXPECTED})"
    return True, f"found {count} localidades"


def check_counts(data_dir: Path):
    """Check 6: layer record-count floors (ips/colegios 1000, parques/barrios 100)."""
    problems = []
    summary = []
    for layer, minimum in MIN_COUNTS.items():
        path = data_dir / f"{layer}.geojson"
        if not path.is_file():
            problems.append(f"{layer}: file missing")
            continue
        count = feature_count(path)
        summary.append(f"{layer}={count}")
        if count < minimum:
            problems.append(f"{layer}: {count} records < {minimum}")
    if problems:
        return False, "; ".join(problems)
    return True, ", ".join(summary)


def check_sin_dato(data_dir: Path):
    """Check 7: at most one localidad flagged ``sin_dato`` (Sumapaz has no indicators)."""
    path = data_dir / "localidades.geojson"
    if not path.is_file():
        return False, "localidades.geojson does not exist"
    features = load_json(path).get("features") or []
    flagged = []
    for index, feat in enumerate(features):
        props = feat.get("properties") or {}
        if props.get("sin_dato"):
            label = props.get("localidad") or props.get("nombre") or f"feature {index}"
            flagged.append(str(label))
    if len(flagged) > SIN_DATO_LIMIT:
        return (
            False,
            f"{len(flagged)} localidades flagged sin_dato "
            f"(limit {SIN_DATO_LIMIT}): " + ", ".join(flagged),
        )
    return True, (
        f"{len(flagged)} localidad(es) flagged sin_dato "
        f"(limit {SIN_DATO_LIMIT})"
    )


def check_data_size(data_dir: Path):
    """Check 8: the data directory is 8 MB or smaller."""
    if not data_dir.is_dir():
        return False, f"{data_dir} does not exist"
    size = dir_size(data_dir)
    if size > MAX_DATA_BYTES:
        return False, f"data dir is {size:,} bytes (limit {MAX_DATA_BYTES:,})"
    return True, f"data dir is {size:,} bytes (<= {MAX_DATA_BYTES:,})"


def check_no_template_syntax(html_path: Path):
    """Check 9: no Jinja template syntax leaked into the published page."""
    if not html_path.is_file():
        return False, "index.html does not exist"
    text = html_path.read_text(encoding="utf-8", errors="replace")
    leaked = [m for m in TEMPLATE_MARKERS if m in text]
    if leaked:
        return False, "template syntax found: " + ", ".join(repr(m) for m in leaked)
    return True, "no '{{ ' or '{%' in index.html"


CHECKS = (
    ("check 1: index.html exists and is under 500 KB", check_html_size, ("html",)),
    (
        "check 2: no inline GeoJSON; HTML smaller than the data payload",
        check_no_inline_geojson,
        ("html", "data"),
    ),
    ("check 3: runtime-loader markers present", check_markers, ("html",)),
    ("check 4: 7 data files present; geolayers in the Bogota bbox", check_bbox, ("data",)),
    ("check 5: exactly 20 localidades", check_localidades, ("data",)),
    ("check 6: layer record-count floors", check_counts, ("data",)),
    ("check 7: at most one localidad flagged 'sin_dato'", check_sin_dato, ("data",)),
    ("check 8: data directory is 8 MB or smaller", check_data_size, ("data",)),
    ("check 9: no template syntax leaked into the site", check_no_template_syntax, ("html",)),
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the generated Bogota map (contract checks 1-9)."
    )
    parser.add_argument(
        "--site",
        type=Path,
        default=DEFAULT_SITE,
        help="site directory holding index.html and data/ (default: <repo>/site)",
    )
    args = parser.parse_args(argv)

    site = args.site
    context = {"html": site / "index.html", "data": site / "data"}
    print(f"Validating site: {site}")

    passed = failed = 0
    for label, func, params in CHECKS:
        try:
            ok, detail = func(*(context[p] for p in params))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {label} -> {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"{passed} passed / {failed} failed")
    if failed:
        print("SITE NOT OK - fix the failed checks before publishing")
        return 1
    print("SITE OK - publishable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
