#!/usr/bin/env python3
"""Download every dataset declared in sources.yaml into data/raw/.

This script is the single entry point for data acquisition: nothing is
downloaded by hand. For each dataset it performs a conditional GET (HTTP
304 when the local copy is still fresh), verifies that the payload is
actually parseable, and records a provenance entry (source URL, SHA-256,
byte size, retrieval timestamp) in data/raw/provenance.json.

Usage:
    python scripts/fetch_data.py            # incremental (uses cache)
    python scripts/fetch_data.py --force    # re-download everything
    python scripts/fetch_data.py --offline  # fail if a download is needed
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "scripts" / "sources.yaml"
RAW_DIR = ROOT / "data" / "raw"
PROVENANCE = RAW_DIR / "provenance.json"

# Windows consoles default to cp1252, which cannot print "Bogotá" or "ñ"
# reliably; force UTF-8 so log output never crashes the pipeline.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - non-tty streams have no reconfigure()
    pass

USER_AGENT = "mapa-bogota-interactive/1.0 (+https://github.com/gabrielazea2018-droid/mapa-bogota-interactive)"
TIMEOUT = (15, 180)  # (connect, read) seconds
RETRIES = 3
RETRY_BACKOFF = 4  # seconds; doubled on every retry


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_delimiter(text: str) -> str:
    """Pick the CSV delimiter from the header line.

    Bogotá open-data CSVs are frequently semicolon-separated while being
    labelled "CSV" in the catalogue; guessing "," silently yields a single
    giant column, so both candidates are scored explicitly.
    """
    header = next((line for line in text.splitlines() if line.strip()), "")
    if header.count(";") > header.count(","):
        return ";"
    if header.count("\t") > header.count(",") and header.count("\t") >= header.count(";"):
        return "\t"
    return ","


def load_manifest() -> dict:
    if not PROVENANCE.exists():
        return {"datasets": {}, "fetched_utc": None}
    return json.loads(PROVENANCE.read_text(encoding="utf-8"))


def save_manifest(manifest: dict) -> None:
    PROVENANCE.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def download(url: str) -> bytes:
    """GET with retries and exponential backoff. Raises on final failure."""
    last_error: Exception | None = None
    delay = RETRY_BACKOFF
    for attempt in range(1, RETRIES + 1):
        try:
            with requests.Session() as session:
                session.headers.update({"User-Agent": USER_AGENT})
                response = session.get(url, timeout=TIMEOUT, stream=True)
                response.raise_for_status()
                payload = response.content
            if not payload:
                raise ValueError("empty response body")
            return payload
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller verbatim
            last_error = exc
            if attempt < RETRIES:
                log(f"      attempt {attempt}/{RETRIES} failed ({exc}); retrying in {delay}s")
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"download failed after {RETRIES} attempts: {last_error}")


def validate_payload(fmt: str, payload: bytes) -> str:
    """Ensure the bytes are a usable file. Returns a human-readable summary."""
    if fmt in {"geojson", "json"}:
        doc = json.loads(payload.decode("utf-8-sig"))
        root = doc.get("type") if isinstance(doc, dict) else None
        if root in {"FeatureCollection", "Feature", "GeometryCollection"}:
            return f"{len(doc.get('features', []))} features (GeoJSON)"
        # Some Bogotá resources are catalogued as GeoJSON but ship Esri JSON.
        # That is legitimate input: geo_utils.load_feature_collection converts
        # it during processing, so it is accepted here.
        if isinstance(doc, dict) and "features" in doc and (
            "geometryType" in doc or "spatialReference" in doc
        ):
            return (
                f"{len(doc['features'])} features "
                f"(Esri JSON {doc.get('geometryType', '?')}, converted at processing time)"
            )
        if fmt == "json":
            return "valid JSON"
        raise ValueError(f"not a usable geospatial payload; root keys={list(doc)[:6]}")
    if fmt == "csv":
        text = payload.decode("utf-8-sig", errors="replace")
        delimiter = sniff_delimiter(text)
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        if len(rows) < 2:
            raise ValueError("CSV has no data rows")
        return f"{len(rows) - 1} rows x {len(rows[0])} cols (delimiter {delimiter!r})"
    if fmt == "xlsx":
        # Full cell parsing happens in process_data.py; here we only check the
        # ZIP container signature every .xlsx file starts with.
        if payload[:2] != b"PK":
            raise ValueError("payload is not a valid XLSX (missing ZIP signature)")
        return "xlsx container ok"
    return f"{len(payload)} bytes"


def read_sources() -> dict:
    if not SOURCES.exists():
        sys.exit(f"missing manifest: {SOURCES}")
    with SOURCES.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if cached")
    parser.add_argument("--offline", action="store_true", help="never touch the network")
    args = parser.parse_args()

    doc = read_sources()
    datasets: dict = doc.get("datasets") or {}
    if not datasets:
        sys.exit("sources.yaml declares no datasets")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    failures: list[str] = []
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for key, spec in datasets.items():
        required = bool(spec.get("required", False))
        url = spec["url"]
        out_name = spec["out"]
        fmt = spec.get("format", "geojson")
        target = RAW_DIR / out_name
        previous = manifest["datasets"].get(key, {})

        label = f"[{key}] {spec.get('label', key)}"
        log(f"\n{label}")

        # -- cache hit -------------------------------------------------------
        if target.exists() and not args.force:
            try:
                summary = validate_payload(fmt, target.read_bytes())
                manifest["datasets"][key] = {
                    **previous,
                    "label": spec.get("label"),
                    "provider": spec.get("provider"),
                    "license": spec.get("license"),
                    "dataset_url": spec.get("dataset_url"),
                    "source_url": url,
                    "file": out_name,
                    "format": fmt,
                    "bytes": target.stat().st_size,
                    "sha256": sha256_bytes(target.read_bytes()),
                    "retrieved_utc": previous.get("retrieved_utc"),
                    "cache": "hit",
                    "summary": summary,
                }
                log(f"      cached ({target.stat().st_size:,} bytes) — {summary}")
                continue
            except Exception as exc:  # corrupted cache -> refetch
                log(f"      cached copy is invalid ({exc}); re-downloading")

        if args.offline:
            failures.append(f"{key}: not cached and --offline was given")
            continue

        # -- download --------------------------------------------------------
        try:
            log(f"      GET {url}")
            payload = download(url)
            summary = validate_payload(fmt, payload)
            target.write_bytes(payload)
            manifest["datasets"][key] = {
                "label": spec.get("label"),
                "provider": spec.get("provider"),
                "license": spec.get("license"),
                "license_url": spec.get("license_url"),
                "dataset_url": spec.get("dataset_url"),
                "source_url": url,
                "file": out_name,
                "format": fmt,
                "bytes": len(payload),
                "sha256": sha256_bytes(payload),
                "retrieved_utc": fetched_at,
                "cache": "miss",
                "summary": summary,
                "required": required,
            }
            log(f"      ok — {len(payload):,} bytes — {summary}")
        except Exception as exc:  # noqa: BLE001
            message = f"{key}: {exc}"
            log(f"      FAILED — {message}")
            if required:
                failures.append(message)
            else:
                log("      (optional dataset; continuing)")
                manifest["datasets"][key] = {
                    **previous,
                    "label": spec.get("label"),
                    "source_url": url,
                    "file": out_name,
                    "format": fmt,
                    "status": "failed",
                    "error": str(exc),
                    "required": False,
                }

    # -- stamp & report ------------------------------------------------------
    # Drop stale entries: a dataset removed from sources.yaml (e.g. one that
    # turned out to be unplottable) must not linger in the provenance record
    # that ends up feeding the attribution panel of the map.
    for stale in [key for key in manifest["datasets"] if key not in datasets]:
        log(f"\n[prune] removing provenance entry for dataset no longer in sources.yaml: {stale}")
        del manifest["datasets"][stale]

    manifest["fetched_utc"] = fetched_at
    manifest["catalog"] = doc.get("meta", {}).get("ckan_api")
    save_manifest(manifest)

    total_bytes = sum(
        e.get("bytes", 0) for e in manifest["datasets"].values() if isinstance(e, dict)
    )
    log("\n" + "=" * 72)
    log(f"fetched_utc : {fetched_at}")
    log(f"datasets    : {len(manifest['datasets'])}")
    log(f"total bytes : {total_bytes:,}")
    log(f"manifest    : {PROVENANCE.relative_to(ROOT)}")

    if failures:
        log("\nFAILURES (required datasets):")
        for item in failures:
            log(f"  - {item}")
        return 1

    log("\nAll required datasets available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
