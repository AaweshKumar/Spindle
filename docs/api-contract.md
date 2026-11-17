# Spindle API contract

Generated from the OpenAPI document by `scripts/gen_api_contract.py`. Do not edit by hand; regenerate after any API change.

Base URL for the local compose stack: `http://127.0.0.1:8000` (the `API_PORT` setting). Request and response bodies are JSON, except `POST /auth/token`, which is form-encoded.

## POST /auth/token

Auth: none.

Issue a JWT access token for valid admin credentials.  Uses ``OAuth2PasswordRequestForm`` (form body, not JSON) as required by the OAuth2 password-grant spec.  The same 401 is returned for both wrong username and wrong password so the response leaks nothing about which field was incorrect.  Passwords and tokens are never logged here.

Request body (application/x-www-form-urlencoded): [Body_login_auth_token_post](#body_login_auth_token_post)

Responses:

- `200` Successful Response - [TokenResponse](#tokenresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## POST /sagas

Auth: bearer token required.

Start a new saga from a named template.

Request body (application/json): [StartSagaRequest](#startsagarequest)

Responses:

- `201` Successful Response - [SagaResponse](#sagaresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /sagas

Auth: bearer token required.

List sagas newest first with optional status filtering and keyset pagination.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `status` | query | no | [SagaStatus](#sagastatus) or null |  |
| `limit` | query | no | integer | min: `1`; max: `200`; default: `50` |
| `cursor` | query | no | string or null | maxLength: `512` |

Responses:

- `200` Successful Response - [SagaListResponse](#sagalistresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /sagas/{saga_id}

Auth: bearer token required.

Return the current status and step details of a saga.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `saga_id` | path | yes | string | maxLength: `64` |

Responses:

- `200` Successful Response - [SagaDetailResponse](#sagadetailresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /templates

Auth: bearer token required.

Return all registered saga templates ordered by name and version.

Responses:

- `200` Successful Response - array of [TemplateResponse](#templateresponse)

## PUT /services/{name}

Auth: bearer token required.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `name` | path | yes | string | maxLength: `64`; pattern: `"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"` |

Request body (application/json): object

Responses:

- `200` Successful Response - [ServiceResponse](#serviceresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /services/{name}

Auth: bearer token required.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `name` | path | yes | string | maxLength: `64`; pattern: `"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"` |

Responses:

- `200` Successful Response - [ServiceResponse](#serviceresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## DELETE /services/{name}

Auth: bearer token required.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `name` | path | yes | string | maxLength: `64`; pattern: `"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"` |

Responses:

- `204` Successful Response
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /services

Auth: bearer token required.

Responses:

- `200` Successful Response - array of [ServiceResponse](#serviceresponse)

## POST /workflows/{name}/versions

Auth: bearer token required.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `name` | path | yes | string | maxLength: `64`; pattern: `"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"` |
| `base_version` | query | no | integer or null | min: `0` |

Request body (application/json): object

Responses:

- `200` Successful Response - [WorkflowResponse](#workflowresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /workflows/{name}/versions

Auth: bearer token required.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `name` | path | yes | string | maxLength: `64`; pattern: `"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"` |

Responses:

- `200` Successful Response - array of [WorkflowResponse](#workflowresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /workflows

Auth: bearer token required.

Responses:

- `200` Successful Response - array of [WorkflowResponse](#workflowresponse)

## GET /workflows/{name}

Auth: bearer token required.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `name` | path | yes | string | maxLength: `64`; pattern: `"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"` |

Responses:

- `200` Successful Response - [WorkflowResponse](#workflowresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /workflows/{name}/versions/{version}

Auth: bearer token required.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `name` | path | yes | string | maxLength: `64`; pattern: `"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"` |
| `version` | path | yes | integer | min: `1` |

Responses:

- `200` Successful Response - [WorkflowResponse](#workflowresponse)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## POST /workflows/{name}/runs

Auth: bearer token required.

Validate workflow run inputs and preconditions, returning 501 until execution is enabled.

| parameter | in | required | type | notes |
|---|---|---|---|---|
| `name` | path | yes | string | maxLength: `64`; pattern: `"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"` |

Request body (application/json): object

Responses:

- `501` Successful Response - any
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## POST /validate/service

Auth: bearer token required.

Request body (application/json): object

Responses:

- `200` Successful Response - [ValidationResult](#validationresult)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## POST /validate/workflow

Auth: bearer token required.

Request body (application/json): object

Responses:

- `200` Successful Response - [ValidationResult](#validationresult)
- `422` Validation Error - [HTTPValidationError](#httpvalidationerror)

## GET /health

Auth: none.

Readiness probe: checks postgres, redis, outbox_relay, and command_router.

Responses:

- `200` Successful Response - any

# Schemas

### Body_login_auth_token_post

| field | type | required | notes |
|---|---|---|---|
| `grant_type` | string or null | no | pattern: `"^password$"` |
| `username` | string | yes |  |
| `password` | string (password) | yes |  |
| `scope` | string | no | default: `""` |
| `client_id` | string or null | no |  |
| `client_secret` | string or null | no |  |

### SagaDetailResponse

Detailed response for saga views including metadata and step states.

| field | type | required | notes |
|---|---|---|---|
| `saga_id` | string | yes |  |
| `status` | string | yes |  |
| `template_name` | string or null | yes |  |
| `template_version` | integer or null | yes |  |
| `created_at` | string (date-time) | yes |  |
| `updated_at` | string (date-time) | yes |  |
| `steps` | array of [StepResponse](#stepresponse) | yes |  |

### SagaListResponse

Paginated list of sagas with opaque continuation cursor.

| field | type | required | notes |
|---|---|---|---|
| `items` | array of [SagaDetailResponse](#sagadetailresponse) | yes |  |
| `next_cursor` | string or null | yes |  |

### SagaResponse

Response for POST /sagas.

| field | type | required | notes |
|---|---|---|---|
| `saga_id` | string | yes |  |
| `status` | string | yes |  |

### SagaStatus

Values: `RUNNING`, `COMPENSATING`, `COMPLETED`, `COMPENSATED`, `FAILED`

### ServiceResponse

| field | type | required | notes |
|---|---|---|---|
| `name` | string | yes |  |
| `definition` | object | yes |  |
| `revision` | integer | yes |  |
| `created_at` | string (date-time) | yes |  |
| `updated_at` | string (date-time) | yes |  |

### StartSagaRequest

Body for POST /sagas.  ``extra="forbid"`` ensures that passing an inline ``steps`` key (deferred feature) results in a 422 rather than silently ignoring it.

Unknown fields are rejected with 422.

| field | type | required | notes |
|---|---|---|---|
| `template` | string | yes | maxLength: `64`; pattern: `"^[a-z0-9][a-z0-9_-]*$"`; Name of the saga template to use. |
| `version` | integer or null | no | min: `1`; Template version. Omit to use the latest version. |

### StepResponse

Step execution status within a saga.

| field | type | required | notes |
|---|---|---|---|
| `step_id` | string | yes |  |
| `step_type` | string | yes |  |
| `status` | string | yes |  |

### TemplateResponse

Template summary with ordered step definitions.

| field | type | required | notes |
|---|---|---|---|
| `name` | string | yes |  |
| `version` | integer | yes |  |
| `steps` | array of [TemplateStepResponse](#templatestepresponse) | yes |  |

### TemplateStepResponse

Step reference within a template response.

| field | type | required | notes |
|---|---|---|---|
| `id` | string | yes |  |
| `type` | string | yes |  |

### TokenResponse

JSON body returned by POST /auth/token.  ``expires_in`` is seconds, matching RFC 6749 §5.1 so clients can derive the absolute expiry without knowing the server clock.

| field | type | required | notes |
|---|---|---|---|
| `access_token` | string | yes |  |
| `token_type` | string | no | default: `"bearer"` |
| `expires_in` | integer | yes |  |

### ValidationResult

| field | type | required | notes |
|---|---|---|---|
| `valid` | boolean | yes |  |
| `errors` | array of object | yes |  |

### WorkflowResponse

| field | type | required | notes |
|---|---|---|---|
| `name` | string | yes |  |
| `version` | integer | yes |  |
| `definition` | object | yes |  |
| `content_hash` | string | yes |  |
| `format_version` | integer | yes |  |
| `created_at` | string (date-time) | yes |  |
| `created_by` | string or null | yes |  |

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

# Examples

Saga bodies and the cursor are produced by the real response models. The token, health and 422 examples are written by hand (a 422 list item may carry extra keys).

### POST /auth/token, 200

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "expires_in": 3600
}
```

### GET /sagas/{saga_id}, 200 (a saga whose payment step failed and was rolled back)

```json
{
  "saga_id": "2275b2ec-fcef-48b9-9ceb-cef96f244c56",
  "status": "COMPENSATED",
  "template_name": "order-fulfillment",
  "template_version": 1,
  "created_at": "2026-11-14T08:00:00.123456Z",
  "updated_at": "2026-11-14T08:00:02.654321Z",
  "steps": [
    {
      "step_id": "create-order",
      "step_type": "CreateOrder",
      "status": "COMPENSATED"
    },
    {
      "step_id": "reserve-inventory",
      "step_type": "ReserveInventory",
      "status": "COMPENSATED"
    },
    {
      "step_id": "charge-payment",
      "step_type": "ChargePayment",
      "status": "FAILED"
    },
    {
      "step_id": "ship-order",
      "step_type": "ShipOrder",
      "status": "PENDING"
    }
  ]
}
```

### GET /sagas, 200 (shortened to one item; next_cursor is null on the last page)

```json
{
  "items": [
    {
      "saga_id": "2275b2ec-fcef-48b9-9ceb-cef96f244c56",
      "status": "COMPENSATED",
      "template_name": "order-fulfillment",
      "template_version": 1,
      "created_at": "2026-11-14T08:00:00.123456Z",
      "updated_at": "2026-11-14T08:00:02.654321Z",
      "steps": [
        {
          "step_id": "create-order",
          "step_type": "CreateOrder",
          "status": "COMPENSATED"
        },
        {
          "step_id": "reserve-inventory",
          "step_type": "ReserveInventory",
          "status": "COMPENSATED"
        },
        {
          "step_id": "charge-payment",
          "step_type": "ChargePayment",
          "status": "FAILED"
        },
        {
          "step_id": "ship-order",
          "step_type": "ShipOrder",
          "status": "PENDING"
        }
      ]
    }
  ],
  "next_cursor": "WyIyMDI2LTExLTE0VDA4OjAwOjAwLjEyMzQ1NiswMDowMCIsIjIyNzViMmVjLWZjZWYtNDhiOS05Y2ViLWNlZjk2ZjI0NGM1NiJd"
}
```

### GET /health, 200

```json
{
  "status": "ok",
  "checks": {
    "postgres": "ok",
    "redis": "ok",
    "outbox_relay": "ok",
    "command_router": "ok"
  }
}
```

### GET /health, 503 (Redis unreachable)

```json
{
  "status": "unavailable",
  "checks": {
    "postgres": "ok",
    "redis": "fail",
    "outbox_relay": "ok",
    "command_router": "ok"
  }
}
```

### 422 from a validation error

```json
{
  "detail": [
    {
      "type": "greater_than_equal",
      "loc": [
        "query",
        "limit"
      ],
      "msg": "Input should be greater than or equal to 1"
    }
  ]
}
```

