# ShareLoader Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract BaiduStorage share-loading wrapper logic into `ShareLoader` while preserving private wrapper compatibility and public behavior.

**Architecture:** Add `storage_loader.py` as a focused collaborator for share entry loading, shared-file-list loading, progress reporting, and empty-share notification. Keep `BaiduStorage` as the orchestrator; its existing `_load_share_entries()`, `_load_share_files()`, and `_load_share_context()` methods remain wrappers that delegate to `ShareLoader`.

**Tech Stack:** Python, `unittest`, existing `storage_progress.ProgressReporter`, `utils.mask_share_url`, existing pyenv environment `transfer_share`.

---

## File Structure

- Create `storage_loader.py`
  - Owns `ShareLoader`.
  - Imports only `ProgressReporter` and `mask_share_url` from local modules.
  - Does not import `storage.py` or depend on `BaiduStorage`.

- Create `tests/test_storage_loader.py`
  - Direct `unittest` coverage for `ShareLoader` with a mock share service.
  - Verifies context shape, progress messages, empty-share notification, context copying, callback forwarding, and exception propagation.

- Modify `storage.py`
  - Import `ShareLoader`.
  - Add `_share_loader(progress_callback=None)` factory.
  - Replace `_load_share_entries()`, `_load_share_files()`, and `_load_share_context()` bodies with wrappers.
  - Leave `transfer_share()`, `get_share_folder_name()`, `storage_streaming.py`, and public APIs unchanged.

- Modify `.kiro/specs/storage-architecture-refactor/tasks.md`
  - After implementation is verified, mark task 6 and subtasks 6.1 through 6.5 complete.
  - Update the verification baseline to include `tests.test_storage_loader`.
  - Back up this document first to `.claude/backups/storage-architecture-refactor-tasks.md`.

---

## Task 1: Add direct ShareLoader tests

**Files:**
- Create: `tests/test_storage_loader.py`
- Read for behavior examples: `storage.py:625-689`
- Read for progress helper: `storage_progress.py`

- [ ] **Step 1: Write the failing test file**

Create `tests/test_storage_loader.py` with this full content:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from storage_loader import ShareLoader
from storage_progress import ProgressReporter
from utils import mask_share_url


class ShareLoaderTest(unittest.TestCase):
    def make_shared_path(self):
        return SimpleNamespace(
            uk=1,
            share_id=2,
            bdstoken="token",
            path="/share/movie.mp4",
            is_dir=False,
        )

    def make_loader(self, share_service=None, progress_messages=None, error_notifications=None):
        share_service = share_service or Mock()
        progress_messages = progress_messages if progress_messages is not None else []
        error_notifications = error_notifications if error_notifications is not None else []

        def notify_error(error, context_message, collect=True):
            error_notifications.append((error, context_message, collect))

        loader = ShareLoader(
            share_service,
            ProgressReporter(
                lambda level, message: progress_messages.append((level, message))
            ),
            notify_error,
        )
        return loader, share_service, progress_messages, error_notifications

    def test_load_entries_returns_share_context(self):
        shared_path = self.make_shared_path()
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = [shared_path]

        result = loader.load_entries("https://pan.baidu.com/s/abc12345?pwd=1a2B", "1a2B")

        self.assertEqual(
            {
                "shared_paths": [shared_path],
                "uk": 1,
                "share_id": 2,
                "bdstoken": "token",
            },
            result,
        )
        share_service.load_shared_paths.assert_called_once_with(
            "https://pan.baidu.com/s/abc12345?pwd=1a2B", "1a2B"
        )
        self.assertIn(
            (
                "info",
                f"【步骤1/4】访问分享链接: {mask_share_url('https://pan.baidu.com/s/abc12345?pwd=1a2B')}",
            ),
            progress_messages,
        )
        self.assertIn(("info", "使用密码访问分享链接"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_entries_without_password_does_not_report_password_message(self):
        shared_path = self.make_shared_path()
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = [shared_path]

        result = loader.load_entries("plain-share-url", None)

        self.assertEqual([shared_path], result["shared_paths"])
        self.assertIn(("info", "【步骤1/4】访问分享链接: plain-share-url"), progress_messages)
        self.assertNotIn(("info", "使用密码访问分享链接"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_entries_empty_share_notifies_and_returns_none(self):
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = []

        result = loader.load_entries("share-url", None)

        self.assertIsNone(result)
        self.assertEqual(1, len(error_notifications))
        error, context_message, collect = error_notifications[0]
        self.assertIsInstance(error, ValueError)
        self.assertEqual("获取分享文件列表失败", str(error))
        self.assertEqual("获取分享文件列表失败", context_message)
        self.assertTrue(collect)
        self.assertIn(("info", "【步骤1/4】访问分享链接: share-url"), progress_messages)

    def test_load_entries_propagates_share_service_error(self):
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.side_effect = RuntimeError("access failed")

        with self.assertRaises(RuntimeError):
            loader.load_entries("share-url", None)

        self.assertIn(("info", "【步骤1/4】访问分享链接: share-url"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_files_returns_copied_context_and_forwards_callback(self):
        shared_path = self.make_shared_path()
        context = {
            "shared_paths": [shared_path],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        shared_files_info = [{"fs_id": 10, "path": "movie.mp4"}]
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.list_shared_files.return_value = shared_files_info

        result = loader.load_files(context, "movies", r"^node_modules$")

        self.assertIsNot(result, context)
        self.assertNotIn("shared_files_info", context)
        self.assertEqual(shared_files_info, result["shared_files_info"])
        self.assertEqual([shared_path], result["shared_paths"])
        args, kwargs = share_service.list_shared_files.call_args
        self.assertEqual([shared_path], args[0])
        self.assertEqual("movies", args[1])
        self.assertEqual(r"^node_modules$", kwargs["exclude_folder_filter"])
        callback = args[2]
        callback("info", "来自 SharedPathService 的进度")
        self.assertIn(("info", "来自 SharedPathService 的进度"), progress_messages)
        self.assertIn(("info", "开始获取共享文件列表"), progress_messages)
        self.assertIn(("info", "获取到 1 个共享文件"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_files_propagates_list_error(self):
        shared_path = self.make_shared_path()
        context = {
            "shared_paths": [shared_path],
            "uk": 1,
            "share_id": 2,
            "bdstoken": "token",
        }
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.list_shared_files.side_effect = RuntimeError("list failed")

        with self.assertRaises(RuntimeError):
            loader.load_files(context, None, None)

        self.assertIn(("info", "开始获取共享文件列表"), progress_messages)
        self.assertNotIn(("info", "获取到 0 个共享文件"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_context_combines_entries_and_files(self):
        shared_path = self.make_shared_path()
        shared_files_info = [{"fs_id": 10, "path": "movie.mp4"}]
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = [shared_path]
        share_service.list_shared_files.return_value = shared_files_info

        result = loader.load_context("share-url", "pwd", "movies", r"^skip$")

        self.assertEqual([shared_path], result["shared_paths"])
        self.assertEqual(shared_files_info, result["shared_files_info"])
        share_service.load_shared_paths.assert_called_once_with("share-url", "pwd")
        args, kwargs = share_service.list_shared_files.call_args
        self.assertEqual([shared_path], args[0])
        self.assertEqual("movies", args[1])
        self.assertEqual(r"^skip$", kwargs["exclude_folder_filter"])
        self.assertIn(("info", "使用密码访问分享链接"), progress_messages)
        self.assertEqual([], error_notifications)

    def test_load_context_stops_when_entries_fail(self):
        loader, share_service, progress_messages, error_notifications = self.make_loader()
        share_service.load_shared_paths.return_value = []

        result = loader.load_context("share-url", None, "movies", r"^skip$")

        self.assertIsNone(result)
        share_service.list_shared_files.assert_not_called()
        self.assertEqual(1, len(error_notifications))
        self.assertIn(("info", "【步骤1/4】访问分享链接: share-url"), progress_messages)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new tests to verify they fail for the expected reason**

Run:

```bash
pyenv version
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_loader
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
ModuleNotFoundError: No module named 'storage_loader'
FAILED (errors=1)
```

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_storage_loader.py
git commit -m "$(cat <<'EOF'
添加分享加载器行为测试
EOF
)"
```

---

## Task 2: Implement ShareLoader

**Files:**
- Create: `storage_loader.py`
- Test: `tests/test_storage_loader.py`

- [ ] **Step 1: Create `storage_loader.py`**

Create `storage_loader.py` with this full content:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分享链接加载逻辑。"""

from storage_progress import ProgressReporter
from utils import mask_share_url


class ShareLoader:
    def __init__(self, share_service, progress=None, error_notifier=None):
        self.share_service = share_service
        self.progress = progress or ProgressReporter()
        self.error_notifier = error_notifier

    def notify_error(self, error, context_message, collect=True):
        if self.error_notifier is not None:
            self.error_notifier(error, context_message, collect=collect)

    def load_entries(self, share_url, pwd=None):
        masked_share_url = mask_share_url(share_url) or share_url
        self.progress.report("info", f"【步骤1/4】访问分享链接: {masked_share_url}")
        if pwd:
            self.progress.report("info", "使用密码访问分享链接")

        shared_paths = self.share_service.load_shared_paths(share_url, pwd)
        if not shared_paths:
            self.notify_error(
                ValueError("获取分享文件列表失败"),
                "获取分享文件列表失败",
                collect=True,
            )
            return None

        return {
            "shared_paths": shared_paths,
            "uk": shared_paths[0].uk,
            "share_id": shared_paths[0].share_id,
            "bdstoken": shared_paths[0].bdstoken,
        }

    def load_files(self, context, folder_filter=None, exclude_folder_filter=None):
        self.progress.report("info", "开始获取共享文件列表")
        shared_files_info = self.share_service.list_shared_files(
            context["shared_paths"],
            folder_filter,
            self.progress.report,
            exclude_folder_filter=exclude_folder_filter,
        )
        self.progress.report("info", f"获取到 {len(shared_files_info)} 个共享文件")

        context = dict(context)
        context["shared_files_info"] = shared_files_info
        return context

    def load_context(
        self,
        share_url,
        pwd=None,
        folder_filter=None,
        exclude_folder_filter=None,
    ):
        context = self.load_entries(share_url, pwd)
        if not context:
            return None
        return self.load_files(
            context,
            folder_filter,
            exclude_folder_filter=exclude_folder_filter,
        )


__all__ = ["ShareLoader"]
```

- [ ] **Step 2: Run direct ShareLoader tests**

Run:

```bash
pyenv version
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_loader
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
Ran 8 tests
OK
```

- [ ] **Step 3: Commit ShareLoader implementation**

```bash
git add storage_loader.py tests/test_storage_loader.py
git commit -m "$(cat <<'EOF'
添加分享加载器
EOF
)"
```

---

## Task 3: Delegate BaiduStorage share-loading wrappers

**Files:**
- Modify: `storage.py:39-45`
- Modify: `storage.py:625-689`
- Test: `tests/test_storage.py`
- Test: `tests/test_storage_loader.py`

- [ ] **Step 1: Update `storage.py` imports**

Change the import area in `storage.py` from:

```python
from storage_filter import CandidateFilter
from storage_models import DirTreeFrame, TransferItem
from storage_paths import StoragePathService
from storage_progress import ProgressReporter
from storage_rules import should_exclude_folder
from storage_shares import SharedPathService
from storage_traverser import DirTreeTraverser
```

to:

```python
from storage_filter import CandidateFilter
from storage_loader import ShareLoader
from storage_models import DirTreeFrame, TransferItem
from storage_paths import StoragePathService
from storage_progress import ProgressReporter
from storage_rules import should_exclude_folder
from storage_shares import SharedPathService
from storage_traverser import DirTreeTraverser
```

- [ ] **Step 2: Replace share-loading helper block with factory and wrappers**

In `storage.py`, replace `_load_share_entries()`, `_load_share_files()`, and `_load_share_context()` with this block. Keep `_normalize_save_dir()` above it and `_candidate_filter()` below it.

```python
    def _share_loader(self, progress_callback=None):
        def notify_error(error, context_message, collect=True):
            handle_error_and_notify(
                error,
                context_message,
                self.wechat_notifier,
                None,
                collect=collect,
            )

        return ShareLoader(
            self.share_service,
            ProgressReporter(progress_callback),
            notify_error,
        )

    def _load_share_entries(self, share_url, pwd, progress_callback=None):
        return self._share_loader(progress_callback).load_entries(share_url, pwd)

    def _load_share_files(
        self,
        context,
        folder_filter,
        progress_callback=None,
        exclude_folder_filter=None,
    ):
        return self._share_loader(progress_callback).load_files(
            context,
            folder_filter,
            exclude_folder_filter=exclude_folder_filter,
        )

    def _load_share_context(
        self,
        share_url,
        pwd,
        folder_filter,
        progress_callback=None,
        exclude_folder_filter=None,
    ):
        return self._share_loader(progress_callback).load_context(
            share_url,
            pwd,
            folder_filter,
            exclude_folder_filter=exclude_folder_filter,
        )
```

This preserves existing monkeypatch behavior because `transfer_share()` still calls `self._load_share_entries()`.

- [ ] **Step 3: Run wrapper compatibility tests**

Run:

```bash
pyenv version
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_loader
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
OK
```

Success requires command exit code 0, zero failures, and zero errors.

- [ ] **Step 4: Commit wrapper delegation**

```bash
git add storage.py storage_loader.py tests/test_storage_loader.py
git commit -m "$(cat <<'EOF'
委托分享加载到加载器
EOF
)"
```

---

## Task 4: Sync Kiro task status after ShareLoader implementation

**Files:**
- Modify: `.kiro/specs/storage-architecture-refactor/tasks.md`
- Modify: `.claude/backups/storage-architecture-refactor-tasks.md`

- [ ] **Step 1: Back up the current Kiro task document**

Run:

```bash
cp ".kiro/specs/storage-architecture-refactor/tasks.md" ".claude/backups/storage-architecture-refactor-tasks.md"
```

Expected: no output.

- [ ] **Step 2: Update completed status and verification baseline**

In `.kiro/specs/storage-architecture-refactor/tasks.md`, update the completed status list by adding these two bullets after the existing `DirTreeTraverser` bullets:

```markdown
- `ShareLoader` is in `storage_loader.py`.
- `BaiduStorage` keeps share-loading private wrapper methods and delegates share entry/context loading to `ShareLoader`.
```

Change the verification baseline block from:

```markdown
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_filter
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_traverser
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_filter tests.test_storage_traverser
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p "test_*.py" -b
```

to:

```markdown
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_filter
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_traverser
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_loader
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_filter tests.test_storage_traverser tests.test_storage_loader
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p "test_*.py" -b
```

Change the ShareLoader task section from:

```markdown
- [ ] 6. Extract ShareLoader
  - [ ] 6.1 Create `storage_loader.py` with `ShareLoader`.
  - [ ] 6.2 Move share-entry loading and shared-file list loading logic out of `BaiduStorage`.
  - [ ] 6.3 Keep `BaiduStorage` wrapper methods for `_load_share_entries`, `_load_share_files`, and `_load_share_context`.
  - [ ] 6.4 Add focused `unittest` coverage.
  - [ ] 6.5 Verify existing workflow and runner tests.
```

to:

```markdown
- [x] 6. Extract ShareLoader
  - [x] 6.1 Create `storage_loader.py` with `ShareLoader`.
  - [x] 6.2 Move share-entry loading and shared-file list loading logic out of `BaiduStorage`.
  - [x] 6.3 Keep `BaiduStorage` wrapper methods for `_load_share_entries`, `_load_share_files`, and `_load_share_context`.
  - [x] 6.4 Add focused `unittest` coverage.
  - [x] 6.5 Verify existing workflow and runner tests.
```

Change the task dependency graph from:

```json
{
  "completed": ["1", "2", "3", "4", "5"],
  "next": "6",
  "remaining_order": ["6", "7", "8", "9"]
}
```

to:

```json
{
  "completed": ["1", "2", "3", "4", "5", "6"],
  "next": "7",
  "remaining_order": ["7", "8", "9"]
}
```

- [ ] **Step 3: Run formatting check for the documentation update**

Run:

```bash
git diff --check -- .kiro/specs/storage-architecture-refactor/tasks.md .claude/backups/storage-architecture-refactor-tasks.md
```

Expected: no output.

- [ ] **Step 4: Commit Kiro status sync**

```bash
git add .kiro/specs/storage-architecture-refactor/tasks.md .claude/backups/storage-architecture-refactor-tasks.md
git commit -m "$(cat <<'EOF'
同步分享加载器任务状态
EOF
)"
```

---

## Task 5: Full verification and cleanup

**Files:**
- Verify: `storage.py`
- Verify: `storage_loader.py`
- Verify: `tests/test_storage_loader.py`
- Verify: `.kiro/specs/storage-architecture-refactor/tasks.md`

- [ ] **Step 1: Verify pyenv environment**

Run:

```bash
pyenv version
```

Expected:

```text
transfer_share (set by /Users/jack/project/transfershare/.python-version)
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_loader
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_loader
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_transfer_runner tests.test_workflows
```

Expected:

```text
OK
OK
OK
```

Each command must exit 0 with zero failures and zero errors.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p "test_*.py" -b
```

Expected:

```text
OK
```

- [ ] **Step 4: Check whitespace and working tree status**

Run:

```bash
git diff --check
git status --short
```

Expected:

```text
# git diff --check has no output
# git status --short shows only intended tracked changes plus the pre-existing untracked .kiro/specs/storage-architecture-refactor/.config.kiro if it is still untracked
```

If Python cache directories appear, remove only those generated by this verification run before reporting completion.

- [ ] **Step 5: Commit final cleanup only if tracked files changed after Task 4**

If Task 5 only verifies and does not change tracked files, skip this commit. If a verification fix changes tracked files, commit the specific changed files:

```bash
git add storage.py storage_loader.py tests/test_storage_loader.py .kiro/specs/storage-architecture-refactor/tasks.md .claude/backups/storage-architecture-refactor-tasks.md
git commit -m "$(cat <<'EOF'
完善分享加载器验证修复
EOF
)"
```

---

## Self-Review Checklist

- Spec coverage:
  - `ShareLoader` component: Task 2 creates `storage_loader.py`.
  - Share entry loading: Task 2 implements `load_entries()` and Task 1 tests success, password progress, empty result, and exception propagation.
  - Shared-file-list loading: Task 2 implements `load_files()` and Task 1 tests callback forwarding, context copying, and exception propagation.
  - Combined context loading: Task 2 implements `load_context()` and Task 1 tests success and entry-failure short circuit.
  - `BaiduStorage` wrapper compatibility: Task 3 keeps old method names and runs `tests.test_storage`.
  - No public API changes: Task 3 leaves `transfer_share()`, `get_share_folder_name()`, and `storage_streaming.py` unchanged.
  - `unittest` only: all test commands use `python -m unittest`.
- Type consistency:
  - `ShareLoader.load_entries()` returns `None` or a dict with `shared_paths`, `uk`, `share_id`, and `bdstoken`.
  - `ShareLoader.load_files()` returns a copied dict with `shared_files_info` added.
  - `BaiduStorage._share_loader()` wraps legacy callbacks with `ProgressReporter(progress_callback)`.
  - `error_notifier` accepts `(error, context_message, collect=True)`.
- Scope check:
  - No `get_share_folder_name()` change, no `storage_streaming.py` change, no `SharedPathService` change, no `_notify_error` centralization, and no `TransferOrchestrator` rename are included.
