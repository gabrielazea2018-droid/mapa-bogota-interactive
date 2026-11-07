# Mapa interactivo de Bogotá

Mapa web interactivo de Bogotá D.C. construido con [Folium](https://python-visualization.github.io/folium/): localidades, barrios legalizados, coropletas de indicadores demográficos y capas de puntos (colegios, IPS, parques), con un pipeline de Python que descarga los datos abiertos, los procesa y genera el `index.html` automáticamente.

## Enlaces

- **Repositorio:** https://github.com/gabrielazea2018-droid/mapa-bogota-interactive
- **Mapa publicado (tras publicar):** https://gabrielazea2018-droid.github.io/mapa-bogota-interactive/

> Las dos URL de arriba funcionan **después** de crear el repositorio público en GitHub y activar GitHub Pages con la fuente "GitHub Actions" (ver la sección [Despliegue automático](#despliegue-automático-github-actions)).

## Vista previa

El archivo `site/index.html` **no está escrito a mano**: se genera con Folium. Para regenerarlo:

```bash
python scripts/build_map.py
```

Ese comando toma los datos ya procesados que están en `site/data/` y escribe `site/index.html`. Luego abre ese archivo en tu navegador (doble clic) para ver el mapa. La captura de esta página se toma sobre ese HTML generado.

## Datos

Todos los datasets se descargan desde el catálogo de datos abiertos de Bogotá. Enlace al portal: https://datosabiertos.bogota.gov.co

La API CKAN que usa el código para buscar paquetes es:
https://datosabiertos.bogota.gov.co/api/3/action/package_search

| Capa publicada | Entidad productora | Licencia |
| --- | --- | --- |
| Localidades | SDP/IDECA | CC BY 4.0 |
| Barrios legalizados | SDHT/IDECA | CC BY 4.0 |
| Indicadores demográficos "Hábitat en cifras" (proyecciones DANE) | SDHT | CC BY-NC-SA 4.0 |
| IPS | SDS | CC BY-SA 4.0 |
| Colegios | SDE | CC BY-SA 4.0 |
| Parques POT | SDHT | CC BY 4.0 |
| Mapa base | CartoDB Positron / OpenStreetMap | ODbL (atribución) |

Dos fuentes más se descargan como **respaldo y control de calidad** pero no se publican como capa propia:

- **Registro social** (SDHT, CC BY-NC-SA 4.0): geometría alternativa por localidad, por si la tabla de indicadores cambia de estructura.
- **Parques IDRD** (IDRD): se evaluó y se descartó del mapa porque su CSV no incluye coordenadas; la capa POT ya trae el atributo `ADMINISTRADOR`.

La lista completa de URLs, formatos y licencias vive en `scripts/sources.yaml`.

## Requisitos

- **Python 3.12 o superior** (probado en Python 3.14).
- `pip` (viene con Python).
- Conexión a internet solo para el paso de descarga (`fetch_data.py`).

## Cómo reproducir localmente

Abre una terminal en la **raíz del repositorio** (la carpeta `mapa-bogota-interactive`) y ejecuta estos 5 comandos, **uno por uno**:

```bash
# 1) Instala las librerías de Python que el proyecto necesita
#    (Folium, pandas, shapely, requests, PyYAML...). Solo se hace la primera vez.
python -m pip install -r requirements.txt

# 2) Descarga los datasets declarados en scripts/sources.yaml -> data/raw/
#    También escribe data/raw/provenance.json (fuente, hash SHA-256, fecha).
python scripts/fetch_data.py

# 3) Procesa los datos: valida, simplifica los polígonos y une el indicador
#    demográfico. Entrada: data/raw/  ->  Salida: site/data/
python scripts/process_data.py

# 4) Genera el mapa: construye site/index.html con Folium a partir de site/data/
python scripts/build_map.py

# 5) Pruebas de humo sobre site/: comprueba que el HTML y los datos están bien.
#    Si algo falla, termina con código de salida 1 (y el pipeline se detiene).
python scripts/validate.py
```

Opcionalmente puedes correr los tests unitarios (no tocan la red; `pytest` no está
en `requirements.txt`, instálalo aparte la primera vez):

```bash
python -m pip install pytest
pytest -q
```

## Estructura del repositorio

```
mapa-bogota-interactive/
├── .github/
│   └── workflows/
│       └── deploy.yml        # GitHub Actions: construye y publica en Pages
├── data/
│   ├── raw/                  # datos descargados (generado, no se sube)
│   └── processed/            # datos intermedios (generado, no se sube)
├── scripts/
│   ├── fetch_data.py         # 2) descarga -> data/raw/
│   ├── process_data.py       # 3) procesa -> site/data/
│   ├── build_map.py          # 4) genera site/index.html (Folium)
│   ├── validate.py           # 5) pruebas de humo sobre site/
│   ├── geo_utils.py          # conversión Esri JSON -> GeoJSON, reproyección, simplificación
│   └── sources.yaml          # manifiesto: URLs de todas las fuentes
├── site/
│   ├── index.html            # generado por build_map.py (no se sube)
│   └── data/                 # generado por process_data.py (no se sube)
├── tests/
│   └── test_pipeline.py      # tests de pytest (sin red)
├── .gitignore
├── LICENSE
├── README.md
├── BITACORA.md               # bitácora de trabajo
└── requirements.txt          # dependencias de Python
```

## Despliegue automático (GitHub Actions)

El workflow `.github/workflows/deploy.yml` se dispara de **4 formas**:

1. **Manual** (`workflow_dispatch`): desde la pestaña *Actions* del repositorio.
2. **Programada** (`schedule`): los lunes a las 06:00 UTC (01:00 de la mañana en Bogotá), para refrescar los datos.
3. **Al hacer push a `main`**, pero solo si cambian `scripts/**`, `requirements.txt` o el propio `deploy.yml`.
4. **Al abrir un pull request** a `main`: solo construye y valida; **nunca despliega**.

Pasos para activarlo la primera vez:

1. Crea el repositorio público `mapa-bogota-interactive` en GitHub y sube el código.
2. Ve a **Settings → Pages** y en *Build and deployment → Source* elige **GitHub Actions**.
3. Ve a la pestaña **Actions**, selecciona **deploy-map** y pulsa **Run workflow** (o espera al primer push a `main`).
4. Cuando termine, el mapa estará en https://gabrielazea2018-droid.github.io/mapa-bogota-interactive/

## Actualización de datos

Todas las URLs de los datasets están en **`scripts/sources.yaml`**. Para añadir un dataset nuevo:

1. Busca el paquete en https://datosabiertos.bogota.gov.co (o usa la API `package_search`).
2. Copia la URL directa del recurso (debe empezar por `https://`).
3. Agrega una entrada nueva bajo `datasets:` con estas claves: `label`, `provider`, `license`, `url`, `out` (nombre del archivo), `format` (`geojson`, `json`, `csv` o `xlsx`) y `layer`.
4. Corre de nuevo `python scripts/fetch_data.py` y el resto del pipeline.

Verás el dataset nuevo en el mapa sin tocar el código de Python.

## Bitácora

El diario de trabajo del proyecto (decisiones, problemas y soluciones) está en [BITACORA.md](BITACORA.md).

## Licencia del código vs licencia de los datos

- **El código de este repositorio** está bajo licencia [MIT](LICENSE).
- **Los datos NO son MIT**: cada dataset conserva la licencia de su entidad productora (SDP, SDHT, SDS, SDE, IDRD...), indicada en la tabla de la sección [Datos](#datos). Por ejemplo, los indicadores de "Hábitat en cifras" y el registro social son CC BY-NC-SA 4.0 (uso no comercial).
- Si reutilizas los datos, respeta la licencia original de cada entidad y cítala en tu trabajo.
