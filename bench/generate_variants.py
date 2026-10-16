#!/usr/bin/env python3
"""Generate the `variantes` benchmark cases with the hybrid method.

The structure and the gold come from code over the database (SQL), the natural
language comes from the model. Each paraphrase is validated in code: it must
keep every number and the food, and it must NOT fix the variant. Every generated
case is marked `needs_review`.

    set -a; . ./.env; set +a
    uv run python bench/generate_variants.py --dry-run
    uv run python bench/generate_variants.py

The script keeps the cases it does not own (ids that do not start with
`genvar-`) and writes the generated ones next to them.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "bench"))

from macro_translator.llm import LLMClient, LLMError  # noqa: E402
from run import load_models, settings_for  # noqa: E402

CASES = ROOT / "bench" / "cases" / "variantes.jsonl"
DB = ROOT / "data" / "macros.duckdb"

COLS = ("fuente", "nombre", "kcal", "prot", "carbs", "grasas")


# --------------------------------------------------------------------------- #
# Catalog: one entry per variant food. The gold is the union of the USDA rows
# (`usda`) and the generic Open Food Facts rows (`off`), kept inside the
# plausible bounds. `cantidades` gives one case per quantity.
# --------------------------------------------------------------------------- #
@dataclass
class Spec:
    key: str
    concept: str
    nombre_es: str
    nombre_en: str
    cantidades: tuple[float, ...]
    assumption: str
    grasas: tuple[float, float]
    kcal: tuple[float, float]
    usda: str | None = None
    off: tuple[str, ...] = field(default_factory=tuple)
    unidad: str = "g"
    estado: str = "n/a"
    forbid: tuple[str, ...] = field(default_factory=tuple)


CATALOGO: tuple[Spec, ...] = (
    Spec(
        "leche",
        "leche",
        "leche",
        "milk",
        (200, 250, 100),
        "grasa de la leche",
        (0, 4),
        (28, 70),
        off=("leche", "leche entera", "leche semidesnatada", "leche desnatada"),
        unidad="ml",
        forbid=("entera", "semidesnatada", "desnatada"),
    ),
    Spec(
        "leche-vaca",
        "leche de vaca",
        "leche de vaca",
        "cow milk",
        (250, 200),
        "grasa de la leche",
        (0, 4),
        (28, 70),
        off=("leche de vaca", "leche de vaca entera", "leche"),
        unidad="ml",
        forbid=("entera", "semidesnatada", "desnatada"),
    ),
    Spec(
        "yogur-griego",
        "yogur griego",
        "yogur griego",
        "greek yogurt",
        (125, 200),
        "entera o desnatada",
        (0, 12),
        (40, 180),
        usda="nombre ILIKE 'Yogurt, Greek, plain%'",
        off=("yogur griego",),
        forbid=("azucarado",),
    ),
    Spec(
        "yogur-natural",
        "yogur natural",
        "yogur natural",
        "plain yogurt",
        (125, 200),
        "entera o desnatada",
        (0, 10),
        (40, 120),
        usda="nombre ILIKE 'Yogurt, plain%'",
        off=("yogur natural",),
        forbid=("azucarado",),
    ),
    Spec(
        "queso",
        "queso",
        "queso",
        "cheese",
        (40, 60, 30),
        "grasa del queso",
        (5, 45),
        (200, 480),
        usda="nombre ILIKE 'Cheese, %'",
        off=("queso",),
        forbid=("curado", "tierno", "fresco"),
    ),
    Spec(
        "queso-curado",
        "queso curado",
        "queso curado",
        "cured cheese",
        (40, 30),
        "grasa del queso",
        (20, 40),
        (300, 480),
        usda="nombre ILIKE 'Cheese, %'",
        off=("queso curado",),
        forbid=("tierno", "fresco", "semicurado"),
    ),
    Spec(
        "mantequilla",
        "mantequilla",
        "mantequilla",
        "butter",
        (15, 25),
        "grasa de la mantequilla",
        (75, 90),
        (600, 800),
        usda="nombre ILIKE 'Butter, %'",
        off=("mantequilla",),
        forbid=("sal",),
    ),
    Spec(
        "nata",
        "nata",
        "nata",
        "cream",
        (30, 50),
        "grasa de la nata",
        (10, 40),
        (100, 400),
        usda="nombre ILIKE 'Cream, %'",
        off=("nata",),
    ),
    Spec(
        "carne-picada-ternera",
        "carne picada de ternera",
        "carne picada de ternera",
        "ground beef",
        (150, 200, 100),
        "grasa de la carne picada",
        (3, 30),
        (100, 300),
        usda="(nombre ILIKE 'Beef, ground,%raw%' OR nombre ILIKE 'Beef, grass-fed, ground%')",
        off=("carne picada de ternera", "carne picada"),
        estado="crudo",
        forbid=("magra", "grasa"),
    ),
    Spec(
        "carne-picada-cerdo",
        "carne picada de cerdo",
        "carne picada de cerdo",
        "ground pork",
        (150, 200),
        "grasa de la carne picada",
        (2, 30),
        (100, 300),
        usda="nombre ILIKE 'Pork, %ground%raw%'",
        off=("carne picada de cerdo", "carne cerdo picada", "carne picada"),
        estado="crudo",
        forbid=("magra", "grasa"),
    ),
    Spec(
        "carne-picada-pavo",
        "carne picada de pavo",
        "carne picada de pavo",
        "ground turkey",
        (150, 200),
        "grasa de la carne picada",
        (1, 20),
        (80, 250),
        usda="nombre ILIKE 'Turkey, ground%raw%'",
        off=("carne picada de pavo",),
        estado="crudo",
        forbid=("magra", "grasa"),
    ),
    Spec(
        "carne-picada",
        "carne picada",
        "carne picada",
        "ground meat",
        (150, 200),
        "grasa de la carne picada",
        (3, 30),
        (100, 300),
        usda="(nombre ILIKE 'Beef, ground,%raw%' OR nombre ILIKE 'Beef, grass-fed, ground%')",
        off=("carne picada", "carne de vacuno picada"),
        estado="crudo",
    ),
    Spec(
        "pollo",
        "pollo",
        "pollo",
        "chicken",
        (150, 200),
        "piel o grasa",
        (1, 20),
        (90, 230),
        usda="nombre ILIKE 'Chicken, broiler%'",
        off=("pollo",),
        forbid=("pechuga", "muslo", "piel"),
    ),
    Spec(
        "pechuga-pollo",
        "pechuga de pollo",
        "pechuga de pollo",
        "chicken breast",
        (150, 120),
        "piel o grasa",
        (1, 6),
        (80, 170),
        usda="nombre ILIKE 'Chicken, broiler or fryers, breast%'",
        off=("pechuga de pollo",),
        forbid=("muslo", "piel"),
    ),
    Spec(
        "atun",
        "atún",
        "atún",
        "tuna",
        (120, 80),
        "grasa del atún",
        (0, 15),
        (60, 250),
        usda="nombre ILIKE 'Fish, tuna%'",
        off=("atún",),
        forbid=("aceite", "natural"),
    ),
    Spec(
        "salmon",
        "salmón",
        "salmón",
        "salmon",
        (150, 120),
        "grasa del salmón",
        (1, 20),
        (100, 280),
        usda="nombre ILIKE 'Fish, salmon%'",
        off=("salmón", "salmon"),
        forbid=("piel",),
    ),
    Spec(
        "gambas",
        "gambas",
        "gambas",
        "shrimp",
        (100, 150),
        "grasa de las gambas",
        (0, 3),
        (60, 140),
        usda="nombre ILIKE 'Crustaceans, shrimp%'",
        off=("gambas",),
    ),
    Spec(
        "mayonesa",
        "mayonesa",
        "mayonesa",
        "mayonnaise",
        (20, 15),
        "grasa de la mayonesa",
        (20, 100),
        (150, 750),
        usda="nombre ILIKE 'Salad dressing, mayonnaise%'",
        off=("mayonesa",),
        forbid=("light", "ligera"),
    ),
    Spec(
        "almendras",
        "almendras",
        "almendras",
        "almonds",
        (30, 20),
        "grasa de los frutos secos",
        (40, 60),
        (500, 700),
        usda="nombre ILIKE 'Nuts, almonds%'",
        off=("almendras",),
        forbid=("tostadas", "saladas"),
    ),
    Spec(
        "chocolate-negro",
        "chocolate negro",
        "chocolate negro",
        "dark chocolate",
        (20, 30),
        "grasa del chocolate",
        (25, 45),
        (400, 620),
        usda="nombre ILIKE 'Chocolate, dark, %'",
        off=("chocolate negro",),
        forbid=("cacao", "%"),
    ),
)


# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _numbers(text: str) -> list[str]:
    return [n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", _norm(text))]


def _fetch(db: duckdb.DuckDBPyConnection, clause: str) -> list[dict]:
    query = f"SELECT {', '.join(COLS)} FROM alimentos WHERE {clause}"
    return [dict(zip(COLS, r, strict=True)) for r in db.execute(query).fetchall()]


def rows_for(db: duckdb.DuckDBPyConnection, spec: Spec) -> list[dict]:
    rows: list[dict] = []
    if spec.usda:
        rows += _fetch(db, f"fuente='USDA' AND {spec.usda}")
    if spec.off:
        names = ", ".join("'" + n.replace("'", "''") + "'" for n in spec.off)
        rows += _fetch(db, f"fuente='OFF' AND lower(nombre) IN ({names})")
    rows = [
        r
        for r in rows
        if spec.grasas[0] <= r["grasas"] <= spec.grasas[1]
        and spec.kcal[0] <= r["kcal"] <= spec.kcal[1]
    ]
    return sorted(rows, key=lambda r: r["grasas"])


def build_gold(rows: list[dict], grams: float) -> dict:
    factor = grams / 100.0
    kcal = [r["kcal"] * factor for r in rows]
    macros = {m: [r[m] * factor for r in rows] for m in ("prot", "carbs", "grasas")}
    med = sorted(rows, key=lambda r: r["kcal"])[len(rows) // 2]

    def span(values: list[float]) -> list[float]:
        low, high = min(values), max(values)
        margin = max(0.5, (high - low) * 0.15)
        return [round(max(0.0, low - margin), 1), round(high + margin, 1)]

    return {
        "kcal_range": [math.floor(min(kcal) * 0.9), math.ceil(max(kcal) * 1.1)],
        "macros_range": {m: span(macros[m]) for m in macros},
        "expected_per100": {m: med[m] for m in ("kcal", "prot", "carbs", "grasas")},
        "acceptable_candidates": list(dict.fromkeys(f"{r['fuente']}: {r['nombre']}" for r in rows))[
            :20
        ],
    }


# --------------------------------------------------------------------------- #
# LLM paraphrase + validation
# --------------------------------------------------------------------------- #
class Parafrasis(BaseModel):
    texto: str = Field(min_length=1, max_length=300)


PARA_SYSTEM = (
    "Reescribes listas de comida en español natural, como las escribiría una persona "
    "en una app de calorías. No cambias ingredientes, cantidades ni unidades. No añades "
    "ni quitas información. No concretas variantes que el texto no diga."
)
PARA_PREGUNTA = (
    "Reescribe esta lista como UNA frase natural en español. Conserva EXACTAMENTE los "
    "mismos números, unidades e ingredientes. No concretes la variante (grasa, "
    "entera/desnatada, tipo, cocción). Devuelve solo JSON con la clave «texto».\n\n{spec}"
)
PARA_RETRY = (
    "Tu respuesta no sirvió: {error}. Vuelve a reescribir la lista conservando los "
    "mismos números e ingredientes y sin concretar la variante. Devuelve JSON con «texto»."
)


def _estructurado(spec: Spec, grams: float) -> str:
    partes = [f"{spec.nombre_es}: {grams:g} {spec.unidad}"]
    if spec.estado != "n/a":
        partes.append(f"estado: {spec.estado}")
    return "; ".join(partes)


def validar(texto: str, spec: Spec, grams: float) -> str | None:
    faltan = [n for n in _numbers(_estructurado(spec, grams)) if n not in _numbers(texto)]
    if faltan:
        return f"faltan los números {faltan}"
    palabras = [p for p in _norm(spec.concept).split() if len(p) > 3]
    if not all(p in _norm(texto) for p in palabras):
        return f"no aparece el alimento «{spec.concept}»"
    for palabra in spec.forbid:
        if _norm(palabra) in _norm(texto):
            return f"concreta la variante «{palabra}», que debe quedar abierta"
    return None


def parafrasear(llm: LLMClient, spec: Spec, grams: float) -> tuple[str, str | None]:
    struct = _estructurado(spec, grams)
    texto = struct
    for _ in range(3):
        try:
            out = llm.chat_json(
                PARA_SYSTEM,
                PARA_PREGUNTA.format(spec=struct),
                Parafrasis,
                f"parafrasis-{spec.key}-{grams:g}",
                PARA_RETRY,
            )
        except LLMError as exc:
            return struct, f"el modelo no dio JSON: {exc}"
        error = validar(out.texto, spec, grams)
        if error is None:
            return out.texto.strip(), None
        texto = out.texto
        struct = f"{_estructurado(spec, grams)}\n(aviso: {error})"
    return texto, "la paráfrasis no pasa la validación; se revisa a mano"


# --------------------------------------------------------------------------- #
def make_case(spec: Spec, grams: float, rows: list[dict], texto: str) -> dict:
    gold = build_gold(rows, grams)
    return {
        "id": f"genvar-{spec.key}-{grams:g}",
        "category": "variantes",
        "split": "test",
        "input": texto,
        "expected_items": [
            {
                "concept": spec.concept,
                "grams": grams,
                "grams_tolerance_pct": 2,
                "state": spec.estado,
                "acceptable_candidates": gold["acceptable_candidates"],
                "kcal_range": gold["kcal_range"],
                "expected_per100": gold["expected_per100"],
                "macro_tolerance_pct": 50,
                "required_assumptions": [spec.assumption],
                "variant_unspecified": True,
                "label_priority": False,
                "needs_review": True,
                "notes": f"generado: {len(rows)} variantes válidas en la BD",
                "macros_range": gold["macros_range"],
            }
        ],
        "expected_not_found": [],
        "expected_total": None,
        "needs_review": True,
        "notes": "generado con el método híbrido (gold de la BD, lenguaje del LLM)",
    }


def load_existing() -> list[dict]:
    out = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if not c["id"].startswith("genvar-"):
            out.append(c)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=str(ROOT / "bench" / "models.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="gold only, no LLM")
    parser.add_argument("--only", default="", help="comma-separated spec keys")
    parser.add_argument("--out", default=str(CASES))
    args = parser.parse_args()

    only = {k for k in args.only.split(",") if k}
    db = duckdb.connect(str(DB), read_only=True)
    existing = load_existing()

    llm: LLMClient | None = None
    if not args.dry_run:
        entry = load_models(Path(args.models))[0]
        llm = LLMClient(settings_for(entry))

    nuevos: list[dict] = []
    for spec in CATALOGO:
        if only and spec.key not in only:
            continue
        rows = rows_for(db, spec)
        if not rows:
            print(f"  {spec.key}: sin filas en la BD, se omite")
            continue
        for grams in spec.cantidades:
            if args.dry_run:
                texto, aviso = _estructurado(spec, grams), None
            else:
                texto, aviso = parafrasear(llm, spec, grams)
            nuevos.append(make_case(spec, grams, rows, texto))
            flag = "" if aviso is None else f"  [{aviso}]"
            print(f"  {spec.key} {grams:g}{spec.unidad}: «{texto}»{flag}")

    if llm is not None:
        llm.close()

    casos = existing + nuevos
    Path(args.out).write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in casos) + "\n", encoding="utf-8"
    )
    print(f"Escritos {len(casos)} casos ({len(existing)} previos + {len(nuevos)} generados)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
