#!/usr/bin/env python3
"""Metrics and report for the macro-translator benchmark.

Reads every bench/results/*.jsonl produced by bench/run.py, scores each run
against the case expectations and writes bench/REPORT.md.

    uv run python bench/report.py
    uv run python bench/report.py --results 'bench/results/2026-*_local-*.jsonl'
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "bench" / "cases"
RESULTS_DIR = ROOT / "bench" / "results"
REPORT = ROOT / "bench" / "REPORT.md"

STOP = {
    "de",
    "del",
    "la",
    "el",
    "los",
    "las",
    "en",
    "y",
    "a",
    "al",
    "con",
    "un",
    "una",
    "the",
    "of",
    "or",
    "and",
    "to",
    "raw",
    "fresh",
    "whole",
    "sin",
}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def toks(text: str) -> set[str]:
    return {
        singular(w) for w in re.findall(r"[a-z0-9%]+", norm(text)) if w not in STOP and len(w) > 2
    }


def concept_match(concept: str, nombre: str) -> bool:
    c, n = toks(concept), toks(nombre)
    if not c:
        return False
    return len(c & n) >= max(1, math.ceil(0.6 * len(c)))


def assumption_match(assumption: str, texts: list[str]) -> bool:
    a = toks(assumption)
    if not a:
        return False
    joined = toks(" ".join(texts))
    return len(a & joined) >= max(1, math.ceil(0.5 * len(a)))


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def num(x: float | None, d: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{d}f}"


# --------------------------------------------------------------------------- #
def load_cases() -> dict[str, dict]:
    cases = {}
    for path in sorted(CASES_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                c = json.loads(line)
                cases[c["id"]] = c
    return cases


def load_results(pattern: str) -> list[dict]:
    files = sorted(glob.glob(pattern))
    records = []
    for f in files:
        for line in Path(f).read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# --------------------------------------------------------------------------- #
def candidate_key(fuente: str, nombre: str) -> str:
    return f"{fuente}: {nombre}"


def evaluate_run(case: dict, rec: dict) -> dict:
    parsed = rec.get("ingredientes") or []
    resultado = rec.get("resultado")
    filas = resultado["filas"] if resultado else []
    elecciones = rec.get("elecciones") or {}
    items = case["expected_items"]

    matched: dict[int, int] = {}
    used: set[int] = set()
    for ii, item in enumerate(items):
        for pi, p in enumerate(parsed):
            if pi in used:
                continue
            if concept_match(item["concept"], p.get("nombre_es", "")):
                matched[ii] = pi
                used.add(pi)
                break

    counts = defaultdict(int)
    fails: list[str] = []
    details: dict = {"grams": {}, "selection": {}, "honesty": {}, "matching": {}}

    counts["items"] = len(items)
    counts["parsed"] = len(parsed)
    counts["matched_items"] = len(matched)
    counts["matched_parsed"] = len(used)
    # Ingredient matching log: expected ↔ parsed, for the failure-mode split.
    details["matching"] = {
        "expected": [item["concept"] for item in items],
        "parsed": [p.get("nombre_es") for p in parsed],
        "pairs": {items[ii]["concept"]: parsed[pi].get("nombre_es") for ii, pi in matched.items()},
        "unmatched_expected": [
            item["concept"] for ii, item in enumerate(items) if ii not in matched
        ],
        "unmatched_parsed": [p.get("nombre_es") for pi, p in enumerate(parsed) if pi not in used],
    }

    # parsing accuracy
    for ii, item in enumerate(items):
        pi = matched.get(ii)
        if pi is None:
            fails.append(f"no se parseó «{item['concept']}»")
            continue
        p = parsed[pi]
        g = float(p.get("gramos") or 0)
        gtol = item["grams_tolerance_pct"] / 100
        grams_ok = abs(g - item["grams"]) <= max(0.5, item["grams"] * gtol)
        counts["grams_total"] += 1
        counts["grams_ok"] += int(grams_ok)
        details["grams"][item["concept"]] = {"esperado": item["grams"], "real": g, "ok": grams_ok}
        if not grams_ok:
            fails.append(f"gramos «{item['concept']}»: esperado {item['grams']} g, real {g} g")
        if item["state"] != "n/a":
            counts["state_total"] += 1
            state_ok = norm(p.get("estado", "")) == norm(item["state"])
            counts["state_ok"] += int(state_ok)
            if not state_ok:
                fails.append(
                    f"estado «{item['concept']}»: esperado {item['state']}, real {p.get('estado')}"
                )

    # selection
    for ii, item in enumerate(items):
        if not item["acceptable_candidates"] or item["label_priority"]:
            continue
        pi = matched.get(ii)
        if pi is None:
            continue
        el = elecciones.get(str(pi)) or {}
        cand = el.get("candidato")
        chosen = None
        if cand:
            chosen = candidate_key(cand["fuente"], cand["nombre"])
        elif pi < len(filas) and filas[pi].get("origen"):
            o = filas[pi]["origen"]
            if o.get("tipo") in ("OFF", "USDA"):
                chosen = candidate_key(o["tipo"], o.get("nombre") or "")
        counts["select_total"] += 1
        ok = chosen is not None and any(
            norm(chosen) == norm(a) for a in item["acceptable_candidates"]
        )
        counts["select_ok"] += int(ok)
        offered = False
        if not ok:
            cands = (rec.get("candidatos") or {}).get(str(pi)) or {}
            pool = (cands.get("pref") or []) + (cands.get("alt") or [])
            names = {norm(candidate_key(c["fuente"], c["nombre"])) for c in pool}
            offered = any(norm(a) in names for a in item["acceptable_candidates"])
        details["selection"][item["concept"]] = {"chosen": chosen, "ok": ok, "offered": offered}
        if not ok:
            fails.append(
                f"selección «{item['concept']}»: {chosen!r} no está en las respuestas válidas"
            )

    # honesty
    for ii, item in enumerate(items):
        pi = matched.get(ii)
        if pi is None:
            continue
        fila = filas[pi] if pi < len(filas) else {}
        el = elecciones.get(str(pi)) or {}
        texts = (
            (fila.get("supuestos") or []) + (el.get("supuestos") or []) + [el.get("motivo") or ""]
        )
        if item["variant_unspecified"]:
            counts["variant_total"] += 1
            declared = any(assumption_match(a, texts) for a in item["required_assumptions"])
            counts["variant_declared"] += int(declared)
            details["honesty"][item["concept"]] = {"declared": declared}
            if not declared:
                fails.append(
                    f"no declara el supuesto «{item['concept']}»: {item['required_assumptions']}"
                )
        if item["variant_unspecified"] or item["required_assumptions"]:
            motivo = norm(fila.get("motivo") or "")
            if re.search(r"\bexact|identic", motivo):
                counts["exact_claim"] += 1
                fails.append(f"el motivo dice exacto con supuestos: «{item['concept']}»")

    # expected not found
    for concept in case.get("expected_not_found", []):
        counts["nf_total"] += 1
        bad = any(
            f.get("encontrado") and concept_match(concept, f.get("ingrediente", "")) for f in filas
        )
        counts["nf_ok"] += int(not bad)
        if bad:
            fails.append(f"alimento inexistente estimado: «{concept}»")

    # false "not found"
    for ii, item in enumerate(items):
        pi = matched.get(ii)
        if pi is not None and pi < len(filas):
            counts["found_total"] += 1
            if not filas[pi].get("encontrado"):
                fails.append(f"no encontrado falso: «{item['concept']}»")
            else:
                counts["found_ok"] += 1

    # final values
    for ii, item in enumerate(items):
        pi = matched.get(ii)
        if pi is None or pi >= len(filas):
            continue
        fila = filas[pi]
        counts["final_total"] += 1
        if not fila.get("encontrado") or not fila.get("valores"):
            continue
        v = fila["valores"]
        mr = item.get("macros_range") or {}
        ok = item["kcal_range"][0] <= v["kcal"] <= item["kcal_range"][1]
        ok = ok and all(
            (not mr.get(m)) or mr[m][0] <= v[m] <= mr[m][1] for m in ("prot", "carbs", "grasas")
        )
        counts["final_ok"] += int(ok)
        if not ok:
            offs = []
            if not (item["kcal_range"][0] <= v["kcal"] <= item["kcal_range"][1]):
                offs.append(f"kcal {round(v['kcal'], 1)} fuera de {item['kcal_range']}")
            for macro in ("prot", "carbs", "grasas"):
                if mr.get(macro) and not (mr[macro][0] <= v[macro] <= mr[macro][1]):
                    offs.append(f"{macro} {round(v[macro], 1)} fuera de {mr[macro]}")
            fails.append(f"valores «{item['concept']}»: " + "; ".join(offs))

    # total (for compound dishes and any case with a known total)
    expected_total = case.get("expected_total")
    if expected_total:
        counts["total_total"] = 1
        total = (resultado or {}).get("total") or {}

        def in_range(value, rng) -> bool:
            return rng is None or rng[0] <= value <= rng[1]

        ok = all(
            in_range(total.get(m, 0.0), expected_total.get(f"{m}_range"))
            for m in ("kcal", "prot", "carbs", "grasas")
        )
        counts["total_ok"] = int(ok)
        if not ok:
            offs = [
                f"{m} {round(total.get(m, 0.0), 1)} fuera de {expected_total[f'{m}_range']}"
                for m in ("kcal", "prot", "carbs", "grasas")
                if not in_range(total.get(m, 0.0), expected_total.get(f"{m}_range"))
            ]
            fails.append("total: " + "; ".join(offs))

    # robustness
    phases = rec.get("phases") or []
    counts["json_first"] = int(all(p.get("ok") and p.get("attempts") == 1 for p in phases))
    if rec.get("error"):
        fails.append(f"error del pipeline: {rec['error']}")

    # security on hostile inputs: the table must stay inside the expected range
    is_hostile = case["category"] == "hostil"
    not_altered = None
    if is_hostile:
        not_altered = counts["final_total"] > 0 and counts["final_ok"] == counts["final_total"]
        if not not_altered:
            fails.append("resultado alterado por entrada hostil")

    return {
        "counts": dict(counts),
        "fails": fails,
        "details": details,
        "hostile_not_altered": not_altered,
    }


def signature(rec: dict) -> tuple:
    resultado = rec.get("resultado") or {}
    rows = []
    for f in resultado.get("filas", []):
        o = f.get("origen") or {}
        rows.append(
            (
                norm(f.get("ingrediente", "")),
                round(float(f.get("gramos") or 0), 1),
                o.get("tipo"),
                o.get("nombre"),
            )
        )
    total = resultado.get("total_redondeado") or {}
    return (
        tuple(rows),
        round(float(total.get("kcal") or 0), 0),
        round(float(total.get("prot") or 0), 0),
    )


# --------------------------------------------------------------------------- #
def rate(counts: dict, num_key: str, den_key: str) -> float | None:
    d = counts.get(den_key, 0)
    if not d:
        return None
    return counts.get(num_key, 0) / d


def aggregate_case(runs: list[dict]) -> dict:
    metrics = defaultdict(list)
    for r in runs:
        c = r["counts"]
        metrics["parse_recall"].append(rate(c, "matched_items", "items") or 0.0)
        metrics["parse_precision"].append(
            rate(c, "matched_parsed", "parsed")
            if c.get("parsed")
            else (1.0 if not c.get("items") else 0.0)
        )
        metrics["grams_acc"].append(rate(c, "grams_ok", "grams_total"))
        metrics["state_acc"].append(rate(c, "state_ok", "state_total"))
        metrics["select_acc"].append(rate(c, "select_ok", "select_total"))
        metrics["notfound_acc"].append(rate(c, "nf_ok", "nf_total"))
        metrics["honesty_declared"].append(rate(c, "variant_declared", "variant_total"))
        metrics["exact_claim"].append(1.0 if c.get("exact_claim") else 0.0)
        metrics["final_acc"].append(rate(c, "final_ok", "final_total"))
        metrics["total_acc"].append(rate(c, "total_ok", "total_total"))
        metrics["false_not_found"].append(
            1.0 if any("no encontrado falso" in f for f in r["fails"]) else 0.0
        )
        metrics["json_first"].append(float(c.get("json_first", 0)))
        metrics["retries"].append(r.get("n_retries", 0))
        metrics["error"].append(1.0 if not r.get("ok") else 0.0)
        metrics["determinism"].append(1.0 if (r.get("determinism") or {}).get("ok") else 0.0)
        metrics["latency"].append(r.get("total_latency_s", 0.0))

    out = {}
    for k, vs in metrics.items():
        clean = [v for v in vs if v is not None]
        out[k] = statistics.mean(clean) if clean else None
    details = runs[0]
    out["fails"] = details["fails"]
    out["runs"] = runs
    out["consistent"] = len({signature(r) for r in runs}) == 1
    hostile = [r["hostile_not_altered"] for r in runs if r["hostile_not_altered"] is not None]
    out["security"] = statistics.mean([1.0 if h else 0.0 for h in hostile]) if hostile else None
    return out


def mean_of(cases: list[dict], key: str) -> float | None:
    vals = [c[key] for c in cases if c.get(key) is not None]
    return statistics.mean(vals) if vals else None


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, int(math.ceil(q * len(vs))) - 1))
    return vs[idx]


# --------------------------------------------------------------------------- #
GLOBAL_METRICS = [
    ("parse_recall", "Recall de ingredientes"),
    ("parse_precision", "Precisión de ingredientes"),
    ("grams_acc", "Acierto en gramos"),
    ("state_acc", "Acierto en estado"),
    ("select_acc", "Selección válida"),
    ("notfound_acc", "«No encontrado» correcto"),
    ("false_not_found", "Falsos «no encontrado»"),
    ("honesty_declared", "Supuesto declarado (variantes)"),
    ("exact_claim", "Motivo dice «exacto» con supuestos"),
    ("final_acc", "Tabla final dentro de rango"),
    ("total_acc", "Total dentro de rango"),
    ("security", "Entrada hostil no altera el resultado"),
    ("json_first", "JSON válido a la primera"),
    ("determinism", "Determinismo del cálculo"),
    ("consistent", "Consistencia entre las 3 ejecuciones"),
]

FIXES = {
    "confunde total con unidad": (
        "La regla «2, 130 g = 130 g en total» ya está en código (`corregir_veces`), pero solo "
        "actúa si el LLM devuelve `veces>1`. Refuerza el prompt de parseo con más ejemplos "
        "negativos y añade en código una comprobación para «N, Xg» sin marca de pieza.",
        "prompt + pipeline",
    ),
    "confunde la cantidad escrita": (
        "Más ejemplos de unidades y formatos en el prompt de parseo; el cálculo ya es correcto.",
        "prompt",
    ),
    "fija variantes sin declararlo": (
        "El código ya añade el % de grasa, la piel y la cocción para USDA "
        "(`atributos.supuestos_del_candidato`), pero no para OFF ni otras variantes. "
        "Ampliar esa tabla y exigir en el prompt de selección el supuesto de cada atributo.",
        "prompt + código determinista",
    ),
    "selección: el modelo elige otro candidato (el correcto estaba ofrecido)": (
        "El candidato correcto sí llegó al modelo y el modelo eligió otro. Mejorar la selección: "
        "un modelo mayor o un reranker de embeddings sobre los candidatos ofrecidos.",
        "modelo / reranker",
    ),
    "selección: el candidato correcto no se ofreció (recuperación)": (
        "El candidato correcto no está en la lista ofrecida. Ningún prompt lo arregla: mejorar el "
        "buscador BM25 (sinónimos es→en, re-ranking, tamaño del pool) o añadir embeddings.",
        "BD/ETL + pipeline",
    ),
    "posible desajuste del gold o del emparejado (revisar a mano)": (
        "El emparejado esperado↔parseado es por solapamiento de palabras. Si no casa ninguno, "
        "puede ser un fallo del gold o del emparejado, no del modelo. Revisar el caso a mano.",
        "revisar gold / harness",
    ),
    "elige un candidato distinto al alimento correcto": (
        "El buscador BM25 devuelve distractores (p. ej. «Oil, oat» para «avena», «Broccoli, "
        "chinese»). Mejorar el re-ranking (sinónimos es→en, penalizar términos ausentes) y, si no "
        "basta, añadir un reranker de embeddings sobre los candidatos.",
        "BD/ETL + pipeline (embeddings)",
    ),
    "marca como no encontrado un alimento existente": (
        "Candidato correcto ausente del top-5 por el ranking BM25. Ampliar el pool y el "
        "re-ranking, o subir el número de candidatos para modelos con contexto amplio.",
        "BD/ETL + pipeline",
    ),
    "estima un alimento inexistente": (
        "El prompt dice que null es mejor que un alimento distinto, pero el modelo elige un "
        "candidato que comparte palabras. Añadir una comprobación en código y ejemplos de null.",
        "prompt + código determinista",
    ),
    "declara coincidencia exacta con supuestos": (
        "`limpiar_motivo` ya reescribe «exacto»→«aproximado» con supuestos; el fallo aparece "
        "en motivos ya formateados o en respuestas raras. Revisar los patrones de reescritura.",
        "código determinista",
    ),
    "no respeta los valores de etiqueta del usuario": (
        "Reforzar el prompt de parseo: los valores por ración no son por 100 g; copiar solo los "
        "valores «por 100 g» y declarar el supuesto cuando la etiqueta es por ración.",
        "prompt",
    ),
    "JSON inválido o error del modelo": (
        "Un modelo pequeño con esquemas grandes falla más. Reducir el esquema, seguir usando "
        "`response_format` y reintentos, o usar un modelo fine-tuneado para el parseo.",
        "pipeline (o fine-tuning)",
    ),
    "resultado alterado por entrada hostil": (
        "El texto se pasa en un mensaje de usuario y el sistema no se deja llevar por "
        "instrucciones incrustadas; los fallos son de calidad del modelo, no de inyección.",
        "prompt",
    ),
}


def failure_mode(counts: dict, case: dict, run: dict) -> list[str]:
    tags = []
    c = counts
    if c.get("grams_total") and c.get("grams_ok", 0) < c.get("grams_total", 0):
        text = case["input"].lower()
        if "cada" in text or "×" in text or " x " in text or re.search(r"\d\s*,\s*\d", text):
            tags.append("confunde total con unidad")
        else:
            tags.append("confunde la cantidad escrita")
    if c.get("select_total") and c.get("select_ok", 0) < c.get("select_total", 0):
        selection = (run.get("details") or {}).get("selection", {})
        if any(not d.get("ok") and d.get("offered") for d in selection.values()):
            tags.append("selección: el modelo elige otro candidato (el correcto estaba ofrecido)")
        if any(not d.get("ok") and not d.get("offered") for d in selection.values()):
            tags.append("selección: el candidato correcto no se ofreció (recuperación)")
    if c.get("variant_total") and c.get("variant_declared", 0) < c.get("variant_total", 0):
        tags.append("fija variantes sin declararlo")
    if any("no encontrado falso" in f for f in run["fails"]):
        tags.append("marca como no encontrado un alimento existente")
    if c.get("nf_total") and c.get("nf_ok", 0) < c.get("nf_total", 0):
        tags.append("estima un alimento inexistente")
    if c.get("exact_claim"):
        tags.append("declara coincidencia exacta con supuestos")
    for f in run["fails"]:
        if "etiqueta" in f.lower():
            tags.append("no respeta los valores de etiqueta del usuario")
    if not run.get("ok"):
        tags.append("JSON inválido o error del modelo")
    if run["hostile_not_altered"] is False:
        tags.append("resultado alterado por entrada hostil")
    if c.get("items") and c.get("matched_items", 0) == 0 and c.get("parsed", 0) > 0:
        tags.append("posible desajuste del gold o del emparejado (revisar a mano)")
    return list(dict.fromkeys(tags))


def write_report(records: list[dict], cases: dict[str, dict], out_path: Path = REPORT) -> None:
    models = sorted({r["model"] for r in records})
    commits = sorted({r["commit"] for r in records})
    by_model: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        by_model[r["model"]][r["case_id"]].append(r)
    for m in by_model:
        for cid in by_model[m]:
            by_model[m][cid].sort(key=lambda r: r["run_idx"])

    evaluated = {}
    for m, cm in by_model.items():
        evaluated[m] = {}
        for cid, runs in cm.items():
            ev_runs = []
            for r in runs:
                ev = evaluate_run(cases[cid], r)
                ev.update(
                    {
                        "run_idx": r["run_idx"],
                        "n_retries": r.get("n_retries", 0),
                        "ok": r.get("ok"),
                        "total_latency_s": r.get("total_latency_s", 0.0),
                        "determinism": r.get("determinism"),
                        "ingredientes": r.get("ingredientes"),
                        "elecciones": r.get("elecciones"),
                        "resultado": r.get("resultado"),
                        "llm_calls": r.get("llm_calls"),
                    }
                )
                ev_runs.append(ev)
            evaluated[m][cid] = aggregate_case(ev_runs)
    all_latencies = {
        m: [c["latency_s"] for r in records if r["model"] == m for c in (r.get("llm_calls") or [])]
        for m in models
    }

    categories = sorted({case["category"] for case in cases.values()})
    n_test = sum(1 for c in cases.values() if c["split"] == "test")

    L: list[str] = []
    L.append("# Benchmark de calidad — Macro Translator\n")
    L.append(
        f"Generado el {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"commit `{', '.join(commits)}` · {len(cases)} casos ({n_test} en `test`) · "
        f"{len(models)} modelo(s): {', '.join(models)}.\n"
    )
    L.append(
        "Cada caso se ejecuta 3 veces por modelo a través del mismo pipeline de la app "
        "(`pipeline.calcular`). Las métricas por caso son la media de las 3 ejecuciones.\n"
    )

    # 1. global
    L.append("## 1. Resumen global por modelo\n")
    header = "| Métrica | " + " | ".join(models) + " |"
    L.append(header)
    L.append("|---|" + "---:|" * len(models))
    for key, label in GLOBAL_METRICS:
        row = [pct(mean_of(list(evaluated[m].values()), key)) for m in models]
        L.append(f"| {label} | " + " | ".join(row) + " |")
    L.append("")

    # 2. by category
    L.append("## 2. Métricas por categoría\n")
    for m in models:
        L.append(f"### {m}\n")
        L.append(
            "| Categoría | n | Gramos | Estado | Selección | Final | Supuesto | No encontrado |"
        )
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for cat in categories:
            subset = [evaluated[m][cid] for cid in evaluated[m] if cases[cid]["category"] == cat]
            if not subset:
                continue
            L.append(
                f"| {cat} | {len(subset)} | {pct(mean_of(subset, 'grams_acc'))} | "
                f"{pct(mean_of(subset, 'state_acc'))} | {pct(mean_of(subset, 'select_acc'))} | "
                f"{pct(mean_of(subset, 'final_acc'))} | "
                f"{pct(mean_of(subset, 'honesty_declared'))} | "
                f"{pct(mean_of(subset, 'notfound_acc'))} |"
            )
        L.append("")

    # 3. cost and robustness
    L.append("## 3. Robustez y coste\n")
    L.append(
        "| Modelo | JSON 1ª | Reintentos medios | Errores | Determinismo | "
        "Consistencia | Latencia media/caso | p95 llamada |"
    )
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for m in models:
        cases_m = list(evaluated[m].values())
        L.append(
            f"| {m} | {pct(mean_of(cases_m, 'json_first'))} | "
            f"{num(mean_of(cases_m, 'retries'), 2)} | "
            f"{pct(mean_of(cases_m, 'error'))} | {pct(mean_of(cases_m, 'determinism'))} | "
            f"{pct(statistics.mean([1.0 if c['consistent'] else 0.0 for c in cases_m]))} | "
            f"{num(mean_of(cases_m, 'latency'), 1)} s | "
            f"{num(percentile(all_latencies[m], 0.95), 1)} s |"
        )
    L.append("")

    # 4. worst failures
    L.append("## 4. Peores fallos (traza completa)\n")
    scored = []
    per_case = {}
    for m in models:
        for cid, agg in evaluated[m].items():
            worst = max(agg["runs"], key=lambda r: len(r["fails"]))
            if worst["fails"]:
                per_case[(m, cid)] = (len(worst["fails"]), m, cid, worst)
    scored = sorted(per_case.values(), key=lambda x: -x[0])[:12]
    if not scored:
        L.append("No hay fallos registrados.\n")
    else:
        L.append("(Se muestra la peor de las 3 ejecuciones por caso.)\n")
    for score, m, cid, run in scored:
        case = cases[cid]
        L.append(f"### {cid} · {m} · run {run['run_idx'] + 1} · {score} fallos\n")
        L.append(f"- **Entrada:** `{case['input']}`")
        L.append(f"- **Categoría/split:** {case['category']} / {case['split']}")
        L.append("- **Fallos:**")
        for f in run["fails"]:
            L.append(f"  - {f}")
        L.append("- **Parseo (ingredientes):**")
        for ing in run.get("ingredientes") or []:
            L.append(
                f"  - `{ing.get('nombre_es')}` ({ing.get('nombre_en')}) "
                f"{ing.get('gramos')} g, estado {ing.get('estado')}"
            )
        L.append("- **Selección:**")
        for k, el in (run.get("elecciones") or {}).items():
            cand = el.get("candidato")
            desc = cand["fuente"] + ": " + cand["nombre"] if cand else "null"
            motivo = el.get("motivo")
            supuestos = el.get("supuestos")
            L.append(f"  - idx {k}: {desc} — «{motivo}» supuestos={supuestos}")
        L.append("- **Tabla final:**")
        res = run.get("resultado") or {}
        for f in res.get("filas", []):
            o = f.get("origen") or {}
            L.append(
                f"  - `{f.get('ingrediente')}` {f.get('gramos')} g → "
                f"{'no encontrado' if not f.get('encontrado') else f'{f.get("redondeado")}'} "
                f"({o.get('tipo', '')}: {o.get('nombre', '')})"
            )
        t = res.get("total_redondeado") or {}
        L.append(
            f"  - **TOTAL** {t.get('kcal')} kcal / {t.get('prot')} P / "
            f"{t.get('carbs')} C / {t.get('grasas')} G"
        )
        L.append("- **Esperado:** ")
        for item in case["expected_items"]:
            L.append(
                f"  - `{item['concept']}` {item['grams']} g, kcal {item['kcal_range']}, "
                f"candidatos {item['acceptable_candidates']}, "
                f"supuestos {item['required_assumptions']}"
            )
        for c in case.get("expected_not_found", []):
            L.append(f"  - no encontrado: `{c}`")
        # first raw LLM answer
        raw = (run.get("llm_calls") or [{}])[0].get("content")
        if raw:
            L.append(
                "- **Salida cruda del LLM (1ª llamada):**\n\n"
                f"  ```json\n  {raw.strip()[:1200]}\n  ```"
            )
        L.append("")

    # 5. failure modes
    L.append("## 5. Modos de fallo agrupados por causa\n")
    mode_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for m in models:
        for cid, agg in evaluated[m].items():
            for run in agg["runs"]:
                for tag in failure_mode(run["counts"], cases[cid], run):
                    mode_counts[tag][m] += 1
    L.append("| Modo de fallo | " + " | ".join(models) + " | Total (ejecuciones) |")
    L.append("|---" + "---:|" * len(models) + "---:|")
    for tag in sorted(mode_counts, key=lambda t: -sum(mode_counts[t].values())):
        per = [str(mode_counts[tag].get(m, 0)) for m in models]
        L.append(f"| {tag} | " + " | ".join(per) + f" | {sum(mode_counts[tag].values())} |")
    L.append("")

    # 6. fixes
    L.append("## 6. Propuestas de arreglo por modo de fallo\n")
    L.append("| Modo de fallo | Frecuencia (ejecuciones) | Arreglo propuesto | Tipo |")
    L.append("|---|---:|---|---|")
    for tag in sorted(mode_counts, key=lambda t: -sum(mode_counts[t].values())):
        freq = sum(mode_counts[tag].values())
        fix, kind = FIXES.get(tag, ("Analizar caso a caso.", "por decidir"))
        L.append(f"| {tag} | {freq} | {fix} | {kind} |")
    L.append("")
    L.append(
        "**Cuándo un arreglo de prompt no basta.** Cuando el fallo es de *recuperación* "
        "(el candidato correcto no está entre los 5 ofrecidos) o de *conocimiento* (mapear "
        "español→inglés, sinónimos, nombres de marca), ningún prompt lo arregla: el dato no está "
        "en el contexto. Ahí hacen falta mejoras de BD/ETL o un reranker de embeddings. El prompt "
        "sí basta para disciplina de formato, reglas de cantidad ya codificadas y declaración de "
        "supuestos, porque son instrucciones que el modelo puede seguir sin información extra.\n"
    )

    # 7. fine-tuning discussion
    L.append("## 7. ¿Modelo fine-tuneado y embeddings con reranker?\n")
    global_stats = {
        m: {k: mean_of(list(evaluated[m].values()), k) for k, _ in GLOBAL_METRICS} for m in models
    }
    m0 = models[0]
    g0 = global_stats[m0]
    L.append(
        f"Con los datos de `{m0}`: recall de parseo {pct(g0['parse_recall'])}, precisión "
        f"{pct(g0['parse_precision'])}, selección válida {pct(g0['select_acc'])} y supuesto "
        f"declarado {pct(g0['honesty_declared'])}.\n"
    )
    model_miss = sum(
        1
        for m in models
        for agg in evaluated[m].values()
        for run in agg["runs"]
        for d in (run.get("details") or {}).get("selection", {}).values()
        if not d.get("ok") and d.get("offered")
    )
    retrieval_miss = sum(
        1
        for m in models
        for agg in evaluated[m].values()
        for run in agg["runs"]
        for d in (run.get("details") or {}).get("selection", {}).values()
        if not d.get("ok") and not d.get("offered")
    )
    total_miss = model_miss + retrieval_miss
    share = f"{100 * model_miss / total_miss:.0f}%" if total_miss else "n/a"
    L.append(
        f"De los {total_miss} fallos de selección (ejecuciones), en {model_miss} ({share}) "
        f"el candidato correcto **sí** estaba entre los ofrecidos y el modelo eligió otro "
        f"(error del modelo); en {retrieval_miss} el candidato correcto **no** se ofreció "
        f"(fallo de recuperación).\n"
    )
    if total_miss and model_miss / total_miss >= 0.5:
        verdict = (
            "**Veredicto:** la mayoría de fallos de selección son del modelo, no del "
            "buscador. Merece la pena invertir en la selección (mejor modelo o un reranker "
            "de embeddings sobre los candidatos) antes que en otro cambio de prompt."
        )
    elif total_miss and retrieval_miss / total_miss > 0.5:
        verdict = (
            "**Veredicto:** la mayoría de fallos de selección son de recuperación. Hay que "
            "arreglar el buscador (sinónimos es→en, ranking, tamaño del pool) antes de añadir "
            "un modelo o un reranker: el dato correcto no llega al modelo."
        )
    else:
        verdict = (
            "**Veredicto:** los fallos de selección se reparten entre modelo y recuperación; "
            "conviene atacar las dos vías."
        )
    L.append(
        "- **Parseo:** el recall de ingredientes y el acierto en gramos son altos "
        f"({pct(g0['parse_recall'])} y {pct(g0['grams_acc'])}). Un modelo pequeño "
        "fine-tuneado aportaría sobre todo velocidad y coste, no corrección; solo tiene "
        "sentido si además hay que ejecutar en local sin GPU o reducir latencia.\n"
        f"- **Selección:** con {pct(g0['select_acc'])} de acierto, es donde está el margen. "
        "Un reranker de embeddings entrenado sobre (ingrediente, candidato correcto) "
        "recuperaría precisión sin cambiar el LLM cuando el candidato correcto ya está en la "
        "lista; si no está, primero hay que arreglar el buscador.\n"
        f"\n{verdict}\n"
    )

    # 8. review and method
    review = [
        cid
        for cid, c in cases.items()
        if c.get("needs_review") or any(it.get("needs_review") for it in c["expected_items"])
    ]
    L.append("## 8. Casos marcados para revisión (`needs_review`)\n")
    L.append(
        f"{len(review)} casos dependen de un criterio discutible (variante legítima, "
        "estimación de peso, plato compuesto). La propuesta caso a caso y la casilla de "
        "aprobación están en `bench/REVIEW.md`. Resumen:\n"
    )
    for cid in sorted(review):
        case = cases[cid]
        reasons = []
        for it in case["expected_items"]:
            if not it.get("needs_review"):
                continue
            if it.get("variant_unspecified"):
                reasons.append(
                    "variante sin especificar ("
                    + "; ".join(it.get("required_assumptions") or ["¿cuál?"])
                    + ")"
                )
            if it.get("label_priority"):
                reasons.append("se priorizan los valores de etiqueta del usuario")
            if it.get("grams_tolerance_pct", 0) >= 25:
                reasons.append("peso estimado por pieza/unidad")
        if case.get("needs_review") or not reasons:
            if case["category"] == "compuestos":
                reasons.append("plato compuesto: la composición varía")
            elif case["category"] == "hostil":
                reasons.append("caso hostil: revisar que el resultado esperado es el correcto")
            elif case["category"] == "genericos_usda":
                reasons.append("nombre genérico: puede haber varios candidatos USDA válidos")
            elif case["category"] == "etiqueta":
                reasons.append("valores de etiqueta: revisar la conversión por ración")
            else:
                reasons.append("criterio discutible")
        note = case.get("notes")
        text = "; ".join(dict.fromkeys(reasons))
        if note:
            text = f"{text} — {note}"
        L.append(f"- `{cid}` ({case['category']}): {text}")
    L.append("")
    L.append("## 9. Método y límites\n")
    L.append(
        "- El harness llama a las mismas funciones que la app, no a la API HTTP.\n"
        "- El emparejamiento ingrediente esperado ↔ parseado es por solapamiento de palabras "
        "(no hay alineación manual): puede sobrevalorar o infravalorar casos límite.\n"
        "- La tabla final se recalcula de forma independiente desde la selección: cualquier "
        "discrepancia con la app es un bug determinista.\n"
        "- Los ficheros de `bench/results/` no se commitean: pueden contener valores de Open "
        "Food Facts más allá de nombres e identificadores.\n"
    )

    out_path.write_text("\n".join(L), encoding="utf-8")
    print(f"Wrote {out_path} ({len(records)} executions, {len(models)} models)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=str(RESULTS_DIR / "*.jsonl"))
    parser.add_argument("--out", default=str(REPORT))
    args = parser.parse_args()
    cases = load_cases()
    records = load_results(args.results)
    if not records:
        print(f"No results in {args.results}. Run bench/run.py first.")
        return 1
    records = [r for r in records if r.get("case_id") in cases]
    write_report(records, cases, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
