"""Deterministic arithmetic: quantities, scaling, rounding, coherence, totals."""

from __future__ import annotations

import pytest
from conftest import ingrediente
from macro_translator import calc
from macro_translator.models import IngredienteLLM, Macros


def gramos(**kw) -> float:
    base = ingrediente("X", "x", kw.pop("cantidad", 100), kw.pop("unidad", "g"))
    return calc.a_gramos(IngredienteLLM.model_validate(base | kw))[0]


def test_weight_with_a_piece_count_is_the_total():
    # "huevos, 2, 130g": the LLM gives cantidad 130, veces 1 -> 130 g in total.
    assert gramos(cantidad=130, veces=1) == 130


def test_weight_per_piece_multiplies():
    # "2×130 g": the LLM gives cantidad 130, veces 2 -> 260 g.
    assert gramos(cantidad=130, veces=2) == 260


def test_oil_millilitres_to_grams():
    assert gramos(cantidad=10, unidad="ml", es_aceite=True) == pytest.approx(9.2)


def test_other_liquid_millilitres():
    assert gramos(cantidad=200, unidad="ml") == 200
    assert gramos(cantidad=0.5, unidad="l") == 500


def test_spoons_and_dash():
    assert gramos(cantidad=1, unidad="chorrito", es_aceite=True) == 10
    assert gramos(cantidad=1, unidad="cucharadita") == 5
    assert gramos(cantidad=2, unidad="cucharada") == 26
    assert gramos(cantidad=1.5, unidad="kg") == 1500


def test_conversions_are_written_as_assumptions():
    ing = IngredienteLLM.model_validate(ingrediente("Aceite", "oil", 10, "ml", es_aceite=True))
    g, notas = calc.a_gramos(ing)
    assert notas == ["10 ml de aceite × 0,92 g/ml = 9,2 g"]


def test_scale_per_100g():
    m = calc.escalar(Macros(kcal=78, prot=18, carbs=0.8, grasas=0.5), 403)
    assert (m.kcal, m.prot, m.carbs, m.grasas) == pytest.approx((314.34, 72.54, 3.224, 2.015))


def test_round_half_up():
    assert calc.redondear(314.35) == 314.4
    assert calc.redondear(0.05) == 0.1
    assert calc.redondear(2.015) == 2.0  # 2.015 is 2.01499... in binary


def test_coherence():
    assert calc.coherente(Macros(kcal=78, prot=18, carbs=0.8, grasas=0.5))
    assert not calc.coherente(Macros(kcal=224, prot=3.46, carbs=6.22, grasas=1.85))
    assert calc.coherente(Macros(kcal=0, prot=0, carbs=0, grasas=0))  # water
    assert calc.aviso_coherencia(Macros(kcal=224, prot=3.46, carbs=6.22, grasas=1.85))


def test_sum():
    t = calc.sumar([Macros(kcal=1, prot=2, carbs=3, grasas=4)] * 3)
    assert (t.kcal, t.prot, t.carbs, t.grasas) == (3, 6, 9, 12)


def test_format_for_notes():
    assert calc.fmt(9.2) == "9,2"
    assert calc.fmt(10.0) == "10"
    assert calc.fmt(0.92, 2) == "0,92"
