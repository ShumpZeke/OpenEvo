"""App factory so uvicorn can construct the broker with env-driven options."""
from __future__ import annotations
import os
from .app import create_app


def app_factory():
    verify = os.environ.get("OE_MAX_VERIFY_ON_START", "0").lower() in ("1", "true", "yes")
    return create_app(verify_on_start=verify)
