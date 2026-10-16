"""Attributes that a database row fixes and the user did not give, found by code.

The LLM writes these assumptions too, but small models forget them. This check
covers the most common cases: the fat percentage, the skin, the cooking method,
the animal part, the dairy variant, the cocoa percentage, the sugar and the brand.

The rules live in one table (`REGLAS_EXTRA`). Add a rule there to cover a new
attribute: a regular expression on the candidate name, the Spanish sentence for
the table, and the words that mean the user already gave the attribute.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

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
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


# --------------------------------------------------------------------------- #
# Extensible table: attributes fixed by the row that the user did not give.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Regla:
    """One attribute. `render` turns the match into the Spanish sentence.

    `usuario_claves` are substrings of the user text (without accents) that mean
    the user already gave the attribute: then the rule does not add a sentence.
    """

    patron: re.Pattern
    render: Callable[[re.Match], str]
    usuario_claves: tuple[str, ...] = ()
    fuentes: tuple[str, ...] = ("USDA", "OFF")


def _texto_cacao(m: re.Match) -> str:
    if m.group(1) and m.group(2):
        return f"La base de datos usa chocolate con {m.group(1)}-{m.group(2)} % de cacao"
    return f"La base de datos usa chocolate con {m.group(3)} % de cacao"


REGLAS_EXTRA: tuple[Regla, ...] = (
    Regla(
        patron=re.compile(
            r"(\d+)\s*[-–]\s*(\d+)\s*%\s*(?:cacao|cocoa)|(\d+)\s*%\s*(?:cacao|cocoa)",
            re.IGNORECASE,
        ),
        render=_texto_cacao,
        usuario_claves=("cacao", "cocoa", "%"),
    ),
    Regla(
        patron=re.compile(r"\b(entera|whole\s?milk|whole-milk)\b", re.IGNORECASE),
        render=lambda m: "La base de datos usa la versión entera",
        usuario_claves=("entera", "desnatad", "semidesnat"),
    ),
    Regla(
        patron=re.compile(r"\b(desnatad\w*|skim\w*|non-?fat|fat-free)\b", re.IGNORECASE),
        render=lambda m: "La base de datos usa la versión desnatada",
        usuario_claves=("entera", "desnatad", "semidesnat"),
    ),
    Regla(
        patron=re.compile(r"\b(plain|natural|sin azúcar|no sugar|unsweetened)\b", re.IGNORECASE),
        render=lambda m: "La base de datos usa la versión sin azúcar",
        usuario_claves=("azucar", "natural"),
    ),
    Regla(
        patron=re.compile(r"\b(azucarad\w*|sweetened|with sugar)\b", re.IGNORECASE),
        render=lambda m: "La base de datos usa la versión con azúcar",
        usuario_claves=("azucar",),
    ),
    Regla(
        patron=re.compile(r"\bbreast\b", re.IGNORECASE),
        render=lambda m: "La base de datos usa la parte «pechuga»",
        usuario_claves=("pechuga", "muslo", "pierna", "ala"),
    ),
    Regla(
        patron=re.compile(r"\bthigh\b", re.IGNORECASE),
        render=lambda m: "La base de datos usa la parte «muslo»",
        usuario_claves=("pechuga", "muslo", "pierna", "ala"),
    ),
    Regla(
        patron=re.compile(r"\bcommercial\b", re.IGNORECASE),
        render=lambda m: "La base de datos usa un producto comercial",
        usuario_claves=("comercial", "envasad"),
    ),
)


def _coccion_candidato(nombre: str) -> str | None:
    low = nombre.lower()
    for palabra in COCCION_EN:  # longest words first: "hard-boiled" before "boiled"
        if re.search(rf"\b{re.escape(palabra)}\b", low):
            return palabra
    return None


def _reglas_extra(nombre: str, usuario: str, ya: str, fuente: str) -> list[str]:
    out: list[str] = []
    for regla in REGLAS_EXTRA:
        if fuente not in regla.fuentes:
            continue
        m = regla.patron.search(nombre)
        if not m:
            continue
        if any(k in usuario for k in regla.usuario_claves):
            continue
        texto = regla.render(m)
        if _sin_acentos(texto) in ya:
            continue
        out.append(texto)
    return out


def supuestos_del_candidato(
    nombre_es: str, cand: Candidato, del_llm: list[str], marca: str | None = None
) -> list[str]:
    """Assumptions to add for a row. Skip the ones that the LLM already wrote."""
    usuario = _sin_acentos(nombre_es)
    ya = _sin_acentos(" ".join(del_llm))
    out: list[str] = []

    if cand.fuente == "USDA":
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

    ya_con_out = _sin_acentos(" ".join(del_llm + out))
    out += _reglas_extra(cand.nombre, usuario, ya_con_out, cand.fuente)

    if marca:
        m_usuario = _sin_acentos(marca)
        m_cand = _sin_acentos(cand.marca or "")
        if m_usuario and m_usuario not in m_cand and m_usuario not in ya_con_out:
            if m_cand:
                out.append(f"La base de datos es de la marca {cand.marca}, no {marca.strip()}")
            else:
                out.append(f"La base de datos no tiene la marca {marca.strip()}")

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
