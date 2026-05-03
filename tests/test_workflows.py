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

    def test_baidu_transfer_workflow_caches_only_encrypted_failed_state(self):
        content = (REPO_ROOT / ".github/workflows/baidu-transfer.yml").read_text(
            encoding="utf-8"
        )

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
        content = (REPO_ROOT / ".github/workflows/baidu-transfer.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("python -m unittest", content)
        self.assertNotIn("Build BaiduPCS-Py submodule", content)
        self.assertNotIn("--show-network-info", content)

    def test_test_workflow_limits_permissions_and_buffers_unittest_output(self):
        content = (REPO_ROOT / ".github/workflows/test-on-push.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("permissions:\n  contents: read", content)
        self.assertIn('python -m unittest discover -s tests -p "test_*.py" -b', content)
        self.assertIn('python -c "from baidupcs_py.baidupcs import BaiduPCSApi"', content)
        self.assertNotIn("print(BaiduPCSApi)", content)

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
        self.assertIn(".transfershare_failed_transfers.json.enc", content)
        self.assertIn(".transfershare_failed_transfers.json.enc.tmp", content)
        self.assertNotIn("\ntest_*.py\n", content)
        self.assertNotIn("\n*_test.py\n", content)


if __name__ == "__main__":
    unittest.main()
