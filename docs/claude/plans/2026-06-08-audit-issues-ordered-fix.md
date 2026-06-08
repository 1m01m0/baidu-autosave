# Ordered Audit Issues Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the verified audit issues in priority order while keeping each behavior covered by a failing test before production changes.

**Architecture:** Keep the current module boundaries. Make targeted fixes in storage filtering, retry/recovery, path scanning, notifications, workflow configuration, and repository hygiene; reuse existing helpers such as `mask_sensitive`, `_clear_local_files_cache`, `retry_share_config_key`, and workflow static tests.

**Tech Stack:** Python 3.9+, unittest, Ruff, GitHub Actions YAML, pyenv virtualenv `transfer_share`.

---

## Context

The previous ultracode audit verified 18 real issues. The most urgent are correctness defects in path handling, rename recovery, local scan failure handling, and Actions notifications. The intended outcome is a safer transfer pipeline: nested paths remain intact, failed renames can recover, scan failures do not masquerade as empty directories, retry notifications are not misleading, sensitive logs are masked, fast paths are reliable, and CI/repository metadata match the documented project workflow.

## File Structure / Responsibilities

- `storage_filter.py`: candidate path normalization; fix single-folder trimming only when the root basename is still present.
- `storage_shares.py`: existing shared-root trimming source of truth; do not duplicate root trimming downstream.
- `storage_paths.py`: local filesystem listing behavior; propagate non-missing scan errors.
- `storage.py`: transfer orchestration, fast-path retry, progress scoping, cache invalidation, and result assembly.
- `storage_rename.py`: rename failure reporting and write-after-rename cache invalidation.
- `storage_transfer_plan.py`: existing retry/cache invalidation reference; avoid replacing it.
- `storage_traverser.py`: directory split fast path and cache invalidation callback.
- `transfer_runner.py`: retry record construction, final notification suppression, operational warnings, and sanitized error persistence.
- `wechat_notifier.py`: failure report details and warning blocks.
- `save_baidu_cookies.py`: browser-login waiting feedback.
- `.github/workflows/baidu-transfer.yml`: first-attempt notification suppression and failed-state enabled flag.
- `.github/workflows/test-on-push.yml`: full Ruff/format quality gate.
- `tests/`: unittest-first regression coverage; add focused tests near existing test classes.
- Repository hygiene files: `.kiro/specs/storage-architecture-refactor/.config.kiro`, `LICENSE`, `pyproject.toml`, remove `pytest.ini`.

## Tasks

### Task 1: Fix V001 single-folder path trimming

**Files:**
- Modify: `storage_filter.py`
- Test: `tests/test_storage_filter.py`

- [ ] Add a unittest showing a single shared directory with already-trimmed path `子目录/a.txt` keeps `clean_path == "子目录/a.txt"` and `dir_path == "子目录"`.
- [ ] Run `python -m unittest tests.test_storage_filter` and verify the new test fails because `clean_path` becomes `a.txt`.
- [ ] Change `CandidateFilter.prepare_candidates()` to trim the first path component only when it equals the shared directory basename.
- [ ] Run `python -m unittest tests.test_storage_filter` and the relevant storage tests.

### Task 2: Fix V002 rename failure recovery

**Files:**
- Modify: `transfer_runner.py`, `storage.py` and/or `storage_rename.py`
- Test: `tests/test_transfer_runner.py`, `tests/test_storage.py`

- [ ] Add a failing test that `build_failed_transfer_records()` preserves `rename_failed_files` as retryable failed records without raw debug fields.
- [ ] Add a failing transfer flow test where `clean_path` exists with matching MD5, `final_path` is missing, and the next run performs rename without re-transfer.
- [ ] Reuse `_split_existing_transfer_items()` / `need_rename` logic to surface rename-only work.
- [ ] Persist enough rename failure context for retry while keeping existing failed-record schema minimal and masked.
- [ ] Run targeted runner/storage tests.

### Task 3: Fix V003 scan exceptions treated as empty directories

**Files:**
- Modify: `storage_paths.py`, `storage.py` if needed
- Test: `tests/test_storage.py`

- [ ] Change the existing worker-exception test expectation from empty list to propagated failure.
- [ ] Add a missing-path control test that still returns `[]`.
- [ ] Make `list_local_files_in_dirs()` and `list_local_files()` only return `[]` for known missing paths; propagate other errors.
- [ ] Adjust callers to convert propagated scan failures into failure/partial results instead of normal empty scans.
- [ ] Run storage path and transfer-flow tests.

### Task 4: Fix V004 first-attempt final notification false alarm

**Files:**
- Modify: `.github/workflows/baidu-transfer.yml`, `transfer_runner.py`
- Test: `tests/test_workflows.py`, `tests/test_transfer_runner.py`

- [ ] Add workflow static test that first attempt sets `TRANSFERSHARE_SUPPRESS_RESULT_NOTIFICATION=1` and second attempt does not.
- [ ] Add runner test that `notify_transfer_result()` does not call notifier when the env var is set.
- [ ] Add the env var to the first attempt only.
- [ ] Gate only final result notifications; do not suppress all logging.
- [ ] Run workflow and runner tests.

### Task 5: Fix V006 sensitive raw error logging

**Files:**
- Modify: `storage_client.py`, `transfer_runner.py`, possibly `utils.py` / `wechat_notifier.py`
- Test: `tests/test_transfer_runner.py`, `tests/test_storage.py` or new focused tests

- [ ] Add tests with `BDUSS`, `STOKEN`, `pwd`, webhook key, and share URL in raw errors; assert logs/failed records/notifications do not contain secrets.
- [ ] Replace debug logging of `error_info.raw_message` with masked text or `error_info.message`.
- [ ] Sanitize failed-record error fields and drop raw/debug/traceback fields before persistence.
- [ ] Run masking, runner, and storage client tests.

### Task 6: Fix V007/V008 fast-path retry and cache invalidation

**Files:**
- Modify: `storage.py`, `storage_traverser.py`, `storage_rename.py`
- Test: `tests/test_storage.py`, `tests/test_storage_traverser.py`

- [ ] Add failing test for directory fast path retrying retryable temporary error once before succeeding.
- [ ] Add failing tests for cache invalidation after directory fast-path success, subtree fast-path success, and rename success.
- [ ] Add local retry loop for fast path that preserves count-limit division and existing target-created fallback behavior.
- [ ] Call `_clear_local_files_cache()` via existing locking helper after each direct write path.
- [ ] Run targeted storage/traverser tests.

### Task 7: Fix V011/V012/V013/V014/V015/V018 UX/recovery transparency

**Files:**
- Modify: `wechat_notifier.py`, `storage.py`, `storage_streaming.py`, `config_utils.py`, `transfer_runner.py`, `save_baidu_cookies.py`, `.github/workflows/baidu-transfer.yml`, docs/config examples if needed
- Test: `tests/test_storage.py`, `tests/test_config_utils.py`, `tests/test_transfer_runner.py`, `tests/test_save_baidu_cookies.py`, `tests/test_workflows.py`

- [ ] Add failure notification test asserting complete failure includes transfer/rename failed file details.
- [ ] Add progress test ensuring streaming progress does not emit step 3 before step 2.
- [ ] Add concurrent progress test ensuring nested progress includes share index/context.
- [ ] Add retry-key canonicalization tests for equivalent configs and distinct configs.
- [ ] Add cookie wait progress test with fake time/cookies and no secret leakage.
- [ ] Add workflow/notification tests for failed-state enabled flag and warning block.
- [ ] Implement minimal changes using existing `mask_share_url`, `mask_sensitive`, `_format_files_block`, and retry config helpers.
- [ ] Run all affected tests.

### Task 8: Fix V009/V010/V016/V017 quality and repository hygiene

**Files:**
- Create: `pyproject.toml`, `LICENSE`, `tests/test_repository_hygiene.py`
- Modify: `.github/workflows/test-on-push.yml`, `.kiro/specs/storage-architecture-refactor/.config.kiro`, `README.md` if needed
- Delete: `pytest.ini`

- [ ] Add repository hygiene tests for unique tracked Kiro config UUIDs, LICENSE existence, and absence of pytest config.
- [ ] Add workflow test requiring full `ruff check .` and `ruff format --check .`.
- [ ] Create Ruff config with Python 3.9 target and vendor exclusions.
- [ ] Generate a unique UUID for `storage-architecture-refactor/.config.kiro` and track it.
- [ ] Add MIT LICENSE matching README claim.
- [ ] Delete `pytest.ini` to align with unittest-only docs/deps/CI.
- [ ] Run `python -m ruff check . --no-cache`, `python -m ruff format --check . --no-cache`, and all unittest tests.

## Verification

Use pyenv virtualenv `transfer_share` for all Python commands.

1. Targeted tests after each task, e.g. `python -m unittest tests.test_storage_filter`.
2. Full test suite: `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p "test_*.py" -b`.
3. Syntax: `PYTHONDONTWRITEBYTECODE=1 python -m compileall -q -x 'vendor/' .`.
4. Quality: `PYTHONDONTWRITEBYTECODE=1 python -m ruff check . --no-cache` and `PYTHONDONTWRITEBYTECODE=1 python -m ruff format --check . --no-cache`.
5. Dependency sanity if environment supports it: `python -m pip check`.
6. Final `git status --short` must show only intended tracked changes plus the known Kiro config decision.
