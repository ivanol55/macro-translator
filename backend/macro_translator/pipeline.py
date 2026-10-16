"""The request pipeline: interpret (LLM) -> search (DB) -> select (LLM) -> calculate (code)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import atributos, calc, etiqueta, unidades
from .db import FoodDB
from .llm import LLMClient
from .models import (
    Candidato,
    Fila,
    Ingrediente,
    IngredienteLLM,
    Interpretacion,
    Macros,
    Origen,
    Resultado,
    Selecciones,
)

log = logging.getLogger("macro_translator.pipeline")

OFF_PRODUCT_URL = "https://world.openfoodfacts.org/product/{code}"
FDC_FOOD_URL = "https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients"


@dataclass
class Prompts:
    parse: str
    select: str
    retry: str

    @classmethod
    def load(cls, folder: Path) -> Prompts:
        return cls(
            parse=(folder / "parse.md").read_text(encoding="utf-8"),
            select=(folder / "select.md").read_text(encoding="utf-8"),
            retry=(folder / "retry.md").read_text(encoding="utf-8"),
        )


# --------------------------------------------------------------------------- #
# Step 1: interpretation
# --------------------------------------------------------------------------- #
def _numero_re(x: float) -> str:
    """Regex for a number as the user can write it: 130, 62.5, 62,5."""
    if x == int(x):
        return rf"(?<![\d.,]){int(x)}(?![\d]|[.,]\d)"
    entero, dec = f"{x:g}".split(".")
    return rf"(?<![\d.,]){entero}[.,]{dec}(?!\d)"


def corregir_veces(ing: IngredienteLLM, texto: str) -> tuple[IngredienteLLM, str | None]:
    """Apply the brief's rule in code: "huevos, 2, 130g" is 130 g in total.

    A weight is per piece only with a marker: "2×130 g", "130 g cada uno",
    "2 huevos de 130 g". When the LLM says per piece (veces > 1), the weight is
    in the text, and no marker is next to it, the weight is the total.
    An estimated weight (not in the text, as in "2 huevos") does not change.
    """
    if ing.veces <= 1 or ing.unidad not in ("g", "kg", "ml", "l"):
        return ing, None
    n = _numero_re(ing.cantidad)
    if not re.search(n, texto):
        return ing, None
    marcas = (
        rf"\d\s*[x×*]\s*{n}",  # 2×130 g, 2 x 130 g
        rf"{n}\s*(g|gr|gramos|kg|ml|l)?\.?\s*(cada|por\s+(pieza|unidad))",  # 130 g cada uno
        rf"\bde\s+{n}\s*(g|gr|gramos|kg|ml|l)\b",  # 2 huevos de 130 g
    )
    if any(re.search(m, texto, re.IGNORECASE) for m in marcas):
        return ing, None
    nota = (
        f"{ing.cantidad:g} {ing.unidad} es el total: el texto no dice que sea por pieza "
        f'(regla: "2, 130 g" = 130 g en total)'
    )
    return ing.model_copy(update={"veces": 1}), nota


def normalizar(ing: IngredienteLLM, texto: str = "") -> Ingrediente:
    ing, nota = corregir_veces(ing, texto)
    rec = unidades.reconstruible(ing, texto)
    avisos: list[str] = []
    if rec.cantidad is not None:  # the text fixes the quantity: use it
        ing = ing.model_copy(update={"cantidad": rec.cantidad})
    if rec.motivo:
        avisos.append(rec.motivo)

    # Label values win over the database, but only when the code can check them.
    valores, aviso_etiqueta = etiqueta.validar(ing.valores_usuario_100g, texto, ing.nombre_es)
    if aviso_etiqueta:
        avisos.append(aviso_etiqueta)

    gramos, notas = calc.a_gramos(ing)
    if nota:
        notas.insert(0, nota)
    if not rec.ok:
        avisos.append("Cantidad no válida: no coincide con el texto; no se incluye en el total.")
    return Ingrediente(
        nombre_es=ing.nombre_es.strip(),
        nombre_en=ing.nombre_en.strip(),
        marca=ing.marca.strip() if ing.marca else None,
        gramos=gramos,
        estado=ing.estado,
        valores_usuario_100g=valores,
        supuestos=[s.strip() for s in ing.supuestos if s.strip()] + notas,
        cantidad_valida=rec.ok,
        avisos=avisos,
    )


def _como_dato(texto: str) -> str:
    """Wrap the user text so the model reads it as data, not as instructions."""
    return f"<texto_usuario>\n{texto}\n</texto_usuario>"


def interpretar(texto: str, llm: LLMClient, prompts: Prompts) -> list[Ingrediente]:
    res = llm.chat_json(
        prompts.parse, _como_dato(texto), Interpretacion, "interpretacion", prompts.retry
    )
    return [normalizar(i, texto) for i in res.ingredientes]


# --------------------------------------------------------------------------- #
# Step 2: search
# --------------------------------------------------------------------------- #
def necesita_bd(ing: Ingrediente) -> bool:
    if not ing.cantidad_valida:
        return False
    return ing.valores_usuario_100g is None or not ing.valores_usuario_100g.completos()


def buscar(db: FoodDB, ing: Ingrediente) -> tuple[list[Candidato], list[Candidato]]:
    """Return (preferred, fallback) candidates.

    Branded food: Open Food Facts first. Generic food: USDA first. The LLM sees
    the fallback source only when nothing in the preferred source is the same food.
    """
    off = db.buscar_off(ing.nombre_es, ing.marca)
    usda = db.buscar_usda(ing.nombre_en, ing.estado)
    return (off, usda) if ing.marca else (usda, off)


# --------------------------------------------------------------------------- #
# Step 3: selection
# --------------------------------------------------------------------------- #
def _num(x: float) -> str:
    return f"{x:g}"


def texto_seleccion(ingredientes: list[Ingrediente], candidatos: dict[int, list[Candidato]]) -> str:
    partes = []
    for idx, cands in candidatos.items():
        ing = ingredientes[idx]
        partes.append(
            f"### Ingrediente {idx}: {ing.nombre_es} (en inglés: {ing.nombre_en}); "
            f"marca: {ing.marca or 'ninguna'}; estado: {ing.estado}"
        )
        for c in cands:
            fuente = (
                "Open Food Facts (producto envasado)" if c.fuente == "OFF" else "USDA (genérico)"
            )
            partes.append(
                f"- {c.clave} | {fuente} | {c.nombre} | marca: {c.marca or '-'} | por 100 g: "
                f"{_num(c.kcal)} kcal, prot {_num(c.prot)} g, hidratos {_num(c.carbs)} g, "
                f"grasas {_num(c.grasas)} g"
            )
        partes.append("")
    return "\n".join(partes)


def esquema_seleccion(candidatos: dict[int, list[Candidato]]) -> dict:
    claves = sorted({c.clave for cs in candidatos.values() for c in cs})
    clave_o_null = {"anyOf": [{"type": "string", "enum": claves}, {"type": "null"}]}
    props = {
        "ingrediente_idx": {"type": "integer", "enum": sorted(candidatos)},
        "motivo": {"type": "string"},
        "candidato_id": clave_o_null,
        "supuestos": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "alternativa_id": clave_o_null,
        "alternativa": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    }
    return {
        "type": "object",
        "properties": {
            "selecciones": {
                "type": "array",
                "items": {"type": "object", "properties": props, "required": list(props)},
            }
        },
        "required": ["selecciones"],
    }


@dataclass
class Eleccion:
    """What the LLM chose for one ingredient, with the candidates resolved."""

    candidato: Candidato | None
    motivo: str
    supuestos: list[str] = field(default_factory=list)
    alternativa: Candidato | None = None
    alternativa_texto: str | None = None
    opciones: list[Candidato] = field(default_factory=list)  # the candidates of the round


def _seleccionar_ronda(
    ingredientes: list[Ingrediente],
    candidatos: dict[int, list[Candidato]],
    llm: LLMClient,
    prompts: Prompts,
) -> dict[int, Eleccion]:
    res = llm.chat_json(
        prompts.select,
        texto_seleccion(ingredientes, candidatos),
        Selecciones,
        "seleccion",
        prompts.retry,
        schema=esquema_seleccion(candidatos),
        context={"candidatos": candidatos},
    )
    out: dict[int, Eleccion] = {}
    for s in res.selecciones:
        por_clave = {c.clave: c for c in candidatos[s.ingrediente_idx]}
        cand = por_clave.get(s.candidato_id) if s.candidato_id else None
        out[s.ingrediente_idx] = Eleccion(
            candidato=cand,
            motivo=s.motivo.strip(),
            supuestos=s.supuestos if cand else [],
            alternativa=por_clave.get(s.alternativa_id) if cand and s.alternativa_id else None,
            alternativa_texto=s.alternativa,
            opciones=candidatos[s.ingrediente_idx],
        )
    return out


def seleccionar(
    ingredientes: list[Ingrediente],
    candidatos: dict[int, tuple[list[Candidato], list[Candidato]]],
    llm: LLMClient,
    prompts: Prompts,
) -> dict[int, Eleccion]:
    """Round 1: the preferred source. Round 2: the other source, only for null answers."""
    ronda1 = {i: pref or alt for i, (pref, alt) in candidatos.items() if pref or alt}
    out = _seleccionar_ronda(ingredientes, ronda1, llm, prompts) if ronda1 else {}
    ronda2 = {
        i: alt
        for i, (pref, alt) in candidatos.items()
        if pref and alt and i in out and out[i].candidato is None
    }
    if ronda2:
        for i, el in _seleccionar_ronda(ingredientes, ronda2, llm, prompts).items():
            if el.candidato is None and el.motivo != out[i].motivo:
                el.motivo = f"{out[i].motivo} / {el.motivo}".strip(" /")
            out[i] = el
    return out


_EXACTA = (
    (re.compile(r"\bexact[ao]mente\b", re.IGNORECASE), "aproximadamente"),
    (re.compile(r"\bexact([ao]s?)\b", re.IGNORECASE), r"aproximad\1"),
    (re.compile(r"\bid[eé]ntic([ao]s?)\b", re.IGNORECASE), r"parecid\1"),
)


def limpiar_motivo(motivo: str, supuestos: list[str]) -> str:
    """A choice with assumptions is not an exact match. The reason must not say so."""
    if not supuestos:
        return motivo
    for patron, cambio in _EXACTA:
        motivo = patron.sub(cambio, motivo)
    return motivo


# --------------------------------------------------------------------------- #
# Step 4: calculation (deterministic)
# --------------------------------------------------------------------------- #
def _origen(c: Candidato) -> Origen:
    url = None
    if c.fuente == "OFF" and c.codigo_barras:
        url = OFF_PRODUCT_URL.format(code=c.codigo_barras)
    elif c.fuente == "USDA" and c.fdc_id:
        url = FDC_FOOD_URL.format(fdc_id=c.fdc_id)
    return Origen(
        tipo=c.fuente, nombre=c.nombre, marca=c.marca, codigo_barras=c.codigo_barras, url=url
    )


def construir_fila(ing: Ingrediente, eleccion: Eleccion | None) -> Fila:
    supuestos = list(ing.supuestos)
    if not ing.cantidad_valida:
        return Fila(
            ingrediente=ing.nombre_es,
            gramos=ing.gramos,
            encontrado=False,
            valores=None,
            redondeado=None,
            origen=None,
            motivo=None,
            aviso=" ".join(ing.avisos) or "Cantidad no válida: no se incluye en el total.",
            supuestos=supuestos,
        )

    user = ing.valores_usuario_100g
    motivo = None
    sensibilidad = None
    alternativa = None

    # A row takes its four values from ONE source. The label values (checked in
    # `etiqueta.validar`) are complete, so they never mix with the database.
    if user is not None:
        v100 = Macros(kcal=user.kcal, prot=user.prot, carbs=user.carbs, grasas=user.grasas)
        origen = Origen(tipo="Usuario", nombre="Valores de la etiqueta escritos por el usuario")
    else:
        el = eleccion or Eleccion(candidato=None, motivo="sin candidatos en la base de datos")
        cand = el.candidato
        motivo = limpiar_motivo(el.motivo, el.supuestos)
        if cand is None:
            return Fila(
                ingrediente=ing.nombre_es,
                gramos=ing.gramos,
                encontrado=False,
                valores=None,
                redondeado=None,
                origen=None,
                motivo=motivo or None,
                aviso=" ".join(ing.avisos) or None,
                supuestos=supuestos,
            )
        # What the database row fixes and the user did not say goes next to the table.
        propios = el.supuestos + atributos.supuestos_del_candidato(
            ing.nombre_es, cand, el.supuestos, ing.marca
        )
        supuestos.extend(propios)
        motivo = limpiar_motivo(el.motivo, propios)
        v100 = Macros(kcal=cand.kcal, prot=cand.prot, carbs=cand.carbs, grasas=cand.grasas)
        origen = _origen(cand)
        alt, variante = el.alternativa, el.alternativa_texto
        if alt is None:  # the LLM gave none: try the same food with another fat %
            alt, variante = atributos.alternativa_por_grasa(cand, el.opciones) or (None, None)
        if alt is not None:
            sensibilidad = calc.aviso_sensibilidad(
                cand.kcal, alt.kcal, ing.gramos, variante or alt.nombre
            )
            if sensibilidad:
                alternativa = _origen(alt)

    valores = calc.escalar(v100, ing.gramos)
    avisos = list(ing.avisos)
    aviso_coherencia = calc.aviso_coherencia(v100)
    if aviso_coherencia:
        avisos.append(aviso_coherencia)
    return Fila(
        ingrediente=ing.nombre_es,
        gramos=ing.gramos,
        encontrado=True,
        valores=valores,
        redondeado=calc.redondear_macros(valores),
        origen=origen,
        motivo=motivo or None,
        aviso=" ".join(avisos) or None,
        sensibilidad=sensibilidad,
        alternativa=alternativa,
        supuestos=supuestos,
    )


def totalizar(filas: list[Fila]) -> Resultado:
    encontradas = [f.valores for f in filas if f.encontrado and f.valores is not None]
    total = calc.sumar(encontradas)
    avisos = []
    faltan = [f.ingrediente for f in filas if not f.encontrado]
    if faltan:
        avisos.append(
            "El total no incluye: " + ", ".join(faltan) + " (no encontrado o cantidad no válida)."
        )
    if not filas:
        avisos.append("No hay alimentos en el texto.")
    return Resultado(
        filas=filas, total=total, total_redondeado=calc.redondear_macros(total), avisos=avisos
    )


# --------------------------------------------------------------------------- #
def calcular(texto: str, llm: LLMClient, db: FoodDB, prompts: Prompts) -> Resultado:
    ingredientes = interpretar(texto, llm, prompts)
    candidatos = {i: buscar(db, ing) for i, ing in enumerate(ingredientes) if necesita_bd(ing)}
    elecciones = seleccionar(ingredientes, candidatos, llm, prompts)
    filas = [construir_fila(ing, elecciones.get(i)) for i, ing in enumerate(ingredientes)]
    return totalizar(filas)
