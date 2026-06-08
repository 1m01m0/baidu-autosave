import json
import subprocess
import unittest
from pathlib import Path
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_license_file_exists_for_readme_link(self):
        license_path = REPO_ROOT / "LICENSE"
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertTrue(license_path.exists())
        self.assertIn("MIT License", license_path.read_text(encoding="utf-8"))
        self.assertIn("[LICENSE](LICENSE)", readme)

    def test_project_does_not_ship_pytest_configuration(self):
        self.assertFalse((REPO_ROOT / "pytest.ini").exists())
        pyproject_path = REPO_ROOT / "pyproject.toml"
        if pyproject_path.exists():
            self.assertNotIn(
                "[tool.pytest.ini_options]",
                pyproject_path.read_text(encoding="utf-8"),
            )

    def test_kiro_spec_configs_are_tracked_and_have_unique_ids(self):
        spec_dirs = [
            path
            for path in (REPO_ROOT / ".kiro" / "specs").iterdir()
            if path.is_dir()
            and any((path / name).exists() for name in ("requirements.md", "design.md", "tasks.md"))
        ]
        tracked_files = set(
            subprocess.check_output(
                ["git", "-C", str(REPO_ROOT), "ls-files"], text=True
            ).splitlines()
        )
        seen_ids = {}

        for spec_dir in spec_dirs:
            config_path = spec_dir / ".config.kiro"
            relative_path = config_path.relative_to(REPO_ROOT).as_posix()
            self.assertTrue(config_path.exists(), f"missing config for {spec_dir.name}")
            self.assertIn(relative_path, tracked_files)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            spec_id = payload.get("specId")
            UUID(spec_id)
            self.assertNotIn(spec_id, seen_ids, f"duplicate specId with {seen_ids.get(spec_id)}")
            seen_ids[spec_id] = spec_dir.name


if __name__ == "__main__":
    unittest.main()
