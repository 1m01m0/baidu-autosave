# Implementation Plan: Storage Architecture Refactor

## Overview

Incrementally extract focused collaborator classes from the monolithic `BaiduStorage` class while preserving current public behavior, private wrapper compatibility used by existing tests, and the `unittest`-based verification workflow.

## Current Status

Completed foundation work:

- Storage constants moved to `storage_constants.py` and re-exported from `storage.py`.
- `storage_transfer_plan.py`, `storage_streaming.py`, and `storage_rename.py` no longer use `import storage as storage_module`.
- `TransferResult`, `TransferResultBuilder`, `DirTreeFrame`, and `_DirTreeFrame` are in `storage_models.py`.
- `ProgressReporter` is in `storage_progress.py`.
- `CandidateFilter` is in `storage_filter.py`.
- `BaiduStorage` keeps candidate-analysis wrapper methods and delegates to `CandidateFilter`.
- `DirTreeTraverser` is in `storage_traverser.py`.
- `BaiduStorage` keeps directory-tree private wrapper methods and delegates directory divide-and-conquer traversal to `DirTreeTraverser`.
- `ShareLoader` is in `storage_loader.py`.
- `BaiduStorage` keeps share-loading private wrapper methods and delegates share entry/context loading to `ShareLoader`.
- `BaiduStorage` centralizes orchestrator-level error notifications through `_notify_error()`.

Current verification baseline:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_filter
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_traverser
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_loader
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage.BaiduStorageErrorNotificationTests
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_filter tests.test_storage_traverser tests.test_storage_loader
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p "test_*.py" -b
```

Most recent full-suite verification after centralized error notification:

```text
Ran 286 tests in 0.254s
OK
```

All future tasks must continue using `unittest`; do not add Hypothesis, pytest-only behavior, or new test dependencies.

## Tasks

- [x] 1. Extract StorageConstants module and fix import coupling
  - [x] 1.1 Create `storage_constants.py` with environment-derived constants.
  - [x] 1.2 Update `storage.py` to re-export constants from `storage_constants`.
  - [x] 1.3 Update `storage_transfer_plan.py` to import constants and helpers directly.
  - [x] 1.4 Update `storage_streaming.py` to import constants and helpers directly.
  - [x] 1.5 Update `storage_rename.py` to import constants and helpers directly.
  - [x] 1.6 Verify no refactored helper module uses `import storage as storage_module`.

- [x] 2. Implement transfer result and progress foundation
  - [x] 2.1 Add `TransferResult` frozen dataclass to `storage_models.py`.
  - [x] 2.2 Add `TransferResultBuilder` to `storage_models.py`.
  - [x] 2.3 Preserve legacy result-dictionary compatibility through `to_dict()` and `from_dict()`.
  - [x] 2.4 Add one-shot builder reuse protection.
  - [x] 2.5 Create `ProgressReporter` in `storage_progress.py`.
  - [x] 2.6 Add `unittest` coverage in `tests/test_storage_models.py` and `tests/test_storage_progress.py`.

- [x] 3. Extract CandidateFilter
  - [x] 3.1 Create `storage_filter.py` with `CandidateFilter`.
  - [x] 3.2 Move candidate preparation, filtering, planned-path conflict checks, warning sampling, and summary reporting into `CandidateFilter`.
  - [x] 3.3 Use `ProgressReporter` in candidate-analysis progress reporting.
  - [x] 3.4 Keep `BaiduStorage` private wrapper methods for candidate analysis.
  - [x] 3.5 Add direct `unittest` coverage in `tests/test_storage_filter.py`.
  - [x] 3.6 Verify `tests.test_storage`, `tests.test_storage_filter`, and the full suite.

- [x] 4. Design DirTreeTraverser extraction
  - [x] 4.1 Review current directory traversal methods in `storage.py`.
  - [x] 4.2 Define the minimal `DirTreeTraverser` constructor and transfer-executor interface.
  - [x] 4.3 Decide which existing `BaiduStorage` private methods remain as wrappers.
  - [x] 4.4 Define how `ProgressReporter` replaces only directory-traversal progress callbacks.
  - [x] 4.5 Write and review a focused design document before implementation.

- [x] 5. Implement DirTreeTraverser
  - [x] 5.1 Create `storage_traverser.py`.
  - [x] 5.2 Move directory-tree frame initialization, child handling, batching, count-limit fallback, and result building into `DirTreeTraverser`.
  - [x] 5.3 Keep `BaiduStorage` wrapper methods for compatibility.
  - [x] 5.4 Add `tests/test_storage_traverser.py` using `unittest`.
  - [x] 5.5 Verify targeted storage/traverser tests and full suite.

- [x] 6. Extract ShareLoader
  - [x] 6.1 Create `storage_loader.py` with `ShareLoader`.
  - [x] 6.2 Move share-entry loading and shared-file list loading logic out of `BaiduStorage`.
  - [x] 6.3 Keep `BaiduStorage` wrapper methods for `_load_share_entries`, `_load_share_files`, and `_load_share_context`.
  - [x] 6.4 Add focused `unittest` coverage.
  - [x] 6.5 Verify existing workflow and runner tests.

- [x] 7. Centralize error notification
  - [x] 7.1 Add `_notify_error(error, context_message, extra_info=None, collect=True)` to `BaiduStorage` or the eventual orchestrator.
  - [x] 7.2 Replace direct `handle_error_and_notify(...)` calls in small batches.
  - [x] 7.3 Verify each replaced call preserves notification arguments.
  - [x] 7.4 Add focused tests for `_notify_error` argument forwarding.

- [ ] 8. Reduce BaiduStorage toward a thin orchestrator
  - [ ] 8.1 Introduce collaborator injection only after `CandidateFilter`, `DirTreeTraverser`, `ShareLoader`, and `_notify_error` are stable.
  - [ ] 8.2 Keep the public `BaiduStorage(cookies, wechat_webhook=None)` constructor signature.
  - [ ] 8.3 Defer any `TransferOrchestrator` rename until the end; if introduced, keep `BaiduStorage = TransferOrchestrator` compatibility.
  - [ ] 8.4 Verify all CLI and import compatibility paths.

- [ ] 9. Final compatibility checkpoint
  - [ ] 9.1 Verify `from storage import BaiduStorage, RATE_LIMIT_WAIT_TIME, _DirTreeFrame, TransferItem` works.
  - [ ] 9.2 Verify no refactored internal module uses `import storage as storage_module`.
  - [ ] 9.3 Verify `transfer_runner.py` requires no call-site changes.
  - [ ] 9.4 Run the full `unittest` suite.

## Task Dependency Graph

```json
{
  "completed": ["1", "2", "3", "4", "5", "6", "7"],
  "next": "8",
  "remaining_order": ["8", "9"]
}
```
