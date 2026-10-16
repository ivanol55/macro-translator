#!/usr/bin/env python3
"""Run the pipeline from the terminal. Useful to tune the prompts without the UI.

Usage:
    LLM_BASE_URL=http://127.0.0.1:8080/v1 LLM_MODEL=gpt-oss-20b \
        python scripts/cli.py "huevos cocidos, 2, 130g. Algo de aceite de oliva"
    python scripts/cli.py --json "..."     # print the full API response
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from macro_translator.config import load_settings  # noqa: E402
from macro_translator.db import FoodDB  # noqa: E402
from macro_translator.llm import LLMClient  # noqa: E402
from macro_translator.pipeline import Prompts, calcular  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("texto")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    llm = LLMClient(settings)
    t0 = time.monotonic()
    res = calcular(args.texto, llm, FoodDB(settings.db_path), Prompts.load(settings.prompts_dir))
    elapsed = time.monotonic() - t0
    if args.json:
        print(res.model_dump_json(indent=2))
        return 0

    def n(x: float) -> str:
        return f"{x:.1f}".replace(".", ",")

    print("| Ingrediente | Cantidad | Kcal | Prot | Carbs | Grasas |")
    print("|---|---:|---:|---:|---:|---:|")
    for f in res.filas:
        g = f"{n(f.gramos).removesuffix(',0')} g"
        if f.encontrado:
            r = f.redondeado
            print(
                f"| {f.ingrediente} | {g} | {n(r.kcal)} | {n(r.prot)} | {n(r.carbs)} | "
                f"{n(r.grasas)} |"
            )
        else:
            print(f"| {f.ingrediente} | {g} | no encontrado | | | |")
    t = res.total_redondeado
    print(
        f"| **TOTAL** | | **{n(t.kcal)}** | **{n(t.prot)}** | **{n(t.carbs)}** | "
        f"**{n(t.grasas)}** |"
    )
    print()
    for f in res.filas:
        o = f.origen
        src = f"{o.tipo}: {o.nombre}" + (f" ({o.marca})" if o and o.marca else "") if o else "-"
        print(f"- {f.ingrediente}: {src}" + (f" — {f.motivo}" if f.motivo else ""))
        for s in f.supuestos:
            print(f"    supuesto: {s}")
        if f.aviso and f.encontrado:
            print(f"    aviso: {f.aviso}")
    for a in res.avisos:
        print(f"AVISO: {a}")
    print(f"\n({elapsed:.1f} s, modelo {settings.llm_model})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
