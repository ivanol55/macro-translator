"""LLM client: JSON extraction, validation retries, response_format fallback, API key."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from macro_translator.llm import LLMClient, LLMError, extract_json, inline_refs
from macro_translator.models import Interpretacion

RETRY = "Error:\n{error}\nCorrige."
VALID = {
    "ingredientes": [{"nombre_es": "Huevo", "nombre_en": "egg", "cantidad": 50, "unidad": "g"}]
}


def answer(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Aquí tienes:\n```json\n{"a": 1}\n```\nListo.') == {"a": 1}
    assert extract_json('<think>{"no": 0} hmm</think>{"a": 1}') == {"a": 1}
    assert extract_json('texto {roto} y luego {"a": {"b": 2}}') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("sin json")


def test_inline_refs_removes_defs():
    schema = inline_refs(Interpretacion.model_json_schema())
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)


def test_retry_with_the_validation_error(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return answer('{"ingredientes": [{"nombre_es": "Huevo"}]}')  # fields missing
        return answer(json.dumps(VALID))

    llm = LLMClient(settings, transport=httpx.MockTransport(handler))
    res = llm.chat_json("sys", "user", Interpretacion, "interpretacion", RETRY)
    assert res.ingredientes[0].cantidad == 50
    assert len(calls) == 2
    retry_msg = calls[1]["messages"][-1]["content"]
    assert "cantidad" in retry_msg and "Field required" in retry_msg


def test_error_after_the_retries(settings):
    llm = LLMClient(settings, transport=httpx.MockTransport(lambda r: answer("no json")))
    with pytest.raises(LLMError):
        llm.chat_json("sys", "user", Interpretacion, "interpretacion", RETRY)


def test_max_retries_is_respected(settings):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return answer("{}")

    llm = LLMClient(replace(settings, llm_max_retries=1), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        llm.chat_json("sys", "user", Interpretacion, "interpretacion", RETRY)
    assert count == 2


def test_response_format_fallback(settings):
    formats = []

    def handler(request: httpx.Request) -> httpx.Response:
        rf = json.loads(request.content).get("response_format")
        formats.append(rf["type"] if rf else None)
        if rf and rf["type"] == "json_schema":
            return httpx.Response(400, json={"error": "response_format not supported"})
        return answer("```json\n" + json.dumps(VALID) + "\n```")

    llm = LLMClient(settings, transport=httpx.MockTransport(handler))
    llm.chat_json("sys", "user", Interpretacion, "interpretacion", RETRY)
    llm.chat_json("sys", "user", Interpretacion, "interpretacion", RETRY)
    assert formats == ["json_schema", "json_object", "json_object"]


def test_fixed_response_format_none(settings):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return answer(json.dumps(VALID))

    llm = LLMClient(
        replace(settings, llm_response_format="none"), transport=httpx.MockTransport(handler)
    )
    llm.chat_json("sys", "user", Interpretacion, "interpretacion", RETRY)
    assert "response_format" not in seen[0]


def test_api_key_only_in_the_backend_request(settings):
    headers = []

    def handler(request):
        headers.append(request.headers.get("authorization"))
        return answer(json.dumps(VALID))

    LLMClient(settings, transport=httpx.MockTransport(handler)).chat_json(
        "s", "u", Interpretacion, "interpretacion", RETRY
    )
    LLMClient(
        replace(settings, llm_api_key="sk-test"), transport=httpx.MockTransport(handler)
    ).chat_json("s", "u", Interpretacion, "interpretacion", RETRY)
    assert headers == [None, "Bearer sk-test"]


def test_reasoning_field_fallback(settings):
    def handler(request):
        msg = {"content": "", "reasoning_content": json.dumps(VALID)}
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    llm = LLMClient(settings, transport=httpx.MockTransport(handler))
    assert llm.chat_json("s", "u", Interpretacion, "interpretacion", RETRY).ingredientes


def test_connection_error(settings):
    def handler(request):
        raise httpx.ConnectError("refused")

    llm = LLMClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="No se puede conectar"):
        llm.chat_json("s", "u", Interpretacion, "interpretacion", RETRY)
