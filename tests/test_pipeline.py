"""Hermetic tests for the Bogotá map pipeline.

These tests NEVER touch the network and NEVER depend on data/raw/ existing:
every fixture is written into pytest's tmp_path.

Note: pytest itself is intentionally NOT in requirements.txt (it is a
development-only tool). Install it with:  python -m pip install pytest
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
VALIDATE = SCRIPTS_DIR / "validate.py"

# Bogotá is roughly lon [-74.46, -73.99], lat [3.70, 4.80]. The fixtures below
# stay well inside that box so any reasonable bbox check accepts them.
BOGOTA_BBOX = (-74.46, 3.70, -73.99, 4.80)

ALLOWED_FORMATS = {"geojson", "json", "csv", "xlsx"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def run(args: list[str], cwd: Path = REPO_ROOT, timeout: int = 180) -> subprocess.CompletedProcess:
    """Run a subprocess and return it. Assertion messages must include the
    captured output so failures are easy to debug."""
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def output_of(proc: subprocess.CompletedProcess) -> str:
    return (
        f"\nexit code: {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_localidades_geojson(out_of_bounds: bool = False) -> dict:
    """A 2-feature FeatureCollection in lon/lat (WGS84)."""
    if out_of_bounds:
        # A polygon clearly outside any Bogotá bbox (longitude -60, Brazil).
        ring = [[-60.5, -20.5], [-60.4, -20.5], [-60.4, -20.4], [-60.5, -20.4], [-60.5, -20.5]]
        ring2 = [[-60.3, -20.3], [-60.2, -20.3], [-60.2, -20.2], [-60.3, -20.2], [-60.3, -20.3]]
    else:
        # Two small squares inside urban Bogotá.
        ring = [[-74.09, 4.60], [-74.07, 4.60], [-74.07, 4.62], [-74.09, 4.62], [-74.09, 4.60]]
        ring2 = [[-74.08, 4.63], [-74.06, 4.63], [-74.06, 4.65], [-74.08, 4.65], [-74.08, 4.63]]

    return {
        "type": "FeatureCollection",
        "name": "localidades",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [
            {
                "type": "Feature",
                "properties": {"localidad": "Teusaquillo", "nombre": "Teusaquillo", "loc_cod": "08"},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            },
            {
                "type": "Feature",
                "properties": {"localidad": "Chapinero", "nombre": "Chapinero", "loc_cod": "03"},
                "geometry": {"type": "Polygon", "coordinates": [ring2]},
            },
        ],
    }


def make_indicators() -> dict:
    return {
        "meta": {
            "variable": "poblacion",
            "titulo": "Población proyectada por localidad",
            "unidad": "habitantes",
            "rango": {"min": 1000, "max": 90000},
            "breaks": [1000, 23000, 45000, 68000, 90000],
            "proyecciones": "DANE",
        },
        "localidades": {
            "Teusaquillo": {"poblacion": 25000, "nombre": "Teusaquillo"},
            "Chapinero": {"poblacion": 80000, "nombre": "Chapinero"},
        },
    }


def make_provenance() -> dict:
    return {
        "datasets": {},
        "fetched_utc": "2026-01-01T00:00:00+00:00",
        "catalog": "https://datosabiertos.bogota.gov.co/api/3/action",
    }


def make_index_html() -> str:
    """A fake but structurally valid Folium page that satisfies the checks in
    scripts/validate.py."""
    return """<!DOCTYPE html>
<html lang="es-CO">
<head>
  <meta charset="utf-8">
  <title>Mapa interactivo de Bogotá</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>
  <div id="map" style="width: 100%; height: 100vh;"></div>
  <script>
    var map = L.map('map').setView([4.6486, -74.0558], 11);
    L.control.layers({}, {"OpenStreetMap": L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png')}).addTo(map);
    fetch('data/localidades.geojson').then(function (r) { return r.json(); })
      .then(function (geojson) { L.geoJSON(geojson).addTo(map); });
  </script>
  <script type="text/javascript">
    var basemap = 'https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png';
  </script>
</body>
</html>
"""


def make_site(root: Path, out_of_bounds: bool = False) -> Path:
    """Build a synthetic site/ tree inside `root` and return its path."""
    site = root / "site"
    (site / "data").mkdir(parents=True, exist_ok=True)

    write_json(site / "data" / "localidades.geojson", make_localidades_geojson(out_of_bounds))
    write_json(site / "data" / "indicators.json", make_indicators())
    write_json(site / "data" / "provenance.json", make_provenance())
    write_json(site / "provenance.json", make_provenance())

    (site / "index.html").write_text(make_index_html(), encoding="utf-8")
    return site


def run_validate(site: Path) -> subprocess.CompletedProcess:
    if not VALIDATE.exists():
        pytest.fail(
            "scripts/validate.py does not exist yet "
            "(still being written by another agent)."
        )
    return run(["scripts/validate.py", "--site", str(site)])


# --------------------------------------------------------------------------- #
# validate.py smoke tests
# --------------------------------------------------------------------------- #
def test_validate_accepts_synthetic_site(tmp_path: Path) -> None:
    """A well-formed synthetic site must pass validate.py (exit code 0)."""
    site = make_site(tmp_path)
    proc = run_validate(site)
    assert proc.returncode == 0, (
        "scripts/validate.py rejected a well-formed synthetic site "
        + output_of(proc)
    )


def test_validate_rejects_out_of_bounds_geometry(tmp_path: Path) -> None:
    """The bbox/feature-count logic must reject features outside Bogotá."""
    site = make_site(tmp_path, out_of_bounds=True)
    proc = run_validate(site)
    assert proc.returncode == 1, (
        "scripts/validate.py should exit 1 for an out-of-bounds polygon "
        + output_of(proc)
    )


# --------------------------------------------------------------------------- #
# scripts/sources.yaml manifest schema
# --------------------------------------------------------------------------- #
def test_sources_yaml_schema() -> None:
    manifest_path = SCRIPTS_DIR / "sources.yaml"
    assert manifest_path.exists(), f"missing manifest: {manifest_path}"

    doc = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "sources.yaml must parse to a mapping"

    datasets = doc.get("datasets")
    assert isinstance(datasets, dict) and datasets, "sources.yaml declares no datasets"
    assert len(datasets) >= 6, (
        f"expected at least 6 datasets, found {len(datasets)}: {sorted(datasets)}"
    )

    required_keys = {"url", "out", "format", "provider", "license"}
    for key, spec in datasets.items():
        assert isinstance(spec, dict), f"dataset '{key}' must be a mapping"
        missing = required_keys - set(spec)
        assert not missing, f"dataset '{key}' is missing keys: {sorted(missing)}"

        url = spec["url"]
        assert isinstance(url, str) and url.startswith("https://"), (
            f"dataset '{key}' must use an https:// url, got: {url!r}"
        )
        assert spec["format"] in ALLOWED_FORMATS, (
            f"dataset '{key}' has unknown format {spec['format']!r}; "
            f"allowed: {sorted(ALLOWED_FORMATS)}"
        )
        assert isinstance(spec["out"], str) and spec["out"], (
            f"dataset '{key}' has an empty 'out' filename"
        )


# --------------------------------------------------------------------------- #
# fetch_data.py importability (no network: just --help)
# --------------------------------------------------------------------------- #
def test_fetch_data_help_exits_zero() -> None:
    fetch = SCRIPTS_DIR / "fetch_data.py"
    assert fetch.exists(), f"missing script: {fetch}"

    proc = run(["scripts/fetch_data.py", "--help"], timeout=60)
    assert proc.returncode == 0, (
        "python scripts/fetch_data.py --help failed "
        "(module import error? missing dependency?) " + output_of(proc)
    )
    assert "usage" in (proc.stdout + proc.stderr).lower(), (
        "--help produced no usage text " + output_of(proc)
    )
