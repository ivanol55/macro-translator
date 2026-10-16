"""Shared fixtures: a small database built by the real ETL code, and a fake LLM."""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import httpx
import pytest
from build_db import SourceInfo, build_database
from macro_translator.config import Settings
from macro_translator.db import FoodDB
from macro_translator.llm import LLMClient
from macro_translator.pipeline import Prompts

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
OFF_DOWNLOAD_DATE = "2026-10-16T00:02:40+00:00"


def build_fixture_db(dest: Path, off_parquet: Path) -> dict:
    usda = [
        (
            SourceInfo(
                "USDA Foundation Foods",
                "https://fdc.test/foundation.zip",
                "2026-10-16T00:03:17+00:00",
                "2026-04-30",
                None,
            ),
            FIXTURES / "usda" / "foundation",
        ),
        (
            SourceInfo(
                "USDA SR Legacy",
                "https://fdc.test/sr_legacy.zip",
                "2026-10-16T00:02:43+00:00",
                "2018-04",
                None,
            ),
            FIXTURES / "usda" / "sr_legacy",
        ),
    ]
    off = SourceInfo(
        "Open Food Facts", "https://off.test/food.parquet", OFF_DOWNLOAD_DATE, None, "en:spain"
    )
    return build_database(dest, off_parquet, usda, off, "en:spain", "es")


@pytest.fixture(scope="session")
def off_parquet(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The OFF fixture is JSON Lines in the repository (no Parquet files in git)."""
    out = tmp_path_factory.mktemp("off") / "off_sample.parquet"
    src = FIXTURES / "off_sample.jsonl"
    duckdb.execute(
        f"COPY (SELECT * FROM read_json('{src}', format = 'newline_delimited')) "
        f"TO '{out}' (FORMAT parquet)"
    )
    return out


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory, off_parquet: Path) -> Path:
    path = tmp_path_factory.mktemp("db") / "macros.duckdb"
    build_fixture_db(path, off_parquet)
    return path


@pytest.fixture(scope="session")
def food_db(db_path: Path) -> FoodDB:
    return FoodDB(db_path)


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_model="fake-model",
        db_path=db_path,
        prompts_dir=ROOT / "backend" / "prompts",
        frontend_dir=ROOT / "frontend",
    )


@pytest.fixture
def prompts() -> Prompts:
    return Prompts.load(ROOT / "backend" / "prompts")


# --------------------------------------------------------------------------- #
# Fake OpenAI-compatible LLM
# --------------------------------------------------------------------------- #
def ingrediente(nombre_es: str, nombre_en: str, cantidad: float, unidad: str = "g", **kw) -> dict:
    base = {
        "nombre_es": nombre_es,
        "nombre_en": nombre_en,
        "marca": None,
        "cantidad": cantidad,
        "unidad": unidad,
        "veces": 1,
        "es_aceite": False,
        "estado": "n/a",
        "valores_usuario_100g": None,
        "supuestos": [],
    }
    return base | kw


class FakeLLM:
    """Answer like an OpenAI-compatible server, without a model.

    interpretacion: the JSON that the first call returns.
    elegir: for each nombre_es, a text that the chosen candidate name contains,
            or None for "no candidate is the same food". A dict gives more:
            {"elegir", "supuestos", "alternativa", "alternativa_texto", "motivo",
             "alternativa_id"} (alternativa is a text in the alternative's name).
    """

    def __init__(self, interpretacion: dict, elegir: dict[str, str | None] | None = None):
        self.interpretacion = interpretacion
        self.elegir = elegir or {}
        self.requests: list[dict] = []
        self.selection_inputs: list[str] = []

    def responder(self, body: dict) -> dict:
        user = body["messages"][1]["content"]
        if "### Ingrediente" not in user:
            return self.interpretacion
        self.selection_inputs.append(user)
        selecciones = []
        for block in user.split("### Ingrediente ")[1:]:
            idx = int(block.split(":", 1)[0])
            nombre = re.match(r"\d+: (.*?) \(en inglés", block).group(1)
            regla = self.elegir.get(nombre)
            if not isinstance(regla, dict):
                regla = {"elegir": regla}
            clave = self._clave(block, regla.get("elegir"))
            alt = self._clave(block, regla.get("alternativa")) if clave else None
            selecciones.append(
                {
                    "ingrediente_idx": idx,
                    "motivo": regla.get("motivo", "fake"),
                    "candidato_id": clave,
                    "supuestos": regla.get("supuestos", []) if clave else [],
                    "alternativa_id": regla.get("alternativa_id", alt),
                    "alternativa": regla.get("alternativa_texto") if alt else None,
                }
            )
        return {"selecciones": selecciones}

    @staticmethod
    def _clave(block: str, buscado: str | None) -> str | None:
        """Key of the first candidate line that contains the text."""
        if not buscado:
            return None
        for line in block.splitlines():
            if line.startswith("- ") and buscado.lower() in line.lower():
                return line[2:].split(" |", 1)[0]
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        content = json.dumps(self.responder(body), ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def client(self, settings: Settings) -> LLMClient:
        return LLMClient(settings, transport=httpx.MockTransport(self))


# The reference case, as a correct LLM reads it.
REFERENCIA_TEXTO = (
    "sepia faraónica Hacendado, el paquete entero de 403 g, la etiqueta pone por 100 g 78 kcal, "
    "18 g proteína, 0,8 g hidratos, 0,5 g grasa. Algo de aceite de oliva en la sartén. "
    "Huevos cocidos, 2, 130g"
)
REFERENCIA_INTERPRETACION = {
    "ingredientes": [
        ingrediente(
            "Sepia",
            "cuttlefish",
            403,
            marca="Hacendado",
            estado="crudo",
            valores_usuario_100g={"kcal": 78, "prot": 18, "carbs": 0.8, "grasas": 0.5},
            supuestos=["El paquete entero: 403 g"],
        ),
        ingrediente(
            "Aceite de oliva",
            "olive oil",
            1,
            "chorrito",
            es_aceite=True,
            supuestos=["algo de aceite = un chorrito"],
        ),
        ingrediente(
            "Huevos cocidos",
            "egg whole hard-boiled",
            130,
            estado="cocido",
            supuestos=["130 g es el total de los 2 huevos"],
        ),
    ]
}
REFERENCIA_ELEGIR = {
    "Aceite de oliva": "Oil, olive, salad or cooking",
    "Huevos cocidos": "Egg, whole, cooked, hard-boiled",
}
