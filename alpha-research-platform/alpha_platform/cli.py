"""Local commands; preview and doctor never authenticate or submit simulations."""
import argparse
from dataclasses import asdict
import json


def main():
    parser = argparse.ArgumentParser(description="Alpha Research: generate, review, confirm")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Open the local research dashboard server")
    serve.add_argument("--port", type=int, default=8765)
    preview = commands.add_parser("preview", help="Generate candidates offline; no DB or BRAIN calls")
    preview.add_argument("--count", type=int, default=20)
    preview.add_argument("--seed", type=int, default=42)
    commands.add_parser("doctor", help="Check database and operator catalog without using BRAIN")
    commands.add_parser("login", help="Connect BRAIN interactively; does not simulate")
    commands.add_parser("history", help="Read saved simulation results")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        print(f"Alpha Research is opening at http://127.0.0.1:{args.port}")
        uvicorn.run("alpha_platform.api.main:app", host="127.0.0.1", port=args.port, workers=1)
    elif args.command == "preview":
        from alpha_platform.generation.gp.population import generate_population
        print(json.dumps([asdict(c) for c in generate_population(count=args.count, seed=args.seed)], indent=2))
    elif args.command == "login":
        from alpha_platform.brain_client.session_cache import get_authenticated_session
        get_authenticated_session()
        print("BRAIN connected. Return to the dashboard. No simulation was submitted.")
    elif args.command == "history":
        from alpha_platform.pipeline.orchestrator import recent_results
        print(json.dumps(recent_results(), indent=2))
    elif args.command == "doctor":
        from alpha_platform.config.operators import validate_catalog
        from alpha_platform.db.session import SessionLocal
        from alpha_platform.pipeline.safety import quota_snapshot
        from sqlalchemy import text
        report = validate_catalog()
        print(f"Catalog: {report.parsed} operators, {len(report.errors)} errors, {len(report.warnings)} warnings")
        for message in report.errors + report.warnings:
            print(message)
        try:
            with SessionLocal() as db:
                version = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
                print(f"Database connected. Schema revision: {version}")
                print(json.dumps(quota_snapshot(db), indent=2))
        except Exception:
            print("Database check failed. Check .env, start Postgres, and run Alembic upgrade head.")
            return 1
        return 0 if report.ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
