"""HTTP API for the OAN Messaging Service (MSG-02). See ``docs/openapi.yaml``.

Run (after migrations): ``uvicorn --factory oan_messaging.api:create_app``.
"""

from oan_messaging.api.app import ApiDependencies, create_app

__all__ = ["ApiDependencies", "create_app"]
