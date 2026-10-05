# Architecture

## Implemented execution path

The local browser calls `alpha_platform/api/main.py`. Authentication, catalogue discovery and provider adapters are separate modules. Research plans become persisted campaigns and candidates; the guarded simulation adapter reserves quota before making a remote POST. Results, checks, lineage and human feedback are persisted independently of the browser.

| Layer | Main code | Responsibility |
| --- | --- | --- |
| Browser | `dashboard/` | Research forms, metadata browsing, simulation progress and review |
| Local API | `alpha_platform/api/main.py` | Validated input, local write token and sanitized errors |
| Catalogue | `alpha_platform/research/catalog*.py` | Paced requests, resumable page checkpoints, scoped normalized rows and SQL browsing |
| Planning | `alpha_platform/research/planning.py`, `variants.py`, `redevelopment.py` | Field evidence, supported expression structure and controlled experiment plans |
| Structure | `alpha_platform/structure/`, `generation/gp/trees.py` | Parsing, serialization, AST features and reviewed scalar grammar |
| Execution | `alpha_platform/research/campaigns.py`, `brain_client/ace_lib_adapter.py` | Campaign state, reported progress, saved errors, finite metrics and durable results |
| Safety | `alpha_platform/pipeline/safety.py` | Confirmed payload hashes, transactional quota reservation and conservative recovery |
| Learning | `alpha_platform/generation/bandit/` | Versioned UCB decisions and outcome rewards |
| Storage | `alpha_platform/db/`, `db/migrations/` | PostgreSQL records and additive schema migrations |

## Data boundaries

`catalog_dataset` and `catalog_field` use compound keys that include scope. Each record stores searchable metadata and the original JSON payload. Browsing and pagination run in SQL. Legacy JSON arrays remain as migration compatibility backups. A downloaded selection is published atomically after it completes; the current workflow replaces that scope's saved selection rather than accumulating every download.

Catalogue JSONL checkpoints are authoritative for interrupted downloads. Progress summaries are advisory: a transient file-access failure must not lose a downloaded page or abort the sync. An unchanged failed selection retains its checkpoint identity on retry. HTTP 429 pauses requests and respects cooldown; moving to another scope must not bypass it.

API keys supplied through the UI remain in process memory; configured provider model identifiers may persist in PostgreSQL. BRAIN session cookies are plaintext local credentials under ignored `data/`. Passwords are not persisted by the browser-login workflow. Research source expressions and results belong to the user's private database.

## Failure semantics

A PostgreSQL advisory lock serializes quota reservations. A saved WAITING run exists before a remote submission is attempted. An uncertain outcome retains its reservation and blocks automatic replay. Reported concurrency rejection is distinguished from daily quota exhaustion. Missing metrics and checks remain unverified, rather than silently becoming zero or passing.

Campaign-level stop prevents later dispatch. Per-candidate stop of an accepted simulation stops local monitoring, not remote execution. A process restart interrupts active campaigns, and recovery requires inspection of saved runs.

## Scalability boundary

Indexed catalogue access and reusable AST/planning modules reduce repeated work. This release still uses in-process threads and a single local server. It is not a multi-user, distributed deployment. Modules in `workers/`, older API-router files and some ingestion modules are scaffolding; they are not separate deployed services. Optional Qdrant/embedding code is experimental. Docker infrastructure beyond PostgreSQL is optional and is not required by the dashboard.

For a distributed version, first add durable worker leases, idempotent task dispatch, account-scoped secrets, remote-run reconciliation and authenticated multi-user access. Merely increasing concurrency does not solve remote platform limits.
