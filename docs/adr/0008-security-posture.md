# ADR-0008: Untrusted model text is escaped, the renderer is network-isolated, the API is key-protected, production refuses defaults

* Status: Accepted · Date: 2026-11-05

## Context
0.1 was documented as "bind to localhost, do not expose". Reviewing it for deployment found: no authentication; model and filing text flowing through
Markdown into HTML that was both served (`report.html`) and rendered to PDF by WeasyPrint; risk-table cells that bypassed escaping if they began with `<`;
a PDF renderer that would fetch any URL in that HTML (SSRF); development credentials as defaults; an object-store client that failed slowly.

## Decision
* **Auth:** `x-api-key` / bearer token against `FINROBOT_API_KEYS` (constant-time compare). `/health` and `/ready` stay open for probes.
* **Untrusted text:** model and filing prose is HTML-escaped before Markdown; table cells are always escaped unless explicitly marked `Raw`.
* **Renderer:** WeasyPrint gets a URL fetcher that only allows `data:` URIs. `report.html` is served with `Content-Security-Policy: default-src 'none'; img-src
  data:; style-src 'unsafe-inline'; sandbox` and `nosniff`.
* **Input:** tickers, company queries, question length, peer count and assumption keys are validated before any work; artifact and run ids are matched against
  strict patterns; reading an artifact never creates directories.
* **Production gate:** `FINROBOT_ENV=production` refuses to start with empty/placeholder API keys, development S3 or database credentials, or no SEC user agent.
* **Containers:** non-root user, `no-new-privileges`, all capabilities dropped, read-only root filesystem for the API, secrets only from the environment.
* **Privacy:** tracing is off without keys; `FINROBOT_TRACE_INPUTS=false` redacts content; `.env` is never baked into the image.

## Alternatives considered
| Option | Why not |
|---|---|
| Leave it to a reverse proxy | the proxy cannot fix HTML injection or SSRF in the renderer; defence in depth is cheap here |
| OAuth / per-user accounts | no multi-user data to protect yet; API keys fit service-to-service use. Revisit with multiple tenants |
| Sanitise HTML with an allow-list library | escaping everything is simpler and the report needs no model-authored markup |

## Consequences
* (+) Each control has a test (`tests/test_security.py`, `tests/test_api.py`), including one that proves the PDF render makes no network request.
* (−) API keys are shared secrets, not identities: no per-user audit trail or rate limits (put a gateway in front for those).
* (−) Prompt injection through filing text can still steer a model's *wording*; the verification layer limits the blast radius to unsupported prose being removed,
  not to a fabricated figure being published.
