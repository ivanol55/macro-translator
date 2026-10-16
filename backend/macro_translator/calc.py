"""Deterministic arithmetic. The LLM never does any of this.

- Quantity as written (10 ml, 2 x 130 g, a dash) -> grams.
- Values per 100 g -> values for the grams.
- Row coherence check, sums and rounding for display.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .models import IngredienteLLM, Macros
from .unidades import (
    CUCHARADAS_G,
    DENSIDAD_ACEITE_G_ML,
    DENSIDAD_DEFECTO_G_ML,
    SPOON_LABEL,
    SPOON_PLURAL,
)

# Kept as aliases: the conversion table lives in `unidades.py` (one source).
OIL_DENSITY_G_PER_ML = DENSIDAD_ACEITE_G_ML
DEFAULT_DENSITY_G_PER_ML = DENSIDAD_DEFECTO_G_ML
SPOON_GRAMS = CUCHARADAS_G

COHERENCE_TOLERANCE = 0.20
COHERENCE_MIN_ABS_KCAL = 5.0


def fmt(x: float, decimals: int = 1) -> str:
    """Format a number for the notes shown to the user: 9.2 -> "9,2", 10.0 -> "10"."""
    r = redondear(x, decimals)
    if r == int(r):
        return str(int(r))
    return f"{r:.{decimals}f}".rstrip("0").replace(".", ",")


def a_gramos(ing: IngredienteLLM) -> tuple[float, list[str]]:
    """Convert the quantity the user wrote into grams. Return the grams and the notes."""
    notas: list[str] = []
    unidad, cantidad, veces = ing.unidad, ing.cantidad, ing.veces
    if unidad == "g":
        por_vez = cantidad
    elif unidad == "kg":
        por_vez = cantidad * 1000
    elif unidad in ("ml", "l"):
        ml = cantidad * (1000 if unidad == "l" else 1)
        if ing.es_aceite:
            por_vez = ml * OIL_DENSITY_G_PER_ML
            notas.append(
                f"{fmt(ml)} ml de aceite × {fmt(OIL_DENSITY_G_PER_ML, 2)} g/ml = {fmt(por_vez)} g"
            )
        else:
            por_vez = ml * DEFAULT_DENSITY_G_PER_ML
            notas.append(f"{fmt(ml)} ml ≈ {fmt(por_vez)} g (densidad 1 g/ml supuesta)")
    else:
        g = SPOON_GRAMS[unidad]
        por_vez = cantidad * g
        if cantidad == 1:
            notas.append(f"1 {SPOON_LABEL[unidad]} = {fmt(g)} g")
        else:
            notas.append(f"{fmt(cantidad)} {SPOON_PLURAL[unidad]} × {fmt(g)} g = {fmt(por_vez)} g")
    gramos = por_vez * veces
    if veces > 1:
        notas.append(f"{veces} × {fmt(por_vez)} g = {fmt(gramos)} g")
    return gramos, notas


def escalar(valores_100g: Macros, gramos: float) -> Macros:
    f = gramos / 100.0
    return Macros(
        kcal=valores_100g.kcal * f,
        prot=valores_100g.prot * f,
        carbs=valores_100g.carbs * f,
        grasas=valores_100g.grasas * f,
    )


def coherente(m: Macros) -> bool:
    """|P*4 + C*4 + G*9 - kcal| / kcal <= 20 %, with a small absolute floor."""
    atwater = m.prot * 4 + m.carbs * 4 + m.grasas * 9
    return abs(atwater - m.kcal) <= max(COHERENCE_TOLERANCE * m.kcal, COHERENCE_MIN_ABS_KCAL)


def aviso_coherencia(m: Macros) -> str | None:
    if coherente(m):
        return None
    atwater = m.prot * 4 + m.carbs * 4 + m.grasas * 9
    return (
        f"Los valores no cuadran: P×4 + C×4 + G×9 = {fmt(atwater)} kcal "
        f"frente a {fmt(m.kcal)} kcal declaradas por 100 g. Revisa la etiqueta."
    )


SENSITIVITY_REL = 0.25  # warn when the other plausible variant differs by more than 25 %
SENSITIVITY_MIN_KCAL = 10.0  # ... and by at least 10 kcal for the amount eaten


def aviso_sensibilidad(
    kcal_100g: float, alt_kcal_100g: float, gramos: float, variante: str
) -> str | None:
    """'Si es picada magra: unas 230 kcal menos' when the variant changes the kcal a lot."""
    diff = (alt_kcal_100g - kcal_100g) * gramos / 100
    rel = abs(alt_kcal_100g - kcal_100g) / kcal_100g if kcal_100g > 0 else float("inf")
    if rel <= SENSITIVITY_REL or abs(diff) < SENSITIVITY_MIN_KCAL:
        return None
    unas = max(10, int(redondear(abs(diff) / 10, 0)) * 10)
    return f"Si es {variante.strip()}: unas {unas} kcal {'menos' if diff < 0 else 'más'}"


def sumar(filas: list[Macros]) -> Macros:
    return Macros(
        kcal=sum(m.kcal for m in filas),
        prot=sum(m.prot for m in filas),
        carbs=sum(m.carbs for m in filas),
        grasas=sum(m.grasas for m in filas),
    )


def redondear(x: float, decimals: int = 1) -> float:
    """Round half up (314.35 -> 314.4), not banker's rounding."""
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


def redondear_macros(m: Macros) -> Macros:
    return Macros(
        kcal=redondear(m.kcal),
        prot=redondear(m.prot),
        carbs=redondear(m.carbs),
        grasas=redondear(m.grasas),
    )
