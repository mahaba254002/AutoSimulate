# Security and private data

This is a single-user local application. The server binds to `127.0.0.1`; it is not designed for public hosting or shared accounts. Writes require a process-local token and request validation avoids echoing credentials. No multi-user authentication, role management or encrypted secret vault is implemented.

Keep `.env`, cached BRAIN cookies, logs, exports, database backups and research results private. Session cookies are stored as plaintext local credentials; protect access to the project directory and revoke sessions when needed. Provider keys entered in the browser remain in memory for the server session and are excluded from saved research prompts and database records.

Use a private communication channel for vulnerability details. Do not post credentials, session cookies or private research data in a public issue. If a real credential enters Git history, revoke it before considering history cleanup; adding an ignore rule is not sufficient.

The public-source audit detects selected credential patterns and sensitive paths, including saved notebook output. It cannot recognize every credential or determine data redistribution rights. Use the publication policy and review new additions.
