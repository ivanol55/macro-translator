r"""Acceptance tests against a real LLM. They are skipped unless LIVE_LLM=1.

    LIVE_LLM=1 LLM_BASE_URL=http://127.0.0.1:8080/v1 LLM_MODEL=gpt-oss-20b \
        pytest tests/test_live_llm.py

They use the fixture database, unless DB_PATH points to a real one. Run them
after you change a prompt. CI does not run them (no model there).
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from conftest import REFERENCIA_TEXTO
from macro_translator.config import load_settings
from macro_translator.db import FoodDB
from macro_translator.llm import LLMClient
from macro_translator.pipeline import calcular

pytestmark = pytest.mark.skipif(os.environ.get("LIVE_LLM") != "1", reason="set LIVE_LLM=1")


@pytest.fixture(scope="module")
def live(db_path):
    s = load_settings()
    if not os.environ.get("DB_PATH"):
        s = replace(s, db_path=db_path)
    return s, LLMClient(s), FoodDB(s.db_path)


def run(live, prompts, texto):
    s, llm, db = live
    return calcular(texto, llm, db, prompts)


def by_name(res, word):
    return next(f for f in res.filas if word.lower() in f.ingrediente.lower())


def test_reference_case(live, prompts):
    res = run(live, prompts, REFERENCIA_TEXTO)
    assert by_name(res, "sepia").gramos == pytest.approx(403)
    assert by_name(res, "aceite").gramos == pytest.approx(10)
    assert by_name(res, "huevo").gramos == pytest.approx(130)
    t = res.total
    assert t.kcal == pytest.approx(604, rel=0.02)
    assert t.prot == pytest.approx(88.9, rel=0.02)
    assert t.carbs == pytest.approx(4.6, rel=0.05, abs=0.1)
    assert t.grasas == pytest.approx(25.8, rel=0.02)


def test_quantity_rules(live, prompts):
    res = run(live, prompts, "huevos cocidos, 2, 130g. Pechuga de pollo 2×130 g. 10 ml de aceite")
    assert by_name(res, "huevo").gramos == pytest.approx(130)
    assert by_name(res, "pollo").gramos == pytest.approx(260)
    assert by_name(res, "aceite").gramos == pytest.approx(9.2)


def test_unknown_food(live, prompts):
    res = run(live, prompts, "unicornio a la plancha, 200 g")
    assert len(res.filas) == 1 and not res.filas[0].encontrado
    assert res.total.kcal == 0


def test_choice_assumptions_and_sensitivity(live, prompts):
    res = run(live, prompts, "carne picada de ternera 200 g, cruda")
    fila = res.filas[0]
    assert fila.encontrado
    if "%" in (fila.origen.nombre or ""):
        assert any("%" in s for s in fila.supuestos)
        assert "exact" not in (fila.motivo or "").lower()
    if fila.sensibilidad:
        assert fila.sensibilidad.startswith("Si es ")
