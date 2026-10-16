"""Pydantic models: what the LLM must return, and what the API returns.

The LLM output models only describe interpretation and choice. They contain no
nutrition values except the label values that the user typed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

Unidad = Literal["g", "kg", "ml", "l", "cucharadita", "cucharada", "chorrito"]
Estado = Literal["crudo", "cocido", "n/a"]
Fuente = Literal["OFF", "USDA", "Usuario"]


# --------------------------------------------------------------------------- #
# LLM call 1: interpretation
# --------------------------------------------------------------------------- #
class ValoresEtiqueta(BaseModel):
    """Values per 100 g that the user copied from a label. Each value is optional."""

    kcal: float | None = Field(default=None, ge=0, le=900)
    prot: float | None = Field(default=None, ge=0, le=100)
    carbs: float | None = Field(default=None, ge=0, le=100)
    grasas: float | None = Field(default=None, ge=0, le=100)

    def completos(self) -> bool:
        return None not in (self.kcal, self.prot, self.carbs, self.grasas)

    def vacios(self) -> bool:
        return all(v is None for v in (self.kcal, self.prot, self.carbs, self.grasas))


class IngredienteLLM(BaseModel):
    """One ingredient as the LLM reads it. The backend converts the quantity to grams."""

    model_config = ConfigDict(extra="ignore")

    nombre_es: str = Field(min_length=1, max_length=120)
    nombre_en: str = Field(min_length=1, max_length=120)
    marca: str | None = Field(default=None, max_length=80)
    cantidad: float = Field(gt=0, le=100_000)
    unidad: Unidad
    veces: int = Field(default=1, ge=1, le=100)
    es_aceite: bool = False
    estado: Estado = "n/a"
    valores_usuario_100g: ValoresEtiqueta | None = None
    supuestos: list[str] = Field(default_factory=list)

    @field_validator("marca", mode="before")
    @classmethod
    def _marca_vacia(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "ninguna", "n/a"}:
            return None
        return v

    @field_validator("valores_usuario_100g", mode="after")
    @classmethod
    def _valores_vacios(cls, v: ValoresEtiqueta | None) -> ValoresEtiqueta | None:
        return None if v is not None and v.vacios() else v


class Interpretacion(BaseModel):
    ingredientes: list[IngredienteLLM] = Field(max_length=30)


# --------------------------------------------------------------------------- #
# Ingredient after the backend converted the quantity (the documented schema)
# --------------------------------------------------------------------------- #
class Ingrediente(BaseModel):
    nombre_es: str
    nombre_en: str
    marca: str | None
    gramos: float
    estado: Estado
    valores_usuario_100g: ValoresEtiqueta | None
    supuestos: list[str]


# --------------------------------------------------------------------------- #
# Search candidates and LLM call 2: selection
# --------------------------------------------------------------------------- #
class Candidato(BaseModel):
    clave: str  # short key for the LLM: O1..O5 (Open Food Facts), U1..U5 (USDA)
    id: int
    fuente: Literal["OFF", "USDA"]
    nombre: str
    marca: str | None
    codigo_barras: str | None
    fdc_id: int | None = None
    kcal: float
    prot: float
    carbs: float
    grasas: float


class Seleccion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ingrediente_idx: int
    candidato_id: str | None
    motivo: str = Field(default="", max_length=400)
    # What the chosen candidate fixes and the user did not say (fat %, variant, cut).
    supuestos: list[str] = Field(default_factory=list, max_length=5)
    # Another candidate that can also be the food, because the text does not say the variant.
    alternativa_id: str | None = None
    alternativa: str | None = Field(default=None, max_length=80)  # "picada magra"

    @field_validator("candidato_id", "alternativa_id", mode="before")
    @classmethod
    def _null(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "ninguno"}:
            return None
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("supuestos", mode="after")
    @classmethod
    def _sin_vacios(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x and x.strip()]

    @field_validator("alternativa", mode="before")
    @classmethod
    def _alternativa_vacia(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "ninguna"}:
            return None
        return v


class Selecciones(BaseModel):
    """Validated against the candidates in the validation context."""

    selecciones: list[Seleccion]

    @model_validator(mode="after")
    def _contra_candidatos(self, info: ValidationInfo) -> Selecciones:
        candidatos: dict[int, list[Candidato]] | None = (info.context or {}).get("candidatos")
        if candidatos is None:
            return self
        vistos: set[int] = set()
        for s in self.selecciones:
            if s.ingrediente_idx not in candidatos:
                raise ValueError(
                    f"ingrediente_idx {s.ingrediente_idx} no existe. "
                    f"Valores permitidos: {sorted(candidatos)}"
                )
            if s.ingrediente_idx in vistos:
                raise ValueError(f"ingrediente_idx {s.ingrediente_idx} aparece dos veces")
            vistos.add(s.ingrediente_idx)
            claves = {c.clave for c in candidatos[s.ingrediente_idx]}
            if s.candidato_id is not None and s.candidato_id not in claves:
                raise ValueError(
                    f"candidato_id {s.candidato_id!r} no es un candidato del ingrediente "
                    f"{s.ingrediente_idx}. Valores permitidos: {sorted(claves)} o null"
                )
            # A wrong alternative is not worth a retry: drop it.
            if (
                s.alternativa_id not in claves
                or s.alternativa_id == s.candidato_id
                or s.candidato_id is None
            ):
                s.alternativa_id = None
                s.alternativa = None
        faltan = sorted(set(candidatos) - vistos)
        if faltan:
            raise ValueError(f"faltan selecciones para ingrediente_idx {faltan}")
        return self


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
class Peticion(BaseModel):
    texto: str = Field(min_length=1)


class Macros(BaseModel):
    kcal: float
    prot: float
    carbs: float
    grasas: float


class Origen(BaseModel):
    tipo: Fuente
    nombre: str | None = None
    marca: str | None = None
    codigo_barras: str | None = None
    url: str | None = None
    complemento: Literal["OFF", "USDA"] | None = None  # DB source for label values the user omitted


class Fila(BaseModel):
    ingrediente: str
    gramos: float
    encontrado: bool
    valores: Macros | None  # exact values, not rounded
    redondeado: Macros | None  # 1 decimal, for display only
    origen: Origen | None
    motivo: str | None = None
    aviso: str | None = None
    # The kcal change if the food is another plausible variant ("si es picada magra...").
    sensibilidad: str | None = None
    alternativa: Origen | None = None  # the variant behind the sensitivity warning
    supuestos: list[str] = Field(default_factory=list)


class Resultado(BaseModel):
    filas: list[Fila]
    total: Macros  # sum of the exact row values
    total_redondeado: Macros
    avisos: list[str] = Field(default_factory=list)
