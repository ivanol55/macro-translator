"""The request pipeline: interpret (LLM) -> search (DB) -> select (LLM) -> calculate (code)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import calc
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
def normalizar(ing: IngredienteLLM) -> Ingrediente:
    gramos, notas = calc.a_gramos(ing)
    return Ingrediente(
        nombre_es=ing.nombre_es.strip(),
        nombre_en=ing.nombre_en.strip(),
        marca=ing.marca.strip() if ing.marca else None,
        gramos=gramos,
        estado=ing.estado,
        valores_usuario_100g=ing.valores_usuario_100g,
        supuestos=[s.strip() for s in ing.supuestos if s.strip()] + notas,
    )


def interpretar(texto: str, llm: LLMClient, prompts: Prompts) -> list[Ingrediente]:
    res = llm.chat_json(prompts.parse, texto, Interpretacion, "interpretacion", prompts.retry)
    return [normalizar(i) for i in res.ingredientes]


# --------------------------------------------------------------------------- #
# Step 2: search
# --------------------------------------------------------------------------- #
def necesita_bd(ing: Ingrediente) -> bool:
    return ing.valores_usuario_100g is None or not ing.valores_usuario_100g.completos()


def buscar(db: FoodDB, ing: Ingrediente) -> list[Candidato]:
    off = db.buscar_off(ing.nombre_es, ing.marca)
    usda = db.buscar_usda(ing.nombre_en, ing.estado)
    # Branded food: Open Food Facts first. Generic food: USDA first.
    return off + usda if ing.marca else usda + off


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
            fuente = "Open Food Facts" if c.fuente == "OFF" else "USDA"
            partes.append(
                f"- {c.clave} | {fuente} | {c.nombre} | marca: {c.marca or '-'} | por 100 g: "
                f"{_num(c.kcal)} kcal, prot {_num(c.prot)} g, hidratos {_num(c.carbs)} g, "
                f"grasas {_num(c.grasas)} g"
            )
        partes.append("")
    return "\n".join(partes)


def esquema_seleccion(candidatos: dict[int, list[Candidato]]) -> dict:
    claves = sorted({c.clave for cs in candidatos.values() for c in cs})
    return {
        "type": "object",
        "properties": {
            "selecciones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ingrediente_idx": {"type": "integer", "enum": sorted(candidatos)},
                        "motivo": {"type": "string"},
                        "candidato_id": {
                            "anyOf": [{"type": "string", "enum": claves}, {"type": "null"}]
                        },
                    },
                    "required": ["ingrediente_idx", "motivo", "candidato_id"],
                },
            }
        },
        "required": ["selecciones"],
    }


def seleccionar(
    ingredientes: list[Ingrediente],
    candidatos: dict[int, list[Candidato]],
    llm: LLMClient,
    prompts: Prompts,
) -> dict[int, tuple[Candidato | None, str]]:
    if not candidatos:
        return {}
    res = llm.chat_json(
        prompts.select,
        texto_seleccion(ingredientes, candidatos),
        Selecciones,
        "seleccion",
        prompts.retry,
        schema=esquema_seleccion(candidatos),
        context={"candidatos": candidatos},
    )
    out: dict[int, tuple[Candidato | None, str]] = {}
    for s in res.selecciones:
        cand = next((c for c in candidatos[s.ingrediente_idx] if c.clave == s.candidato_id), None)
        out[s.ingrediente_idx] = (cand, s.motivo.strip())
    return out


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


def construir_fila(ing: Ingrediente, eleccion: tuple[Candidato | None, str] | None) -> Fila:
    supuestos = list(ing.supuestos)
    user = ing.valores_usuario_100g
    motivo = None

    if user is not None and user.completos():
        v100 = Macros(kcal=user.kcal, prot=user.prot, carbs=user.carbs, grasas=user.grasas)
        origen = Origen(tipo="Usuario", nombre="Valores de la etiqueta escritos por el usuario")
    else:
        cand, motivo = eleccion if eleccion else (None, "sin candidatos en la base de datos")
        if cand is None:
            return Fila(
                ingrediente=ing.nombre_es,
                gramos=ing.gramos,
                encontrado=False,
                valores=None,
                redondeado=None,
                origen=None,
                motivo=motivo or None,
                aviso="No encontrado: no se estima.",
                supuestos=supuestos,
            )
        v100 = Macros(kcal=cand.kcal, prot=cand.prot, carbs=cand.carbs, grasas=cand.grasas)
        origen = _origen(cand)
        if user is not None:  # some label values: they win over the database
            v100 = Macros(**(v100.model_dump() | user.model_dump(exclude_none=True)))
            faltan = [k for k, v in user.model_dump().items() if v is None]
            origen = origen.model_copy(update={"tipo": "Usuario", "complemento": cand.fuente})
            supuestos.append(f"Etiqueta incompleta: {', '.join(faltan)} de {cand.fuente}")

    valores = calc.escalar(v100, ing.gramos)
    return Fila(
        ingrediente=ing.nombre_es,
        gramos=ing.gramos,
        encontrado=True,
        valores=valores,
        redondeado=calc.redondear_macros(valores),
        origen=origen,
        motivo=motivo or None,
        aviso=calc.aviso_coherencia(v100),
        supuestos=supuestos,
    )


def totalizar(filas: list[Fila]) -> Resultado:
    encontradas = [f.valores for f in filas if f.encontrado and f.valores is not None]
    total = calc.sumar(encontradas)
    avisos = []
    faltan = [f.ingrediente for f in filas if not f.encontrado]
    if faltan:
        avisos.append("El total no incluye: " + ", ".join(faltan) + " (no encontrado).")
    if not filas:
        avisos.append("No hay alimentos en el texto.")
    return Resultado(
        filas=filas, total=total, total_redondeado=calc.redondear_macros(total), avisos=avisos
    )


# --------------------------------------------------------------------------- #
def calcular(texto: str, llm: LLMClient, db: FoodDB, prompts: Prompts) -> Resultado:
    ingredientes = interpretar(texto, llm, prompts)
    candidatos: dict[int, list[Candidato]] = {}
    for idx, ing in enumerate(ingredientes):
        if necesita_bd(ing):
            cands = buscar(db, ing)
            if cands:
                candidatos[idx] = cands
    elecciones = seleccionar(ingredientes, candidatos, llm, prompts)
    filas = [construir_fila(ing, elecciones.get(i)) for i, ing in enumerate(ingredientes)]
    return totalizar(filas)
