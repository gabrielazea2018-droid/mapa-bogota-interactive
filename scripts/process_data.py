#!/usr/bin/env python3
"""Turn the raw downloads in data/raw/ into the published layers in site/data/.

What this stage guarantees for the map builder:

* every layer is valid GeoJSON in EPSG:4326 inside the Bogotá envelope,
* polygon layers are simplified and rounded so the site stays small,
* point layers carry only the properties the map actually shows,
* each point is spatially joined to the localidad that contains it,
* the choropleth indicator (population per localidad, DANE projections
  distributed by the Secretaría Distrital del Hábitat) is merged onto the
  localidad polygons, with an explicit ``sin_dato`` flag for localities the
  source does not cover (Sumapal),
* a machine-readable legend (range + class breaks) is emitted so the
  JavaScript side never has to recompute statistics.

Usage:
    python scripts/process_data.py
    python scripts/process_data.py --raw data/raw --out site/data
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from shapely import STRtree
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import geo_utils as gu  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW = ROOT / "data" / "raw"
DEFAULT_OUT = ROOT / "site" / "data"

# Simplification is a quality/weight trade-off: 0.0004 deg is ~44 m at this
# latitude, invisible at the default city zoom but it removes most vertices.
TOLERANCES = {
    "localidades": 0.0004,
    "barrios": 0.0006,
    "parques": 0.0008,
}
COORD_PRECISION = 5  # ~1 m resolution, halves the payload vs. raw floats

POPULATION_INDICATOR = "Población"
HOUSEHOLDS_INDICATOR = "Hogares"
N_CLASSES = 6

# The Esri layer writes some localidad names in the short form while the
# indicator table (and everyday usage) use the full official name.
LOCALITY_ALIASES = {"candelaria": "la candelaria"}

# VOCACION arrives as an IDECA code; only the values whose meaning is
# unambiguous are decoded, the rest are shown as the code they are.
VOCATION_LABELS = {"SIN": "Sin vocación especial"}

PARK_LEVEL_LABELS = {
    "proximidad": "Parque de proximidad",
    "estructurante": "Parque estructurante",
    "estructurante propuesto": "Parque estructurante propuesto",
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def log(msg: str = "") -> None:
    print(msg, flush=True)


def slug(text: str) -> str:
    """Accent-free lowercase key used to join names from different sources."""
    text = "" if text is None else str(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().split())


def clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\t", " ").replace("\n", " ").strip().strip("'\"")
    return " ".join(text.split())


def as_number(value, kind=float):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return int(round(number)) if kind is int else number


def out_of_bbox(geom_dict: dict) -> bool:
    try:
        bounds = shape(geom_dict).bounds
    except Exception:  # noqa: BLE001
        return True
    return not (
        gu.BOGOTA_BBOX[0] <= bounds[0] <= gu.BOGOTA_BBOX[2]
        and gu.BOGOTA_BBOX[0] <= bounds[2] <= gu.BOGOTA_BBOX[2]
        and gu.BOGOTA_BBOX[1] <= bounds[1] <= gu.BOGOTA_BBOX[3]
        and gu.BOGOTA_BBOX[1] <= bounds[3] <= gu.BOGOTA_BBOX[3]
    )


# --------------------------------------------------------------------------- #
# indicator extraction
# --------------------------------------------------------------------------- #
def read_indicators(xlsx_path: Path) -> tuple[pd.DataFrame, int]:
    """Long-format indicator table filtered to the latest available year.

    Returns a frame with BOTH the accented display name (``localidad``) and a
    match key (``localidad_key``): the polygon layer stores names in
    uppercase without accents, so joining on the raw string would silently
    miss several localities.
    """
    df = pd.read_excel(xlsx_path, engine="openpyxl")
    required = {"indicador", "año", "localidad", "valor"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{xlsx_path.name}: missing columns {sorted(missing)}")

    target = df[df["indicador"] == POPULATION_INDICATOR]
    if target.empty:
        raise ValueError(
            f"{xlsx_path.name}: no rows for indicator {POPULATION_INDICATOR!r}; "
            f"available: {sorted(df['indicador'].unique())[:10]}"
        )
    year = int(target["año"].max())
    latest = df[(df["año"] == year) & (df["indicador"].isin([POPULATION_INDICATOR, HOUSEHOLDS_INDICATOR]))]
    wide = latest.pivot_table(index="localidad", columns="indicador", values="valor", aggfunc="first")
    wide = wide.reset_index()
    wide["localidad_key"] = wide["localidad"].map(slug)
    return wide, year


def match_locality(name: str, table: pd.DataFrame):
    """Find the indicator row for a polygon name, tolerating short/long forms."""
    key = LOCALITY_ALIASES.get(slug(name), slug(name))
    hit = table[table["localidad_key"] == key]
    if hit.empty:
        hit = table[table["localidad_key"] == f"la {key}"]
    if hit.empty:
        hit = table[table["localidad_key"] == key.removeprefix("la ")]
    return hit.iloc[0] if not hit.empty else None


def _jenks_breaks(values: list[float], n_classes: int) -> list[float]:
    """Fisher-Jenks natural breaks via dynamic programming (pure Python).

    Guarantees well-separated classes: with equal-count quantiles, several
    Bogotá localities cluster around ~400k inhabitants, which would produce
    adjacent legend steps almost the same colour. n is tiny (19 localities),
    so the O(k*n^2) search is negligible.
    """
    data = sorted(float(v) for v in values)
    n = len(data)
    if n < n_classes:
        return [data[0], data[-1]]

    cumulative = [0.0] * (n + 1)
    cumulative_sq = [0.0] * (n + 1)
    for i, value in enumerate(data, 1):
        cumulative[i] = cumulative[i - 1] + value
        cumulative_sq[i] = cumulative_sq[i - 1] + value * value

    def sse(start: int, end: int) -> float:
        """Sum of squared deviations of data[start:end]."""
        count = end - start
        if count <= 0:
            return 0.0
        total = cumulative[end] - cumulative[start]
        total_sq = cumulative_sq[end] - cumulative_sq[start]
        return total_sq - (total * total) / count

    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(n_classes + 1)]
    split = [[0] * (n + 1) for _ in range(n_classes + 1)]
    cost[0][0] = 0.0
    for classes in range(1, n_classes + 1):
        for end in range(classes, n + 1):
            for cut in range(classes - 1, end):
                candidate = cost[classes - 1][cut] + sse(cut, end)
                if candidate < cost[classes][end]:
                    cost[classes][end] = candidate
                    split[classes][end] = cut

    edges: list[float] = []
    cursor = n
    for classes in range(n_classes, 0, -1):
        cut = split[classes][cursor]
        edges.append(data[cut])
        cursor = cut
    edges.reverse()
    edges.append(data[-1])
    return sorted(set(edges))


def compute_breaks(values: list[float]) -> list[float]:
    """Choropleth class edges: Jenks first, quantiles, then equal interval.

    Population across Bogotá's localities spans two orders of magnitude
    (La Candelaria ~18k vs Suba ~1.27M), so equal-interval classes would paint
    almost the whole city in the palest colour. Every fallback guarantees at
    least 5 strictly increasing edges, which the map builder and the validator
    both rely on.
    """
    if len(values) < 2:
        return [float(min(values)), float(max(values))] if values else [0.0, 1.0]

    lo, hi = float(min(values)), float(max(values))

    for attempt in (_jenks_breaks(values, N_CLASSES), _quantile_breaks(values)):
        increasing = sorted(set(float(b) for b in attempt))
        if len(increasing) >= 5 and increasing[0] == lo and increasing[-1] == hi:
            return increasing

    # Last resort: equal spacing on a log scale keeps the pale class useful.
    step = (hi / lo) ** (1 / 5) if lo > 0 else 1.0
    return sorted(set([lo] + [lo * step**i for i in range(1, 5)] + [hi]))


def _quantile_breaks(values: list[float]) -> list[float]:
    series = pd.Series(sorted(values), dtype="float64")
    quantiles = [i / N_CLASSES for i in range(N_CLASSES + 1)]
    return [float(v) for v in series.quantile(quantiles).tolist()]


# --------------------------------------------------------------------------- #
# layer builders
# --------------------------------------------------------------------------- #
def build_localidades(raw: Path, population: pd.DataFrame, year: int):
    """20 localidad polygons enriched with the choropleth indicator."""
    fc = gu.load_feature_collection(raw / "localidades.geojson")

    features: list[dict] = []
    report: list[tuple[str, int | None, bool]] = []
    for feature in fc["features"]:
        props = feature.get("properties") or {}
        source_name = gu.get_property(props, ["LocNombre", "NOMBRE", "localidad"], "")
        code = gu.get_property(props, ["LocCodigo", "CODIGO", "codigo"], "")
        area_m2 = as_number(props.get("LocArea"), float)

        row = match_locality(source_name, population)
        has_data = row is not None
        poblacion = as_number(row.get(POPULATION_INDICATOR), int) if has_data else None
        hogares = as_number(row.get(HOUSEHOLDS_INDICATOR), int) if has_data else None
        if poblacion is None:
            has_data = False
            poblacion = 0
            hogares = None

        # Prefer the accented official spelling from the indicator table; fall
        # back to title-casing the raw uppercase layer value.
        display = clean_text(row["localidad"]) if has_data else source_name.title()

        superficie = round(area_m2 / 1_000_000, 3) if area_m2 and area_m2 > 1000 else None
        densidad = (
            round(poblacion / superficie, 1)
            if superficie and superficie > 0 and has_data
            else None
        )

        report.append((display, poblacion if has_data else None, not has_data))
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "localidad": display,
                    "codigo": code,
                    "poblacion": int(poblacion),
                    "hogares": hogares,
                    "anio": year if has_data else None,
                    "densidad": densidad,
                    "superficie_km2": superficie,
                    "sin_dato": not has_data,
                },
                "geometry": feature["geometry"],
            }
        )

    if len(features) != 20:
        raise ValueError(f"expected 20 localidades, got {len(features)}")

    simplified = gu.simplify_collection(
        {"type": "FeatureCollection", "features": features},
        TOLERANCES["localidades"],
        COORD_PRECISION,
    )
    return simplified, report


def build_barrios(raw: Path):
    fc = gu.load_feature_collection(raw / "barrios.geojson")
    features = []
    for feature in fc["features"]:
        props = feature.get("properties") or {}
        name = gu.get_property(props, ["NOMBRE", "barrio"], "")
        if not name or not feature.get("geometry"):
            continue
        features.append(
            {"type": "Feature", "properties": {"barrio": clean_text(name)},
             "geometry": feature["geometry"]}
        )
    if len(features) < 100:
        raise ValueError(f"only {len(features)} barrios survived filtering (expected >=100)")
    return gu.simplify_collection(
        {"type": "FeatureCollection", "features": features},
        TOLERANCES["barrios"],
        COORD_PRECISION,
    )


def _locality_tree(localidades: dict):
    """STRtree over localidad polygons + their names, for point-in-polygon."""
    polygons, names = [], []
    for feature in localidades["features"]:
        if not feature.get("geometry"):
            continue
        try:
            polygons.append(shape(feature["geometry"]))
        except Exception:  # noqa: BLE001
            continue
        names.append(feature["properties"].get("localidad", ""))
    return STRtree(polygons), names


def localidad_of(tree, names, geom_dict: dict) -> str:
    try:
        point = shape(geom_dict)
    except Exception:  # noqa: BLE001
        return ""
    # "intersects" is symmetric, so the predicate argument order cannot bite.
    hits = tree.query(point, predicate="intersects")
    for index in hits:
        index = int(index)
        if 0 <= index < len(names):
            return names[index]
    return ""


def build_points(raw: Path, source: str, mapping_spec: dict, tree, names):
    """Read one point layer and keep only the documented properties."""
    fc = gu.load_feature_collection(raw / source)
    features = []
    dropped = 0
    for feature in fc["features"]:
        geom = feature.get("geometry")
        if not geom or geom.get("type") not in {"Point", "MultiPoint"}:
            dropped += 1
            continue
        if out_of_bbox(geom):
            dropped += 1
            continue
        props = feature.get("properties") or {}
        out_props = {}
        for target, candidates in mapping_spec.items():
            out_props[target] = clean_text(gu.get_property(props, candidates))
        if not out_props["nombre"]:
            dropped += 1
            continue
        out_props["localidad"] = out_props.get("localidad") or localidad_of(tree, names, geom)
        features.append({"type": "Feature", "properties": out_props, "geometry": geom})
    if len(features) < 100:
        raise ValueError(f"{source}: only {len(features)} usable points (dropped {dropped})")
    return {"type": "FeatureCollection", "features": features}, dropped


def build_parques(raw: Path):
    fc = gu.load_feature_collection(raw / "parques_pot.geojson")
    features = []
    for feature in fc["features"]:
        props = feature.get("properties") or {}
        name = clean_text(gu.get_property(props, ["NOMBRE"], ""))
        if not name or not feature.get("geometry"):
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "nombre": name,
                    # TIPO_PARQUE is a coded field whose domain
                    # (cdom_tipo_parque) is only referenced, never published, in
                    # the SDP object catalogue; surfacing "4" or "28" would mean
                    # nothing to a reader, so the readable NIVEL drives the
                    # human label instead.
                    "tipo": PARK_LEVEL_LABELS.get(slug(gu.get_property(props, ["NIVEL"], "")), "Parque"),
                    "nivel": clean_text(gu.get_property(props, ["NIVEL"], "")),
                    "vocacion": VOCATION_LABELS.get(
                        clean_text(gu.get_property(props, ["VOCACION"], "")).upper(),
                        clean_text(gu.get_property(props, ["VOCACION"], "")),
                    ),
                },
                "geometry": feature["geometry"],
            }
        )
    if len(features) < 100:
        raise ValueError(f"only {len(features)} parques survived filtering")
    return gu.simplify_collection(
        {"type": "FeatureCollection", "features": features},
        TOLERANCES["parques"],
        COORD_PRECISION,
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    raw: Path = args.raw
    out: Path = args.out
    problems: list[str] = []

    required = ["localidades.geojson", "indicadores_demograficos.xlsx", "ips.geojson",
                "colegios.geojson", "parques_pot.geojson"]
    for name in required:
        if not (raw / name).exists():
            problems.append(f"missing raw input: {raw / name} (run scripts/fetch_data.py)")
    if problems:
        for item in problems:
            log(f"FAIL {item}")
        return 1

    out.mkdir(parents=True, exist_ok=True)
    processed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sizes: dict[str, int] = {}

    log("=" * 72)
    log("processing raw data -> site/data")
    log("=" * 72)

    # -- 1. indicator table ---------------------------------------------------
    population, year = read_indicators(raw / "indicadores_demograficos.xlsx")
    log(f"\n[indicators] {POPULATION_INDICATOR} year={year}, "
        f"{len(population)} localidades in source")

    # -- 2. localidades + choropleth join ------------------------------------
    localidades, join_report = build_localidades(raw, population, year)
    sizes["localidades.geojson"] = gu.write_geojson(localidades, out / "localidades.geojson")
    missing_names = [name for name, pop, is_missing in join_report if is_missing]
    log(f"[localidades] {len(localidades['features'])} features, "
        f"{sizes['localidades.geojson']:,} bytes")
    for name, pop, is_missing in join_report:
        log(f"    {'SIN DATO' if is_missing else f'{pop:>9,}'}  {name}")
    if missing_names:
        log(f"    -> no data in source for: {', '.join(missing_names)} (flagged sin_dato)")

    valid_values = [p for _, p, missing in join_report if not missing and p]
    if len(valid_values) < 15:
        problems.append(f"only {len(valid_values)} localidades have population data (need >=15)")

    # -- 3. choropleth statistics --------------------------------------------
    breaks = compute_breaks(valid_values)
    rango = [float(min(valid_values)), float(max(valid_values))]
    log(f"\n[choropleth] range={rango[0]:,.0f}..{rango[1]:,.0f} "
        f"breaks={[f'{b:,.0f}' for b in breaks]}")

    # -- 4. barrios -----------------------------------------------------------
    barrios = build_barrios(raw)
    sizes["barrios.geojson"] = gu.write_geojson(barrios, out / "barrios.geojson")
    log(f"\n[barrios] {len(barrios['features'])} features, {sizes['barrios.geojson']:,} bytes")

    # -- 5. point layers ------------------------------------------------------
    tree, names = _locality_tree(localidades)

    ips, ips_dropped = build_points(
        raw, "ips.geojson",
        {"nombre": ["nombre", "nombre_pre"], "tipo": ["naturaleza", "clase_pres"],
         "direccion": ["direccion"], "barrio": ["barrio"], "localidad": []},
        tree, names,
    )
    sizes["ips.geojson"] = gu.write_geojson(ips, out / "ips.geojson")
    log(f"[ips] {len(ips['features'])} points ({ips_dropped} dropped), "
        f"{sizes['ips.geojson']:,} bytes")

    colegios, col_dropped = build_points(
        raw, "colegios.geojson",
        {"nombre": ["NOMBRE_EST"], "sede": ["NOMBRE_SED"], "direccion": ["DIRECCION"],
         "localidad": ["COD_LOCA"], "orden": ["ORDEN_DE_S"]},
        tree, names,
    )
    # COD_LOCA is a numeric code (e.g. "01"), not a name: replace with the
    # spatial result so the popup shows a human-readable localidad.
    code_to_name = {str(f["properties"].get("codigo")): f["properties"].get("localidad", "")
                    for f in localidades["features"]}
    for feature in colegios["features"]:
        code = feature["properties"].get("localidad", "")
        feature["properties"]["localidad"] = code_to_name.get(code) or localidad_of(
            tree, names, feature["geometry"]
        )
    sizes["colegios.geojson"] = gu.write_geojson(colegios, out / "colegios.geojson")
    log(f"[colegios] {len(colegios['features'])} points ({col_dropped} dropped), "
        f"{sizes['colegios.geojson']:,} bytes")

    parques = build_parques(raw)
    sizes["parques.geojson"] = gu.write_geojson(parques, out / "parques.geojson")
    log(f"[parques] {len(parques['features'])} features, {sizes['parques.geojson']:,} bytes")

    # -- 6. indicators.json ---------------------------------------------------
    localidad_rows = []
    for feature in localidades["features"]:
        props = feature["properties"]
        localidad_rows.append(
            {
                "localidad": props["localidad"],
                "poblacion": props["poblacion"],
                "hogares": props["hogares"],
                "sin_dato": props["sin_dato"],
            }
        )
    localidad_rows.sort(key=lambda r: r["localidad"])

    provenance = json.loads((raw / "provenance.json").read_text(encoding="utf-8"))
    indicator_doc = {
        "meta": {
            "indicador": POPULATION_INDICATOR,
            "unidad": "personas",
            "anio": year,
            "fuente": ("Proyecciones DANE, distribución por localidad — Secretaría "
                       "Distrital del Hábitat, 'Hábitat en cifras'"),
            "fuente_url": provenance["datasets"].get("poblacion_localidad", {}).get("dataset_url"),
            "rango": rango,
            "breaks": breaks,
            "metodo": "Jenks natural breaks (Fisher, programación dinámica)",
            "sin_dato_label": "Sin dato",
        },
        "localidades": localidad_rows,
    }
    (out / "indicators.json").write_text(
        json.dumps(indicator_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sizes["indicators.json"] = (out / "indicators.json").stat().st_size

    # -- 7. provenance --------------------------------------------------------
    provenance["processed_utc"] = processed_at
    provenance["pipeline"] = {
        "simplification_deg": TOLERANCES,
        "coordinate_precision": COORD_PRECISION,
        "choropleth_breaks": breaks,
        "outputs": sizes,
    }
    (out / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sizes["provenance.json"] = (out / "provenance.json").stat().st_size

    # -- summary --------------------------------------------------------------
    total = sum(sizes.values())
    log("\n" + "=" * 72)
    for name, size in sorted(sizes.items()):
        log(f"  {name:24s} {size:>12,} bytes")
    log(f"  {'TOTAL':24s} {total:>12,} bytes")
    raw_total = sum(p.stat().st_size for p in raw.glob('*') if p.is_file() and p.name != 'provenance.json')
    log(f"  {'raw inputs':24s} {raw_total:>12,} bytes "
        f"(reduction to {100 * total / max(raw_total, 1):.1f}%)")

    if total > 8 * 1024 * 1024:
        problems.append(f"site/data total {total:,} bytes exceeds the 8 MB budget")
    if problems:
        log("\nFAILURES:")
        for item in problems:
            log(f"  - {item}")
        return 1

    log("\nProcessing OK — site/data is ready for scripts/build_map.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
