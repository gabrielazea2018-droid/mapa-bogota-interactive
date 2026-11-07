#!/usr/bin/env python3
"""Build site/index.html: the interactive map of Bogotá.

The generated page is a single self-contained HTML document (inline CSS and
inline JS) except for two kinds of runtime requests made by the BROWSER:

  * the Esri Light Gray Canvas basemap tiles, and
  * the five GeoJSON documents under site/data/, downloaded with
    ``fetch('data/<file>.geojson')`` when the page opens.

Nothing is embedded on purpose: parques.geojson alone is ~14 MB raw, so
inlining any feature array would blow the 500 KB budget for index.html.
All the runtime loading lives in one custom ``branca.element.MacroElement``
subclass (BogotaRuntimeMap) whose Jinja template emits the panels, the CSS
and the JavaScript.

Usage:
    python scripts/build_map.py
    python scripts/build_map.py --data-dir DIR --out FILE
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

import branca.colormap as bc
import folium
from branca.element import MacroElement
from folium.plugins import MarkerCluster
from folium.template import Template

# Windows consoles default to cp1252: emit UTF-8 so accents print correctly.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "site" / "data"
DEFAULT_OUT = ROOT / "site" / "index.html"

# Map view: Bogotá city centre.
CENTER = [4.6486, -74.0590]
ZOOM = 11

# Basemap: Esri Light Gray Canvas (light, cartographic) plus its label overlay.
# CARTO now answers every unauthenticated basemap request with an
# "API KEY REQUIRED" placeholder tile - verified 2026-11: light_all, @2x and
# rastertiles all return the same 2,049-byte image - so the basemap moved to
# Esri's keyless Canvas tiles. The browser requests them at view time; Python
# never touches the network here.
TILE_URL = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/"
    "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
)
LABELS_URL = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/"
    "World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}"
)
TILE_ATTRIBUTION = (
    'Sources: <a href="https://www.esri.com" target="_blank" '
    'rel="noopener noreferrer">Esri</a>, HERE, Garmin, FAO, NOAA, USGS, '
    '<a href="https://www.openstreetmap.org/copyright" target="_blank" '
    'rel="noopener noreferrer">OpenStreetMap</a> contributors, '
    "and the GIS User Community"
)

# Inputs produced by scripts/process_data.py. Every one of them must exist.
DATA_FILES = (
    "localidades.geojson",
    "barrios.geojson",
    "ips.geojson",
    "colegios.geojson",
    "parques.geojson",
    "indicators.json",
    "provenance.json",
)

# ColorBrewer-style sequential ramp (YlGnBu family): readable over a light
# basemap, single family of hues, and branca turns it into the exact number of
# steps the class breaks require.
RAMP_COLORS = ["#ffffd9", "#c7e9b4", "#7fcdbb", "#41b6c4", "#2c7fb8", "#253494"]

# Marker colours: mid/dark tones with a white ring so they stay legible on the
# pale canvas tiles and on top of the choropleth.
COLOR_SALUD = "#d62828"  # red    - Salud (IPS)
COLOR_EDUCACION = "#6d28d9"  # purple - Educación (colegios)
COLOR_PARQUES = "#15803d"  # green  - Parques

# Popup field order for each point layer: (property, Spanish label).
POINT_FIELDS = {
    "ips": [
        ("nombre", "Nombre"),
        ("tipo", "Tipo"),
        ("direccion", "Dirección"),
        ("barrio", "Barrio"),
        ("localidad", "Localidad"),
    ],
    "colegios": [
        ("nombre", "Nombre"),
        ("sede", "Sede"),
        ("direccion", "Dirección"),
        ("localidad", "Localidad"),
        ("orden", "Orden"),
    ],
    "parques": [
        ("nombre", "Nombre"),
        ("tipo", "Tipo"),
        ("nivel", "Nivel"),
        ("vocacion", "Vocación"),
    ],
}


class BuildError(Exception):
    """A clear, user-facing build failure (reported without a traceback)."""


# --------------------------------------------------------------------------- #
# formatting helpers (Spanish UI text is built here, in Python)
# --------------------------------------------------------------------------- #
def esc(value: object) -> str:
    """HTML-escape a value coming from the provenance/indicator metadata."""
    return html.escape(str(value), quote=True)


def format_es(value: float, decimals: int = 0) -> str:
    """Format a number the es-CO way: 1234567 -> '1.234.567'."""
    if decimals:
        text = f"{float(value):,.{decimals}f}"
    else:
        text = f"{float(value):,.0f}"
    whole, _, fraction = text.partition(".")
    thousands = whole.replace(",", ".")
    return f"{thousands},{fraction}" if fraction else thousands


def date_label(fetched_utc: str) -> str:
    """Short 'YYYY-MM-DD' label out of an ISO-8601 timestamp (or a fallback)."""
    stamp = str(fetched_utc or "").strip()
    return stamp[:10] if len(stamp) >= 10 else "desconocida"


# --------------------------------------------------------------------------- #
# panel markup (Spanish)
# --------------------------------------------------------------------------- #
def build_left_column(meta: dict, fetched_utc: str) -> str:
    """Title panel + click info panel, stacked in the top-left corner."""
    anio = meta.get("anio")
    indicator = meta.get("indicador") or "Población por localidad"
    unidad = meta.get("unidad")

    parts = [f"Indicador: {indicator}"]
    if unidad:
        parts.append(f"({unidad})")
    if anio:
        parts.append(f"· {anio}")
    parts.append(f"· Datos al {date_label(fetched_utc)} (UTC)")

    title = (
        '<div id="bog-title" class="bog-panel">'
        "<h1>Mapa interactivo de Bogotá</h1>"
        '<p class="bog-sub">Localidades, barrios, salud, educación y parques'
        " de la ciudad</p>"
        f'<p class="bog-meta">{esc(" ".join(parts))}</p>'
        '<button type="button" id="bog-reset" class="bog-button">'
        "Restablecer vista</button>"
        "</div>"
    )
    info = (
        '<div id="bog-info" class="bog-panel">'
        "<h2>Ficha de la localidad</h2>"
        '<div id="bog-info-body">'
        '<p class="bog-hint">Selecciona una localidad en el mapa para ver su'
        " ficha.</p>"
        "</div>"
        '<button type="button" id="bog-info-close" class="bog-button">Cerrar</button>'
        "</div>"
    )
    return f'<div id="bog-left">{title}{info}</div>'


def build_legend(meta: dict, breaks: list, colors: list) -> str:
    """Bottom-right legend: population ranges + 'Sin dato' + point markers."""
    anio = meta.get("anio")
    heading = "Población por localidad"
    if anio:
        heading = f"{heading} ({anio})"

    rows = []
    for index in range(len(breaks) - 1):
        low = format_es(breaks[index])
        high = format_es(breaks[index + 1])
        rows.append(
            '<div class="bog-legend-row">'
            f'<span class="bog-swatch" style="background:{colors[index]}"></span>'
            f"<span>{low} – {high}</span></div>"
        )
    rows.append(
        '<div class="bog-legend-row">'
        '<span class="bog-swatch bog-swatch-nodata"></span>'
        "<span>Sin dato</span></div>"
    )

    point_rows = [
        (COLOR_SALUD, "Salud (IPS)"),
        (COLOR_EDUCACION, "Educación (colegios)"),
        (COLOR_PARQUES, "Parques"),
    ]
    dots = "".join(
        '<div class="bog-legend-row">'
        f'<span class="bog-dot" style="background:{color}"></span>'
        f"<span>{label}</span></div>"
        for color, label in point_rows
    )

    return (
        '<div id="bog-legend" class="bog-panel">'
        f"<h3>{esc(heading)}</h3>"
        + "".join(rows)
        + '<div class="bog-legend-sep"></div>'
        + dots
        + "</div>"
    )


def build_attribution(provenance: dict) -> str:
    """Footer listing every provider and license declared in provenance.json."""
    datasets = provenance.get("datasets") or {}
    items = []
    for key in sorted(datasets):
        entry = datasets.get(key) or {}
        label = entry.get("label") or key
        provider = entry.get("provider") or "Proveedor no indicado"
        license_name = entry.get("license") or "Licencia no indicada"
        url = entry.get("dataset_url") or entry.get("source_url")
        link = ""
        if isinstance(url, str) and url:
            link = (
                ' · <a href="'
                + esc(url)
                + '" target="_blank" rel="noopener noreferrer">conjunto de datos</a>'
            )
        items.append(
            "<li><strong>"
            + esc(label)
            + "</strong><br>"
            + esc(provider)
            + " · Licencia: <strong>"
            + esc(license_name)
            + "</strong>"
            + link
            + "</li>"
        )
    if not items:
        items.append("<li>Sin metadatos de procedencia disponibles.</li>")

    fetched = date_label(provenance.get("fetched_utc"))
    return (
        '<div id="bog-attribution" class="bog-panel">'
        "<h4>Fuentes y licencias</h4><ul>"
        + "".join(items)
        + "</ul>"
        + '<p class="bog-attr-foot">Mapa base: Esri Light Gray Canvas · '
        + '<a href="https://www.esri.com" target="_blank" '
        + 'rel="noopener noreferrer">Esri</a>, HERE, Garmin, FAO, NOAA, USGS, '
        + '<a href="https://www.openstreetmap.org/copyright" target="_blank" '
        + 'rel="noopener noreferrer">OpenStreetMap</a> contributors y la GIS '
        + "User Community. "
        + f"Datos descargados: {esc(fetched)} (UTC).</p>"
        + "</div>"
    )


# --------------------------------------------------------------------------- #
# the single custom element: panels + CSS + runtime GeoJSON loader
# --------------------------------------------------------------------------- #
class BogotaRuntimeMap(MacroElement):
    """Inline panels/CSS/JS that fetch every layer at view time.

    Subclass of ``branca.element.MacroElement``: the Jinja template stored in
    ``_template`` renders three macros (header -> <style>, html -> panels,
    script -> loader). The script never receives feature arrays, only file
    names, layer-group ids and the class breaks.
    """

    _template = Template(
        r"""
        {% macro header(this, kwargs) %}
        <style>
          /* ---------------------------------------------------- panels --- */
          .bog-panel {
            position: fixed;
            z-index: 1400;
            box-sizing: border-box;
            background: rgba(255, 255, 255, 0.97);
            border: 1px solid #cbd5e1;
            border-radius: 10px;
            box-shadow: 0 4px 14px rgba(15, 23, 42, 0.18);
            color: #1e293b;
            font-family: "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            font-size: 13px;
            line-height: 1.45;
          }
          .bog-panel h1 { margin: 0 0 4px; font-size: 17px; color: #0f172a; }
          .bog-panel h2 { margin: 0 0 6px; font-size: 14px; color: #0f172a; }
          .bog-panel h3, .bog-panel h4 {
            margin: 0 0 6px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #334155;
          }

          #bog-left {
            position: fixed;
            top: 12px;
            left: 12px;
            width: 322px;
            max-width: calc(100vw - 24px);
            z-index: 1400;
            display: flex;
            flex-direction: column;
            gap: 10px;
            pointer-events: none;
          }
          #bog-left > .bog-panel { position: static; pointer-events: auto; }
          #bog-title, #bog-info { padding: 10px 12px; }
          .bog-sub { margin: 0 0 6px; font-size: 12px; color: #475569; }
          .bog-meta { margin: 0 0 8px; font-size: 11px; color: #64748b; }
          .bog-hint { margin: 0; font-size: 12px; font-style: italic; color: #64748b; }

          .bog-button {
            cursor: pointer;
            padding: 4px 10px;
            font-size: 12px;
            font-family: inherit;
            color: #0f172a;
            background: #f1f5f9;
            border: 1px solid #94a3b8;
            border-radius: 6px;
          }
          .bog-button:hover { background: #e2e8f0; }

          .bog-info-table { width: 100%; border-collapse: collapse; }
          .bog-info-table th {
            padding: 3px 8px 3px 0;
            text-align: left;
            font-weight: 600;
            color: #334155;
            white-space: nowrap;
          }
          .bog-info-table td { padding: 3px 0; text-align: right; color: #0f172a; }
          .bog-badge {
            display: inline-block;
            margin: 0 0 6px;
            padding: 2px 8px;
            border-radius: 999px;
            background: #e2e8f0;
            color: #475569;
            font-size: 11px;
            font-weight: 700;
          }

          /* ------------------------------------------------------ legend --- */
          #bog-legend { right: 12px; bottom: 44px; width: 236px; padding: 10px 12px; }
          .bog-legend-row {
            display: flex;
            align-items: center;
            gap: 7px;
            margin: 3px 0;
            font-size: 12px;
          }
          .bog-swatch {
            flex: 0 0 auto;
            width: 24px;
            height: 12px;
            border: 1px solid #94a3b8;
            border-radius: 2px;
          }
          .bog-swatch-nodata {
            background: repeating-linear-gradient(45deg, #9aa5b1 0 3px, #e2e8f0 3px 6px);
          }
          .bog-dot {
            flex: 0 0 auto;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            border: 2px solid #ffffff;
            box-shadow: 0 0 0 1px rgba(15, 23, 42, 0.5);
          }
          .bog-legend-sep { height: 1px; margin: 7px 0; background: #e2e8f0; }

          /* --------------------------------------------------- credits --- */
          #bog-attribution {
            left: 12px;
            bottom: 44px;
            width: 330px;
            max-width: calc(100vw - 24px);
            max-height: 32vh;
            overflow-y: auto;
            padding: 8px 10px;
            font-size: 11px;
            color: #475569;
          }
          #bog-attribution ul { margin: 0; padding-left: 15px; }
          #bog-attribution li { margin-bottom: 4px; }
          #bog-attribution a { color: #1d4ed8; }
          .bog-attr-foot {
            margin: 6px 0 0;
            padding-top: 5px;
            border-top: 1px solid #e2e8f0;
          }

          /* -------------------------------------------------- loading --- */
          #bog-loading {
            position: fixed;
            inset: 0;
            z-index: 2000;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 12px;
            background: rgba(255, 255, 255, 0.88);
            color: #334155;
            font-family: "Segoe UI", Roboto, Arial, sans-serif;
            font-size: 14px;
          }
          .bog-hidden { display: none !important; }
          .bog-spinner {
            box-sizing: border-box;
            width: 36px;
            height: 36px;
            border: 4px solid #cbd5e1;
            border-top-color: #2563eb;
            border-radius: 50%;
            animation: bog-spin 0.9s linear infinite;
          }
          @keyframes bog-spin { to { transform: rotate(360deg); } }

          /* ------------------------------------------- markers & popups --- */
          .bog-point { border: 0; background: transparent; }
          .bog-point__dot {
            display: block;
            box-sizing: border-box;
            width: 13px;
            height: 13px;
            border: 2px solid #ffffff;
            border-radius: 50%;
            box-shadow: 0 0 0 1px rgba(15, 23, 42, 0.55), 0 1px 3px rgba(0, 0, 0, 0.4);
          }
          .bog-popup { min-width: 190px; border-collapse: collapse; font-size: 12px; }
          .bog-popup th {
            padding: 2px 8px 2px 0;
            text-align: left;
            font-weight: 600;
            color: #334155;
            white-space: nowrap;
          }
          .bog-popup td { padding: 2px 0; color: #0f172a; }

          /* "Sin dato" features: neutral grey + dashed (hatch-like) outline */
          .leaflet-overlay-pane svg path.bog-sin-dato { stroke-dasharray: 4 3; }
        </style>
        {% endmacro %}

        {% macro html(this, kwargs) %}
        {{ this.left_html | safe }}
        {{ this.legend_html | safe }}
        {{ this.attribution_html | safe }}
        <div id="bog-loading" role="status" aria-live="polite">
          <div class="bog-spinner" aria-hidden="true"></div>
          <span>Cargando datos del mapa…</span>
        </div>
        {% endmacro %}

        {% macro script(this, kwargs) %}
        (function () {
          "use strict";

          /* Injected by build_map.py: file names, layer-group ids, class
             breaks and colours. Feature arrays are never embedded here. */
          var cfg = {{ this.config_json | safe }};
          var map = window[cfg.mapName];
          if (!map) {
            console.warn("[bogota] no se encontró la instancia del mapa");
            return;
          }

          /* ------------------------------------------------------------ utils */
          function byId(id) { return document.getElementById(id); }

          function esc(value) {
            var text = value === null || value === undefined ? "" : String(value);
            return text.replace(/[&<>"']/g, function (ch) {
              return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
            });
          }

          /* es-CO number formatting: 1234567 -> "1.234.567" */
          function formatEs(value, decimals) {
            if (value === null || value === undefined || value === "" || isNaN(Number(value))) {
              return null;
            }
            var digits = decimals || 0;
            return Number(value).toLocaleString("es-CO", {
              minimumFractionDigits: digits,
              maximumFractionDigits: digits
            });
          }

          function yearSuffix() { return cfg.anio ? " (" + cfg.anio + ")" : ""; }

          /* ------------------------------------------------- popup text --- */
          function popupRow(label, value) {
            var text = value === null || value === undefined || value === "" ? "Sin dato" : String(value);
            return "<tr><th>" + esc(label) + "</th><td>" + esc(text) + "</td></tr>";
          }

          function popupHtml(feature, fields) {
            var props = (feature && feature.properties) || {};
            var rows = fields.map(function (field) {
              return popupRow(field[1], props[field[0]]);
            }).join("");
            return '<table class="bog-popup"><tbody>' + rows + "</tbody></table>";
          }

          function choroplethTooltip(props) {
            var population = props.sin_dato ? null : formatEs(props.poblacion);
            return "Localidad: " + (props.localidad || "—") +
                   " — Población: " + (population || "Sin dato") + yearSuffix();
          }

          /* ------------------------------------------------- info panel --- */
          function infoRow(label, value) {
            return "<tr><th>" + esc(label) + "</th><td>" + esc(value) + "</td></tr>";
          }

          function showInfo(props) {
            var panel = byId("bog-info");
            var body = byId("bog-info-body");
            if (!panel || !body) { return; }
            var population = props.sin_dato ? null : formatEs(props.poblacion);
            var rows = [
              infoRow("Localidad", props.localidad || "—"),
              infoRow("Población" + yearSuffix(), population || "Sin dato"),
              infoRow("Hogares", formatEs(props.hogares) || "Sin dato"),
              infoRow("Superficie (km²)", formatEs(props.superficie_km2, 2) || "Sin dato"),
              infoRow("Densidad (hab/km²)", formatEs(props.densidad, 1) || "Sin dato")
            ];
            var badge = props.sin_dato ? '<p class="bog-badge">Sin dato</p>' : "";
            body.innerHTML = badge +
              '<table class="bog-info-table"><tbody>' + rows.join("") + "</tbody></table>";
            panel.classList.remove("bog-hidden");
          }

          /* --------------------------------- hatch pattern for "sin dato" --- */
          /* The pattern lives inside Leaflet's own overlay <svg> so the
             reference resolves in the same SVG root; if it cannot be inserted
             the fill falls back to a neutral grey (SVG paint fallback). */
          var HATCH_DEFS = '<defs><pattern id="bog-hatch" width="8" height="8"' +
            ' patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
            '<rect width="8" height="8" fill="#dfe3e8"></rect>' +
            '<rect width="3" height="8" fill="#a9b4c0"></rect>' +
            "</pattern></defs>";

          function ensureHatchDefs() {
            try {
              var pane = map.getPanes().overlayPane;
              var svg = pane ? pane.querySelector("svg") : null;
              if (!svg || svg.querySelector("#bog-hatch")) { return; }
              svg.insertAdjacentHTML("afterbegin", HATCH_DEFS);
            } catch (error) {
              console.warn("[bogota] no se pudo insertar el patrón de trama", error);
            }
          }

          /* ------------------------------------------------- choropleth --- */
          function binColor(value, breaks, colors) {
            if (value === null || value === undefined || isNaN(Number(value))) { return null; }
            for (var i = 0; i < colors.length; i++) {
              if (value <= breaks[i + 1]) { return colors[i]; }
            }
            return colors[colors.length - 1];
          }

          function choroplethStyle(feature) {
            var props = feature.properties || {};
            if (props.sin_dato) {
              return {
                className: "bog-sin-dato",
                fillColor: "url(#bog-hatch) #d6dade",
                fillOpacity: 1,
                color: "#64748b",
                opacity: 0.95,
                weight: 1.2,
                dashArray: "4 3"
              };
            }
            var fill = binColor(props.poblacion, cfg.breaks, cfg.colors) || "#cbd5e1";
            return {
              fillColor: fill,
              fillOpacity: 0.72,
              color: "#334155",
              opacity: 0.95,
              weight: 0.8
            };
          }

          function attachChoropleth(data, group) {
            ensureHatchDefs();  // best case: the overlay <svg> already exists
            var layer = L.geoJSON(data, {
              style: choroplethStyle,
              onEachFeature: function (feature, path) {
                var props = feature.properties || {};
                path.bindTooltip(choroplethTooltip(props), { sticky: true, direction: "top" });
                path.on({
                  mouseover: function (event) {
                    var target = event.target;
                    target.setStyle({ weight: 2.4, color: "#0f172a", fillOpacity: 0.92 });
                    if (target.bringToFront) { target.bringToFront(); }
                  },
                  mouseout: function (event) {
                    layer.resetStyle(event.target);
                  },
                  click: function (event) {
                    var own = event.target.feature;
                    showInfo(own ? own.properties || {} : {});
                  }
                });
              }
            });
            group.addLayer(layer);
            ensureHatchDefs();  // Leaflet has just created its overlay <svg>
          }

          /* -------------------------------------------- barrio outlines --- */
          function attachOutline(data, group) {
            var layer = L.geoJSON(data, {
              style: { fill: false, weight: 0.7, color: "#64748b", opacity: 0.85 },
              onEachFeature: function (feature, path) {
                var name = (feature.properties || {}).barrio;
                if (name) { path.bindTooltip(esc(name), { sticky: true }); }
              }
            });
            group.addLayer(layer);
          }

          /* ------------------------------------------------ point layers --- */
          function pointIcon(color) {
            return L.divIcon({
              className: "bog-point",
              html: '<span class="bog-point__dot" style="background:' + color + ';"></span>',
              iconSize: [13, 13],
              iconAnchor: [6, 6]
            });
          }

          function latLngOf(layer) {
            if (typeof layer.getLatLng === "function") { return layer.getLatLng(); }
            if (typeof layer.getBounds === "function") {
              var bounds = layer.getBounds();
              if (bounds.isValid()) { return bounds.getCenter(); }
            }
            return null;
          }

          function attachPoints(data, group, spec) {
            /* L.geoJSON parses the geometries; one marker is created per
               feature (polygons such as parques collapse to their centre) and
               added to the layer group / marker cluster. */
            var parsed = L.geoJSON(data);
            var added = 0;
            parsed.eachLayer(function (layer) {
              var latlng = latLngOf(layer);
              if (!latlng) { return; }
              var marker = L.marker(latlng, { icon: pointIcon(spec.color), keyboard: false });
              marker.bindPopup(popupHtml(layer.feature, spec.fields));
              group.addLayer(marker);
              added += 1;
            });
            if (!added) {
              console.warn("[bogota] " + spec.file + " no contiene geometrías utilizables");
            }
          }

          /* --------------------------------------------- runtime loading --- */
          function groupOf(spec) {
            /* folium declares each layer group with `var` earlier in the same
               script block; resolving it through window[] when the fetch
               settles keeps the loader independent of the emission order. */
            var group = window[spec.group];
            if (!group) {
              console.warn("[bogota] capa no encontrada: " + spec.group);
              return null;
            }
            return group;
          }

          function loadLayer(spec) {
            return fetch(spec.file)
              .then(function (response) {
                if (!response.ok) { throw new Error("HTTP " + response.status); }
                return response.json();
              })
              .then(function (data) {
                var group = groupOf(spec);
                if (!group) { return; }
                if (spec.kind === "choropleth") { attachChoropleth(data, group); }
                else if (spec.kind === "outline") { attachOutline(data, group); }
                else { attachPoints(data, group, spec); }
              })
              .catch(function (error) {
                /* A broken layer must never break the page: warn and carry on. */
                console.warn("[bogota] no se pudo cargar " + spec.file, error);
              });
          }

          function hideLoader() {
            var overlay = byId("bog-loading");
            if (overlay) { overlay.classList.add("bog-hidden"); }
          }

          /* ------------------------------------------------------- init --- */
          var safetyNet = setTimeout(hideLoader, 15000);  // never trap the user
          Promise.allSettled(cfg.layers.map(loadLayer)).then(function () {
            clearTimeout(safetyNet);
            hideLoader();
          });

          var resetButton = byId("bog-reset");
          if (resetButton) {
            resetButton.addEventListener("click", function () {
              map.setView(cfg.center, cfg.zoom);
            });
          }

          var closeButton = byId("bog-info-close");
          if (closeButton) {
            closeButton.addEventListener("click", function () {
              var panel = byId("bog-info");
              if (panel) { panel.classList.add("bog-hidden"); }
              map.closePopup();
            });
          }

          /* Keep the hatch pattern in place if Leaflet rebuilds its SVG pane. */
          map.on("layeradd", ensureHatchDefs);
        })();
        {% endmacro %}
        """
    )

    def __init__(
        self,
        config: dict,
        left_html: str,
        legend_html: str,
        attribution_html: str,
    ) -> None:
        super().__init__()
        self._name = "BogotaRuntimeMap"
        # JSON can never contain "{{", so injecting it into Jinja is safe.
        self.config_json = json.dumps(config, ensure_ascii=False)
        self.left_html = left_html
        self.legend_html = legend_html
        self.attribution_html = attribution_html


# --------------------------------------------------------------------------- #
# input handling
# --------------------------------------------------------------------------- #
def require_inputs(data_dir: Path) -> None:
    """Fail clearly (no traceback) when an input file is missing."""
    missing = [name for name in DATA_FILES if not (data_dir / name).is_file()]
    if missing:
        listed = "\n  - ".join(str(data_dir / name) for name in missing)
        raise BuildError(f"missing input file(s):\n  - {listed}")


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise BuildError(f"missing input file: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            doc = json.load(handle)
    except json.JSONDecodeError as exc:
        raise BuildError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise BuildError(f"cannot read {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise BuildError(f"expected a JSON object in {path}")
    return doc


def read_breaks(meta: dict) -> list:
    """Validate and return the class breaks used to bin the choropleth."""
    breaks = meta.get("breaks")
    if not isinstance(breaks, list) or len(breaks) < 2:
        raise BuildError(
            "indicators.json: meta.breaks must be a list of at least 2 numbers"
        )
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in breaks
    ):
        raise BuildError("indicators.json: meta.breaks must contain only numbers")
    if any(breaks[i] >= breaks[i + 1] for i in range(len(breaks) - 1)):
        raise BuildError("indicators.json: meta.breaks must be strictly increasing")
    return [float(value) for value in breaks]


def step_colors(breaks: list) -> list:
    """One colour per class interval, derived with branca's LinearColormap."""
    ramp = bc.LinearColormap(RAMP_COLORS, vmin=breaks[0], vmax=breaks[-1])
    stepped = ramp.to_step(n=len(breaks) - 1)
    out = []
    for color in stepped.colors:
        red, green, blue = (int(round(channel * 255)) for channel in color[:3])
        out.append(f"#{red:02x}{green:02x}{blue:02x}")
    return out


# --------------------------------------------------------------------------- #
# page assembly
# --------------------------------------------------------------------------- #
def build_map(data_dir: Path, provenance: dict, indicators: dict) -> str:
    meta = indicators.get("meta") or {}
    breaks = read_breaks(meta)
    colors = step_colors(breaks)
    fetched_utc = str(provenance.get("fetched_utc") or "")

    # --- basemap + view ---------------------------------------------------- #
    folium_map = folium.Map(
        location=CENTER,
        zoom_start=ZOOM,
        tiles=None,  # the base and label layers are declared explicitly below
        control_scale=True,
        zoom_control="topright",  # keeps the top-left corner free for the title
    )
    folium.TileLayer(
        tiles=TILE_URL,
        name="Mapa base clara",
        attr=TILE_ATTRIBUTION,
        overlay=False,
        show=True,
    ).add_to(folium_map)
    folium.TileLayer(
        tiles=LABELS_URL,
        name="Etiquetas",
        attr=TILE_ATTRIBUTION,
        overlay=True,
        show=True,
    ).add_to(folium_map)

    # --- overlay placeholders: they give LayerControl its checkboxes ------- #
    # Added BEFORE the loader so their `var` declarations precede the loader
    # script; the loader resolves them through window[] once fetches settle.
    fg_localidades = folium.FeatureGroup(name="Población por localidad", show=True)
    fg_localidades.add_to(folium_map)
    fg_barrios = folium.FeatureGroup(name="Barrios", show=False)
    fg_barrios.add_to(folium_map)
    mc_ips = MarkerCluster(
        name="Salud (IPS)", show=True, options={"chunkedLoading": True}
    )
    mc_ips.add_to(folium_map)
    mc_colegios = MarkerCluster(
        name="Educación (colegios)", show=True, options={"chunkedLoading": True}
    )
    mc_colegios.add_to(folium_map)
    mc_parques = MarkerCluster(
        name="Parques", show=True, options={"chunkedLoading": True}
    )
    mc_parques.add_to(folium_map)

    # --- runtime loader + panels ------------------------------------------- #
    config = {
        "mapName": folium_map.get_name(),
        "center": list(CENTER),
        "zoom": ZOOM,
        "anio": meta.get("anio"),
        "breaks": breaks,
        "colors": colors,
        "layers": [
            {
                "file": "data/localidades.geojson",
                "group": fg_localidades.get_name(),
                "kind": "choropleth",
            },
            {
                "file": "data/barrios.geojson",
                "group": fg_barrios.get_name(),
                "kind": "outline",
            },
            {
                "file": "data/ips.geojson",
                "group": mc_ips.get_name(),
                "kind": "points",
                "color": COLOR_SALUD,
                "fields": POINT_FIELDS["ips"],
            },
            {
                "file": "data/colegios.geojson",
                "group": mc_colegios.get_name(),
                "kind": "points",
                "color": COLOR_EDUCACION,
                "fields": POINT_FIELDS["colegios"],
            },
            {
                "file": "data/parques.geojson",
                "group": mc_parques.get_name(),
                "kind": "points",
                "color": COLOR_PARQUES,
                "fields": POINT_FIELDS["parques"],
            },
        ],
    }
    BogotaRuntimeMap(
        config=config,
        left_html=build_left_column(meta, fetched_utc),
        legend_html=build_legend(meta, breaks, colors),
        attribution_html=build_attribution(provenance),
    ).add_to(folium_map)

    # LayerControl must be added last so it picks up every layer above.
    folium.LayerControl(collapsed=False).add_to(folium_map)

    figure = folium_map.get_root()
    figure.title = "Mapa interactivo de Bogotá"
    return figure.render()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build site/index.html from the processed site/data files."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory with the processed inputs (default: <repo>/site/data)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="HTML file to write (default: <repo>/site/index.html)",
    )
    args = parser.parse_args(argv)

    data_dir = args.data_dir
    out_path = args.out

    try:
        require_inputs(data_dir)
        provenance = load_json(data_dir / "provenance.json")
        indicators = load_json(data_dir / "indicators.json")
        page = build_map(data_dir, provenance, indicators)
    except BuildError as exc:
        print(f"ERROR: {exc}")
        return 1

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(page, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot write {out_path}: {exc}")
        return 1

    size = out_path.stat().st_size
    print(f"Built {out_path} ({size:,} bytes)")
    print("  basemap   : Esri Light Gray Canvas + labels")
    print("  layers    : Población por localidad, Barrios, Salud (IPS),")
    print("              Educación (colegios), Parques  (all fetched at runtime)")
    print(f"  data dir  : {data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
