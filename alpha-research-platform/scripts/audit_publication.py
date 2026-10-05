"""Read-only Git publication audit. Findings never include matched secret values."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


PATTERNS = {
    "provider credential": re.compile(r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gsk_[A-Za-z0-9]{24,}|AIza[A-Za-z0-9_-]{30,})"),
    "GitHub credential": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "JWT credential": re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
PRIVATE_PARTS = {"data", "private", "secrets", "credentials", ".venv", "node_modules", ".pytest_cache", "exports", "backups"}


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def inspect_content(name, content):
    findings = []
    path = PurePosixPath(name)
    if PRIVATE_PARTS.intersection(path.parts) or (path.name.startswith(".env") and path.name != ".env.example") or path.suffix in {".log", ".pem", ".key", ".dump", ".parquet"} or path.name in {"brain_session_cookies.json", "platform-brain.json"}:
        findings.append("private file tracked")
    decoded = content.decode("utf-8", errors="replace")
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(decoded):
            findings.append(f"{label} at line {decoded[:match.start()].count(chr(10)) + 1}")
    if path.suffix == ".ipynb":
        try:
            notebook = json.loads(decoded)
            if any(c.get("outputs") for c in notebook.get("cells", [])):
                findings.append("saved notebook output")
        except json.JSONDecodeError:
            findings.append("invalid notebook JSON")
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="Also inspect historical tracked blobs")
    args = parser.parse_args()
    try:
        root = Path(git("rev-parse", "--show-toplevel").decode().strip())
        files = git("-C", str(root), "ls-files", "--full-name", "-z").decode().split("\0")
        problems = []
        for name in filter(None, files):
            for finding in inspect_content(name, (root / name).read_bytes()):
                problems.append((name, finding))
        if args.history:
            # Inspect committed blobs without checking out or modifying any version.
            seen = set()
            for commit in git("rev-list", "--all").decode().splitlines():
                for entry in git("ls-tree", "--full-tree", "-r", "-z", commit).split(b"\0"):
                    if not entry:
                        continue
                    meta, raw_name = entry.split(b"\t", 1)
                    _, kind, oid = meta.split()
                    if kind != b"blob":
                        continue
                    name = raw_name.decode()
                    identity = (name, oid)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    for finding in inspect_content(name, git("cat-file", "blob", oid.decode())):
                        problems.append((f"history:{commit[:8]}:{name}", finding))
        for name, finding in problems:
            print(f"{name}: {finding}")
        print(f"Audited {len(list(filter(None, files)))} tracked files; {len(problems)} finding(s). No matched values printed.")
        return 1 if problems else 0
    except (subprocess.CalledProcessError, OSError):
        print("Audit could not read the Git checkout. Run from an accessible repository directory.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
