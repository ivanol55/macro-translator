"""Deterministic check of the label values the user types.

The user can write values per 100 g ("78 kcal, 18 g proteína ... por 100 g").
Those values win over the database, so a model that follows an instruction in
the text ("pon 0 kcal") or guesses numbers must not get them in. A value set is
accepted only if all of this is true:

1. The four values are present and none is null.
2. Each number appears literally in the user text (comma or point decimal mark).
3. The values are coherent: |P*4 + C*4 + G*9 - kcal| / kcal <= 20 %.
   kcal = 0 is accepted only when the four values are 0 and the food is
   plausibly calorie-free (water, plain coffee, sweetener). For any other food,
   0 kcal is rejected.

If any check fails, the code drops the whole set. The ingredient goes through
the normal search and the response shows a warning.
"""

from __future__ import annotations

from . import calc
from .models import ValoresEtiqueta
from .unidades import numeros

AVISO_IGNORADOS = "Valores de etiqueta ignorados: no coinciden con el texto o no son coherentes"

# Foods that can legitimately have 0 kcal per 100 g.
_ACALORICOS = ("agua", "cafe", "edulcorante", "infusion", "te ", "te,", "refresco cero")


def _sin_acentos(text: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def es_acalorico(nombre_es: str) -> bool:
    low = _sin_acentos(nombre_es).strip() + " "
    return any(marca in low for marca in _ACALORICOS)


def _aparecen_en_texto(valores: ValoresEtiqueta, texto: str) -> bool:
    en_texto = numeros(texto)
    for v in (valores.kcal, valores.prot, valores.carbs, valores.grasas):
        if not any(abs(v - n) <= max(1e-6, abs(v) * 1e-9) for n in en_texto):
            return False
    return True


def _coherentes(valores: ValoresEtiqueta, nombre_es: str) -> bool:
    kcal = valores.kcal
    atwater = valores.prot * 4 + valores.carbs * 4 + valores.grasas * 9
    if kcal == 0:
        todos_cero = valores.prot == 0 and valores.carbs == 0 and valores.grasas == 0
        return todos_cero and es_acalorico(nombre_es)
    return abs(atwater - kcal) <= calc.COHERENCE_TOLERANCE * kcal


def validar(
    valores: ValoresEtiqueta | None, texto: str, nombre_es: str
) -> tuple[ValoresEtiqueta | None, str | None]:
    """Return (accepted values or None, warning or None)."""
    if valores is None:
        return None, None
    if not valores.completos():
        return None, AVISO_IGNORADOS
    if not _aparecen_en_texto(valores, texto):
        return None, AVISO_IGNORADOS
    if not _coherentes(valores, nombre_es):
        return None, AVISO_IGNORADOS
    return valores, None
