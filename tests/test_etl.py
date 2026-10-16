"""ETL: cleaning rules, country filter, USDA energy fallback, metadata, downloads."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import httpx
import pytest
from build_db import download, extract_usda_zip, usda_release
from conftest import OFF_DOWNLOAD_DATE, build_fixture_db


def rows(db_path: Path, sql: str, params: list | None = None) -> list[tuple]:
    with duckdb.connect(str(db_path), read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


def codes(db_path: Path) -> set[str]:
    return {r[0] for r in rows(db_path, "SELECT codigo_barras FROM alimentos WHERE fuente = 'OFF'")}


def test_off_keeps_the_reference_products(db_path):
    kept = codes(db_path)
    assert "2324200006036" in kept  # Sepia Faraónica Limpia (Hacendado)
    assert "8480000310101" in kept  # Huevos cocidos (Hacendado)
    assert "8480000047403" in kept  # Aceite de oliva virgen extra (Hacendado)
    sepia = rows(
        db_path,
        "SELECT nombre, marca, kcal, prot, carbs, grasas FROM alimentos "
        "WHERE codigo_barras = '2324200006036'",
    )[0]
    assert sepia == ("Sepia Faraónica Limpia", "Hacendado", 78.0, 18.0, 0.8, 0.5)


def test_off_country_filter(db_path):
    assert "6111099000254" not in codes(db_path)  # Huile d'olive, not sold in Spain


def test_off_cleaning(db_path):
    kept = codes(db_path)
    assert "0696859027390" not in kept  # 928 kcal per 100 g
    assert "8410762220059" not in kept  # kcal and macros do not match
    assert "647302400720002902" not in kept  # 888 kcal typo
    assert "8427889251132" not in kept  # no fat value


def test_no_row_breaks_the_rules(db_path):
    bad = rows(
        db_path,
        """
        SELECT count(*) FROM alimentos
        WHERE kcal > 900 OR kcal < 0 OR prot < 0 OR carbs < 0 OR grasas < 0
           OR abs(prot * 4 + carbs * 4 + grasas * 9 - kcal) > greatest(0.2 * kcal, 5)
    """,
    )
    assert bad == [(0,)]


def test_usda_energy_and_cleaning(db_path):
    usda = dict(rows(db_path, "SELECT nombre, kcal FROM alimentos WHERE fuente = 'USDA'"))
    assert usda["Egg, whole, cooked, hard-boiled"] == 155.0
    assert usda["Oil, olive, salad or cooking"] == 884.0
    # Foundation food without nutrient 1008: energy from Atwater specific factors (2048).
    assert usda["Eggplant, raw"] == pytest.approx(22.359, abs=0.001)
    assert "Oil, olive, extra virgin" not in usda  # Foundation oil without macros
    assert "Alcoholic beverage, beer, regular, all" not in usda  # alcohol: incoherent
    assert all(
        m is None for (m,) in rows(db_path, "SELECT marca FROM alimentos WHERE fuente = 'USDA'")
    )


def test_metadata(db_path):
    meta = {r[0]: r for r in rows(db_path, "SELECT * FROM metadata")}
    assert set(meta) == {"Open Food Facts", "USDA Foundation Foods", "USDA SR Legacy"}
    off = meta["Open Food Facts"]
    assert off[2] == OFF_DOWNLOAD_DATE
    assert off[4] == "en:spain"
    fuente, antes, despues = off[5], off[6], off[7]
    assert fuente == 23 and antes == 22  # 24 rows: 1 not in Spain, 1 without fat
    assert despues == len(codes(db_path))
    assert meta["USDA SR Legacy"][3] == "2018-04"


def test_rebuild_is_idempotent(tmp_path, off_parquet):
    db = tmp_path / "macros.duckdb"
    first = build_fixture_db(db, off_parquet)
    second = build_fixture_db(db, off_parquet)
    assert first == second
    assert not (tmp_path / "macros.duckdb.tmp").exists()


def test_name_in_the_configured_language(tmp_path):
    from build_db import load_off

    src = tmp_path / "names.parquet"
    duckdb.execute(f"""COPY (SELECT
        '0001' AS code,
        [{{'lang': 'main', 'text': 'Cuttlefish'}},
         {{'lang': 'es', 'text': 'Sepia'}}] AS product_name,
        'Marca' AS brands, ['en:spain'] AS countries_tags, false AS obsolete, 1 AS unique_scans_n,
        [{{'name': 'energy-kcal', '100g': 78.0}}, {{'name': 'proteins', '100g': 18.0}},
         {{'name': 'carbohydrates', '100g': 0.8}}, {{'name': 'fat', '100g': 0.5}}] AS nutriments
        ) TO '{src}' (FORMAT parquet)""")
    con = duckdb.connect()
    load_off(con, src, "en:spain", "es")
    assert con.execute("SELECT nombre FROM off_clean").fetchall() == [("Sepia",)]


def test_usda_release_from_url():
    assert (
        usda_release("https://x/FoodData_Central_foundation_food_csv_2026-04-30.zip")
        == "2026-04-30"
    )
    assert usda_release("https://x/FoodData_Central_sr_legacy_food_csv_2018-04.zip") == "2018-04"


def test_extract_ignores_paths_in_the_zip(tmp_path):
    import zipfile

    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name in ("food.csv", "food_nutrient.csv", "nutrient.csv"):
            zf.writestr(f"../../evil/{name}", "a,b\n")
    out = extract_usda_zip(z, tmp_path / "out")
    assert sorted(p.name for p in out.iterdir()) == [
        "food.csv",
        "food_nutrient.csv",
        "nutrient.csv",
    ]
    assert not (tmp_path.parent / "evil").exists()


# --------------------------------------------------------------------------- #
# Downloads: cache, resume, SHA-256
# --------------------------------------------------------------------------- #
class FakeServer:
    """Hugging Face style: a redirect with X-Linked-Etag, then a CDN with Range support."""

    def __init__(self, content: bytes, etag: str = "v1", sha: str | None = None):
        self.content = content
        self.etag = etag
        self.sha = sha if sha is not None else hashlib.sha256(content).hexdigest()
        self.gets: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "hub.test":
            return httpx.Response(
                302, headers={"location": "https://cdn.test/file", "x-linked-etag": f'"{self.sha}"'}
            )
        headers = {"etag": f'"{self.etag}"', "content-length": str(len(self.content))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        self.gets.append(dict(request.headers))
        rng = request.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            body = self.content[start:]
            return httpx.Response(206, content=body, headers={"etag": f'"{self.etag}"'})
        return httpx.Response(200, content=self.content, headers=headers)


def client(server: FakeServer) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server))


URL = "https://hub.test/food.parquet"


def test_download_then_cache(tmp_path):
    server = FakeServer(b"x" * 1000)
    dest = tmp_path / "food.parquet"
    first = download(client(server), URL, dest)
    assert dest.read_bytes() == server.content and first.sha256 == server.sha
    second = download(client(server), URL, dest)
    assert len(server.gets) == 1  # the second call uses the cache
    assert second.downloaded_at == first.downloaded_at


def test_download_again_when_the_remote_changes(tmp_path):
    dest = tmp_path / "food.parquet"
    download(client(FakeServer(b"old")), URL, dest)
    download(client(FakeServer(b"new content", etag="v2")), URL, dest)
    assert dest.read_bytes() == b"new content"


def test_download_continues_a_partial_file(tmp_path):
    server = FakeServer(bytes(range(256)) * 10)
    dest = tmp_path / "food.parquet"
    part = tmp_path / "food.parquet.part"
    part.write_bytes(server.content[:1000])
    (tmp_path / "food.parquet.part.json").write_text(json.dumps({"etag": "v1"}))
    download(client(server), URL, dest)
    assert server.gets[0]["range"] == "bytes=1000-"
    assert dest.read_bytes() == server.content


def test_download_rejects_a_bad_sha256(tmp_path):
    server = FakeServer(b"abc", sha="0" * 64)
    with pytest.raises(SystemExit):
        download(client(server), URL, tmp_path / "food.parquet")
    assert not (tmp_path / "food.parquet.part").exists()


def test_download_offline(tmp_path):
    dest = tmp_path / "food.parquet"
    with pytest.raises(SystemExit):
        download(None, URL, dest)
    download(client(FakeServer(b"abc")), URL, dest)
    assert download(None, URL, dest).path == dest


def test_download_uses_the_cache_when_the_network_fails(tmp_path):
    dest = tmp_path / "food.parquet"
    download(client(FakeServer(b"abc")), URL, dest)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    info = download(httpx.Client(transport=httpx.MockTransport(down)), URL, dest)
    assert info.path == dest
