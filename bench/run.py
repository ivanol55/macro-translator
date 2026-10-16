#!/usr/bin/env python3
"""Benchmark harness for the macro-translator pipeline.

It runs every case in bench/cases/*.jsonl through the SAME backend functions the
app uses (`macro_translator.pipeline.calcular` with `LLMClient` and `FoodDB`),
once per run (3 by default) for every model in bench/models.yaml.

For each execution it stores the raw LLM outputs, the validated JSON, the number
of retries, the candidates offered, the selection, the final table and the
latency of every LLM call. It also recomputes the table from the selection to
check that the deterministic part is exact.

    uv run python bench/run.py                         # all models, dev+test, 3 runs
    uv run python bench/run.py --split dev --runs 1    # quick pass
    uv run python bench/run.py --only qty-001,eti-001

The results go to bench/results/<date>_<model>_<commit>.jsonl (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from macro_translator import calc, pipeline  # noqa: E402
from macro_translator.config import Settings  # noqa: E402
from macro_translator.db import FoodDB  # noqa: E402
from macro_translator.llm import LLMClient, LLMError  # noqa: E402
from macro_translator.models import Macros  # noqa: E402
from macro_translator.pipeline import Prompts  # noqa: E402

CASES_DIR = ROOT / "bench" / "cases"
RESULTS_DIR = ROOT / "bench" / "results"


# --------------------------------------------------------------------------- #
# models.yaml (minimal parser: no PyYAML dependency)
# --------------------------------------------------------------------------- #
def _parse_value(raw: str):
    raw = raw.strip()
    if not raw:
        return ""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    low = raw.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"null", "none"}:
        return None
    if raw[0] in "[{":
        return json.loads(raw)
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def load_models(path: Path) -> list[dict]:
    models: list[dict] = []
    current: dict | None = None
    in_models = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped == "models:":
            in_models = True
            continue
        if not in_models:
            continue
        if stripped.startswith("- "):
            current = {}
            models.append(current)
            stripped = stripped[2:]
        if current is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        current[key.strip()] = _parse_value(value)
    return models


def settings_for(entry: dict) -> Settings:
    base = Settings()
    api_key = entry.get("api_key") or ""
    if not api_key and entry.get("api_key_env"):
        api_key = os.environ.get(str(entry["api_key_env"]), "")
    extra = entry.get("extra_body") or {}
    if isinstance(extra, str):
        extra = json.loads(extra) if extra.strip() else {}
    return replace(
        base,
        llm_base_url=str(entry["base_url"]).rstrip("/"),
        llm_api_key=api_key.strip(),
        llm_model=str(entry["model"]),
        llm_response_format=str(entry.get("response_format", "auto")),
        llm_max_retries=int(entry.get("max_retries", 2)),
        llm_temperature=float(entry.get("temperature", 0)),
        llm_timeout=float(entry.get("timeout", 180)),
        llm_extra_body=extra,
        db_path=Path(
            entry.get("db_path") or os.environ.get("DB_PATH") or ROOT / "data" / "macros.duckdb"
        ),
        prompts_dir=ROOT / "backend" / "prompts",
    )


# --------------------------------------------------------------------------- #
# Instrumented LLM client: records every call and every chat_json phase
# --------------------------------------------------------------------------- #
class RecordingLLM(LLMClient):
    def __init__(self, settings: Settings, **kw):
        super().__init__(settings, **kw)
        self.calls: list[dict] = []
        self.phases: list[dict] = []

    def _complete(self, messages: list[dict], name: str, schema: dict) -> str:
        t0 = time.monotonic()
        content: str | None = None
        error: str | None = None
        try:
            content = super()._complete(messages, name, schema)
        except Exception as exc:  # noqa: BLE001 - recorded and re-raised
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.calls.append(
                {
                    "phase": name,
                    "latency_s": round(time.monotonic() - t0, 3),
                    "content": content,
                    "error": error,
                }
            )
        return content

    def chat_json(self, system, user, model, name, retry_template, schema=None, context=None):
        start = len(self.calls)
        try:
            res = super().chat_json(
                system, user, model, name, retry_template, schema=schema, context=context
            )
        except LLMError as exc:
            self.phases.append(
                {
                    "kind": name,
                    "ok": False,
                    "attempts": len(self.calls) - start,
                    "validated": None,
                    "error": str(exc),
                    "raw": [c["content"] for c in self.calls[start:]],
                }
            )
            raise
        self.phases.append(
            {
                "kind": name,
                "ok": True,
                "attempts": len(self.calls) - start,
                "validated": res.model_dump(mode="json"),
                "error": None,
                "raw": [c["content"] for c in self.calls[start:]],
            }
        )
        return res


# --------------------------------------------------------------------------- #
# Deterministic recomputation from the selection
# --------------------------------------------------------------------------- #
def check_determinism(resultado: dict, ingredientes, elecciones) -> dict:
    """Rebuild every row from the selection with calc. Must equal the app table."""
    discrepancias: list[str] = []
    for idx, fila in enumerate(resultado["filas"]):
        ing = ingredientes[idx]
        user = ing.get("valores_usuario_100g")
        el = elecciones.get(str(idx)) or {}
        cand = el.get("candidato")
        # Mirror pipeline.construir_fila: a row takes its values from ONE source.
        if not ing.get("cantidad_valida", True):
            if fila.get("encontrado"):
                discrepancias.append(f"{idx}: cantidad no válida pero la app dice encontrado")
            continue
        try:
            if user and all(user.get(k) is not None for k in ("kcal", "prot", "carbs", "grasas")):
                v100 = Macros(
                    kcal=user["kcal"], prot=user["prot"], carbs=user["carbs"], grasas=user["grasas"]
                )
            elif cand is not None:
                v100 = Macros(
                    kcal=cand["kcal"], prot=cand["prot"], carbs=cand["carbs"], grasas=cand["grasas"]
                )
            else:
                if fila.get("encontrado"):
                    discrepancias.append(f"{idx}: fila encontrada sin candidato")
                continue
        except Exception as exc:  # noqa: BLE001
            discrepancias.append(f"{idx}: {exc}")
            continue
        if not fila.get("encontrado"):
            discrepancias.append(f"{idx}: esperado encontrado pero la app dice no encontrado")
            continue
        esperado = calc.escalar(v100, ing["gramos"])
        real = fila["valores"]
        for m in ("kcal", "prot", "carbs", "grasas"):
            if abs(getattr(esperado, m) - real[m]) > 1e-6:
                discrepancias.append(
                    f"{idx}.{m}: app={real[m]:.6f} recomputado={getattr(esperado, m):.6f}"
                )
    encontradas = [
        fila["valores"] for fila in resultado["filas"] if fila.get("encontrado") and fila["valores"]
    ]
    recalc = calc.sumar([Macros(**v) for v in encontradas])
    for m in ("kcal", "prot", "carbs", "grasas"):
        if abs(getattr(recalc, m) - resultado["total"][m]) > 1e-6:
            discrepancias.append(
                f"total.{m}: app={resultado['total'][m]:.6f} recomputado={getattr(recalc, m):.6f}"
            )
    return {"ok": not discrepancias, "discrepancias": discrepancias}


# --------------------------------------------------------------------------- #
# One execution of one case
# --------------------------------------------------------------------------- #
def run_once(texto, llm: RecordingLLM, db: FoodDB, prompts: Prompts) -> dict:
    captured: dict = {}
    orig = (pipeline.interpretar, pipeline.buscar, pipeline.seleccionar)

    def interp_wrap(texto, llm_client, prompts_obj):
        out = orig[0](texto, llm_client, prompts_obj)
        captured["ingredientes"] = [i.model_dump(mode="json") for i in out]
        return out

    def buscar_wrap(d, ing):
        pref, alt = orig[1](d, ing)
        captured.setdefault("buscar", []).append(
            {
                "id": id(ing),
                "pref": [c.model_dump(mode="json") for c in pref],
                "alt": [c.model_dump(mode="json") for c in alt],
            }
        )
        return pref, alt

    def sel_wrap(ings, cands, llm_client, prompts_obj):
        out = orig[2](ings, cands, llm_client, prompts_obj)
        captured["elecciones"] = {
            str(k): {
                "candidato": v.candidato.model_dump(mode="json") if v.candidato else None,
                "motivo": v.motivo,
                "supuestos": v.supuestos,
                "alternativa": v.alternativa.model_dump(mode="json") if v.alternativa else None,
                "alternativa_texto": v.alternativa_texto,
            }
            for k, v in out.items()
        }
        return out

    pipeline.interpretar, pipeline.buscar, pipeline.seleccionar = interp_wrap, buscar_wrap, sel_wrap
    t0 = time.monotonic()
    record = {
        "ok": False,
        "error": None,
        "resultado": None,
        "ingredientes": [],
        "candidatos": {},
        "elecciones": {},
        "determinism": {"ok": None, "discrepancias": []},
        "total_latency_s": 0.0,
    }
    try:
        resultado = pipeline.calcular(texto, llm, db, prompts)
        record["ok"] = True
        record["resultado"] = resultado.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - the benchmark records failures
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        pipeline.interpretar, pipeline.buscar, pipeline.seleccionar = orig
        record["total_latency_s"] = round(time.monotonic() - t0, 3)

    ingredientes = captured.get("ingredientes", [])
    record["ingredientes"] = ingredientes
    # Map the database searches back to their ingredient index. `buscar` is called
    # in order over the ingredients that need the database (same order).
    idx_by_id = {}
    if captured.get("buscar"):
        needs = [i for i, ing in enumerate(ingredientes) if needs_db(ing)]
        for pos, rec in enumerate(captured["buscar"]):
            if pos < len(needs):
                idx_by_id[rec["id"]] = needs[pos]
    candidatos = {}
    for rec in captured.get("buscar", []):
        i = idx_by_id.get(rec["id"])
        if i is not None:
            candidatos[str(i)] = {"pref": rec["pref"], "alt": rec["alt"]}
    record["candidatos"] = candidatos
    record["elecciones"] = captured.get("elecciones", {})
    if record["ok"]:
        record["determinism"] = check_determinism(
            record["resultado"], ingredientes, record["elecciones"]
        )
    return record


def needs_db(ing: dict) -> bool:
    if not ing.get("cantidad_valida", True):
        return False
    user = ing.get("valores_usuario_100g")
    if user is None:
        return True
    return not all(user.get(k) is not None for k in ("kcal", "prot", "carbs", "grasas"))


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def load_cases(only: set[str] | None, categories: set[str] | None, split: str) -> list[dict]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            if only and c["id"] not in only:
                continue
            if categories and c["category"] not in categories:
                continue
            if split != "all" and c["split"] != split:
                continue
            cases.append(c)
    return cases


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "nogit"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=str(ROOT / "bench" / "models.yaml"))
    parser.add_argument("--cases", dest="only", default="", help="comma-separated case ids")
    parser.add_argument("--category", default="", help="comma-separated categories")
    parser.add_argument("--split", default="all", choices=["all", "dev", "test"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="max cases (0 = all)")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR))
    args = parser.parse_args()

    models_path = Path(args.models)
    if not models_path.exists():
        example = models_path.with_suffix(".yaml.example")
        print(f"No {models_path}; using {example}. Copy and edit it for real runs.")
        models_path = example
    models = load_models(models_path)
    if not models:
        print(f"No models in {models_path}")
        return 2

    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    categories = {x.strip() for x in args.category.split(",") if x.strip()} or None
    cases = load_cases(only, categories, args.split)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No cases selected.")
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fecha = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S")
    commit = git_commit()

    print(f"{len(cases)} cases x {len(models)} models x {args.runs} runs")
    for entry in models:
        name = entry.get("name", entry.get("model", "model"))
        settings = settings_for(entry)
        out_path = out_dir / f"{fecha}_{name}_{commit}.jsonl"
        db = FoodDB(settings.db_path)
        if not db.available():
            print(f"[{name}] database not found at {settings.db_path}; skipping")
            continue
        prompts = Prompts.load(settings.prompts_dir)
        llm = RecordingLLM(settings)
        print(f"[{name}] -> {out_path.name}  ({settings.llm_base_url}, {settings.llm_model})")
        with out_path.open("w", encoding="utf-8") as fh:
            for case in cases:
                for run_idx in range(args.runs):
                    llm.calls.clear()
                    llm.phases.clear()
                    record = run_once(case["input"], llm, db, prompts)
                    attempts = sum(p["attempts"] for p in llm.phases)
                    phases = sum(1 for p in llm.phases)
                    record = {
                        "model": name,
                        "commit": commit,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "case_id": case["id"],
                        "category": case["category"],
                        "split": case["split"],
                        "run_idx": run_idx,
                        "input": case["input"],
                        "needs_review": case.get("needs_review", False),
                        "llm_calls": llm.calls,
                        "phases": llm.phases,
                        "n_llm_calls": len(llm.calls),
                        "n_retries": max(0, attempts - phases),
                        **record,
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fh.flush()
                    status = "ok" if record["ok"] else "ERROR"
                    det = "" if record["determinism"]["ok"] in (None, True) else " DET-MISMATCH"
                    print(
                        f"  {case['id']} run {run_idx + 1}/{args.runs}: {status}"
                        f" {record['total_latency_s']}s{det}"
                    )
        llm.close()
        print(f"[{name}] done")


if __name__ == "__main__":
    sys.exit(main())
