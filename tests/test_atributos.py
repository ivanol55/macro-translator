"""Assumptions that the code adds when a USDA row fixes an attribute the user did not give."""

from __future__ import annotations

import pytest
from macro_translator.atributos import supuestos_del_candidato
from macro_translator.models import Candidato


def cand(nombre: str, fuente: str = "USDA") -> Candidato:
    return Candidato(
        clave="U1",
        id=1,
        fuente=fuente,
        nombre=nombre,
        marca=None,
        codigo_barras=None,
        kcal=100,
        prot=10,
        carbs=0,
        grasas=5,
    )


BEEF = "Beef, ground, 80% lean meat / 20% fat, raw"


def test_fat_percentage_is_added():
    assert supuestos_del_candidato("Carne picada de ternera", cand(BEEF), []) == [
        "La base de datos fija 80 % magra, 20 % grasa"
    ]


def test_fat_percentage_is_not_added_twice():
    assert supuestos_del_candidato("Carne picada", cand(BEEF), ["picada 80 % magra"]) == []
    assert supuestos_del_candidato("Carne picada 80 % magra", cand(BEEF), []) == []


@pytest.mark.parametrize(
    ("nombre_es", "usda", "esperado"),
    [
        (
            "Pechuga de pollo a la plancha",
            "Chicken, breast, cooked, roasted",
            "La base de datos usa la cocción «asado» (roasted), no la del texto",
        ),
        ("Pechuga de pollo a la plancha", "Chicken, breast, cooked, grilled", None),
        ("Huevos cocidos", "Egg, whole, cooked, hard-boiled", None),
        (
            "Huevos cocidos",
            "Egg, whole, cooked, fried",
            "La base de datos usa la cocción «frito» (fried), no la del texto",
        ),
        (
            "Salchicha",
            "Kielbasa, fully cooked, grilled",
            "La base de datos usa la cocción «a la parrilla» (grilled)",
        ),
        ("Plátano", "Bananas, raw", None),
        ("Aceite de oliva", "Oil, olive, salad or cooking", None),
    ],
)
def test_cooking_method(nombre_es, usda, esperado):
    out = supuestos_del_candidato(nombre_es, cand(usda), [])
    assert out == ([esperado] if esperado else [])


def test_off_rows_are_checked_for_the_extra_table():
    # The extra rules apply to OFF too: the dairy variant is fixed by the row.
    assert supuestos_del_candidato("Leche", cand("Leche entera", "OFF"), []) == [
        "La base de datos usa la versión entera"
    ]
    assert supuestos_del_candidato("Leche entera", cand("Leche entera", "OFF"), []) == []


def test_cocoa_percentage_is_added():
    cands = cand("Chocolate, dark, 70-85% cacao solids")
    assert supuestos_del_candidato("Chocolate negro", cands, []) == [
        "La base de datos usa chocolate con 70-85 % de cacao"
    ]
    assert supuestos_del_candidato("Chocolate negro 85 %", cands, []) == []


def test_animal_part_is_added():
    pechuga = "Chicken, broiler or fryers, breast, meat only, cooked, grilled"
    out = supuestos_del_candidato("Pollo a la plancha", cand(pechuga), [])
    assert "La base de datos usa la parte «pechuga»" in out
    assert not any(
        "parte" in s
        for s in supuestos_del_candidato("Pechuga de pollo a la plancha", cand(pechuga), [])
    )


def test_sugar_variant_is_added():
    azucarado = cand("Yogurt, Greek, sweetened", "OFF")
    assert supuestos_del_candidato("Yogur griego", azucarado, []) == [
        "La base de datos usa la versión con azúcar"
    ]


def test_brand_is_declared_when_it_differs():
    off = cand("Tomate frito", "OFF")
    off = off.model_copy(update={"marca": "Hacendado"})
    assert supuestos_del_candidato("Tomate frito", off, [], "Mercadona") == [
        "La base de datos es de la marca Hacendado, no Mercadona"
    ]
    assert supuestos_del_candidato("Tomate frito", off, [], "Hacendado") == []
    assert supuestos_del_candidato("Tomate frito", off, [], None) == []


def test_skin():
    grilled = "Chicken, broiler or fryers, breast, skinless, boneless, meat only, cooked, grilled"
    assert supuestos_del_candidato("Pechuga de pollo a la plancha", cand(grilled), []) == [
        "La base de datos usa la versión sin piel"
    ]
    assert supuestos_del_candidato("Pechuga sin piel a la plancha", cand(grilled), []) == []
    con_piel = "Chicken, broilers or fryers, breast, meat and skin, cooked, roasted"
    assert supuestos_del_candidato("Pechuga al horno", cand(con_piel), []) == [
        "La base de datos usa la versión con piel"
    ]


def test_meat_only_means_without_skin():
    roasted = "Chicken, broilers or fryers, breast, meat only, cooked, roasted"
    assert supuestos_del_candidato("Pechuga de pollo a la plancha", cand(roasted), []) == [
        "La base de datos usa la versión sin piel",
        "La base de datos usa la cocción «asado» (roasted), no la del texto",
    ]
