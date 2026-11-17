"""Generate docs/api-contract.md from the application's OpenAPI document."""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

from pydantic import SecretStr

from spindle_core.api.cursor import encode_cursor
from spindle_core.api.sagas import SagaDetailResponse, SagaListResponse, StepResponse
from spindle_core.config import Settings
from spindle_core.main import create_app

SKIP_SCHEMAS = {"HTTPValidationError", "ValidationError"}

NOTES = """\
## Behaviour notes

### Authentication
- `POST /auth/token` takes a form body (`username`, `password`) and returns a bearer token. Send it as `Authorization: Bearer <token>`. Send only `username` and `password`; the other form fields are unused.
- Every `/sagas` and `/templates` route requires the token. `/auth/token` and `/health` do not.
- The token lifetime is server-configured (default 60 minutes). On any 401 the GUI should log in again.
- Local development login from `.env.example`: `admin` / `spindle-dev-only` (dev only; `scripts/gen-env.sh` generates real credentials).

### Saga lifecycle
- Saga statuses: `RUNNING`, `COMPENSATING`, `COMPLETED`, `COMPENSATED`, `FAILED`. Terminal: `COMPLETED`, `COMPENSATED`, `FAILED`.
- Step statuses (plain strings): `PENDING`, `DISPATCHED`, `SUCCEEDED`, `FAILED`, `COMPENSATING`, `COMPENSATED`.
- The `steps` array is in execution order, and steps run strictly in template order. When a step fails, completed steps are undone in reverse order. The failed step itself is never undone. A failure on the first step goes straight to `COMPENSATED`.
- `FAILED` means an undo call itself failed and a human must look at it.
- Treat any unknown status string neutrally, so the GUI keeps working if a status is ever added.
- Sagas change state asynchronously after `POST /sagas` returns `RUNNING`. There is no push channel: poll `GET /sagas/{id}` (about every 1 to 2 seconds) while the status is not terminal.

### Listing
- `GET /sagas` returns newest first. `next_cursor` is null when there are no more rows; otherwise pass it back unchanged as `cursor`.
- `limit` is 1 to 200 (default 50). `status` is an exact, uppercase match on one saga status.
- The cursor is opaque. Do not parse or build it. Any malformed cursor gives 422 with `{"detail": "Invalid cursor"}`.
- Paging is stable while new sagas are created: rows are neither repeated nor skipped.
- There is no total count and no free-text search.
- List items have exactly the same shape as `GET /sagas/{id}`.

### Data details
- Timestamps are ISO 8601; the server normally sends UTC with a trailing `Z` (see the examples). Parse them with a date parser that accepts any UTC offset, never compare them as strings, and do not rely on fractional seconds being present. `updated_at` moves on every state change.
- `template_name` and `template_version` are null for sagas created before migration 002.

### Errors
- Every error body is JSON. The OpenAPI lists only 200 and 422 per endpoint, so these also occur:
  - `POST /auth/token`: 401 for wrong credentials.
  - Any `/sagas` or `/templates` route: 401 for a missing, invalid or expired token.
  - `GET /sagas/{saga_id}`: 404 for an unknown saga (`{"detail": "Saga not found"}`); 422 for an id longer than 64 characters.
  - `POST /sagas`: 404 for an unknown template name or version (`{"detail": "Saga template not found"}`); 409 only in the theoretical case of an id collision.
  - 401 and 404 bodies are `{"detail": "<message>"}`; branch on the status code, not on the message text.
- 422: validation failures. `detail` is a string for the cursor error and a list of objects for FastAPI validation errors, so the GUI must handle both. Each list item has `loc`, `msg` and `type`.
- `POST /sagas` accepts only `template` and optional `version`. Unknown fields give 422.

### CORS
- Allowed origins come from the server setting `CORS_ALLOW_ORIGINS` (a JSON list, single-quoted in `.env`). The default allows none.
- For GUI development add the Vite origin: `CORS_ALLOW_ORIGINS='["http://localhost:5173"]'`. Credentials are not allowed, so use the bearer header, not cookies.
- The Electron shell's origin is decided in the GUI task.

### Health
- `GET /health` is unauthenticated. It returns 200 `ok` or 503 `unavailable`, with a per-check map (`postgres`, `redis`, `outbox_relay`, `command_router`; values `ok` or `fail`). The OpenAPI shows its response as `any` because the handler builds the response itself; see the examples. It can take up to about 4 seconds, so do not poll it aggressively.

### Not available (do not build UI for these)
- Saga failure reason text, cancel or retry actions, a workers panel, total counts, search, push notifications.

### Authoring API: Services (/services)
- Services define registered backend services with `name`, `base_url`, `health`, and `auth`.
- Services are mutable: a changed base_url affects in-flight sagas.
- `SERVICE_ALLOWED_HOSTS` is unset by default (permissive); setting it restricts registrable hosts to the list.
- `PUT /services/{name}`: body name must match path name. Returns 201 when created, 200 on update or identical content.
- `DELETE /services/{name}`: delete guard checks the LATEST version of each workflow only. If any workflow references the service in its latest version, returns 409 with `{"detail": "...", "referenced_by": [first 50 workflow names]}`. Otherwise returns 204. Returns 404 if the service does not exist.

### Authoring API: Workflows (/workflows)
- Workflows are versioned saga templates published via `POST /workflows/{name}/versions`.
- Workflow names use ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$, wider than the legacy POST /sagas pattern ^[a-z0-9][a-z0-9_-]*$ (a workflow named "Foo" cannot be referenced by the legacy start path; no change to that path).
- Workflows are stored and validated but not yet executable.
- Publishing with optional `base_version=N` enforces optimistic concurrency: returns 201 if created, 200 if identical content exists, 409 on version conflict. Definition text is never leaked in error messages.
- `GET /workflows` lists the latest version of each workflow (unpaginated).

### Authoring API: Workflow Runs (/workflows/{name}/runs)
- `POST /workflows/{name}/runs`: validates workflow preconditions and input before execution. Workflows are stored and validated but not yet executable (returns 501).
- Status codes:
  - `401`: Missing or invalid bearer token.
  - `404`: Unknown workflow name or version (`{"detail": "Workflow not found"}`).
  - `409`: Workflow references a service that was unregistered since publication (`{"detail": "workflow references unregistered services", "services": [...]}`).
  - `413`: Request body larger than 262144 bytes (`{"detail": "Request entity too large"}`).
  - `422`: Validation error (missing required inputs, serialized input > 65536 bytes, nesting depth > 32, non-object input, extra body fields). Returned in standard 422 shape without echoing input values.
  - `501`: Preconditions and inputs validated successfully; returns `{"detail": "workflow execution is not enabled yet"}`.
- Input caps: serialized JSON input must not exceed 65536 bytes; maximum nesting depth is 32; must be a JSON object.
- Input is write-only: input and definition snapshots are write-only. They are never returned by any GET endpoint, never logged, and never included in error messages.
- `sagas.source` semantics: the `sagas` table includes a `source` column (`'template'` or `'workflow'`). For `'template'`, `template_name`/`template_version` refer to file templates and `definition_snapshot` is NULL. For `'workflow'`, `template_name`/`template_version` hold the workflow name and version, and `definition_snapshot` is non-NULL.

### Authoring API: Validation (/validate)
- `POST /validate/service` and `POST /validate/workflow` perform dry-run validation with no side effects and no DB writes.
- Returns 200 `{"valid": true, "errors": []}` on success, or `200 {"valid": false, "errors": [...]}` on validation failure.
- Malformed envelope (body not an object, invalid JSON) returns 422.

### Authoring API: Error Handling
- The 422 shape on new routes differs from FastAPI's default and never echoes input. Errors are returned as `{"detail": "validation failed", "errors": [{"path": "...", "message": "..."}]}` built solely from `loc` and `msg`.
- Every new authoring route requires bearer token authentication (`Depends(get_current_user)`) and returns 401 when unauthenticated.
- Request bodies with `Content-Length > 262144` bytes are rejected with 413.
"""


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def tname(sc: dict) -> str:
    if not sc:
        return "any"
    if "$ref" in sc:
        n = ref_name(sc["$ref"])
        return f"[{n}](#{n.lower()})"
    if "anyOf" in sc:
        return " or ".join(tname(b) for b in sc["anyOf"])
    if sc.get("type") == "array":
        return "array of " + tname(sc.get("items", {}))
    if "enum" in sc:
        return "enum(" + ", ".join(str(v) for v in sc["enum"]) + ")"
    t = sc.get("type", "object")
    return f"{t} ({sc['format']})" if "format" in sc else t


def core(sc: dict) -> dict:
    if "anyOf" in sc:
        for branch in sc["anyOf"]:
            if branch.get("type") != "null":
                merged = dict(branch)
                merged.update({k: v for k, v in sc.items() if k != "anyOf"})
                return merged
    return sc


def constraints(sc: dict) -> str:
    c = core(sc)
    parts = []
    for key, label in (("minimum", "min"), ("maximum", "max"), ("minLength", "minLength"),
                       ("maxLength", "maxLength"), ("pattern", "pattern"), ("default", "default")):
        if key in c:
            v = c[key]
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            parts.append(f"{label}: `{json.dumps(v)}`")
    if sc.get("description"):
        parts.append(sc["description"])
    return "; ".join(parts)


def schema_lines(name: str, sc: dict) -> list[str]:
    out = [f"### {name}", ""]
    if "enum" in sc:
        out += ["Values: " + ", ".join(f"`{v}`" for v in sc["enum"]), ""]
        return out
    if sc.get("description"):
        out += [esc(sc["description"]), ""]
    if sc.get("additionalProperties") is False:
        out += ["Unknown fields are rejected with 422.", ""]
    req = set(sc.get("required", []))
    out += ["| field | type | required | notes |", "|---|---|---|---|"]
    for key, val in sc.get("properties", {}).items():
        out.append(f"| `{key}` | {esc(tname(val))} | {'yes' if key in req else 'no'} | {esc(constraints(val))} |")
    out.append("")
    return out


def op_lines(path: str, method: str, op: dict) -> list[str]:
    out = [f"## {method.upper()} {path}", ""]
    out += ["Auth: " + ("bearer token required." if op.get("security") else "none."), ""]
    if op.get("description"):
        out += [esc(op["description"]), ""]
    params = op.get("parameters", [])
    if params:
        out += ["| parameter | in | required | type | notes |", "|---|---|---|---|---|"]
        for p in params:
            sc = p.get("schema", {})
            out.append(f"| `{p['name']}` | {p['in']} | {'yes' if p.get('required') else 'no'} | {esc(tname(sc))} | {esc(constraints(sc))} |")
        out.append("")
    rb = op.get("requestBody")
    if rb:
        for ctype, c in rb.get("content", {}).items():
            out += [f"Request body ({ctype}): {tname(c.get('schema', {}))}", ""]
    out += ["Responses:", ""]
    for code, r in op.get("responses", {}).items():
        body = ""
        for c in r.get("content", {}).values():
            body = f" - {tname(c.get('schema', {}))}"
        out.append(f"- `{code}` {r.get('description', '')}{body}")
    out.append("")
    return out


def examples() -> str:
    t1 = datetime(2026, 11, 14, 8, 0, 0, 123456, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 14, 8, 0, 2, 654321, tzinfo=timezone.utc)
    steps = [
        StepResponse(step_id="create-order", step_type="CreateOrder", status="COMPENSATED"),
        StepResponse(step_id="reserve-inventory", step_type="ReserveInventory", status="COMPENSATED"),
        StepResponse(step_id="charge-payment", step_type="ChargePayment", status="FAILED"),
        StepResponse(step_id="ship-order", step_type="ShipOrder", status="PENDING"),
    ]
    detail = SagaDetailResponse(
        saga_id="2275b2ec-fcef-48b9-9ceb-cef96f244c56",
        status="COMPENSATED",
        template_name="order-fulfillment",
        template_version=1,
        created_at=t1,
        updated_at=t2,
        steps=steps,
    )
    page = SagaListResponse(items=[detail], next_cursor=encode_cursor(t1, detail.saga_id))
    checks = ["postgres", "redis", "outbox_relay", "command_router"]
    blocks = [
        ("POST /auth/token, 200", {"access_token": "<jwt>", "token_type": "bearer", "expires_in": 3600}),
        ("GET /sagas/{saga_id}, 200 (a saga whose payment step failed and was rolled back)", json.loads(detail.model_dump_json())),
        ("GET /sagas, 200 (shortened to one item; next_cursor is null on the last page)", json.loads(page.model_dump_json())),
        ("GET /health, 200", {"status": "ok", "checks": {c: "ok" for c in checks}}),
        ("GET /health, 503 (Redis unreachable)", {"status": "unavailable", "checks": {**{c: "ok" for c in checks}, "redis": "fail"}}),
        ("422 from a validation error", {"detail": [{"type": "greater_than_equal", "loc": ["query", "limit"], "msg": "Input should be greater than or equal to 1"}]}),
    ]
    out = [
        "# Examples",
        "",
        "Saga bodies and the cursor are produced by the real response models. The token, health and 422 examples are written by hand (a 422 list item may carry extra keys).",
        "",
    ]
    for title, body in blocks:
        out += [f"### {title}", "", "```json", json.dumps(body, indent=2), "```", ""]
    return "\n".join(out)


def main(out_path: str) -> int:
    settings = Settings(
        DATABASE_URL=SecretStr("postgresql+psycopg2://u:p@h/d"),
        REDIS_URL=SecretStr("redis://h:6379/0"),
        JWT_SECRET=SecretStr("x" * 32),
        ADMIN_USERNAME="a",
        ADMIN_PASSWORD_HASH=SecretStr("$argon2id$fake"),
    )
    spec = create_app(settings).openapi()
    info = spec.get("info", {})
    lines = [
        f"# {info.get('title', 'API')} API contract",
        "",
        "Generated from the OpenAPI document by `scripts/gen_api_contract.py`. Do not edit by hand; regenerate after any API change.",
        "",
        "Base URL for the local compose stack: `http://127.0.0.1:8000` (the `API_PORT` setting). Request and response bodies are JSON, except `POST /auth/token`, which is form-encoded.",
        "",
    ]
    count = 0
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            lines += op_lines(path, method, op)
            count += 1
    lines += ["# Schemas", ""]
    schemas = spec.get("components", {}).get("schemas", {})
    shown = 0
    for name, sc in schemas.items():
        if name not in SKIP_SCHEMAS:
            lines += schema_lines(name, sc)
            shown += 1
    lines += [NOTES, examples()]
    pathlib.Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out_path}: {count} endpoints, {shown} schemas, {len(lines)} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
