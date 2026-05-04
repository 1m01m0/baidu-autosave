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
        self.assertIn("pip install -r requirements.txt -r requirements-test.txt", content)
        self.assertNotIn("print(BaiduPCSApi)", content)
        actionlint_steps = steps_using(workflow_steps(workflow, "actionlint"), "rhysd/actionlint@v1")
        self.assertEqual(1, len(actionlint_steps))

    def test_workflow_tests_keep_test_dependencies_out_of_runtime_requirements(self):
        self.assertNotIn("PyYAML", read_repo_file("requirements.txt"))
        self.assertIn("PyYAML>=6.0.2", read_repo_file("requirements-test.txt"))

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
            ["3.9", "3.10", "3.11", "3.12"],
            test_job["strategy"]["matrix"]["python-version"],
        )

    def test_workflows_use_setup_python_v5(self):
        for workflow_file, job_name in (
            (".github/workflows/baidu-transfer.yml", "transfer"),
            (".github/workflows/test-on-push.yml", "tests"),
        ):
            with self.subTest(workflow_file=workflow_file):
                content, workflow = load_workflow(workflow_file)
                setup_steps = steps_using(workflow_steps(workflow, job_name), "actions/setup-python@v5")

                self.assertNotIn("actions/setup-python@v4", content)
                self.assertEqual(1, len(setup_steps))

        _, test_workflow = load_workflow(".github/workflows/test-on-push.yml")
        setup_step = steps_using(workflow_steps(test_workflow, "tests"), "actions/setup-python@v5")[0]
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
