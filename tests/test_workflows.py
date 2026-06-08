import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def load_workflow(relative_path):
    content = read_repo_file(relative_path)
    return content, yaml.safe_load(content)


def workflow_steps(workflow, job_name):
    return workflow["jobs"][job_name]["steps"]


def steps_using(steps, action):
    return [step for step in steps if step.get("uses") == action]


def requirement_lines(relative_path):
    lines = []
    for raw_line in read_repo_file(relative_path).splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and not line.startswith("-"):
            lines.append(line)
    return lines


def requirement_name(line):
    return line.split("==", 1)[0].lower().replace("_", "-")


class WorkflowStaticTests(unittest.TestCase):
    def test_baidu_transfer_workflow_limits_permissions_and_uses_off_hour_cron(self):
        content, workflow = load_workflow(".github/workflows/baidu-transfer.yml")

        self.assertEqual({"contents": "read"}, workflow["permissions"])
        self.assertIn("- cron: '17 */6 * * *'", content)
        self.assertNotIn("- cron: '0 */6 * * *'", content)

    def test_baidu_transfer_workflow_has_safe_concurrency(self):
        _, workflow = load_workflow(".github/workflows/baidu-transfer.yml")

        self.assertEqual("baidu-transfer-${{ github.ref }}", workflow["concurrency"]["group"])
        self.assertIs(workflow["concurrency"]["cancel-in-progress"], False)

    def test_baidu_transfer_workflow_keeps_secrets_out_of_job_env(self):
        content, workflow = load_workflow(".github/workflows/baidu-transfer.yml")
        job_env = workflow["jobs"]["transfer"].get("env", {})

        self.assertEqual({"GITHUB_ACTIONS": "true"}, job_env)
        self.assertNotIn("secrets.", str(job_env))
        for name in ("BAIDU_COOKIES", "SHARE_URLS", "SAVE_DIR", "WECHAT_WEBHOOK"):
            self.assertIn(f"        {name}: ${{{{ secrets.{name} }}}}", content)

    def test_baidu_transfer_workflow_suppresses_first_attempt_result_notification(self):
        _, workflow = load_workflow(".github/workflows/baidu-transfer.yml")
        steps = workflow_steps(workflow, "transfer")
        first_attempt = next(step for step in steps if step.get("id") == "first_attempt")
        second_attempt = next(
            step for step in steps if step.get("name") == "Run transfer task (Second attempt)"
        )

        self.assertEqual(
            "1",
            first_attempt["env"].get("TRANSFERSHARE_SUPPRESS_RESULT_NOTIFICATION"),
        )
        self.assertNotIn(
            "TRANSFERSHARE_SUPPRESS_RESULT_NOTIFICATION",
            second_attempt.get("env", {}),
        )

    def test_baidu_transfer_workflow_passes_failed_state_enabled_flag(self):
        _, workflow = load_workflow(".github/workflows/baidu-transfer.yml")
        steps = workflow_steps(workflow, "transfer")
        transfer_steps = [
            step for step in steps if step.get("name", "").startswith("Run transfer task")
        ]

        self.assertEqual(2, len(transfer_steps))
        for step in transfer_steps:
            self.assertEqual(
                "${{ steps.failed_state.outputs.enabled }}",
                step["env"].get("TRANSFERSHARE_FAILED_STATE_ENABLED"),
            )
            self.assertNotIn("TRANSFERSHARE_STATE_KEY", step["env"])

    def test_baidu_transfer_workflow_caches_only_encrypted_failed_state(self):
        content, _ = load_workflow(".github/workflows/baidu-transfer.yml")

        self.assertIn("actions/cache/restore@v4", content)
        self.assertIn("actions/cache/save@v4", content)
        self.assertIn("TRANSFERSHARE_STATE_KEY", content)
        self.assertIn("path: .transfershare_failed_transfers.json.enc", content)
        self.assertIn("openssl enc -aes-256-cbc", content)
        self.assertIn("printf '[]\\n'", content)
        self.assertIn('rm -f "$state_file"', content)
        self.assertNotIn("upload-artifact", content)
        self.assertNotIn("GITHUB_ENV", content)
        self.assertNotIn("path: .transfershare_failed_transfers.json\n", content)

    def test_baidu_transfer_workflow_skips_tests_and_redundant_build(self):
        content, _ = load_workflow(".github/workflows/baidu-transfer.yml")

        self.assertNotIn("python -m unittest", content)
        self.assertNotIn("Build BaiduPCS-Py submodule", content)
        self.assertNotIn("--show-network-info", content)

    def test_test_workflow_limits_permissions_buffers_output_and_lints_actions(self):
        content, workflow = load_workflow(".github/workflows/test-on-push.yml")

        self.assertEqual({"contents": "read"}, workflow["permissions"])
        self.assertIn('python -m unittest discover -s tests -p "test_*.py" -b', content)
        self.assertIn('python -c "from baidupcs_py.baidupcs import BaiduPCSApi"', content)
        self.assertIn("bash scripts/install_dependencies.sh test", content)
        self.assertNotIn("print(BaiduPCSApi)", content)
        actionlint_steps = steps_using(
            workflow_steps(workflow, "actionlint"), "rhysd/actionlint@v1"
        )
        self.assertEqual(1, len(actionlint_steps))

    def test_test_workflow_has_quality_security_job(self):
        content, workflow = load_workflow(".github/workflows/test-on-push.yml")
        quality_job = workflow["jobs"]["quality-security"]

        checkout_steps = steps_using(quality_job["steps"], "actions/checkout@v4")
        self.assertEqual(1, len(checkout_steps))
        self.assertIs(checkout_steps[0]["with"]["persist-credentials"], False)
        setup_steps = steps_using(quality_job["steps"], "actions/setup-python@v5")
        self.assertEqual(1, len(setup_steps))
        cache_dependency_path = str(setup_steps[0]["with"]["cache-dependency-path"])
        self.assertIn("requirements-quality.txt", cache_dependency_path)
        self.assertIn("constraints.txt", cache_dependency_path)
        self.assertIn("bash scripts/install_dependencies.sh quality", content)
        self.assertIn("python -m compileall -q -x 'vendor/' .", content)
        self.assertIn("python -m ruff check .", content)
        self.assertIn("python -m ruff format --check .", content)
        self.assertNotIn("--select E9,F63,F7,F82", content)
        self.assertIn("python -m pip check", content)
        self.assertIn(
            "python -m pip_audit -r requirements.txt -r requirements-test.txt -r requirements-quality.txt",
            content,
        )

    def test_test_workflow_has_dependency_lock_python_matrix(self):
        _, workflow = load_workflow(".github/workflows/test-on-push.yml")
        lock_job = workflow["jobs"]["dependency-lock"]

        self.assertIs(lock_job["strategy"]["fail-fast"], False)
        self.assertEqual(
            ["3.9", "3.12"],
            lock_job["strategy"]["matrix"]["python-version"],
        )
        checkout_steps = steps_using(lock_job["steps"], "actions/checkout@v4")
        self.assertEqual(1, len(checkout_steps))
        self.assertIs(checkout_steps[0]["with"]["persist-credentials"], False)
        setup_steps = steps_using(lock_job["steps"], "actions/setup-python@v5")
        self.assertEqual(1, len(setup_steps))
        self.assertEqual("${{ matrix.python-version }}", setup_steps[0]["with"]["python-version"])
        cache_dependency_path = str(setup_steps[0]["with"]["cache-dependency-path"])
        self.assertIn("requirements-quality.txt", cache_dependency_path)
        self.assertIn("constraints.txt", cache_dependency_path)
        runs = "\n".join(step.get("run", "") for step in lock_job["steps"])
        self.assertIn("bash scripts/install_dependencies.sh quality", runs)
        self.assertIn("python scripts/check_dependency_lock.py", runs)

    def test_workflows_use_shared_dependency_install_script(self):
        baidu_content, baidu_workflow = load_workflow(".github/workflows/baidu-transfer.yml")
        test_content, _ = load_workflow(".github/workflows/test-on-push.yml")
        baidu_setup = steps_using(
            workflow_steps(baidu_workflow, "transfer"), "actions/setup-python@v5"
        )[0]
        baidu_cache_dependency_path = str(baidu_setup["with"]["cache-dependency-path"])

        self.assertIn("constraints.txt", baidu_cache_dependency_path)
        self.assertIn("bash scripts/install_dependencies.sh runtime", baidu_content)
        self.assertIn("bash scripts/install_dependencies.sh test", test_content)
        self.assertIn("bash scripts/install_dependencies.sh quality", test_content)
        for content in (baidu_content, test_content):
            self.assertNotIn("python -m pip install --upgrade pip", content)
            self.assertNotIn("pip install -r", content)

        install_script = read_repo_file("scripts/install_dependencies.sh")
        self.assertIn("pip==", install_script)
        self.assertIn("python -m pip install --upgrade", install_script)
        self.assertIn("requirements-quality.txt", install_script)

    def test_workflow_tests_keep_test_dependencies_out_of_runtime_requirements(self):
        runtime_requirements = read_repo_file("requirements.txt")
        test_requirements = read_repo_file("requirements-test.txt")
        quality_requirements = read_repo_file("requirements-quality.txt")

        self.assertNotIn("PyYAML", runtime_requirements)
        self.assertNotIn("ruff", runtime_requirements)
        self.assertNotIn("pip-audit", runtime_requirements)
        self.assertIn("PyYAML==6.0.3", test_requirements)
        self.assertNotIn("ruff", test_requirements)
        self.assertNotIn("pip-audit", test_requirements)
        self.assertIn("ruff==0.15.12", quality_requirements)
        self.assertIn("pip-audit==2.10.0", quality_requirements)

    def test_requirements_use_exact_pins(self):
        for filename in ("requirements.txt", "requirements-test.txt", "requirements-quality.txt"):
            with self.subTest(filename=filename):
                for line in requirement_lines(filename):
                    self.assertIn("==", line)
                    self.assertNotRegex(line, r"(?<![=!<>])(?:>=|~=|>|<)")

    def test_constraints_file_uses_exact_pins(self):
        for line in requirement_lines("constraints.txt"):
            self.assertIn("==", line)
            self.assertNotRegex(line, r"(?<![=!<>])(?:>=|~=|>|<)")
            self.assertNotIn("://", line)
            self.assertFalse(line.startswith("."))
            self.assertFalse(line.startswith("/"))

    def test_constraints_file_only_pins_transitive_dependencies(self):
        direct_names = set()
        for filename in ("requirements.txt", "requirements-test.txt", "requirements-quality.txt"):
            direct_names.update(requirement_name(line) for line in requirement_lines(filename))
        constraint_names = {requirement_name(line) for line in requirement_lines("constraints.txt")}

        self.assertEqual(set(), direct_names & constraint_names)

    def test_test_workflow_has_canceling_concurrency_and_python_matrix(self):
        _, workflow = load_workflow(".github/workflows/test-on-push.yml")
        test_job = workflow["jobs"]["tests"]

        self.assertIs(workflow["concurrency"]["cancel-in-progress"], True)
        self.assertEqual(
            "tests-${{ github.workflow }}-${{ github.ref }}",
            workflow["concurrency"]["group"],
        )
        self.assertIs(test_job["strategy"]["fail-fast"], False)
        self.assertEqual(
            ["3.9", "3.12"],
            test_job["strategy"]["matrix"]["python-version"],
        )

    def test_workflows_use_setup_python_v5(self):
        for workflow_file, job_name in (
            (".github/workflows/baidu-transfer.yml", "transfer"),
            (".github/workflows/test-on-push.yml", "tests"),
        ):
            with self.subTest(workflow_file=workflow_file):
                content, workflow = load_workflow(workflow_file)
                setup_steps = steps_using(
                    workflow_steps(workflow, job_name), "actions/setup-python@v5"
                )

                self.assertNotIn("actions/setup-python@v4", content)
                self.assertEqual(1, len(setup_steps))

        _, test_workflow = load_workflow(".github/workflows/test-on-push.yml")
        setup_step = steps_using(workflow_steps(test_workflow, "tests"), "actions/setup-python@v5")[
            0
        ]
        self.assertEqual("${{ matrix.python-version }}", setup_step["with"]["python-version"])

    def test_workflows_disable_checkout_persist_credentials(self):
        for workflow_file in (
            ".github/workflows/baidu-transfer.yml",
            ".github/workflows/test-on-push.yml",
        ):
            with self.subTest(workflow_file=workflow_file):
                _, workflow = load_workflow(workflow_file)
                for job in workflow["jobs"].values():
                    checkout_steps = steps_using(job.get("steps", []), "actions/checkout@v4")
                    for step in checkout_steps:
                        self.assertIs(step["with"]["persist-credentials"], False)

    def test_network_info_curl_has_timeout_and_fail_flags(self):
        content = read_repo_file("scripts/run_transfer_task.sh")

        self.assertIn("--fail", content)
        self.assertIn("--show-error", content)
        self.assertIn("--silent", content)
        self.assertIn("--connect-timeout 5", content)
        self.assertIn("--max-time 10", content)

    def test_gitignore_keeps_tests_directory_trackable(self):
        content = read_repo_file(".gitignore")

        self.assertIn("/test_*.py", content)
        self.assertIn("/*_test.py", content)
        self.assertIn(".transfershare_failed_transfers.json.enc", content)
        self.assertIn(".transfershare_failed_transfers.json.enc.tmp", content)
        self.assertNotIn("\ntest_*.py\n", content)
        self.assertNotIn("\n*_test.py\n", content)

    def test_docs_describe_actions_operational_parameters(self):
        readme = read_repo_file("README.md")
        config_guide = read_repo_file("CONFIG_GUIDE.md")
        combined = f"{readme}\n{config_guide}"

        for text in (readme, config_guide):
            self.assertIn("TRANSFERSHARE_STATE_KEY", text)
            self.assertIn(".transfershare_failed_transfers.json.enc", text)
            self.assertIn("17 分钟", text)
            self.assertIn("并发", text)
            self.assertIn("persist-credentials", text)
            self.assertIn("actionlint", text)
        self.assertIn("7 分钟", combined)
        self.assertIn("10 分钟", combined)
        self.assertIn("Python 3.9", combined)
        self.assertIn("Python 3.12", combined)

    def test_regex_replace_examples_use_python_re_sub_syntax(self):
        files = {
            "README.md": read_repo_file("README.md"),
            "CONFIG_GUIDE.md": read_repo_file("CONFIG_GUIDE.md"),
            "config.example.json": read_repo_file("config.example.json"),
        }

        for filename, content in files.items():
            with self.subTest(filename=filename):
                self.assertNotIn("$1", content)
                self.assertNotIn("$2", content)
        self.assertIn("\\\\1_\\\\2.pdf", files["config.example.json"])


if __name__ == "__main__":
    unittest.main()
