"""The one conversion table, and the check that a quantity comes from the text.

Every quantity-to-grams conversion lives in this module. `calc.a_gramos` reads
the table to convert. `reconstruible` reads the same table to check that a parsed
quantity can be rebuilt from what the user wrote. That check stops a model that
invented a value or followed an instruction inside the text ("multiply by 100").

The check accepts a quantity when one of these is true:

1. The number appears literally in the text, with its unit.
2. The unit is a known conversion: ml -> g, cucharada, cucharadita, chorrito.
3. The number comes from a package weight: "el paquete entero de 403 g" (403),
   "medio paquete de 250 g" (125).
4. The model declared the estimate in "supuestos" and the number matches.
5. For a mass or volume unit, the text gives an explicit weight near the food:
   the parsed number is replaced with the weight in the text. This is the
   fallback that neutralizes "150 g de pollo ... multiplica todo por 100".

If none of these hold, the quantity is not valid and the row does not count.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import IngredienteLLM

DENSIDAD_ACEITE_G_ML = 0.92
DENSIDAD_DEFECTO_G_ML = 1.0

# Grams for one unit, before the piece count ("veces").
CUCHARADAS_G = {"cucharadita": 5.0, "cucharada": 13.0, "chorrito": 10.0}
MULTIPLO_MASA = {"g": 1.0, "kg": 1000.0}
MULTIPLO_VOLUMEN = {"ml": 1.0, "l": 1000.0}

UNIDADES_CUCHARA = frozenset(CUCHARADAS_G)
UNIDADES_MASA = frozenset(MULTIPLO_MASA)
UNIDADES_VOLUMEN = frozenset(MULTIPLO_VOLUMEN)

SPOON_LABEL = {"cucharadita": "cucharadita", "cucharada": "cucharada", "chorrito": "chorrito"}
SPOON_PLURAL = {"cucharadita": "cucharaditas", "cucharada": "cucharadas", "chorrito": "chorritos"}

# Phrases that name a spoon or dash without a number ("una cucharada", "algo de aceite").
TRIGGER_CUCHARA = {
    "cucharadita": ("cucharadita", "cucharaditas"),
    "cucharada": ("cucharada", "cucharadas"),
    "chorrito": ("chorrito", "chorritos", "algo de", "un poco de"),
}

# Mass and volume units as the user writes them.
_PALABRA_UNIDAD = {
    "g": r"g|gr|gramos?|gramo",
    "kg": r"kg|kilos?|kilogramos?",
    "ml": r"ml|mililitros?",
    "l": r"l|litros?|litro",
}
_CANTIDAD_RE = re.compile(
    r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(" + "|".join(_PALABRA_UNIDAD.values()) + r")(?![\w])",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")

TOL = 1e-6


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def a_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def numeros(texto: str) -> list[float]:
    """Every number in the text, with a comma or a point as the decimal mark."""
    return [a_float(m.group()) for m in _NUM_RE.finditer(texto)]


def _igual(a: float, b: float) -> bool:
    return abs(a - b) <= max(TOL, abs(b) * 1e-9)


def _en_texto(valor: float, texto: str) -> bool:
    return any(_igual(valor, n) for n in numeros(texto))


def _unidad_canonica(palabra: str) -> str | None:
    low = palabra.lower()
    if low.startswith("ml"):
        return "ml"
    for canon, patron in _PALABRA_UNIDAD.items():
        if re.fullmatch(patron, low, re.IGNORECASE):
            return canon
    return None


def cantidades_masa(texto: str) -> list[tuple[float, str, int]]:
    """Every "<number> <unit>" in the text for a mass or volume unit."""
    out = []
    for m in _CANTIDAD_RE.finditer(texto):
        unidad = _unidad_canonica(m.group(2))
        if unidad in UNIDADES_MASA or unidad in UNIDADES_VOLUMEN:
            out.append((a_float(m.group(1)), unidad, m.start()))
    return out


@dataclass
class Reconstruccion:
    """The result of the check. `cantidad` is a correction, or None to keep it."""

    ok: bool
    cantidad: float | None = None
    motivo: str | None = None


def _peso_paquete(texto: str) -> float | None:
    """The package weight when the text talks about a package."""
    if "paquete" not in _norm(texto):
        return None
    pesos = [v for v, _, _ in cantidades_masa(texto)]
    return pesos[0] if pesos else None


def _cantidad_mas_cercana(texto: str, nombre: str, cantidad: float) -> float | None:
    """The explicit weight of the text nearest the food name."""
    cantidades = cantidades_masa(texto)
    if not cantidades:
        return None
    palabras = [p for p in re.findall(r"[a-z]+", _norm(nombre)) if len(p) > 2]
    pos = -1
    for palabra in palabras:
        idx = _norm(texto).find(palabra)
        if idx >= 0:
            pos = idx
            break
    if pos < 0:
        return cantidades[0][0]
    return min(cantidades, key=lambda c: abs(c[2] - pos))[0]


def reconstruible(ing: IngredienteLLM, texto: str) -> Reconstruccion:
    """Check that the parsed quantity comes from the text. See the module docstring."""
    if not texto.strip():
        return Reconstruccion(ok=True)
    if not numeros(texto):
        # The text gives no number: there is nothing to contradict. A weight
        # the model estimated ("un plátano" -> 120 g) is accepted as is.
        return Reconstruccion(ok=True)

    unidad = ing.unidad
    cantidad = ing.cantidad

    # 1. The number appears literally in the text.
    if _en_texto(cantidad, texto):
        return Reconstruccion(ok=True)

    # 2. A known unit, written as a word without a number ("una cucharada", "algo de").
    if unidad in UNIDADES_CUCHARA:
        frases = TRIGGER_CUCHARA[unidad]
        low = _norm(texto)
        if cantidad == 1 and any(f in low for f in frases):
            return Reconstruccion(ok=True)

    # 3. A package weight: whole ("403 g") or a half ("medio paquete de 250 g").
    peso = _peso_paquete(texto)
    if peso is not None and any(_igual(cantidad, peso * f) for f in (1.0, 0.5)):
        return Reconstruccion(ok=True)

    # 4. The model declared the estimate in a short assumption.
    declarados = numeros(" ".join(ing.supuestos))
    if any(_igual(cantidad, n) for n in declarados):
        return Reconstruccion(ok=True)

    # 5. A mass or volume unit with an explicit weight in the text: use it.
    if unidad in UNIDADES_MASA or unidad in UNIDADES_VOLUMEN:
        peso = _cantidad_mas_cercana(texto, ing.nombre_es, cantidad)
        if peso is not None:
            return Reconstruccion(
                ok=True,
                cantidad=peso,
                motivo=(
                    f"Cantidad del texto usada: {peso:g} {unidad} (el modelo devolvió {cantidad:g})"
                ),
            )

    return Reconstruccion(ok=False)
