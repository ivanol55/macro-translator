"""Assumptions that the code adds when a USDA row fixes an attribute the user did not give."""

from __future__ import annotations

import pytest
from macro_translator.atributos import (
    compatible_con_texto,
    es_espurio,
    filtrar_supuestos,
    limpiar_marca,
    limpiar_motivo_espurio,
    supuesto_de_marca,
    supuestos_del_candidato,
)
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
    assert supuesto_de_marca("Mercadona", off) == (
        "No está el de Mercadona en la base de datos; uso el de Hacendado"
    )
    assert supuesto_de_marca("Hacendado", off) is None
    assert supuesto_de_marca(None, off) is None
    sin_marca = cand("Tomate frito", "OFF")
    assert supuesto_de_marca("Mercadona", sin_marca) == (
        "No está el de Mercadona en la base de datos"
    )
    # The brand claim leaves the reason: it must not say the brands match.
    assert limpiar_marca("mismo alimento y marca") == "mismo alimento"
    assert "marca" not in limpiar_marca("carne picada cruda, misma marca").lower()


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


# --------------------------------------------------------------------------- #
# Fixes 1-2: no assumptions about what the user said, no format assumptions
# --------------------------------------------------------------------------- #
CASO = (
    "71g chorizo sarta extra picante alcampo\n"
    "75g palitos de surimi hacendado\n"
    "193g pepino fresco\n"
    "50g aceitunas verdes sin hueso hacendado\n"
    "95g yogur estilo griego ligero natural hacendado"
)


@pytest.mark.parametrize(
    ("supuesto", "espurio"),
    [
        ("formato: sarta", True),  # the user wrote "sarta"
        ("formato: palitos", True),  # the user wrote "palitos"
        ("formato: entero", True),  # format never changes the values
        ("sin azúcar (natural)", True),  # the user wrote "natural"
        ("piel: pelado", False),  # the user did not say the skin
        ("algo de aceite = un chorrito", False),  # quantity, not an attribute
        ("130 g es el total de los 2 huevos", False),  # quantity, not an attribute
        ("El paquete entero: 403 g", False),  # quantity, not format
        ("La base de datos fija 80 % magra, 20 % grasa", False),  # the text gives no %
    ],
)
def test_es_espurio(supuesto, espurio):
    assert es_espurio(supuesto, CASO) is espurio


@pytest.mark.parametrize(
    ("motivo", "esperado"),
    [
        ("El texto no especifica si es sarta o en rodajas", ""),
        ("El texto no especifica si son palitos o trozos", ""),
        ("Cocción al horno. El texto no especifica si es sarta o en rodajas", "Cocción al horno"),
        ("El texto no especifica si es natural o azucarado", ""),
        (
            "Carne picada de ternera cruda; el texto no dice el porcentaje de grasa",
            "Carne picada de ternera cruda, el texto no dice el porcentaje de grasa",
        ),
    ],
)
def test_motivo_espurio_se_limpia(motivo, esperado):
    assert limpiar_motivo_espurio(motivo, CASO) == esperado


# --------------------------------------------------------------------------- #
# Fix 4: an alternative must satisfy the attributes the user gave
# --------------------------------------------------------------------------- #
def test_alternativa_incompatible_se_descarta():
    assert not compatible_con_texto("Yogur al estilo griego natural azucarado (Hacendado)", CASO)
    assert compatible_con_texto("Yogur griego natural (Hacendado)", CASO)
    assert compatible_con_texto("Cualquier cosa sin datos", CASO)


def test_reg_003_sin_supuestos_espurios():
    # Fix 1-2: the format assumptions and the ones the user already made disappear.
    assert filtrar_supuestos(["formato: sarta"], CASO) == []
    assert filtrar_supuestos(["formato: palitos"], CASO) == []
    assert filtrar_supuestos(["formato: entero"], CASO) == []
    assert filtrar_supuestos(["sin azúcar (natural)"], CASO) == []
    # The skin is a real nutritional assumption the user did not give: it stays.
    assert filtrar_supuestos(["piel: pelado"], CASO) == ["piel: pelado"]
    # Fix 3: the reason loses the brand claim, which the brand assumption replaces.
    assert limpiar_marca("mismo alimento y marca") == "mismo alimento"
    # Fix 4: the sweetened alternative contradicts "natural" and "ligero".
    assert not compatible_con_texto("Yogur griego natural azucarado Hacendado", CASO)
