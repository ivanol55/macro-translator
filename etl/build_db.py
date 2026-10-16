#!/usr/bin/env python3
"""Build the macro-translator DuckDB database from the official data dumps.

Sources
-------
- Open Food Facts (packaged products): the official Parquet dump on Hugging Face.
  Licence: ODbL 1.0 (database) and DbCL 1.0 (contents).
- USDA FoodData Central (generic foods): the Foundation Foods and SR Legacy CSV
  downloads. Licence: public domain (CC0 1.0).

What the script does
--------------------
1. Downloads each source into DATA_DIR/cache, or uses the cached copy when the
   remote file did not change. Interrupted downloads continue where they stopped.
2. Reads the sources with DuckDB, keeps the rows that have kcal, protein,
   carbohydrate and fat per 100 g, and removes incoherent rows.
3. Writes the table `alimentos`, two full-text search indexes and the table
   `metadata` into a new database file, then replaces DATA_DIR/macros.duckdb.

The script is idempotent: every run builds the database from zero.
This file is the complete method that produces the derived database (ODbL 4.6).

Usage
-----
    python etl/build_db.py                   # download if needed, then build
    python etl/build_db.py --force-download  # download everything again
    python etl/build_db.py --offline         # use only the cached files
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx

log = logging.getLogger("etl")

USER_AGENT = "macro-translator-etl/0.1 (+https://github.com/ivanol55/macro-translator)"

OFF_PARQUET_URL = (
    "https://huggingface.co/datasets/openfoodfacts/product-database/"
    "resolve/main/food.parquet?download=true"
)
FDC_DOWNLOAD_PAGE = "https://fdc.nal.usda.gov/download-datasets"
FDC_DATASETS_BASE = "https://fdc.nal.usda.gov/fdc-datasets/"
# Used only when the download page cannot be read.
USDA_FOUNDATION_FALLBACK_URL = (
    FDC_DATASETS_BASE + "FoodData_Central_foundation_food_csv_2026-04-30.zip"
)
USDA_SR_LEGACY_URL = FDC_DATASETS_BASE + "FoodData_Central_sr_legacy_food_csv_2018-04.zip"

# FoodData Central nutrient IDs. The script checks them against nutrient.csv.
USDA_NUTRIENTS = {
    1003: ("Protein", "G"),
    1004: ("Total lipid (fat)", "G"),
    1005: ("Carbohydrate, by difference", "G"),
    1008: ("Energy", "KCAL"),
    2047: ("Energy (Atwater General Factors)", "KCAL"),
    2048: ("Energy (Atwater Specific Factors)", "KCAL"),
}
USDA_DATA_TYPES = ("foundation_food", "sr_legacy_food")
USDA_CSV_FILES = ("food.csv", "food_nutrient.csv", "nutrient.csv")

# Cleaning rules (both sources).
MAX_KCAL_PER_100G = 900
COHERENCE_TOLERANCE = 0.20  # |P*4 + C*4 + G*9 - kcal| / kcal
# Absolute tolerance in kcal/100 g. Without it, foods near 0 kcal (water, diet
# drinks, spices) fail the relative test because of rounding on the label.
COHERENCE_MIN_ABS_KCAL = 5
# Protein + carbohydrate + fat cannot be more than 100 g in 100 g.
MAX_MACRO_GRAMS = 101

# Small stop-word list for the Spanish index. "sin" and "con" stay searchable
# because "sin azucar" and "con tinta" change the product.
SPANISH_STOPWORDS = (
    "de",
    "del",
    "la",
    "el",
    "los",
    "las",
    "en",
    "y",
    "a",
    "al",
    "un",
    "una",
    "unos",
    "unas",
    "para",
    "por",
    "o",
    "e",
)

COUNTRY_TAG_RE = re.compile(r"^[a-z]{2,3}:[a-z0-9-]+$")
LANG_RE = re.compile(r"^[a-z]{2,3}$")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    data_dir: Path
    db_path: Path
    off_url: str
    country_tag: str
    lang: str
    usda_foundation_url: str  # empty = newest release on the download page
    usda_sr_legacy_url: str
    force_download: bool
    offline: bool
    memory_limit: str

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def load_config(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    parser.add_argument("--db", default=os.environ.get("DB_PATH", ""))
    parser.add_argument(
        "--force-download",
        action="store_true",
        default=os.environ.get("ETL_FORCE_DOWNLOAD", "0") == "1",
    )
    parser.add_argument(
        "--offline", action="store_true", help="Do not use the network. Use only the cached files."
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    country = os.environ.get("OFF_COUNTRY_TAG", "en:spain").strip().lower()
    lang = os.environ.get("OFF_LANG", "es").strip().lower()
    if not COUNTRY_TAG_RE.match(country):
        raise SystemExit(f"OFF_COUNTRY_TAG has an incorrect format: {country!r} (en:spain)")
    if not LANG_RE.match(lang):
        raise SystemExit(f"OFF_LANG has an incorrect format: {lang!r} (example: es)")
    return Config(
        data_dir=data_dir,
        db_path=Path(args.db) if args.db else data_dir / "macros.duckdb",
        off_url=os.environ.get("OFF_PARQUET_URL") or OFF_PARQUET_URL,
        country_tag=country,
        lang=lang,
        usda_foundation_url=os.environ.get("USDA_FOUNDATION_URL", "").strip(),
        usda_sr_legacy_url=os.environ.get("USDA_SR_LEGACY_URL") or USDA_SR_LEGACY_URL,
        force_download=args.force_download,
        offline=args.offline,
        memory_limit=os.environ.get("DUCKDB_MEMORY_LIMIT", "").strip(),
    )


# --------------------------------------------------------------------------- #
# Downloads with cache and resume
# --------------------------------------------------------------------------- #
@dataclass
class Download:
    path: Path
    url: str
    downloaded_at: str  # ISO 8601, UTC
    last_modified: str | None
    etag: str | None
    size: int
    sha256: str | None = None


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def _read_meta(path: Path) -> dict | None:
    try:
        return json.loads(_meta_path(path).read_text())
    except (OSError, ValueError):
        return None


def _from_meta(meta: dict, dest: Path) -> Download:
    """Cached download facts. The path is where the file is now (host or container)."""
    fields = {k: meta.get(k) for k in Download.__dataclass_fields__}
    return Download(**(fields | {"path": dest}))


def _clean_etag(value: str | None) -> str | None:
    if not value:
        return None
    return value.removeprefix("W/").strip('"')


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download(client: httpx.Client | None, url: str, dest: Path, force: bool = False) -> Download:
    """Download url to dest. Use the cached file when the remote ETag did not change.

    The partial file (dest + ".part") stays on disk when the download stops,
    and the next run continues it with an HTTP Range request.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    cached = _read_meta(dest) if dest.exists() else None

    if client is None:  # offline
        if cached is None:
            raise SystemExit(f"Offline mode, but there is no cached copy of {url} at {dest}")
        log.info("offline: use cached %s (downloaded %s)", dest.name, cached["downloaded_at"])
        return _from_meta(cached, dest)

    try:
        head = client.head(url, follow_redirects=True)
        head.raise_for_status()
        etag = _clean_etag(head.headers.get("etag"))
        size = int(head.headers.get("content-length") or 0)
        last_modified = head.headers.get("last-modified")
        # Hugging Face puts the SHA-256 of LFS/Xet files in X-Linked-Etag on the
        # redirect. The ETag of the final CDN response is a different hash.
        sha256 = None
        for hop in [*head.history, head]:
            linked = _clean_etag(hop.headers.get("x-linked-etag"))
            if linked and re.fullmatch(r"[0-9a-f]{64}", linked):
                sha256 = linked
    except httpx.HTTPError as exc:
        if cached is not None:
            log.warning("cannot reach %s (%s). Use the cached copy.", url, exc)
            return _from_meta(cached, dest)
        raise

    if cached is not None and not force:
        # Some servers (FDC) run several backends with different ETags for the
        # same file. Size plus Last-Modified is then a good enough match.
        same = (
            (sha256 and sha256 == cached.get("sha256"))
            or (etag and etag == cached.get("etag"))
            or (
                size
                and size == cached.get("size")
                and last_modified
                and last_modified == cached.get("last_modified")
            )
        )
        if same and dest.stat().st_size == cached.get("size"):
            log.info("cache is current: %s (downloaded %s)", dest.name, cached["downloaded_at"])
            return _from_meta(cached, dest)

    part = dest.with_name(dest.name + ".part")
    part_meta = _read_meta(part) or {}
    if force or (etag and part_meta.get("etag") and part_meta.get("etag") != etag):
        part.unlink(missing_ok=True)
    _meta_path(part).write_text(json.dumps({"etag": etag, "url": url}))

    start = part.stat().st_size if part.exists() else 0
    if size and start > size:
        part.unlink()
        start = 0

    if not size or start < size:
        headers = {}
        if start:
            headers["Range"] = f"bytes={start}-"
            if etag:
                headers["If-Range"] = f'"{etag}"'
        log.info("download %s -> %s (%s bytes, from byte %s)", url, dest.name, size or "?", start)
        t0 = time.monotonic()
        with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
            if resp.status_code == 416:  # nothing left to download
                pass
            else:
                resp.raise_for_status()
                mode = "ab" if resp.status_code == 206 else "wb"
                done = start if mode == "ab" else 0
                next_report = done + 500 * 1024 * 1024
                with part.open(mode) as fh:
                    for chunk in resp.iter_bytes(1024 * 1024):
                        fh.write(chunk)
                        done += len(chunk)
                        if done >= next_report:
                            log.info("  %.1f GB", done / 1e9)
                            next_report += 500 * 1024 * 1024
        log.info("download finished in %.0f s", time.monotonic() - t0)

    got = part.stat().st_size
    if size and got != size:
        raise SystemExit(f"Download of {url} is incomplete: {got} of {size} bytes. Run again.")
    if sha256:
        log.info("check the SHA-256 of %s", dest.name)
        if _sha256(part) != sha256:
            part.unlink()
            raise SystemExit(f"The SHA-256 of {dest.name} is not correct. Run the ETL again.")

    part.replace(dest)
    _meta_path(part).unlink(missing_ok=True)
    info = Download(
        path=dest,
        url=url,
        downloaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        last_modified=last_modified,
        etag=etag,
        size=got,
        sha256=sha256,
    )
    meta = asdict(info) | {"path": dest.name}
    _meta_path(dest).write_text(json.dumps(meta, indent=2))
    return info


def newest_foundation_url(client: httpx.Client | None) -> str:
    """Find the newest Foundation Foods CSV release on the FDC download page."""
    if client is None:
        return USDA_FOUNDATION_FALLBACK_URL
    try:
        html = client.get(FDC_DOWNLOAD_PAGE, follow_redirects=True).text
    except httpx.HTTPError as exc:
        log.warning(
            "cannot read the FDC download page (%s). Use %s", exc, USDA_FOUNDATION_FALLBACK_URL
        )
        return USDA_FOUNDATION_FALLBACK_URL
    dates = re.findall(r"FoodData_Central_foundation_food_csv_(\d{4}-\d{2}-\d{2})\.zip", html)
    if not dates:
        log.warning("no Foundation Foods release found on the download page. Use the fallback.")
        return USDA_FOUNDATION_FALLBACK_URL
    return f"{FDC_DATASETS_BASE}FoodData_Central_foundation_food_csv_{max(dates)}.zip"


def extract_usda_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Extract only the CSV files the ETL needs. Never trust paths inside the zip."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = Path(member.filename).name
            if name in USDA_CSV_FILES and not member.is_dir():
                with zf.open(member) as src, (dest_dir / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    missing = [f for f in USDA_CSV_FILES if not (dest_dir / f).exists()]
    if missing:
        raise SystemExit(f"{zip_path.name} does not contain {missing}")
    return dest_dir


def usda_release(url: str) -> str:
    m = re.search(r"_(\d{4}-\d{2}(?:-\d{2})?)\.zip", url)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# Database build
# --------------------------------------------------------------------------- #
@dataclass
class SourceInfo:
    """Facts about one source for the metadata table."""

    fuente: str
    url: str
    fecha_descarga: str
    publicacion: str | None
    filtro_pais: str | None


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _coherent_sql(k: str, p: str, c: str, g: str) -> str:
    return (
        f"abs({p} * 4 + {c} * 4 + {g} * 9 - {k}) "
        f"<= greatest({COHERENCE_TOLERANCE} * {k}, {COHERENCE_MIN_ABS_KCAL})"
    )


def _clean_filter_sql() -> str:
    return (
        f"kcal >= 0 AND prot >= 0 AND carbs >= 0 AND grasas >= 0 "
        f"AND kcal <= {MAX_KCAL_PER_100G} "
        f"AND prot + carbs + grasas <= {MAX_MACRO_GRAMS} "
        f"AND {_coherent_sql('kcal', 'prot', 'carbs', 'grasas')}"
    )


def load_off(con: duckdb.DuckDBPyConnection, parquet: Path, country_tag: str, lang: str) -> dict:
    """Load Open Food Facts rows for one country into the table off_clean."""
    src = _sql_str(str(parquet))
    tag = _sql_str(country_tag)
    lng = _sql_str(lang)

    def text_in(lang_expr: str) -> str:
        return (
            "list_filter(product_name, lambda x: x.lang = "
            f"{lang_expr} AND nullif(trim(x.text), '') IS NOT NULL)[1].text"
        )

    def nutrient(name: str) -> str:
        return (
            f"round(CAST(list_filter(nutriments, lambda x: x.name = '{name}')[1].\"100g\" "
            "AS DOUBLE), 3)"
        )

    con.execute(f"""
        CREATE TEMP TABLE off_country AS
        SELECT code, product_name, brands, nutriments, unique_scans_n
        FROM read_parquet({src})
        WHERE list_contains(countries_tags, {tag}) AND NOT coalesce(obsolete, false)
    """)
    con.execute(f"""
        CREATE TEMP TABLE off_flat AS
        SELECT
            nullif(trim(code), '') AS codigo_barras,
            trim(regexp_replace(coalesce(
                {text_in(lng)},
                {text_in("'main'")},
                list_filter(product_name, lambda x: nullif(trim(x.text), '') IS NOT NULL)[1].text
            ), '\\s+', ' ', 'g')) AS nombre,
            nullif(trim(regexp_replace(brands, '\\s*,\\s*', ', ', 'g')), '') AS marca,
            {nutrient("energy-kcal")} AS kcal,
            {nutrient("proteins")} AS prot,
            {nutrient("carbohydrates")} AS carbs,
            {nutrient("fat")} AS grasas,
            coalesce(unique_scans_n, 0) AS popularidad
        FROM off_country
    """)
    con.execute("""
        CREATE TEMP TABLE off_complete AS
        SELECT * FROM off_flat
        WHERE nullif(nombre, '') IS NOT NULL
          AND kcal IS NOT NULL AND prot IS NOT NULL AND carbs IS NOT NULL AND grasas IS NOT NULL
    """)
    con.execute(f"""
        CREATE TEMP TABLE off_clean AS
        SELECT * FROM off_complete
        WHERE {_clean_filter_sql()}
        QUALIFY row_number() OVER (
            PARTITION BY coalesce(codigo_barras, nombre) ORDER BY popularidad DESC) = 1
    """)
    counts = con.execute("""
        SELECT (SELECT count(*) FROM off_country), (SELECT count(*) FROM off_complete),
               (SELECT count(*) FROM off_clean)
    """).fetchone()
    return dict(
        zip(("filas_fuente", "filas_antes_limpieza", "filas_despues_limpieza"), counts, strict=True)
    )


def check_usda_nutrients(con: duckdb.DuckDBPyConnection, csv_dir: Path) -> None:
    rows = con.execute(
        "SELECT id, name, unit_name FROM read_csv(?, header = true, all_varchar = true)",
        [str(csv_dir / "nutrient.csv")],
    ).fetchall()
    found = {int(r[0]): (r[1], r[2]) for r in rows}
    for nid, (name, unit) in USDA_NUTRIENTS.items():
        if found.get(nid) != (name, unit):
            raise SystemExit(
                f"USDA nutrient {nid} is {found.get(nid)!r} in {csv_dir}, "
                f"expected {(name, unit)!r}. "
                "Check the nutrient IDs in etl/build_db.py."
            )


def load_usda(con: duckdb.DuckDBPyConnection, csv_dir: Path, table: str) -> dict:
    """Load one USDA FDC CSV release into a table with values per 100 g."""
    check_usda_nutrients(con, csv_dir)
    food = _sql_str(str(csv_dir / "food.csv"))
    fn = _sql_str(str(csv_dir / "food_nutrient.csv"))
    types = ", ".join(_sql_str(t) for t in USDA_DATA_TYPES)
    ids = ", ".join(str(i) for i in USDA_NUTRIENTS)
    con.execute(f"""
        CREATE TEMP TABLE {table}_food AS
        SELECT CAST(fdc_id AS BIGINT) AS fdc_id, trim(description) AS nombre
        FROM read_csv({food}, header = true, all_varchar = true)
        WHERE data_type IN ({types}) AND nullif(trim(description), '') IS NOT NULL
    """)
    con.execute(f"""
        CREATE TEMP TABLE {table}_wide AS
        WITH n AS (
            SELECT CAST(fdc_id AS BIGINT) AS fdc_id, CAST(nutrient_id AS INTEGER) AS nid,
                   CAST(amount AS DOUBLE) AS amount
            FROM read_csv({fn}, header = true, all_varchar = true)
            WHERE CAST(nutrient_id AS INTEGER) IN ({ids})
        )
        SELECT f.fdc_id, f.nombre,
            round(coalesce(max(amount) FILTER (nid = 1008), max(amount) FILTER (nid = 2048),
                           max(amount) FILTER (nid = 2047)), 3) AS kcal,
            round(max(amount) FILTER (nid = 1003), 3) AS prot,
            round(max(amount) FILTER (nid = 1005), 3) AS carbs,
            round(max(amount) FILTER (nid = 1004), 3) AS grasas
        FROM {table}_food f LEFT JOIN n USING (fdc_id)
        GROUP BY f.fdc_id, f.nombre
    """)
    con.execute(f"""
        CREATE TEMP TABLE {table} AS
        SELECT * FROM {table}_wide
        WHERE kcal IS NOT NULL AND prot IS NOT NULL AND carbs IS NOT NULL AND grasas IS NOT NULL
          AND {_clean_filter_sql()}
    """)
    counts = con.execute(f"""
        SELECT (SELECT count(*) FROM {table}_food),
               (SELECT count(*) FROM {table}_wide WHERE kcal IS NOT NULL AND prot IS NOT NULL
                    AND carbs IS NOT NULL AND grasas IS NOT NULL),
               (SELECT count(*) FROM {table})
    """).fetchone()
    return dict(
        zip(("filas_fuente", "filas_antes_limpieza", "filas_despues_limpieza"), counts, strict=True)
    )


def build_database(
    db_path: Path,
    off_parquet: Path,
    usda_dirs: list[tuple[SourceInfo, Path]],
    off_info: SourceInfo,
    country_tag: str,
    lang: str,
    memory_limit: str = "",
    temp_dir: Path | None = None,
) -> dict:
    """Build the database into a temporary file, then replace db_path."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(db_path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    Path(str(tmp) + ".wal").unlink(missing_ok=True)

    con = duckdb.connect(str(tmp))
    try:
        if memory_limit:
            con.execute(f"SET memory_limit = {_sql_str(memory_limit)}")
        if temp_dir:
            temp_dir.mkdir(parents=True, exist_ok=True)
            con.execute(f"SET temp_directory = {_sql_str(str(temp_dir))}")
        con.execute("SET preserve_insertion_order = false")
        con.execute("INSTALL fts")
        con.execute("LOAD fts")

        t0 = time.monotonic()
        stats = {off_info.fuente: load_off(con, off_parquet, country_tag, lang)}
        log.info("Open Food Facts: %s (%.0f s)", stats[off_info.fuente], time.monotonic() - t0)
        for i, (info, csv_dir) in enumerate(usda_dirs):
            stats[info.fuente] = load_usda(con, csv_dir, f"usda_{i}")
            log.info("%s: %s", info.fuente, stats[info.fuente])

        usda_union = " UNION ALL ".join(
            f"SELECT fdc_id, nombre, kcal, prot, carbs, grasas FROM usda_{i}"
            for i in range(len(usda_dirs))
        ) or (
            "SELECT NULL::BIGINT fdc_id, NULL::VARCHAR nombre, NULL::DOUBLE kcal, "
            "NULL::DOUBLE prot, NULL::DOUBLE carbs, NULL::DOUBLE grasas WHERE false"
        )

        con.execute("""
            CREATE TABLE alimentos (
                id            INTEGER PRIMARY KEY,
                fuente        VARCHAR NOT NULL CHECK (fuente IN ('OFF', 'USDA')),
                nombre        VARCHAR NOT NULL,
                marca         VARCHAR,
                codigo_barras VARCHAR,
                kcal          DOUBLE NOT NULL,
                prot          DOUBLE NOT NULL,
                carbs         DOUBLE NOT NULL,
                grasas        DOUBLE NOT NULL
            )
        """)
        con.execute("""
            CREATE TEMP TABLE off_all AS
            SELECT row_number() OVER (ORDER BY codigo_barras, nombre) AS id, * FROM off_clean
        """)
        con.execute("""
            INSERT INTO alimentos
            SELECT id, 'OFF', nombre, marca, codigo_barras, kcal, prot, carbs, grasas FROM off_all
        """)
        offset = con.execute("SELECT coalesce(max(id), 0) FROM alimentos").fetchone()[0]
        con.execute(f"""
            CREATE TEMP TABLE usda_all AS
            SELECT {offset} + row_number() OVER (ORDER BY fdc_id) AS id, *
            FROM ({usda_union})
        """)
        con.execute("""
            INSERT INTO alimentos
            SELECT id, 'USDA', nombre, NULL, NULL, kcal, prot, carbs, grasas FROM usda_all
        """)

        # Search tables: one per source, so each index uses its own language stemmer.
        con.execute("""
            CREATE TABLE indice_off AS
            SELECT id, nombre || coalesce(' ' || marca, '') AS texto, popularidad FROM off_all
        """)
        con.execute("CREATE TABLE indice_usda AS SELECT id, nombre AS texto, fdc_id FROM usda_all")
        con.execute("CREATE TABLE stopwords_es (sw VARCHAR)")
        con.executemany("INSERT INTO stopwords_es VALUES (?)", [[w] for w in SPANISH_STOPWORDS])
        stemmer = "spanish" if lang == "es" else "english" if lang == "en" else "none"
        stopwords = "stopwords_es" if lang == "es" else "english" if lang == "en" else "none"
        con.execute(f"""
            PRAGMA create_fts_index('indice_off', 'id', 'texto', stemmer = '{stemmer}',
                stopwords = '{stopwords}', strip_accents = 1, lower = 1, overwrite = 1)
        """)
        con.execute("""
            PRAGMA create_fts_index('indice_usda', 'id', 'texto', stemmer = 'english',
                stopwords = 'english', strip_accents = 1, lower = 1, overwrite = 1)
        """)

        now = datetime.now(UTC).isoformat(timespec="seconds")
        con.execute("""
            CREATE TABLE metadata (
                fuente                 VARCHAR PRIMARY KEY,
                url                    VARCHAR,
                fecha_descarga         VARCHAR,
                publicacion            VARCHAR,
                filtro_pais            VARCHAR,
                filas_fuente           BIGINT,
                filas_antes_limpieza   BIGINT,
                filas_despues_limpieza BIGINT,
                generado_en            VARCHAR
            )
        """)
        for info in [off_info] + [i for i, _ in usda_dirs]:
            s = stats[info.fuente]
            con.execute(
                "INSERT INTO metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    info.fuente,
                    info.url,
                    info.fecha_descarga,
                    info.publicacion,
                    info.filtro_pais,
                    s["filas_fuente"],
                    s["filas_antes_limpieza"],
                    s["filas_despues_limpieza"],
                    now,
                ],
            )
        con.execute("CHECKPOINT")
    finally:
        con.close()

    tmp.replace(db_path)
    log.info("database ready: %s (%.1f MB)", db_path, db_path.stat().st_size / 1e6)
    return stats


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(argv)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    client = (
        None
        if cfg.offline
        else httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(60, read=300))
    )
    try:
        off = download(client, cfg.off_url, cfg.cache_dir / "off_food.parquet", cfg.force_download)
        foundation_url = cfg.usda_foundation_url or newest_foundation_url(client)
        usda = []
        for name, url in (
            ("USDA Foundation Foods", foundation_url),
            ("USDA SR Legacy", cfg.usda_sr_legacy_url),
        ):
            dl = download(
                client, url, cfg.cache_dir / Path(url.split("?")[0]).name, cfg.force_download
            )
            csv_dir = extract_usda_zip(dl.path, cfg.cache_dir / (dl.path.stem + "_csv"))
            usda.append((SourceInfo(name, url, dl.downloaded_at, usda_release(url), None), csv_dir))
    finally:
        if client is not None:
            client.close()

    off_info = SourceInfo(
        "Open Food Facts", cfg.off_url, off.downloaded_at, off.last_modified, cfg.country_tag
    )
    build_database(
        cfg.db_path,
        off.path,
        usda,
        off_info,
        cfg.country_tag,
        cfg.lang,
        cfg.memory_limit,
        cfg.data_dir / "tmp",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
