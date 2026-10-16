"""Attributes that a USDA row fixes and the user did not give, found by code.

The LLM writes these assumptions too, but small models forget them. This check
covers the most common cases: the fat percentage, the skin and the cooking method.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Candidato

GRASA_RE = re.compile(r"(\d+)\s*%\s*lean[^/,]*/\s*(\d+)\s*%\s*fat", re.IGNORECASE)

# Cooking words in USDA names -> Spanish text for the assumption.
COCCION_EN = {
    "hard-boiled": "cocido (duro)",
    "pan-browned": "dorado en sartén",
    "pan-fried": "a la sartén",
    "pan-broiled": "a la plancha",
    "moist heat": "cocido en líquido",
    "microwaved": "al microondas",
    "scrambled": "revuelto",
    "poached": "escalfado",
    "steamed": "al vapor",
    "braised": "estofado",
    "stewed": "guisado",
    "boiled": "hervido",
    "fried": "frito",
    "roasted": "asado",
    "baked": "al horno",
    "grilled": "a la parrilla",
    "broiled": "al grill",
}
# What the user wrote (Spanish, without accents) -> the USDA words that agree with it.
COCCION_ES = {
    "plancha": {"grilled", "broiled", "pan-broiled", "pan-fried", "pan-browned"},
    "parrilla": {"grilled", "broiled"},
    "barbacoa": {"grilled", "broiled"},
    "horno": {"roasted", "baked"},
    "asad": {"roasted", "baked", "grilled", "broiled"},
    "frit": {"fried", "pan-fried"},
    "sarten": {"pan-fried", "pan-browned", "fried", "pan-broiled"},
    "cocid": {"boiled", "hard-boiled", "moist heat", "stewed", "poached"},
    "hervid": {"boiled", "hard-boiled", "moist heat"},
    "guisad": {"stewed", "braised", "moist heat"},
    "estofad": {"stewed", "braised", "moist heat"},
    "vapor": {"steamed"},
    "escalfad": {"poached"},
    "revuelt": {"scrambled"},
    "microondas": {"microwaved"},
}


def _sin_acentos(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _coccion_candidato(nombre: str) -> str | None:
    low = nombre.lower()
    for palabra in COCCION_EN:  # longest words first: "hard-boiled" before "boiled"
        if re.search(rf"\b{re.escape(palabra)}\b", low):
            return palabra
    return None


def supuestos_del_candidato(nombre_es: str, cand: Candidato, del_llm: list[str]) -> list[str]:
    """Assumptions to add for a USDA row. Skip the ones that the LLM already wrote."""
    if cand.fuente != "USDA":
        return []
    usuario = _sin_acentos(nombre_es)
    ya = _sin_acentos(" ".join(del_llm))
    out: list[str] = []

    grasa = GRASA_RE.search(cand.nombre)
    if grasa and "%" not in usuario and "%" not in ya:
        out.append(f"La base de datos fija {grasa.group(1)} % magra, {grasa.group(2)} % grasa")

    low = cand.nombre.lower()
    if "piel" not in usuario and "piel" not in ya:
        # USDA "meat only" means without skin, "meat and skin" means with skin.
        if any(w in low for w in ("skinless", "without skin", "meat only")):
            out.append("La base de datos usa la versión sin piel")
        elif "with skin" in low or "meat and skin" in low:
            out.append("La base de datos usa la versión con piel")

    coccion = _coccion_candidato(cand.nombre)
    if coccion:
        dichos = [k for k in COCCION_ES if k in usuario]
        compatible = any(coccion in COCCION_ES[k] for k in dichos)
        texto = COCCION_EN[coccion]
        if not compatible and _sin_acentos(texto) not in ya:
            if dichos:
                out.append(
                    f"La base de datos usa la cocción «{texto}» ({coccion}), no la del texto"
                )
            else:
                out.append(f"La base de datos usa la cocción «{texto}» ({coccion})")
    return out


def _familia_grasa(nombre: str) -> str | None:
    """The name without its fat percentage: the same food in another fat variant."""
    m = GRASA_RE.search(nombre)
    if not m:
        return None
    return re.sub(r"[\s,]+", " ", (nombre[: m.start()] + nombre[m.end() :]).lower()).strip()


def alternativa_por_grasa(
    cand: Candidato, opciones: list[Candidato]
) -> tuple[Candidato, str] | None:
    """Fallback when the LLM gives no alternative: the same food with another fat %.

    Of those variants, take the one that changes the kcal most.
    """
    familia = _familia_grasa(cand.nombre)
    if familia is None:
        return None
    variantes = [c for c in opciones if c.id != cand.id and _familia_grasa(c.nombre) == familia]
    if not variantes:
        return None
    alt = max(variantes, key=lambda c: abs(c.kcal - cand.kcal))
    return alt, f"{GRASA_RE.search(alt.nombre).group(1)} % magra"
