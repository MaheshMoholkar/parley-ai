"""The FastAPI application.

Run it with:  uvicorn parley.api.app:create_app --factory
"""

from fastapi import FastAPI

from parley.api import voice
from parley.api.routes import router
from parley.bootstrap import build_runtime, configure_logging
from parley.services.runtime import Runtime


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Tests pass their own runtime; production builds one from settings."""
    if runtime is None:
        configure_logging()
        runtime = build_runtime()
    app = FastAPI(title="Parley collections agent", version="0.1.0")
    app.state.runtime = runtime
    app.include_router(router)
    app.include_router(voice.router)
    return app
