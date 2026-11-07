# Bitácora — Mapa interactivo de Bogotá (GitHub Pages)

Registro de cómo se construyó este proyecto: proveedor y modelo, configuración de
OpenCode, orquestación de agentes, fuentes consultadas, problemas encontrados y
soluciones, estimación de tokens y enlaces.

- **Fecha de elaboración:** 6-7 de noviembre de 2026
- **Repositorio:** https://github.com/gabrielazea2018-droid/mapa-bogota-interactive
- **Mapa publicado:** https://gabrielazea2018-droid.github.io/mapa-bogota-interactive/

---

## 1. Proveedor y modelo usados

| Campo | Valor |
| --- | --- |
| Herramienta / orquestador | **OpenCode** |
| Proveedor (provider ID) | `opencode` |
| Modelo (model ID) | `mimo-v2.6-flash-free` |
| Nombre comercial | **MiMo-V2.6-Flash Free** |
| Agente utilizado | `plan` (agente por defecto de la configuración) para el diseño; agentes `general` y `explore` del enjambre para la implementación |

Todo el trabajo —exploración, escritura de código, ejecución de scripts, descarga de
datos y publicación— se hizo desde OpenCode. No se usó ningún trabajo manual de
descarga, edición ni creación de archivos.

---

## 2. Configuración de OpenCode para esta tarea

### 2.1 Archivo de configuración

`C:\Users\Gabriela Zea\.config\opencode\opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "default_agent": "plan",
  "permission": {
    "*": "allow",
    "edit": "allow",
    "webfetch": "allow",
    "external_directory": "ask",
    "bash": { "*": "allow", "rm *": "ask", "rmdir *": "ask",
              "git push *": "ask", "git checkout *": "ask" }
  },
  "instructions": ["https://opencode.school/api/instructions/scrappy-maker-8559"],
  "mcp": {
    "open-meteo": { "type": "local",
      "command": ["npx", "-y", "-p", "open-meteo-mcp-server", "open-meteo-mcp-server"] },
    "servers": { "chrome-devtools": { "type": "local",
      "command": ["npx", "-y", "chrome-devtools-mcp@latest", "--autoConnect"] } }
  },
  "plugin": ["opencode-helicone-session"]
}
```

Puntos que importaron para este proyecto:

- **`default_agent: "plan"`**: la conversación arrancó en modo plan (solo lectura),
  lo que obligó a diseñar el pipeline y a hacer las preguntas de alcance **antes**
  de escribir un solo archivo. Recién después se cambió a modo de implementación.
- **`permission.bash."git push *": "ask"`**: todo push a Git requiere confirmación
  explícita, de modo que ninguna rama se publica sin que tú lo apruebes.
- **`permission."external_directory": "ask"`**: el trabajo quedó confinado al
  directorio del proyecto.
- **`instructions` remotas** y el **`AGENTS.md` global**
  (`C:\Users\Gabriela Zea\.config\opencode\AGENTS.md`): impusieron las reglas de
  comunicación (español con la persona, inglés en código/comentarios/mensajes de
  commit) y el prefijo semántico de commits (`feat:`, `fix:`, `docs:`, …).
- **MCP `chrome-devtools`**: disponible para inspeccionar el HTML generado en un
  navegador real si hiciera falta depurar el mapa.
- **Plugin `opencode-helicone-session`**: registra las sesiones fuera de la máquina.
  No existe telemetría de tokens en el disco local (ver §7).

### 2.2 Preguntas de alcance hechas antes de construir

Se formularon 8 preguntas en dos tandas; sus respuestas definieron el producto:

1. ¿Qué debe mostrar el mapa? → **Capas base + ciudad, coropleta de indicadores y
   equipamientos** (se descartó la capa de movilidad TransMilenio).
2. ¿Qué stack? → **Folium (Python)**.
3. ¿Cómo manejar el repositorio? → **repo nuevo público**.
4. ¿Cómo se refrescan los datos? → **manual + semanal** (`workflow_dispatch` + `schedule`).
5. ¿Qué indicador usa la coropleta? → **población por localidad**.
6. ¿Qué categorías de puntos? → **salud, educación y parques** (se descartó cultura).
7. ¿Nombre del repositorio? → `mapa-bogota-interactive`.
8. ¿Estilo visual? → **cartográfico claro**.

### 2.3 Orquestación del enjambre

Yo hice de orquestador y lancé los agentes **en paralelo** (`background`), con un
**contrato de datos escrito antes de empezar** para que ningún agente tuviera que
adivinar la salida de otro:

| Agente | Rol | Archivos que poseía (nadie más tocaba esos archivos) |
| --- | --- | --- |
| Orquestador (yo) | esqueleto del repo, verificación de URLs, extracción, reproyección, procesamiento, integración, push | `scripts/fetch_data.py`, `scripts/process_data.py`, `scripts/geo_utils.py`, `scripts/sources.yaml`, `BITACORA.md` |
| Agente B (`general`, en segundo plano) | generador del mapa Folium y del validador | `scripts/build_map.py`, `scripts/validate.py` |
| Agente C (`general`, en segundo plano) | CI/Docs/tests | `.github/workflows/deploy.yml`, `README.md`, `LICENSE`, `.gitignore`, `tests/test_pipeline.py` |

El contrato acordado (entrada/salida del pipeline) quedó fijado así:

```
data/raw/*  --fetch_data.py-->  (descarga + provenance.json)
data/raw/*  --process_data.py-->  site/data/{localidades,barrios,ips,colegios,parques}.geojson
                                   site/data/{indicators.json,provenance.json}
site/data/* --build_map.py----->  site/index.html
site/*      --validate.py------>  código de salida 0/1
```

Skills disponibles en la sesión: `frontend-design` (dirección visual) y `web-perf`
(peso del artefacto) estaban cargados como referencia para la capa de presentación.

---

## 3. Fuentes consultadas

Catálogo principal: **Datos Abiertos Bogotá D.C.**
<https://datosabiertos.bogota.gov.co> — API CKAN usada:
`https://datosabiertos.bogota.gov.co/api/3/action/package_search`
(y `package_show`, `package_list`).

Todas las URLs se resolvieron **programáticamente** con `package_show` y se
verificaron descargándolas; ninguna se copió a mano. El manifiesto vivo está en
[`scripts/sources.yaml`](scripts/sources.yaml) y el registro con hash SHA-256 y
fecha de cada descarga en `data/raw/provenance.json` (se genera con
`fetch_data.py`).

| # | Dataset | Entidad productora | Licencia | Formato real recibido | Uso |
| --- | --- | --- | --- | --- | --- |
| 1 | `localidad-bogota-d-c` | SDP / IDECA | CC BY 4.0 | **Esri JSON** (advertido como GeoJSON) | polígonos de las 20 localidades |
| 2 | `barrio-legalizado-bogota-d-c` | SDHT / IDECA | CC BY 4.0 | Esri JSON en **CRS proyectado** | capa de barrios |
| 3 | `habitat-en-cifras-…-indicadores-demograficos` | SDHT | CC BY-NC-SA 4.0 | XLSX (380 filas) | coropleta: población y hogares por localidad (proyecciones DANE) |
| 4 | `localidad-registro-social` | SDHT | CC BY-NC-SA 4.0 | GeoJSON (20 features) | respaldo del indicador (no se publica) |
| 5 | `instituciones-prestadoras-de-salud-bogota` | SDS | CC BY-SA 4.0 | GeoJSON (2 900 puntos) | capa Salud |
| 6 | `colegios-bogota-d-c` | SDE | CC BY-SA 4.0 | GeoJSON (2 176 puntos) en **Web Mercator** | capa Educación |
| 7 | `parque-bogota-d-c` (POT) | SDHT | CC BY 4.0 | GeoJSON (5 328 polígonos, 14,3 MB) | capa Parques |
| 8 | `parques-idrd` | IDRD | CC BY-SA 4.0 | CSV **sin coordenadas** | **descartado** (ver §6.9) |
| — | Esri Light Gray Canvas + Reference | Esri / OSM | uso con atribución | tiles | mapa base (CARTO exige API key, ver §4 #16) |

Otras fuentes de consulta (catálogo, metadatos y catálogo de objetos):

- API CKAN `package_list` / `package_search` / `package_show` de datosabiertos.bogota.gov.co
- Catálogo de Objetos Geográficos de la SDP
  (`https://www.sdp.gov.co/system/tdf/repositorio-dice/catalogo_objetos.zip`, 67 MB,
  PDF por objeto) — descargado y analizado para intentar descifrar `TIPO_PARQUE`.
- Servicios IDECA/ArcGIS identificados como respaldo:
  `serviciosgis.catastrobogota.gov.co/arcgis/rest/…`,
  `serversig.habitatbogota.gov.co/arcgis/rest/…`,
  `gis.transmilenio.gov.co/arcgis/rest/…`.

### Información analizada

| Concepto | Volumen |
| --- | --- |
| Descargas crudas (`data/raw/`) | **32 110 566 bytes** (8 datasets) |
| Salida publicada (`site/data/`) | **3 527 819 bytes** (7 archivos) → 11,0 % del crudo |
| Features descargadas | 20 localidades + 1 770 barrios + 2 900 IPS + 2 176 colegios + 5 328 parques = **12 194 geometrías** |
| Tabla de indicadores | 380 filas × 6 columnas; 20 indicadores × 19 localidades × 5 años (2018-2022) |
| Respuestas CKAN inspeccionadas en la exploración | ~10 consultas (`localidades` 236, `equipamiento` 57, `colegios` 35, `establecimientos de salud` 372, `transmilenio` 37, …) |

---

## 4. Problemas encontrados y cómo se dieron soluciones

Ninguno de los siguientes se resolvió a mano: cada uno terminó en código del pipeline.

| # | Problema | Solución |
| --- | --- | --- |
| 1 | `package_search?q=limite urbano` devolvía **0 resultados** aunque el dataset existía | El índice CKAN de Bogotá tokeniza distinto. Se pasó a `package_list` + términos de una o dos palabras (`localidades`, `colegios`, `equipamiento`) y a `package_show?id=…` para obtener las URLs exactas de cada recurso. |
| 2 | Los recursos de límites venían **advertidos como "GeoJSON" en el catálogo pero eran Esri JSON** (`geometryType: esriGeometryPolygon`, `attributes`+`rings`), lo que hacía fallar la validación y no se guardaban | `scripts/geo_utils.py`: detector `detect_format()` + convertidor `esri_to_feature_collection()` (rings → polígonos resolviendo agujeros por contención, no por sentido de giro) |
| 3 | El CSV de parques IDRD se leía como **1 columna de 139 filas** | El archivo usa `;` y el catálogo dice "CSV". Se añadió `sniff_delimiter()` que puntúa `,` `;` y tabulador en la cabecera antes de parsear. |
| 4 | `barrio-legalizado` venía en un **CRS proyectado propio** `PCS_CarMAGBOG` (Transverse Mercator en metros, declarado solo como WKT, sin código EPSG) | Se agregó `pyproj`: `CRS.from_wkt(...)` + `Transformer` a EPSG:4326 aplicado con `shapely.ops.transform` en la carga. Verificación: la muestra (88802, 102263) → **-74.178, 4.616**, dentro de Bogotá. |
| 5 | `colegios` traía **coordenadas en Web Mercator dentro de un "GeoJSON" sin CRS declarado** (x ≈ -8 281 429) | Se añadió una comprobación de rango lon/lat + heurística `_looks_like_web_mercator()` que reproyecta desde EPSG:3857. Si las coordenadas no encajan en lon/lat ni en 3857, el pipeline **falla con mensaje claro** en lugar de publicar un mapa desplazado. |
| 6 | **La consola de Windows es cp1252**: imprimir "Bogotá" o "ñ" rompía el script con `UnicodeEncodeError` | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` al inicio de cada script y `$env:PYTHONIOENCODING='utf-8'` en las corridas. |
| 7 | La tabla oficial de indicadores tiene **19 localidades, no 20** (falta Sumapaz, rural) | El polígono se publica igual con `"sin_dato": true`; la coropleta le asigna un color neutro y la leyenda incluye el rótulo **"Sin dato"**. Se valida que a lo sumo 1 localidad esté en esa condición. |
| 8 | El cruce por nombre **falló en La Candelaria**: la capa dice `CANDELARIA` y la tabla `La Candelaria` | Clave de unión normalizada (`slug()` sin acentos ni mayúsculas) + mapa de alias `{"candelaria": "la candelaria"}` + búsqueda con y sin el artículo "la". Además, el nombre visible se toma de la tabla oficial para conservar los acentos (`Usaquén`, `San Cristóbal`). |
| 9 | `TIPO_PARQUE` llega como **código opaco** (4, 28, 3…) y el catálogo de la SDP solo referencia el dominio `cdom_tipo_parque` sin publicar sus valores | Se descargó el ZIP de catálogos (67 MB) y se extrajo el texto de `100102_Parque.pdf` con `pypdf` (33 páginas): el PDF define el atributo pero no la tabla de códigos. **Decisión:** no mostrar códigos sin significado; la etiqueta legible se deriva de `NIVEL` ("Parque de proximidad", "Parque estructurante") y `vocacion` decodifica solo lo inequívoco (`SIN` → "Sin vocación especial"). |
| 10 | La capa POT de parques pesaba **14,3 MB** y la de barrios **8,2 MB** | `simplify_collection()` con Douglas-Peucker (`preserve_topology=True`), tolerancias por capa (0,0004°–0,0008° ≈ 44–88 m) y redondeo de coordenadas a 5 decimales (~1 m), además de eliminar los campos que el mapa no muestra: parques 14,3 MB → **1,7 MB**; barrios 8,2 MB → **0,4 MB**; localidades 2,4 MB → **44 KB**. |
| 11 | Con igual intervalo, **casi toda Bogotá quedaba en el tramo más pálido**; con cuantiles, tramos vecinos quedaban casi del mismo color (hay 4 localidades entre 386 k y 404 k) | Se implementó **Jenks natural breaks** (Fisher) con programación dinámica pura en Python (O(k·n²), n=19). Clases resultantes: 18 143 – 150 151 – 386 696 – 579 447 – 1 034 293 – 1 273 909, con respaldos a cuantiles y a intervalo logarítmico. |
| 12 | Embeber todo en el HTML de Folium habría generado un **index.html de varios MB** | Cada capa se carga en tiempo de ejecución con `fetch('data/…')` desde una macro propia de `branca`, así el HTML queda < 500 KB y los datos se cachean por separado. |
| 13 | `Invoke-WebRequest` de Windows PowerShell 5.1 fallaba en modo no interactivo | Se usó `-UseBasicParsing` y, sobre todo, se migró la descarga a `requests` dentro de `fetch_data.py` (con reintentos y backoff exponencial). |
| 14 | El repositorio no existía aún y `gh repo view` devolvía `Could not resolve to a Repository` | Se creó con `gh repo create … --public --source . --remote origin`. |
| 15 | Conflicto con la regla global **"nunca hacer push a la main"**: `schedule` y `workflow_dispatch` de GitHub Actions solo se activan desde la rama por defecto y Pages despliega desde allí | El trabajo se ramifica en `feature/initial-map`, se abre PR con `gh pr create` y **la fusión a `main` queda pendiente de tu aprobación explícita**; después de fusionar se dispara `workflow_dispatch` para publicar. |
| 16 | El mapa base **CartoDB Positron dejó de funcionar**: `basemaps.cartocdn.com` responde a toda petición sin clave con una imagen de **2 049 bytes** con la marca `API KEY REQUIRED` (verificado en `light_all`, `light_all@2x` y `rastertiles`), así que el mapa se publicaba sin calles | Se sondearon 9 proveedores descargando tiles reales de la misma celda: OSM (3 442 B), Esri Street Map (13 457 B) y Esri Light Gray (4 639 B) devolvían imagen verdadera. Se migró a **Esri Light Gray Canvas** más su capa `World_Light_Gray_Reference` para etiquetas, con atribución completa; `validate.py` (marker del check 3), `sources.yaml`, README y esta bitácora se actualizaron en el mismo cambio. |
| 17 | La validación del agente B **rechazaba los datos reales**: su bbox (lon -74.4..-73.9, lat 3.7..4.8) no cubre Engativá (-74.45) ni Sumapaz (3.73); además el **test de aceptación fallaba** porque el sitio sintético solo traía 3 archivos y 2 localidades | Bbox ampliado a lon -74.60..-73.70 y lat 3.50..5.10, con verificación de que las 5 capas quedan al **100 % dentro**. El fixture de `tests/test_pipeline.py` se hizo realista: 20 localidades, 1 050 IPS, 1 050 colegios, 130 barrios y 130 parques, con el mismo contrato de 7 archivos que publica `process_data.py`. |
| 18 | Dos agentes escribieron `build_map.py` y `validate.py` **en paralelo**: la versión del orquestador fue sobrescrita por la del agente B | Se probó la de B en el navegador (leyenda de 9 filas, ficha al hacer clic, 34 clústeres, panel de fuentes y licencias, consola sin errores) y se **conservó**, corrigiendo únicamente el basemap del punto 16; la versión descartada no quedó en el repositorio. |

---

## 5. Reproducibilidad

Todo se regenera desde cero con cinco comandos (sin ningún archivo generado a mano):

```bash
python -m pip install -r requirements.txt   # Folium, shapely, pyproj, pandas, requests, PyYAML
python scripts/fetch_data.py                # sources.yaml -> data/raw/ (+ provenance.json)
python scripts/process_data.py              # data/raw/    -> site/data/
python scripts/build_map.py                 # site/data/   -> site/index.html
python scripts/validate.py                  # pruebas de humo; sale con 1 si algo falla
```

- `requirements.txt` con versiones mínimas fijadas; probado en **Python 3.14.3**
  (Windows) y fijado a **3.12** en CI.
- **Sin `geopandas`/`fiona`/`GDAL`**: se usan `shapely` + `json` + `pyproj`, para que
  la instalación sea idéntica en Windows y en `ubuntu-latest`.
- `.github/workflows/deploy.yml` ejecuta exactamente esos 5 pasos en cada disparo y
  sube `site/` como artifact de Pages.
- `data/raw/`, `site/data/` y `site/index.html` **no se versionan**: se regeneran en
  cada corrida de CI, de modo que el repositorio nunca guarda datos sin atribución
  ni artefactos obsoletos.

---

## 6. Decisiones descartadas

1. **Capa de TransMilenio** — ofrecida y no elegida en las preguntas de alcance.
2. **Capa de cultura (IDARTES)** — no elegida.
3. **MapLibre / React / HTML+JS a mano** — se eligió Folium.
4. **`geopandas`** — descartado por la cadena de compilación de GDAL y su
   comportamiento distintivo en Windows/Python 3.14.
5. **Parques IDRD** — descartado por no tener coordenadas (ver §6.9 de la tabla §3).
6. **Mostrar `TIPO_PARQUE`** — ver problema 9 de la tabla §4.

---

## 7. Estimación de tokens

No hay telemetría de tokens en el equipo: `C:\Users\Gabriela Zea\.local\share\opencode\log\opencode.log`
no registra consumo (se buscó `tokens|usage|input_tokens` sin coincidencias de uso) y
el plugin `opencode-helicone-session` envía ese dato a su panel externo, no a disco.
Por eso estas cifras son una **estimación por conteo de caracteres**, no un dato leído:

| Fase | Qué produjo tokens | Estimación |
| --- | --- | --- |
| Fase de plan (preguntas + verificación de fuentes) | 4 respuestas de la API CKAN muy extensas (~70 000 caracteres), lectura de configuración y directorios | 60 000 – 90 000 |
| Extracción y diagnóstico (agente orquestador) | inspecciones de estructura GeoJSON/Esri/XLSX, pruebas de CRS, escritura de `fetch_data.py`, `geo_utils.py`, `process_data.py` | 150 000 – 250 000 |
| Agente B (mapa + validador) | sesión independiente con autopruebas contra datos sintéticos | 80 000 – 150 000 |
| Agente C (CI + README + tests) | sesión independiente con verificación de YAML, `--help` y `pytest` | 80 000 – 130 000 |
| Integración, prueba del pipeline completo, despliegue y esta bitácora | iteraciones sobre `site/`, ejecución de Actions, lectura de logs | 100 000 – 200 000 |
| **Total estimado** | | **470 000 – 820 000 tokens** |

Regla usada para cada fila: tamaño de las salidas de herramientas (caracteres ÷ 4)
más el contexto acumulado de la sesión, que crece con cada turno y se repite en cada
llamada al modelo. Si necesitas el número exacto, consulta el panel de Helicone
para las sesiones del 6-7/11/2026.

---

## 8. Enlaces

- **Repositorio:** https://github.com/gabrielazea2018-droid/mapa-bogota-interactive
- **Mapa publicado (GitHub Pages):** https://gabrielazea2018-droid.github.io/mapa-bogota-interactive/
- **Workflow de despliegue:** https://github.com/gabrielazea2018-droid/mapa-bogota-interactive/actions/workflows/deploy.yml
- **Portal de datos abiertos:** https://datosabiertos.bogota.gov.co
- **API CKAN utilizada:** https://datosabiertos.bogota.gov.co/api/3/action/package_search
- **IDECA (Instituto de Desarrollo Urbano):** https://www.ideca.gov.co
- **Mapas Bogotá:** https://mapas.bogota.gov.co

> El estado final de la publicación (commit, PR, ejecución de Actions y URL viva)
> se registra en la §9 al cierre del trabajo.

---

## 9. Estado de publicación

_Pendiente de cerrar al finalizar la implementación._
