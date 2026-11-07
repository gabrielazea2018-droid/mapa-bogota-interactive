#!/usr/bin/env python3
"""Geography helpers shared by the Bogotá map pipeline.

Why this module exists
----------------------
Several Bogotá open-data resources are advertised as "GeoJSON" in the CKAN
catalogue but are actually **Esri JSON** (`geometryType: esriGeometryPolygon`,
features shaped as ``{"attributes": {...}, "geometry": {"rings": [...]}}``).
Hand-verified the hard way during the first fetch — see BITACORA.md.

Everything here is pure Python + shapely: no geopandas/fiona/pyproj, so the
behaviour is identical on Windows and on ubuntu-latest CI.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon, mapping, shape

try:  # pyproj is only needed when a source ships projected coordinates
    from pyproj import CRS as _CRS
    from pyproj import Transformer as _Transformer
    from shapely.ops import transform as _shapely_transform

    HAS_PYPROJ = True
except Exception:  # noqa: BLE001 - degrade to the strict geographic-only path
    HAS_PYPROJ = False

# WGS84 and the Colombian datum MAGNA-SIRGAS geographic are interchangeable at
# web-map scale (they differ by well under a metre), so both are accepted as
# "already in lon/lat" without any datum transformation.
GEOGRAPHIC_WKIDS = {4326, 4686, 4269, 4110}

# Rough sanity envelope for Bogotá D.C. and its rural periphery.
BOGOTA_BBOX = (-75.0, 3.5, -73.5, 5.5)  # min_lon, min_lat, max_lon, max_lat


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def detect_format(doc: Any) -> str:
    """Return ``geojson``, ``esri`` or ``unknown`` for a parsed JSON document."""
    if isinstance(doc, dict):
        if doc.get("type") in {"FeatureCollection", "Feature", "GeometryCollection"}:
            return "geojson"
        if "features" in doc and ("geometryType" in doc or "spatialReference" in doc):
            return "esri"
        if "rings" in doc or "paths" in doc or ("x" in doc and "y" in doc):
            return "esri"
    return "unknown"


# --------------------------------------------------------------------------- #
# Esri JSON -> GeoJSON
# --------------------------------------------------------------------------- #
def _source_transformer(doc: dict):
    """Return a pyproj transformer to EPSG:4326, or ``None`` if already lon/lat.

    Handles the two real cases found in Bogotá's catalogue: resources declared
    as EPSG:4686 (geographic, no-op) and resources stored in a local projected
    system such as ``PCS_CarMAGBOG`` — a Transverse Mercator in metres, whose
    WKT is embedded in the payload itself.
    """
    sr = doc.get("spatialReference") or {}
    if not sr:
        # Undeclared CRS on a Bogotá dataset is overwhelmingly lon/lat.
        return None

    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid is not None and int(wkid) in GEOGRAPHIC_WKIDS:
        return None

    if not HAS_PYPROJ:
        raise ValueError(
            f"source uses projected coordinates (wkid={wkid!r}) but pyproj is not "
            "installed; run `pip install -r requirements.txt`"
        )

    if sr.get("wkt"):
        source = _CRS.from_wkt(sr["wkt"])
    elif wkid is not None:
        source = _CRS.from_epsg(int(wkid))
    else:
        return None  # no usable declaration: assume lon/lat

    return _Transformer.from_crs(source, _CRS.from_epsg(4326), always_xy=True)


def _rings_to_polygons(rings: Iterable[list[list[float]]]) -> list[Polygon]:
    """Turn Esri rings into shapely polygons, resolving holes by containment.

    Esri rings carry no explicit shell/hole flag that is guaranteed to be
    ordered, so the robust rule is used: a ring whose representative point
    sits inside an odd number of other rings is a hole, otherwise it is a
    shell. Depth-based, no reliance on ring winding order.
    """
    cleaned: list[Polygon] = []
    for ring in rings:
        pts = [(float(x), float(y)) for x, y in (p[:2] for p in ring)]
        if len(pts) < 4:
            continue
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        try:
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                continue
            if poly.geom_type == "MultiPolygon":
                cleaned.extend(list(poly.geoms))
            elif poly.geom_type == "Polygon":
                cleaned.append(poly)
        except Exception:  # noqa: BLE001 - malformed ring, drop rather than crash
            continue
    if not cleaned:
        return []
    if len(cleaned) == 1:
        return cleaned

    probes = [p.representative_point() for p in cleaned]
    depths = [
        sum(1 for j, other in enumerate(cleaned) if i != j and other.contains(pr))
        for i, pr in enumerate(probes)
    ]

    shells = [p for p, d in zip(cleaned, depths) if d % 2 == 0]
    holes = [p for p, d in zip(cleaned, depths) if d % 2 == 1]
    if not shells:  # degenerate: treat the biggest ring as the shell
        shells = [max(cleaned, key=lambda g: g.area)]

    built: list[Polygon] = []
    for shell in shells:
        inner = [h.exterior.coords for h in holes if shell.contains(h.representative_point())]
        built.append(Polygon(shell.exterior.coords, inner) if inner else shell)
    return built


def esri_geometry_to_geojson(geometry: dict, geometry_type: str) -> dict | None:
    """Convert one Esri geometry object into a GeoJSON geometry dict."""
    if not isinstance(geometry, dict):
        return None

    if geometry_type == "esriGeometryPoint" or ("x" in geometry and "y" in geometry):
        return mapping(Point(float(geometry["x"]), float(geometry["y"])))

    if geometry_type == "esriGeometryMultipoint" or "points" in geometry:
        pts = [(float(x), float(y)) for x, y in geometry["points"]]
        return mapping(MultiPoint(pts)) if len(pts) > 1 else mapping(Point(pts[0]))

    if geometry_type == "esriGeometryPolyline" or "paths" in geometry:
        lines = [
            LineString([(float(x), float(y)) for x, y in path])
            for path in geometry.get("paths") or []
            if len(path) >= 2
        ]
        if not lines:
            return None
        return mapping(MultiLineString(lines)) if len(lines) > 1 else mapping(lines[0])

    if geometry_type == "esriGeometryPolygon" or "rings" in geometry:
        polys = _rings_to_polygons(geometry.get("rings") or [])
        if not polys:
            return None
        return mapping(MultiPolygon(polys)) if len(polys) > 1 else mapping(polys[0])

    return None


def esri_to_feature_collection(doc: dict) -> dict:
    """Normalise an Esri JSON layer document into a GeoJSON FeatureCollection.

    Coordinates are reprojected to EPSG:4326 in a single pass when the source
    declares a projected system (see ``_source_transformer``).
    """
    transformer = _source_transformer(doc)
    aliases: dict[str, str] = doc.get("fieldAliases") or {}
    geometry_type = doc.get("geometryType", "")

    features: list[dict] = []
    for item in doc.get("features", []):
        geom = esri_geometry_to_geojson(item.get("geometry") or {}, geometry_type)
        if geom is None:
            continue
        if transformer is not None:
            try:
                geom = _shapely_transform(transformer.transform, shape(geom)).__geo_interface__
            except Exception as exc:  # noqa: BLE001 - skip the bad feature, keep the layer
                raise ValueError(f"reprojection failed for one feature: {exc}") from exc
        attributes = item.get("attributes") or {}
        # Keep the human-readable alias when the field name is an opaque code
        # (LocNombre -> "Nombre de la localidad"); raw keys are preserved too.
        properties = dict(attributes)
        for key, alias in aliases.items():
            if key in properties and alias and alias != key:
                properties.setdefault(f"{key}__alias", alias)
        features.append({"type": "Feature", "properties": properties, "geometry": geom})

    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------------------------------- #
# loading / normalising
# --------------------------------------------------------------------------- #
def _looks_like_web_mercator(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> bool:
    """True when bounds are metre coordinates inside the EPSG:3857 envelope.

    Several Bogotá exports ship Web Mercator coordinates inside a file whose
    root says "GeoJSON" (which by spec must be lon/lat) and declare no CRS at
    all. The signature is unambiguous: values far beyond +/-180 but inside the
    +-20037508.34 m world extent.
    """
    limit = 20037508.342789244
    inside_envelope = (
        abs(min_lon) <= limit
        and abs(max_lon) <= limit
        and abs(min_lat) <= limit
        and abs(max_lat) <= limit
    )
    beyond_degrees = max(abs(min_lon), abs(max_lon)) > 180.0
    return inside_envelope and beyond_degrees


def reproject_collection(fc: dict, src_epsg: int, dst_epsg: int = 4326) -> dict:
    """Reproject every geometry of a FeatureCollection with pyproj."""
    if not HAS_PYPROJ:
        raise ValueError(
            f"collection is in EPSG:{src_epsg} but pyproj is not installed; "
            "run `pip install -r requirements.txt`"
        )
    transformer = _Transformer.from_crs(_CRS.from_epsg(src_epsg), _CRS.from_epsg(dst_epsg), always_xy=True)
    out: list[dict] = []
    for feature in fc.get("features", []):
        geom = feature.get("geometry")
        if not geom:
            continue
        try:
            moved = _shapely_transform(transformer.transform, shape(geom))
            out.append(
                {"type": "Feature", "properties": feature.get("properties") or {},
                 "geometry": mapping(moved)}
            )
        except Exception:  # noqa: BLE001 - drop unusable geometry, keep the layer alive
            continue
    return {"type": "FeatureCollection", "features": out}


def load_feature_collection(path: str | Path) -> dict:
    """Read a file that may be GeoJSON *or* Esri JSON and return GeoJSON."""
    path = Path(path)
    doc = json.loads(path.read_bytes().decode("utf-8-sig"))
    kind = detect_format(doc)
    if kind == "geojson":
        fc = doc if doc["type"] == "FeatureCollection" else {"type": "FeatureCollection", "features": [doc]}
        # RFC 7946 mandates lon/lat, but mislabelled exports are common here.
        bounds = geometry_bounds(fc)
        if not _is_lonlat(*bounds):
            if _looks_like_web_mercator(*bounds):
                fc = reproject_collection(fc, 3857)
                bounds = geometry_bounds(fc)
            else:
                raise ValueError(
                    f"{path.name}: coordinates are outside lon/lat range "
                    f"({bounds[0]:.1f},{bounds[1]:.1f})-({bounds[2]:.1f},{bounds[3]:.1f}) "
                    "and do not match Web Mercator either; the file is projected "
                    "but declares no CRS"
                )
        if not _is_bogota(*bounds):
            raise ValueError(
                f"{path.name}: bounds {tuple(round(v, 4) for v in bounds)} fall outside "
                "the Bogotá envelope - refusing to publish a misplaced layer"
            )
        return fc
    if kind == "esri":
        return esri_to_feature_collection(doc)
    raise ValueError(
        f"{path.name}: unsupported JSON structure (root keys: {list(doc)[:8]})"
    )


def _is_lonlat(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> bool:
    return (
        -180.0 <= min_lon <= 180.0
        and -180.0 <= max_lon <= 180.0
        and -90.0 <= min_lat <= 90.0
        and -90.0 <= max_lat <= 90.0
    )


def _is_bogota(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> bool:
    """Loose envelope: the whole district plus a safety margin."""
    return (
        BOGOTA_BBOX[0] <= min_lon <= BOGOTA_BBOX[2]
        and BOGOTA_BBOX[0] <= max_lon <= BOGOTA_BBOX[2]
        and BOGOTA_BBOX[1] <= min_lat <= BOGOTA_BBOX[3]
        and BOGOTA_BBOX[1] <= max_lat <= BOGOTA_BBOX[3]
    )


def get_property(properties: dict, candidates: list[str], default: str = "") -> str:
    """Case-insensitive first hit among candidate field names."""
    lowered = {str(k).lower(): v for k, v in properties.items()}
    for name in candidates:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip() not in {"", "None", "nan", "NaN"}:
            return str(value).strip()
    return default


# --------------------------------------------------------------------------- #
# geometry post-processing
# --------------------------------------------------------------------------- #
def iter_geometries(fc: dict) -> Iterable[dict]:
    for feature in fc.get("features", []):
        geom = feature.get("geometry")
        if geom:
            yield geom


def geometry_bounds(fc: dict) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) of the whole collection."""
    min_lon = min_lat = math.inf
    max_lon = max_lat = -math.inf
    for geom in iter_geometries(fc):
        try:
            g = shape(geom)
            if g.is_empty:
                continue
            x0, y0, x1, y1 = g.bounds
        except Exception:  # noqa: BLE001
            continue
        min_lon, min_lat = min(min_lon, x0), min(min_lat, y0)
        max_lon, max_lat = max(max_lon, x1), max(max_lat, y1)
    if min_lon is math.inf:
        raise ValueError("collection has no usable geometry")
    return min_lon, min_lat, max_lon, max_lat


def simplify_collection(
    fc: dict,
    tolerance_deg: float,
    precision: int = 5,
    keep_points: bool = True,
) -> dict:
    """Simplify and round coordinates to keep the published payload small.

    ``preserve_topology`` avoids the self-intersections that naive
    Douglas-Peucker creates on tight urban polygons; point layers are left
    untouched by default (there is nothing to simplify).
    """
    from shapely.geometry import mapping as _mapping

    out: list[dict] = []
    for feature in fc.get("features", []):
        geom = feature.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
            if g.is_empty:
                continue
            if keep_points and g.geom_type not in {"Point", "MultiPoint"} and tolerance_deg > 0:
                g = g.simplify(tolerance_deg, preserve_topology=True)
                if g.is_empty:
                    continue
            cleaned = _round_geometry(_mapping(g), precision)
            out.append(
                {"type": "Feature", "properties": feature.get("properties") or {}, "geometry": cleaned}
            )
        except Exception:  # noqa: BLE001 - drop unusable geometry, keep the pipeline alive
            continue
    return {"type": "FeatureCollection", "features": out}


def _round_geometry(geom: dict, precision: int) -> dict:
    def walk(node: Any) -> Any:
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                return [round(float(v), precision) for v in node]
            return [walk(child) for child in node]
        return node

    return {**geom, "coordinates": walk(geom.get("coordinates"))}


def write_geojson(fc: dict, path: str | Path, indent: int | None = None) -> int:
    """Serialise without ASCII escaping (Spanish accents must stay readable)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(fc, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
    path.write_text(payload, encoding="utf-8")
    return path.stat().st_size
