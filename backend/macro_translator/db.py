"""Read-only access to the DuckDB file that the ETL builds."""

from __future__ import annotations

import math
import re
import threading
import unicodedata
from pathlib import Path

import duckdb

from .models import Candidato

# How many BM25 hits to read before the re-ranking and de-duplication.
POOL = 40
# Words that the USDA index needs for the state of the food.
ESTADO_EN = {"crudo": "raw", "cocido": "cooked"}
# Re-ranking weights for Open Food Facts, on top of the BM25 score.
W_POPULARIDAD = 0.15  # x ln(1 + unique scans)
W_MARCA = 2.0  # the brand the user wrote is in the product brands
W_PRECISION = 2.0  # x share of the product-name words that are in the query
STOPWORDS_ES = {"de", "del", "la", "el", "los", "las", "en", "y", "a", "al", "con", "un", "una"}


class DatabaseMissing(RuntimeError):
    pass


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _norm(text)))


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def precision(nombre: str, query: str) -> float:
    """Share of the product-name words that the query contains (0..1).

    It moves plain products ("Griego") above variants with extra words
    ("Yogur griego stracciatella") when the user did not write those words.
    """
    q = {_singular(w) for w in _words(query)}
    n = {_singular(w) for w in _words(nombre) - STOPWORDS_ES}
    return len(n & q) / len(n) if n else 0.0


class FoodDB:
    """Open the database read-only. Open it again when the ETL replaces the file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._con: duckdb.DuckDBPyConnection | None = None
        self._stamp: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def _connection(self) -> duckdb.DuckDBPyConnection:
        try:
            st = self.path.stat()
        except FileNotFoundError as exc:
            raise DatabaseMissing(
                f"No database at {self.path}. Run the ETL: "
                "docker compose --profile etl run --rm etl"
            ) from exc
        stamp = (st.st_ino, st.st_mtime_ns)
        with self._lock:
            if self._con is None or stamp != self._stamp:
                if self._con is not None:
                    self._con.close()
                self._con = duckdb.connect(str(self.path), read_only=True)
                self._con.execute("LOAD fts")
                self._stamp = stamp
            return self._con.cursor()

    def available(self) -> bool:
        try:
            self._connection().close()
            return True
        except (DatabaseMissing, duckdb.Error):
            return False

    def metadata(self) -> list[dict]:
        cur = self._connection()
        try:
            rows = cur.execute("SELECT * FROM metadata ORDER BY fuente").fetchall()
            cols = [d[0] for d in cur.description]
        finally:
            cur.close()
        return [dict(zip(cols, r, strict=True)) for r in rows]

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    def _bm25(self, index: str, query: str, extra_cols: str) -> list[tuple]:
        if not _words(query):
            return []
        cur = self._connection()
        try:
            return cur.execute(
                f"""
                SELECT a.id, a.fuente, a.nombre, a.marca, a.codigo_barras,
                       a.kcal, a.prot, a.carbs, a.grasas, s.score {extra_cols}
                FROM (
                    SELECT *, fts_main_{index}.match_bm25(id, ?) AS score FROM {index}
                ) s
                JOIN alimentos a USING (id)
                WHERE s.score IS NOT NULL
                ORDER BY s.score DESC
                LIMIT {POOL}
                """,
                [query],
            ).fetchall()
        finally:
            cur.close()

    @staticmethod
    def _dedupe(rows: list[tuple], limit: int) -> list[tuple]:
        seen: set[tuple] = set()
        out = []
        for r in rows:
            # Same name and same values = same food for the user, also with another brand.
            key = (_norm(r[2]), r[5], r[6], r[7], r[8])
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            if len(out) == limit:
                break
        return out

    def buscar_off(self, nombre: str, marca: str | None = None, limit: int = 5) -> list[Candidato]:
        query = f"{nombre} {marca or ''}".strip()
        rows = self._bm25("indice_off", query, ", s.popularidad")
        marca_words = _words(marca or "")

        def score(r: tuple) -> float:
            s = r[9] + W_POPULARIDAD * math.log1p(r[10] or 0) + W_PRECISION * precision(r[2], query)
            if marca_words and marca_words & _words(r[3] or ""):
                s += W_MARCA
            return s

        rows.sort(key=score, reverse=True)
        return [
            Candidato(
                clave=f"O{i}",
                id=r[0],
                fuente="OFF",
                nombre=r[2],
                marca=r[3],
                codigo_barras=r[4],
                kcal=r[5],
                prot=r[6],
                carbs=r[7],
                grasas=r[8],
            )
            for i, r in enumerate(self._dedupe(rows, limit), start=1)
        ]

    def buscar_usda(self, nombre_en: str, estado: str = "n/a", limit: int = 5) -> list[Candidato]:
        extra = ESTADO_EN.get(estado, "")
        query = f"{nombre_en} {extra}".strip() if extra not in _words(nombre_en) else nombre_en
        rows = self._bm25("indice_usda", query, ", s.fdc_id")
        return [
            Candidato(
                clave=f"U{i}",
                id=r[0],
                fuente="USDA",
                nombre=r[2],
                marca=None,
                codigo_barras=None,
                fdc_id=r[10],
                kcal=r[5],
                prot=r[6],
                carbs=r[7],
                grasas=r[8],
            )
            for i, r in enumerate(self._dedupe(rows, limit), start=1)
        ]
