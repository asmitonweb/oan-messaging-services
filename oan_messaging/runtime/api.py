"""API process entrypoint (MSG-08). Operational routes are separate from the public
messaging OpenAPI contract and are documented in deployment docs only.

    uvicorn --factory oan_messaging.runtime.api:create_app --host 0.0.0.0 --port 8000

``/livez``: the process serves requests (no dependencies checked).
``/readyz``: the database answers ``SELECT 1``; 503 otherwise.
"""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from oan_messaging.api import create_app as message_app
from oan_messaging.redaction import install_redaction
from oan_messaging.storage.config import StorageSettings


def create_app() -> FastAPI:
    # Register this process's own secrets before anything can log them.
    settings = StorageSettings()
    install_redaction(
        settings.database_url.get_secret_value(),
        settings.content_keys.get_secret_value(),
        settings.fingerprint_keys.get_secret_value(),
        settings.credential_pepper.get_secret_value(),
    )
    app = message_app()

    @app.get("/livez", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> PlainTextResponse:
        try:
            async with app.state.deps.repository._sf() as session:
                await session.execute(text("SELECT 1"))
            return PlainTextResponse("ready\n")
        except Exception:
            return PlainTextResponse("unavailable\n", status_code=503)

    return app
