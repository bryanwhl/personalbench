import copy
import json
import tempfile
import unittest
from pathlib import Path

from personalbench.privacy import findings, scan_paths, write_private
from personalbench.schema import ValidationError, validate_benchmark, validate_config


class SchemaPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.bench = json.loads(Path("examples/benchmark.json").read_text())
        self.config = {"name": "test", "harness": "codex", "model": "specified-model"}

    def test_malformed_mapping_fields_raise_validation_error(self):
        for field in ("prices",):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_config(dict(self.config, **{field: []}))
        for field in ("scoring",):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_benchmark(dict(self.bench, **{field: []}))
        for field in ("reference", "historical"):
            broken = copy.deepcopy(self.bench)
            broken["tasks"][0][field] = []
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_benchmark(broken)

    def test_input_collisions_and_path_escapes_rejected(self):
        for inputs in (
            {"a": "x", "a/b": "y"},
            {"../escape": "x"},
            {"a//b": "x"},
            {".Git/config": "x"},
        ):
            broken = copy.deepcopy(self.bench)
            broken["tasks"][0]["inputs"] = inputs
            with self.subTest(inputs=inputs), self.assertRaises(ValidationError):
                validate_benchmark(broken)

    def test_artifact_files_private_without_changing_existing_parent_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            parent.chmod(0o755)
            write_private(parent / "artifact.json", '{"private":true}')
            self.assertEqual((parent / "artifact.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
            (parent / "link").symlink_to(parent / "artifact.json")
            with self.assertRaises(ValueError):
                write_private(parent / "link", "overwrite")

    def test_scanner_returns_categories_without_credential_values(self):
        synthetic = "gh" + "p_" + "Q" * 35
        self.assertIn("provider-token", findings(synthetic))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.txt"
            path.write_text(synthetic)
            results = scan_paths(path)
            self.assertTrue(results)
            self.assertNotIn(synthetic, json.dumps(results))
