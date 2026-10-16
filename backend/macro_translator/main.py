"""FastAPI app: the JSON API under /api and the static frontend under /."""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .db import DatabaseMissing, FoodDB
from .llm import LLMClient, LLMError
from .models import Peticion, Resultado
from .pipeline import Prompts, calcular

log = logging.getLogger("macro_translator")

MAX_BODY_BYTES = 64 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    db: FoodDB | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if settings.debug:
        logging.getLogger("macro_translator").setLevel(logging.DEBUG)
    else:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    llm = llm or LLMClient(settings)
    db = db or FoodDB(settings.db_path)

    app = FastAPI(title="macro-translator", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Petición demasiado grande."}, status_code=413)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        # Check the ETag on each load: after an update, browsers get the new page and JS.
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "database": db.available()}

    @app.get("/api/info")
    def info() -> dict:
        try:
            datos = db.metadata()
        except (DatabaseMissing, Exception) as exc:  # noqa: BLE001 - the footer must still load
            log.warning("metadata unavailable: %s", exc)
            datos = []
        return {"max_chars": settings.max_input_chars, "datos": datos}

    @app.post("/api/calcular", response_model=Resultado)
    def calcular_endpoint(peticion: Peticion) -> Resultado:
        texto = peticion.texto.strip()
        if not texto:
            raise HTTPException(422, "Escribe qué has comido.")
        if len(texto) > settings.max_input_chars:
            raise HTTPException(422, f"El texto supera {settings.max_input_chars} caracteres.")
        if settings.debug:
            log.debug("request text: %s", texto)
        prompts = Prompts.load(settings.prompts_dir)  # read on each request: edit without restart
        t0 = time.monotonic()
        try:
            resultado = calcular(texto, llm, db, prompts)
        except DatabaseMissing as exc:
            raise HTTPException(503, str(exc)) from exc
        except LLMError as exc:
            log.warning("LLM error: %s", exc)
            raise HTTPException(502, str(exc)) from exc
        log.info("calculated %d rows in %.1f s", len(resultado.filas), time.monotonic() - t0)
        return resultado

    if settings.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
    return app


def app_factory() -> FastAPI:
    """Entry point for uvicorn --factory."""
    return create_app()
