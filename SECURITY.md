# Security

## Secrets

Credentials are referenced by **environment-variable name**, never by value.
`ModelProfile.secret_ref` holds `"OPENCODE_API_KEY"`; the value is read at call
time and never stored in the database, written to a config file, or returned by
any API endpoint. `/api/providers` reports `secret_present: true|false` only.

## Redaction runs before persistence

Redaction happens on the emitting side, before an event reaches any sink —
storage, stream or log. Nothing downstream is trusted to redact.

That ordering is the whole point: a credential that leaked into a prompt or an
error message is removed from the **stored record**, not merely hidden in the
UI. Hiding at render time would leave the secret on disk.

Three independent mechanisms, because any one alone leaks:

1. **Key-based.** A key naming a credential (`api_key`, `api-key`, `apiKey`,
   `authorization`, `password`, `refresh_token`, `client_secret`, `cookie`,
   `private_key`, …) has its value replaced regardless of shape. Matching
   normalises separators and case.

2. **Value-based.** A string matching a known credential shape is redacted even
   under an innocent key or inline in prose — `sk-…`, `nvapi-…`, `ghp_…`,
   `xox[baprs]-…`, `AKIA…`, `ya29.…`, `Bearer …`, JWTs, PEM private-key blocks.

3. **Registered values.** Live credentials registered at startup are removed by
   exact match, catching secrets with no recognisable shape. Values shorter than
   8 characters are refused, since redacting a 3-character string would blank
   unrelated prose.

### Deliberate non-redactions

An allowlist prevents over-redaction of operational fields that merely contain a
trigger substring: `tokens`, `total_tokens`, `prompt_tokens`, `max_tokens`,
`token_budget`, `authenticated`, `auth_type`, `auth_method`. Blanking these
would empty the Models table while protecting nothing.

Recursion is depth-bounded, so a deeply nested payload cannot cause a stack
overflow during redaction.

Twelve tests in `tests/evolution/test_redaction.py` cover this, including that
redaction has already happened by the time an event reaches a file sink.

## Process boundaries

- Engine runs as a child process in its own process group (POSIX), so force-stop
  reaps the worker pool rather than orphaning it.
- Only the API process writes to the database.
- Checkpoint deletion refuses any path outside the run's own output directory,
  even if a crafted iteration value is supplied.
- Sandbox writes are refused under operator-owned roots — see
  [SANDBOX.md](SANDBOX.md).

## Network

The control plane binds `127.0.0.1` by default. CORS allows only localhost dev
origins. There is no authentication layer: **this is a single-operator local
tool and should not be exposed to a network** without putting a reverse proxy
and authentication in front of it. Binding to `0.0.0.0` via `EVOLUTION_HOST`
would expose run control — start/stop, checkpoint deletion, arbitrary program
and evaluator paths — to anyone who can reach the port.

## What an evolved candidate can do

Under the current implementation, candidates are evaluated by upstream's native
evaluator, which executes candidate code in the engine's own process tree with
the engine's privileges. **That is upstream's model, and Evolution does not
currently narrow it.** The sandbox backends that would bound it are designed and
their isolation boundary is enforced, but the executors are not implemented
([SANDBOX.md](SANDBOX.md)).

Treat evaluator code and evolved programs as you would any code you run locally:
run experiments you understand, in a workspace you are willing to lose.

## Reporting

This is a fork of an upstream project. Issues in `openevolve/` belong upstream
(the directory is byte-identical). Issues in `control_plane/` or `web/` belong
here.
