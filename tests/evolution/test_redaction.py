"""Redaction must run before persistence and must not be defeatable by key naming."""
import pytest
from control_plane.telemetry.redaction import (
    REDACTED, Redactor, is_secret_key, redact,
)
from control_plane.telemetry.events import Component, Event, EventType


@pytest.mark.parametrize("key", [
    "api_key", "API-KEY", "apiKey", "authorization", "Authorization",
    "password", "refresh_token", "client_secret", "cookie", "private_key",
])
def test_secret_keys_detected(key):
    assert is_secret_key(key)


@pytest.mark.parametrize("key", [
    "tokens", "total_tokens", "prompt_tokens", "max_tokens", "token_budget",
    "authenticated", "auth_type", "auth_method",
])
def test_operational_keys_are_not_secrets(key):
    # These carry real telemetry; redacting them would blank the Models table.
    assert not is_secret_key(key)


def test_value_shapes_redacted_under_innocent_keys():
    r = Redactor()
    out = r.redact({"note": "use sk-abcdefghijklmnop1234567890 to auth"})
    assert "sk-abcdefghijklmnop" not in out["note"]
    assert REDACTED in out["note"]


@pytest.mark.parametrize("secret", [
    "sk-abcdefghijklmnop1234567890",
    "nvapi-abcdefghijklmnop1234567890",
    "ghp_abcdefghijklmnopqrstuvwxyz012345",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
])
def test_known_credential_shapes(secret):
    assert secret not in redact(f"Authorization: Bearer {secret}")


def test_registered_value_redacted_even_with_unknown_shape():
    r = Redactor()
    r.register_value("totally-opaque-credential-value")
    out = r.redact({"detail": "failed for totally-opaque-credential-value"})
    assert "totally-opaque-credential-value" not in out["detail"]


def test_short_values_not_registered():
    # Registering a 3-char value would redact it out of all prose.
    r = Redactor()
    r.register_value("abc")
    assert r.redact("abc def") == "abc def"


def test_nested_structures():
    r = Redactor()
    out = r.redact({"a": [{"api_key": "x" * 40}, {"ok": 1}], "b": {"c": {"token": "y" * 40}}})
    assert out["a"][0]["api_key"] == REDACTED
    assert out["a"][1]["ok"] == 1
    assert out["b"]["c"]["token"] == REDACTED


def test_numbers_and_none_pass_through():
    assert redact({"n": 5, "f": 1.5, "b": True, "z": None}) == {
        "n": 5, "f": 1.5, "b": True, "z": None}


def test_event_redacted_in_place():
    r = Redactor()
    ev = Event(
        type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
        input={"prompt": "key sk-abcdefghijklmnop1234567890"},
        metadata={"api_key": "secret-value-here-1234"},
        summary="called with sk-abcdefghijklmnop1234567890",
    )
    r.redact_event(ev)
    assert "sk-abcdefghijklmnop" not in ev.input["prompt"]
    assert ev.metadata["api_key"] == REDACTED
    assert "sk-abcdefghijklmnop" not in ev.summary


def test_recursion_is_bounded():
    d = cur = {}
    for _ in range(50):
        cur["next"] = {}
        cur = cur["next"]
    redact(d)  # must not raise RecursionError
