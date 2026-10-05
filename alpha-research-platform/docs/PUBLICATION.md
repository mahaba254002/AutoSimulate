# What belongs in the public repository

## Publish

Application source, the browser interface, database migrations, synthetic tests, setup scripts, documentation and `.env.example` with empty provider keys and example database settings. The checked-in operator catalogue is interface metadata, not a dump of downloaded account data. Retained ACE helper files are attributed separately.

## Keep private

| Material | Reason | Local location/pattern |
| --- | --- | --- |
| Environment files | Database passwords and provider keys | `.env`, `.env.*` except `.env.example` |
| Session cookies and credentials | Account access | `data/brain_session_cookies.json`, `secrets/`, `credentials/`, `platform-brain.json` |
| Downloaded catalogue/checkpoint files | Account-specific metadata and research state | `data/`, `exports/`, `private/` |
| Saved alpha expressions, results and feedback | Private research IP and account identifiers | PostgreSQL records and database backups |
| Logs | May contain requests, identifiers or diagnostic details | `*.log` |
| Notebook outputs | May reveal account data, URLs, PNL or run identifiers | Clear outputs before publishing notebooks |
| Screenshots | May expose user emails, quota, results or account metadata | Use synthetic demonstrations only |
| Machine artifacts | Not reproducible source | `.venv/`, caches, build output, node modules, test reports |

The ignore file protects new files; it does not remove files already tracked or erase earlier commits. This publication includes a clean current ACE notebook after saving its original output locally. Previously committed notebook outputs remain in Git history. No history rewrite or account-secret rotation is performed automatically.

Run `python scripts/audit_publication.py` from the project folder to check tracked files and high-confidence credential patterns without printing credential values. Use `--history` to inspect historical versions as well. This check is a safeguard, not a proof that every possible secret or intellectual-property issue has been detected.

Before sharing a manual export, inspect it separately: JSON exports can include dataset descriptions, exact scope, filters and field metadata. A public repository should not receive real research exports by default.
