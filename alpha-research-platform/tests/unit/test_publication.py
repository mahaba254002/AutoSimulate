import importlib.util
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch, call
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("audit_publication", ROOT / "scripts/audit_publication.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PublicationTests(unittest.TestCase):
    def test_audit_uses_repository_relative_paths_from_any_working_directory(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'README.md').write_text('Public documentation')
            with patch.object(audit,'git',side_effect=[str(root).encode(),b'README.md\0']) as command,patch('sys.argv',['audit_publication.py']):
                self.assertEqual(audit.main(),0)
                self.assertIn(call('-C',str(root),'ls-files','--full-name','-z'),command.call_args_list)

    def test_audit_reports_sensitive_paths_and_notebook_output_without_secret_values(self):
        secret = "sk-" + "x" * 32
        result = audit.inspect_content("source.py", secret.encode())
        self.assertTrue(result)
        self.assertNotIn(secret, str(result))
        self.assertTrue(audit.inspect_content("alpha-research-platform/data/cache.json", b"{}"))
        self.assertFalse(audit.inspect_content("alpha-research-platform/.env.example", b"OPENAI_API_KEY="))
        self.assertIn("saved notebook output", audit.inspect_content("example.ipynb", b'{"cells":[{"outputs":[{}]}]}'))

    def test_package_includes_operator_metadata_and_core_dependencies_match(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        dependencies = {d.split(">=")[0] for d in project["project"]["dependencies"]}
        requirements = {line.split(">=")[0] for line in (ROOT / "requirements.txt").read_text().splitlines() if line and not line.startswith("#")}
        self.assertEqual(dependencies, requirements)
        self.assertIn("operators_catalog.json", project["tool"]["setuptools"]["package-data"]["alpha_platform.config"])
