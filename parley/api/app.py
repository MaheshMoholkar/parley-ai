"""The FastAPI application.

Run it with:  uvicorn parley.api.app:create_app --factory
"""

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from parley.api import voice
from parley.api.routes import router
from parley.bootstrap import build_runtime, configure_logging, configure_tracing
from parley.config import get_settings
from parley.services.runtime import Runtime


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Tests pass their own runtime; production builds one from settings."""
    tracing = False
    if runtime is None:
        settings = get_settings()
        configure_logging(settings)
        tracing = configure_tracing(settings, process="api")
        runtime = build_runtime(settings)
    app = FastAPI(title="Parley collections agent", version="0.1.0")
    app.state.runtime = runtime
    app.include_router(router)
    app.include_router(voice.router)
    if tracing:
        # One span per request; the work a request does (a sync, a task
        # resolution) nests inside it. Health checks are left out.
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    return app
