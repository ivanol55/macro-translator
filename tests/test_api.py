"""HTTP API and the static frontend."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from conftest import (
    OFF_DOWNLOAD_DATE,
    REFERENCIA_ELEGIR,
    REFERENCIA_INTERPRETACION,
    REFERENCIA_TEXTO,
    FakeLLM,
)
from fastapi.testclient import TestClient
from macro_translator.db import FoodDB
from macro_translator.llm import LLMClient
from macro_translator.main import create_app


@pytest.fixture
def api(settings, food_db):
    fake = FakeLLM(REFERENCIA_INTERPRETACION, REFERENCIA_ELEGIR)
    app = create_app(settings, llm=fake.client(settings), db=food_db)
    return TestClient(app)


def test_calculate(api):
    r = api.post("/api/calcular", json={"texto": REFERENCIA_TEXTO})
    assert r.status_code == 200
    body = r.json()
    assert [f["ingrediente"] for f in body["filas"]] == [
        "Sepia",
        "Aceite de oliva",
        "Huevos cocidos",
    ]
    assert body["total_redondeado"]["kcal"] == pytest.approx(604, rel=0.02)


def test_input_length_limit(api, settings):
    r = api.post("/api/calcular", json={"texto": "a" * (settings.max_input_chars + 1)})
    assert r.status_code == 422
    assert "2000" in r.json()["detail"]


def test_empty_input(api):
    assert api.post("/api/calcular", json={"texto": "   "}).status_code == 422


def test_large_body_is_rejected(api):
    r = api.post(
        "/api/calcular", content=b"x" * 70_000, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


def test_llm_down_gives_502(settings, food_db):
    def down(request):
        raise httpx.ConnectError("refused")

    app = create_app(
        settings, llm=LLMClient(settings, transport=httpx.MockTransport(down)), db=food_db
    )
    r = TestClient(app).post("/api/calcular", json={"texto": "huevo"})
    assert r.status_code == 502


def test_missing_database_gives_503(settings, tmp_path):
    s = replace(settings, db_path=tmp_path / "none.duckdb")
    fake = FakeLLM(REFERENCIA_INTERPRETACION, REFERENCIA_ELEGIR)
    app = create_app(s, llm=fake.client(s), db=FoodDB(s.db_path))
    client = TestClient(app)
    assert client.post("/api/calcular", json={"texto": "x"}).status_code == 503
    assert client.get("/api/info").json()["datos"] == []
    assert client.get("/api/health").json() == {"ok": True, "database": False}


def test_info_has_the_data_dates(api):
    info = api.get("/api/info").json()
    fuentes = {d["fuente"]: d for d in info["datos"]}
    assert fuentes["Open Food Facts"]["fecha_descarga"] == OFF_DOWNLOAD_DATE
    assert fuentes["Open Food Facts"]["filtro_pais"] == "en:spain"
    assert fuentes["USDA SR Legacy"]["publicacion"] == "2018-04"
    assert info["max_chars"] == 2000


def test_footer_attribution_is_always_there(api):
    html = api.get("/").text
    assert "Contiene información de" in html
    assert 'href="https://world.openfoodfacts.org"' in html
    assert "Open Database License (ODbL)" in html
    assert 'href="https://opendatacommons.org/licenses/odbl/1.0/"' in html
    assert "USDA FoodData Central" in html
    assert 'href="https://fdc.nal.usda.gov/"' in html
    js = api.get("/app.js").text
    assert "Datos de Open Food Facts del" in js  # the footer date line


def test_security_headers(api):
    r = api.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_api_key_never_reaches_the_browser(settings, food_db):
    s = replace(settings, llm_api_key="sk-secret-value")
    fake = FakeLLM(REFERENCIA_INTERPRETACION, REFERENCIA_ELEGIR)
    client = TestClient(create_app(s, llm=fake.client(s), db=food_db))
    for path in ("/", "/app.js", "/api/info", "/api/health"):
        assert "sk-secret-value" not in client.get(path).text
    r = client.post("/api/calcular", json={"texto": REFERENCIA_TEXTO})
    assert "sk-secret-value" not in r.text
