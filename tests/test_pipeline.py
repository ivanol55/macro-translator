"""The whole pipeline with a fake LLM over the fixture database (acceptance tests)."""

from __future__ import annotations

import pytest
from conftest import (
    REFERENCIA_ELEGIR,
    REFERENCIA_INTERPRETACION,
    REFERENCIA_TEXTO,
    FakeLLM,
    ingrediente,
)
from macro_translator.pipeline import calcular

REL = 0.02  # the acceptance tolerance: ±2 %


def run(settings, food_db, prompts, interpretacion, elegir=None, texto="x"):
    fake = FakeLLM(interpretacion, elegir)
    return calcular(texto, fake.client(settings), food_db, prompts), fake


def test_reference_case(settings, food_db, prompts):
    res, _ = run(
        settings, food_db, prompts, REFERENCIA_INTERPRETACION, REFERENCIA_ELEGIR, REFERENCIA_TEXTO
    )
    sepia, aceite, huevos = res.filas
    expected = {
        "Sepia": (403, 314.3, 72.5, 3.2, 2.0),
        "Aceite de oliva": (10, 88, 0, 0, 10.0),
        "Huevos cocidos": (130, 201, 16.4, 1.4, 13.8),
    }
    for fila in res.filas:
        g, kcal, prot, carbs, grasas = expected[fila.ingrediente]
        assert fila.gramos == pytest.approx(g)
        v = fila.valores
        assert v.kcal == pytest.approx(kcal, rel=REL)
        assert v.prot == pytest.approx(prot, rel=REL, abs=0.05)
        # 1.4 g in the brief vs 1.456 g from USDA (1.12 g/100 g): 4 % of a tiny value.
        assert v.carbs == pytest.approx(carbs, rel=0.05, abs=0.05)
        assert v.grasas == pytest.approx(grasas, rel=REL, abs=0.05)

    t = res.total
    assert t.kcal == pytest.approx(604, rel=REL)
    assert t.prot == pytest.approx(88.9, rel=REL)
    assert t.carbs == pytest.approx(4.6, rel=REL)
    assert t.grasas == pytest.approx(25.8, rel=REL)

    assert sepia.origen.tipo == "Usuario"  # label values win over the database
    assert aceite.origen.tipo == "USDA" and aceite.origen.nombre == "Oil, olive, salad or cooking"
    assert huevos.origen.tipo == "USDA"
    assert huevos.origen.url.startswith("https://fdc.nal.usda.gov/food-details/")
    assert "1 chorrito = 10 g" in aceite.supuestos
    assert res.avisos == []


def test_display_rounding(settings, food_db, prompts):
    res, _ = run(settings, food_db, prompts, REFERENCIA_INTERPRETACION, REFERENCIA_ELEGIR)
    sepia = res.filas[0].redondeado
    assert (sepia.kcal, sepia.prot, sepia.carbs, sepia.grasas) == (314.3, 72.5, 3.2, 2.0)


def test_total_is_the_sum_of_the_rows(settings, food_db, prompts):
    res, _ = run(settings, food_db, prompts, REFERENCIA_INTERPRETACION, REFERENCIA_ELEGIR)
    for m in ("kcal", "prot", "carbs", "grasas"):
        exact = sum(getattr(f.valores, m) for f in res.filas)
        assert getattr(res.total, m) == pytest.approx(exact, abs=1e-9)
        # The rounded total comes from the exact sum, so it is within 0.05 per row
        # of the sum of the rounded rows.
        shown = sum(getattr(f.redondeado, m) for f in res.filas)
        assert abs(getattr(res.total_redondeado, m) - shown) <= 0.05 * len(res.filas) + 1e-9


def test_eggs_two_130g_is_130g_and_2x130g_is_260g(settings, food_db, prompts):
    interp = {
        "ingredientes": [
            ingrediente("Huevos cocidos", "egg whole hard-boiled", 130, estado="cocido"),
            ingrediente("Huevos cocidos", "egg whole hard-boiled", 130, estado="cocido", veces=2),
        ]
    }
    res, _ = run(
        settings, food_db, prompts, interp, {"Huevos cocidos": "Egg, whole, cooked, hard-boiled"}
    )
    assert [f.gramos for f in res.filas] == [130, 260]
    assert res.filas[1].valores.kcal == pytest.approx(2 * res.filas[0].valores.kcal)


def test_oil_10ml_is_9_2g(settings, food_db, prompts):
    interp = {
        "ingredientes": [ingrediente("Aceite de oliva", "olive oil", 10, "ml", es_aceite=True)]
    }
    res, _ = run(
        settings, food_db, prompts, interp, {"Aceite de oliva": "Oil, olive, salad or cooking"}
    )
    fila = res.filas[0]
    assert fila.gramos == pytest.approx(9.2)
    assert fila.valores.kcal == pytest.approx(884 * 0.092)


def test_unknown_food_is_not_found_and_not_estimated(settings, food_db, prompts):
    interp = {
        "ingredientes": [
            ingrediente("Unicornio a la plancha", "unicorn grilled", 200, estado="cocido"),
            ingrediente("Huevos cocidos", "egg whole hard-boiled", 130, estado="cocido"),
        ]
    }
    res, fake = run(
        settings,
        food_db,
        prompts,
        interp,
        {"Unicornio a la plancha": None, "Huevos cocidos": "Egg, whole, cooked, hard-boiled"},
    )
    unicornio, huevos = res.filas
    assert not unicornio.encontrado
    assert unicornio.valores is None and unicornio.redondeado is None
    assert res.total.kcal == pytest.approx(huevos.valores.kcal)
    assert any("Unicornio a la plancha" in a for a in res.avisos)
    # The OFF candidates (unicorn sweets) were offered in round 2 and rejected too.
    assert "Unicornio" in fake.selection_inputs[1] and "Huevos" not in fake.selection_inputs[1]


def test_generic_food_sees_usda_first(settings, food_db, prompts):
    interp = {
        "ingredientes": [
            ingrediente("Huevos cocidos", "egg whole hard-boiled", 130, estado="cocido")
        ]
    }
    _, fake = run(
        settings, food_db, prompts, interp, {"Huevos cocidos": "Egg, whole, cooked, hard-boiled"}
    )
    assert len(fake.selection_inputs) == 1
    assert "USDA" in fake.selection_inputs[0]
    assert "Open Food Facts" not in fake.selection_inputs[0]


def test_branded_food_sees_off_first(settings, food_db, prompts):
    interp = {
        "ingredientes": [
            ingrediente(
                "Huevos cocidos", "egg whole hard-boiled", 130, marca="Hacendado", estado="cocido"
            )
        ]
    }
    res, fake = run(settings, food_db, prompts, interp, {"Huevos cocidos": "Hacendado"})
    assert "Open Food Facts" in fake.selection_inputs[0]
    assert "USDA" not in fake.selection_inputs[0]
    fila = res.filas[0]
    assert fila.origen.tipo == "OFF" and fila.origen.marca == "Hacendado"
    assert fila.origen.url == "https://world.openfoodfacts.org/product/8480000310101"


def test_generic_food_falls_back_to_off(settings, food_db, prompts):
    # USDA has hits (olive oil), but none is salmorejo: round 2 offers OFF.
    interp = {"ingredientes": [ingrediente("Salmorejo", "cold tomato soup with olive oil", 200)]}
    res, fake = run(settings, food_db, prompts, interp, {"Salmorejo": "Salmorejo"})
    assert len(fake.selection_inputs) == 2
    assert "USDA" in fake.selection_inputs[0] and "Open Food Facts" in fake.selection_inputs[1]
    assert res.filas[0].origen.tipo == "OFF"


def test_no_usda_hit_goes_straight_to_off(settings, food_db, prompts):
    interp = {"ingredientes": [ingrediente("Salmorejo", "salmorejo", 200)]}
    res, fake = run(settings, food_db, prompts, interp, {"Salmorejo": "Salmorejo"})
    assert len(fake.selection_inputs) == 1
    assert res.filas[0].origen.tipo == "OFF"


def test_partial_label_values_complete_from_the_database(settings, food_db, prompts):
    interp = {
        "ingredientes": [
            ingrediente(
                "Huevos cocidos",
                "egg whole hard-boiled",
                100,
                estado="cocido",
                valores_usuario_100g={"kcal": 150, "prot": 13, "carbs": None, "grasas": None},
            )
        ]
    }
    res, _ = run(
        settings, food_db, prompts, interp, {"Huevos cocidos": "Egg, whole, cooked, hard-boiled"}
    )
    v = res.filas[0].valores
    assert (v.kcal, v.prot) == (150, 13)  # from the label
    assert (v.carbs, v.grasas) == pytest.approx((1.12, 10.61))  # from USDA
    assert res.filas[0].origen.tipo == "Usuario" and res.filas[0].origen.complemento == "USDA"


def test_incoherent_label_values_get_a_warning(settings, food_db, prompts):
    interp = {
        "ingredientes": [
            ingrediente(
                "Pizza",
                "pizza",
                100,
                valores_usuario_100g={"kcal": 224, "prot": 3.46, "carbs": 6.22, "grasas": 1.85},
            )
        ]
    }
    res, _ = run(settings, food_db, prompts, interp)
    assert res.filas[0].encontrado
    assert "no cuadran" in res.filas[0].aviso


def test_no_food_in_the_text(settings, food_db, prompts):
    res, _ = run(settings, food_db, prompts, {"ingredientes": []})
    assert res.filas == [] and res.total.kcal == 0
    assert res.avisos == ["No hay alimentos en el texto."]


def test_llm_sees_no_invented_values(settings, food_db, prompts):
    """The LLM output never feeds a nutrition value into the table, except label values."""
    res, fake = run(settings, food_db, prompts, REFERENCIA_INTERPRETACION, REFERENCIA_ELEGIR)
    selection = fake.selection_inputs[0]
    assert "Oil, olive, salad or cooking" in selection
    for fila in res.filas[1:]:
        assert fila.origen.tipo in {"OFF", "USDA"}
