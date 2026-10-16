"""Client for any OpenAI-compatible chat completions endpoint.

- No native tool calling: small local models do not need to support it.
- response_format: json_schema, then json_object, then plain text. In "auto"
  mode the client moves to the next format when the server rejects one.
- The client always extracts the JSON from the text and validates it with
  Pydantic. On a validation error it asks again (max LLM_MAX_RETRIES times)
  and gives the error to the model.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings

log = logging.getLogger("macro_translator.llm")

T = TypeVar("T", bound=BaseModel)

FORMATS = ("json_schema", "json_object", "none")


class LLMError(RuntimeError):
    """The LLM endpoint failed, or its output stayed incorrect after the retries."""


def extract_json(text: str) -> dict:
    """Find the first JSON object in a model answer.

    Removes <think> blocks and Markdown code fences, then tries each "{" in order.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text)
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("La respuesta no contiene un objeto JSON.")


def inline_refs(schema: dict) -> dict:
    """Replace $ref with the definitions. Some local servers do not follow $ref."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return walk(copy.deepcopy(defs[name]))
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def require_all(schema: dict) -> dict:
    """Mark every property of every object as required, in the schema sent to the LLM.

    llama.cpp turns optional properties into a grammar that lets the model skip
    them, and small models then skip fields such as "marca". The Pydantic models
    keep their defaults, so a server without JSON mode can still omit fields.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items()}
            if isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"])
                for prop in node["properties"].values():
                    prop.pop("default", None)
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def _validation_text(exc: ValidationError) -> str:
    lines = []
    for e in exc.errors()[:10]:
        where = ".".join(str(p) for p in e["loc"]) or "(raíz)"
        lines.append(f"- {where}: {e['msg']}")
    return "\n".join(lines)


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self._http = httpx.Client(
            base_url=settings.llm_base_url,
            headers=headers,
            timeout=httpx.Timeout(settings.llm_timeout, connect=10),
            transport=transport,
        )
        mode = settings.llm_response_format
        self._formats = list(FORMATS) if mode == "auto" else [mode]

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ #
    def _response_format(self, fmt: str, name: str, schema: dict) -> dict | None:
        if fmt == "json_schema":
            return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}
        if fmt == "json_object":
            return {"type": "json_object"}
        return None

    def _complete(self, messages: list[dict], name: str, schema: dict) -> str:
        while True:
            fmt = self._formats[0]
            body: dict[str, Any] = {
                "model": self.settings.llm_model,
                "messages": messages,
                "temperature": self.settings.llm_temperature,
                "stream": False,
            }
            if self.settings.llm_max_tokens:
                body["max_tokens"] = self.settings.llm_max_tokens
            rf = self._response_format(fmt, name, schema)
            if rf:
                body["response_format"] = rf
            body.update(self.settings.llm_extra_body)
            try:
                resp = self._http.post("/chat/completions", json=body)
            except httpx.TimeoutException as exc:
                raise LLMError("El modelo no respondió a tiempo.") from exc
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"No se puede conectar con el LLM ({exc.__class__.__name__})."
                ) from exc

            if resp.status_code in (400, 422) and rf and len(self._formats) > 1:
                log.warning(
                    "The LLM endpoint rejected response_format=%s (HTTP %s). Use %s from now on.",
                    fmt,
                    resp.status_code,
                    self._formats[1],
                )
                if self._formats[0] == fmt and len(self._formats) > 1:
                    self._formats.pop(0)
                continue
            if resp.status_code >= 400:
                raise LLMError(f"El LLM devolvió HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                msg = resp.json()["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError("Respuesta del LLM con un formato desconocido.") from exc
            content = msg.get("content") or ""
            if not content.strip():
                # Some servers put everything in the reasoning field.
                content = msg.get("reasoning_content") or msg.get("reasoning") or ""
            return content

    def chat_json(
        self,
        system: str,
        user: str,
        model: type[T],
        name: str,
        retry_template: str,
        schema: dict | None = None,
        context: dict | None = None,
    ) -> T:
        schema = schema or require_all(inline_refs(model.model_json_schema()))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error = ""
        for attempt in range(1 + self.settings.llm_max_retries):
            text = self._complete(messages, name, schema)
            if self.settings.debug:
                log.debug("LLM %s attempt %d output:\n%s", name, attempt + 1, text)
            try:
                return model.model_validate(extract_json(text), context=context)
            except ValidationError as exc:
                last_error = _validation_text(exc)
            except ValueError as exc:
                last_error = str(exc)
            log.info("LLM output for %s failed validation (attempt %d)", name, attempt + 1)
            messages = messages + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": retry_template.replace("{error}", last_error)},
            ]
        raise LLMError(
            f"El modelo no devolvió un JSON válido tras {1 + self.settings.llm_max_retries} "
            f"intentos. Último error:\n{last_error}"
        )
