"""
Secret redaction.

Contract: redaction runs BEFORE an event reaches any sink (storage, stream,
log). Nothing downstream is trusted to redact. See SOURCE_OF_TRUTH section 26.

We redact by two independent mechanisms, because either alone leaks:

  1. Key-based  — a dict key that names a credential ("api_key", "authorization")
                  has its value replaced regardless of what the value looks like.
  2. Value-based — a string that matches a known credential shape is redacted
                  even when it appears under an innocent key, or inline in prose
                  (an error message echoing a curl command, say).

Registered live secrets are additionally redacted by exact match, which catches
credentials whose shape we do not recognise.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set

REDACTED = "«redacted»"

# Keys whose value is always a secret. Matched case-insensitively against the
# key with separators stripped, so "api-key", "api_key" and "apiKey" all hit.
SECRET_KEY_PARTS = (
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "auth",
    "credential",
    "privatekey",
    "sessionkey",
    "cookie",
    "bearer",
    "accesskey",
    "refreshtoken",
    "clientsecret",
    "signingkey",
)

# Keys that contain "auth"/"token" but are not themselves secrets. Without this
# allowlist we would redact useful operational fields.
SECRET_KEY_EXCEPTIONS = {
    "authorized",
    "authenticated",
    "authtype",
    "authmethod",
    "authrequired",
    "authstatus",
    "tokencount",
    "tokens",
    "totaltokens",
    "prompttokens",
    "completiontokens",
    "tokenspersec",
    "tokenbudget",
    "maxtokens",
    "tokenusage",
}

# Value shapes for well-known credential formats.
VALUE_PATTERNS: List[re.Pattern] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),            # OpenAI-style
    re.compile(r"nvapi-[A-Za-z0-9_\-]{16,}"),          # NVIDIA NIM
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),         # GitHub
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),      # Slack
    re.compile(r"AKIA[0-9A-Z]{16}"),                   # AWS access key id
    re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),          # Google OAuth
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # JWT: three base64url segments
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
]

_MAX_DEPTH = 12


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_secret_key(key: str, value: Any = None) -> bool:
    """
    Whether a key's value must be redacted.

    `value` is optional and, when given, is used only to *spare* a value that
    could not possibly be a credential — never to redact one that the key alone
    would have allowed. Redaction stays key-driven; this only narrows the false
    positives.

    `None` is spared for the same reason a number is, and it matters more than
    it looks: an absent token count rendered as «redacted» reads as a hidden
    secret rather than as no data, which is exactly the confusion the no-fake-
    data rule exists to prevent.

    The false positive that motivated this: `reasoning_tokens`, a count, was
    redacted because "token" is a secret key part. The measurement it carries —
    Ox Alpha spending 7,986 of an 8,000-token budget on hidden reasoning — is
    the single most important cost signal in this system, and it was arriving
    as «redacted». The explicit exceptions list could not keep up: every new
    count key (`cached_tokens`, `reasoning_tokens`, `cache_write_tokens`) had
    to be remembered, and forgetting one destroyed data silently.
    """
    norm = _normalize_key(key)
    if norm in SECRET_KEY_EXCEPTIONS:
        return False
    if norm.endswith("tokens") and (
            value is None
            or (isinstance(value, (int, float)) and not isinstance(value, bool))):
        # A numeric "…tokens" key is a count. Restricted to that suffix rather
        # than applied to every numeric value, so a numeric `password` or
        # `api_key` is still redacted.
        return False
    return any(part in norm for part in SECRET_KEY_PARTS)


class Redactor:
    """
    Redacts secrets from arbitrary event payloads.

    Live secret values may be registered at runtime (from the provider secret
    broker) so that credentials with no recognisable shape are still removed.
    """

    def __init__(self, extra_values: Optional[Iterable[str]] = None) -> None:
        self._values: Set[str] = set()
        for v in extra_values or ():
            self.register_value(v)

    def register_value(self, value: Optional[str]) -> None:
        """Register a live secret for exact-match redaction."""
        # Very short values would cause absurd false positives across all text.
        if value and len(value) >= 8:
            self._values.add(value)

    def register_env(self, *env_keys: str) -> None:
        import os

        for k in env_keys:
            self.register_value(os.environ.get(k))

    # -- core -----------------------------------------------------------

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        out = text
        # Registered exact values first — longest first so that a token which
        # contains another registered value is fully removed.
        for v in sorted(self._values, key=len, reverse=True):
            if v in out:
                out = out.replace(v, REDACTED)
        for pat in VALUE_PATTERNS:
            out = pat.sub(REDACTED, out)
        return out

    def redact(self, obj: Any, _depth: int = 0) -> Any:
        """Recursively redact a JSON-like structure."""
        if _depth > _MAX_DEPTH:
            return "«max-depth»"

        if isinstance(obj, str):
            return self.redact_text(obj)

        if isinstance(obj, dict):
            out: Dict[Any, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and is_secret_key(k, v):
                    # Preserve presence and type without leaking the value.
                    out[k] = REDACTED
                else:
                    out[k] = self.redact(v, _depth + 1)
            return out

        if isinstance(obj, (list, tuple)):
            seq = [self.redact(v, _depth + 1) for v in obj]
            return type(obj)(seq) if isinstance(obj, tuple) else seq

        # int/float/bool/None pass through untouched.
        return obj

    def redact_event(self, event: "Any") -> "Any":
        """
        Redact an Event in place and return it.

        Only free-form payload fields are traversed; identity fields (ids,
        timestamps, numeric metrics) cannot carry secrets and skipping them
        keeps the hot path cheap.
        """
        event.input = self.redact(event.input)
        event.output = self.redact(event.output)
        event.metadata = self.redact(event.metadata)
        if event.summary:
            event.summary = self.redact_text(event.summary)
        if event.error:
            event.error = self.redact(event.error)
        return event


# Process-wide default redactor.
_default = Redactor()


def default_redactor() -> Redactor:
    return _default


def redact(obj: Any) -> Any:
    return _default.redact(obj)


def redact_text(text: str) -> str:
    return _default.redact_text(text)
