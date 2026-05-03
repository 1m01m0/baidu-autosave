import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class WorkflowStaticTests(unittest.TestCase):
    def test_baidu_transfer_workflow_limits_permissions_and_uses_off_hour_cron(self):
        content = (REPO_ROOT / ".github/workflows/baidu-transfer.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("permissions:\n  contents: read", content)
        self.assertIn("- cron: '17 */6 * * *'", content)
        self.assertNotIn("- cron: '0 */6 * * *'", content)

    def test_baidu_transfer_workflow_keeps_secrets_out_of_job_env(self):
        content = (REPO_ROOT / ".github/workflows/baidu-transfer.yml").read_text(
            encoding="utf-8"
        )
        job_env_match = re.search(
            r"jobs:\n  transfer:\n(?:.*\n)*?    env:\n(?P<env>(?:      .+\n)+)",
            content,
        )

        self.assertIsNotNone(job_env_match)
        job_env = job_env_match.group("env")
        self.assertNotIn("secrets.", job_env)
        for name in ("BAIDU_COOKIES", "SHARE_URLS", "SAVE_DIR", "WECHAT_WEBHOOK"):
            self.assertRegex(content, rf"        {name}: \$\{{\{{ secrets\.{name} \}}\}}")

    def test_baidu_transfer_workflow_does_not_cache_sensitive_failed_state(self):
        content = (REPO_ROOT / ".github/workflows/baidu-transfer.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("actions/cache/restore", content)
        self.assertNotIn("actions/cache/save", content)
        self.assertNotIn(".transfershare_failed_transfers.json", content)

    def test_test_workflow_limits_permissions(self):
        content = (REPO_ROOT / ".github/workflows/test-on-push.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("permissions:\n  contents: read", content)

    def test_network_info_curl_has_timeout_and_fail_flags(self):
        content = (REPO_ROOT / "scripts/run_transfer_task.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("--fail", content)
        self.assertIn("--show-error", content)
        self.assertIn("--silent", content)
        self.assertIn("--connect-timeout 5", content)
        self.assertIn("--max-time 10", content)

    def test_gitignore_keeps_tests_directory_trackable(self):
        content = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("/test_*.py", content)
        self.assertIn("/*_test.py", content)
        self.assertNotIn("\ntest_*.py\n", content)
        self.assertNotIn("\n*_test.py\n", content)


if __name__ == "__main__":
    unittest.main()
