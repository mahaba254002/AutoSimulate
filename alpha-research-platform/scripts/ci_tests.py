"""Run offline tests and expose redacted failure annotations in public CI checks."""
import os
import sys
import unittest
from pathlib import Path

from audit_publication import PATTERNS


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    suite = unittest.defaultTestLoader.discover("tests/unit")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for case, trace in result.errors + result.failures:
            message = f"{case.id()}\n{trace}"
            for pattern in PATTERNS.values():
                message = pattern.sub("[redacted credential]", message)
            escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error title=Offline test failure::{escaped}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
