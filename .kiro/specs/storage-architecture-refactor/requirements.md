# Requirements Document

## Introduction

Refactor the BaiduStorage God class (~2000 lines) and related modules in the transfershare project to achieve better separation of concerns, reduced coupling, and elimination of repetitive patterns. The refactoring must preserve all existing behavior while making the codebase more maintainable, testable, and extensible.

## Glossary

- **BaiduStorage**: The current monolithic orchestrator class in storage.py that coordinates all transfer operations
- **TransferOrchestrator**: The new top-level coordinator class responsible for multi-share batch execution and result aggregation
- **CandidateFilter**: A new class responsible for determining which files should be transferred by checking local existence, MD5 conflicts, and regex rules
- **TransferResultBuilder**: A new class or dataclass responsible for constructing structured transfer results
- **TransferResult**: A frozen dataclass representing the outcome of a transfer operation (success/partial/error status, message, transferred files, failed files)
- **ProgressReporter**: A null-object wrapper around the progress callback pattern that eliminates repetitive `if progress_callback` guards
- **StorageConstants**: A dedicated module holding all configuration constants (batch sizes, delays, concurrency settings) currently defined at module level in storage.py
- **DirTreeTraverser**: A new class responsible for recursive directory-tree scanning and divide-and-conquer transfer logic
- **ShareLoader**: A new class responsible for loading and validating share link entries and contexts

## Requirements

### Requirement 1: Extract TransferResult Dataclass

**User Story:** As a developer, I want transfer results represented as a typed dataclass, so that I can rely on structured data instead of hand-built dictionaries with inconsistent keys.

#### Acceptance Criteria

1. THE TransferResult SHALL be a frozen dataclass with fields: success (bool), partial (bool), message (str, default empty string), transferred_files (list of dicts, default empty list), failed_files (list of dicts, default empty list), skipped_count (int, default 0), and error_details (optional str, default None)
2. WHEN a transfer operation completes, THE TransferResultBuilder SHALL produce a TransferResult instance instead of a raw dictionary, populating all fields from the operation outcome
3. THE TransferResult SHALL provide a `to_dict()` method that returns a dictionary with keys matching the current result format: "success", "partial", "message", "transferred_files", "transfer_failed_files", "transfer_failed_count", "rename_failed_files", "rename_failed_count", "completed_count", "transfer_success_count", and "error" (mapped from error_details when not None)
4. FOR ALL valid TransferResult instances, calling `to_dict()` then constructing a TransferResult from that dict via a `from_dict()` class method SHALL produce an object where all fields compare equal to the original instance
5. IF a TransferResult is constructed with success=True and failed_files containing one or more entries, THEN the dataclass `__post_init__` SHALL raise a ValueError indicating an inconsistent state
6. WHEN constructing a TransferResult via `from_dict()` with a dictionary missing required keys, THE `from_dict()` method SHALL raise a KeyError indicating which required key is absent

### Requirement 2: Introduce ProgressReporter with Null-Object Pattern

**User Story:** As a developer, I want to eliminate 50+ scattered `if progress_callback` guards, so that progress reporting logic is clean and centralized.

#### Acceptance Criteria

1. THE ProgressReporter SHALL accept an optional callback function during construction, where the callback signature is `(level: str, message: str) -> None`
2. WHEN no callback is provided (None or omitted), THE ProgressReporter SHALL silently discard all `report` invocations without raising errors or performing any side effects
3. WHEN a callback is provided, THE ProgressReporter SHALL invoke the callback with the positional arguments `(level, message)` exactly as passed to the `report` method
4. THE ProgressReporter SHALL expose a `report(level: str, message: str)` method as the single public interface for progress reporting, where `level` is one of "info", "warning", "error", or "success"
5. WHEN the ProgressReporter replaces an existing `if progress_callback` guard, THE ProgressReporter SHALL produce identical callback invocations (same `level` and `message` argument values in the same order) as the original guard
6. IF the provided callback raises an exception during invocation, THEN THE ProgressReporter SHALL propagate the exception to the caller without catching or suppressing it

### Requirement 3: Extract StorageConstants Module

**User Story:** As a developer, I want all configuration constants in a dedicated module, so that storage_streaming.py, storage_transfer_plan.py, and storage_rename.py can import constants directly without importing the entire storage module.

#### Acceptance Criteria

1. THE StorageConstants module SHALL contain all environment-derived constants currently defined at module level in storage.py (RATE_LIMIT_WAIT_TIME, RENAME_DELAY, RENAME_CONCURRENCY, BATCH_SHARE_DELAY, MULTI_SHARE_CONCURRENCY, TRANSFER_BATCH_SIZE, TRANSFER_FAILED_RETRY_ATTEMPTS, TRANSFER_FAILED_RETRY_DELAY, STREAM_PRODUCER_JOIN_TIMEOUT, TRANSFER_PIPELINE_ENABLED), each exported as a module-level name using the same environment variable keys and default values as the current storage.py definitions.
2. WHEN storage_streaming.py, storage_transfer_plan.py, or storage_rename.py need a constant, THE module SHALL import it from StorageConstants instead of using `import storage as storage_module`.
3. THE StorageConstants module SHALL not import from storage.py, storage_streaming.py, storage_transfer_plan.py, or storage_rename.py to avoid circular dependencies.
4. THE StorageConstants module SHALL only import from env_utils (read_non_negative_float_env, read_positive_int_env) and the Python standard library.
5. WHEN the StorageConstants module is imported, THE module SHALL produce identical constant values as the current storage.py module-level definitions for the same environment variable state.
6. WHEN storage.py is imported, THE module SHALL re-export all constants from StorageConstants so that existing code importing constants from storage.py continues to work without modification.

### Requirement 4: Eliminate `import storage as storage_module` Pattern

**User Story:** As a developer, I want to remove the tight coupling where helper modules import the entire storage module, so that the dependency graph is acyclic and each module depends only on what it directly needs.

#### Acceptance Criteria

1. WHEN storage_transfer_plan.py needs constants (TRANSFER_BATCH_SIZE, TRANSFER_FAILED_RETRY_ATTEMPTS, RATE_LIMIT_WAIT_TIME, TRANSFER_FAILED_RETRY_DELAY), THE module SHALL import them from their defining source module instead of accessing them via `import storage as storage_module`
2. WHEN storage_transfer_plan.py needs the handle_error_and_notify function or time.sleep, THE module SHALL import handle_error_and_notify from utils and time from the standard library directly instead of accessing them via `storage_module`
3. WHEN storage_streaming.py needs constants (TRANSFER_BATCH_SIZE, TRANSFER_PIPELINE_ENABLED, STREAM_PRODUCER_JOIN_TIMEOUT), the handle_error_and_notify function, or get_logger, THE module SHALL import them from their defining source modules (constants source, utils, logger) instead of accessing them via `import storage as storage_module`
4. WHEN storage_rename.py needs constants (RENAME_CONCURRENCY, RENAME_DELAY), the classify_storage_error function, handle_error_and_notify, or time.sleep, THE module SHALL import them from their defining source modules (constants source, storage_errors, utils, time) instead of accessing them via `import storage as storage_module`
5. IF any of storage_transfer_plan.py, storage_streaming.py, or storage_rename.py contains the statement `import storage as storage_module` after refactoring, THEN THE build or lint check SHALL report a violation and fail the check
6. WHEN the refactored modules are imported, THE Python interpreter SHALL resolve all imports without raising ImportError or circular import errors

### Requirement 5: Extract CandidateFilter Class

**User Story:** As a developer, I want candidate filtering logic separated from the orchestrator, so that filtering rules can be tested and evolved independently.

#### Acceptance Criteria

1. THE CandidateFilter SHALL encapsulate the logic currently in `_filter_transfer_candidates`, `_filter_transfer_candidates_core`, `_filter_planned_path_conflict`, and `_report_transfer_candidate_summary`
2. WHEN given transfer candidates, a local files dictionary (mapping normalized paths to MD5 hashes), a summary Counter, and warning sample storage, THE CandidateFilter SHALL return a list of TransferItem objects representing files that need transfer and update the summary Counter with at minimum the keys: existing_count, conflict_count, transfer_needed_count, and rename_needed_count
3. THE CandidateFilter SHALL accept a path normalization service and optional ProgressReporter at construction time, while regex_pattern and regex_replace remain per-call arguments to candidate preparation/build methods
4. THE CandidateFilter SHALL not depend on BaiduStorage instance state beyond the path normalization service, local files dictionary, and per-call configuration passed explicitly
5. WHEN given an empty candidates list, THE CandidateFilter SHALL return an empty transfer list and a summary Counter with all count values set to zero
6. WHEN multiple candidates resolve to the same normalized path within a single filter invocation, THE CandidateFilter SHALL retain only the first occurrence and increment the existing_count in the summary for each subsequent duplicate

### Requirement 6: Extract DirTreeTraverser Class

**User Story:** As a developer, I want directory-tree traversal and divide-and-conquer transfer logic in a focused class, so that this complex recursive algorithm is isolated and testable.

#### Acceptance Criteria

1. THE DirTreeTraverser SHALL encapsulate the logic currently in `_transfer_dir_tree_divide`, `_transfer_dir_tree_divide_collect`, `_initialize_dir_tree_frame`, `_finish_dir_tree_frame`, `_handle_dir_tree_iter_error`, `_handle_dir_tree_file_child`, `_handle_dir_tree_dir_child`, `_flush_dir_tree_file_batch`, `_new_dir_tree_divide_stats`, and `_build_dir_tree_divide_result` methods.
2. WHEN given a share context (containing `uk`, `share_id`, `bdstoken`, and `shared_paths`) and a target directory path, THE DirTreeTraverser SHALL produce a result dictionary with the same keys and value semantics as the current `_transfer_dir_tree_divide` method (including `success`, `partial`, `divide_path`, `message`, `transferred_files`, `transfer_failed_files`, `transfer_failed_count`, `completed_count`, `transfer_success_count`, `skipped_dir_count`, `failed_count`, `rename_failed_count`).
3. THE DirTreeTraverser SHALL accept a ProgressReporter instance during construction and use it for all progress reporting, invoking `report(level, message)` instead of raw `if progress_callback` guards.
4. THE DirTreeTraverser SHALL delegate actual file and directory transfer execution to a provided transfer executor interface that exposes at minimum: a method to execute a batch transfer plan (accepting a list of TransferItem, share credentials, and target directory) and a method to transfer a single directory group (accepting target directory, fs_id list, and share credentials).
5. THE DirTreeTraverser SHALL accept an `exclude_folder_filter` parameter and apply `should_exclude_folder` to skip matching directories, incrementing `skipped_dir_count` for each skipped directory.
6. THE DirTreeTraverser SHALL batch file children into groups of at most `TRANSFER_BATCH_SIZE` items before flushing them to the transfer executor, matching the current batching behavior in `_handle_dir_tree_file_child`.
7. IF the transfer executor raises an exception indicating a transfer-count-limit error during directory child transfer, THEN THE DirTreeTraverser SHALL push that directory onto the traversal stack for recursive divide-and-conquer processing instead of recording a failure.
8. IF creating a target directory fails (via the transfer executor or path service), THEN THE DirTreeTraverser SHALL increment `failed_count`, report an error-level message, and skip processing that directory frame without aborting the entire traversal.

### Requirement 7: Centralize handle_error_and_notify Calls

**User Story:** As a developer, I want error notification logic centralized so that the ~20 repeated `handle_error_and_notify` calls with similar arguments are reduced to a single reusable pattern.

#### Acceptance Criteria

1. THE TransferOrchestrator SHALL provide a `_notify_error(error, context_message, extra_info=None)` instance method that internally calls `handle_error_and_notify` using the instance's `self.wechat_notifier` and `self.config` as arguments, accepting an optional `collect` parameter defaulting to `True`
2. WHEN an error occurs during transfer operations, THE TransferOrchestrator SHALL call `_notify_error` instead of directly calling `handle_error_and_notify`, reducing direct call sites within the TransferOrchestrator class to zero
3. THE `_notify_error` method SHALL produce identical WeChat notifications as the current direct `handle_error_and_notify` calls, verified by asserting the same arguments are passed to `handle_error_and_notify` for each replaced call site
4. IF `extra_info` is provided to `_notify_error`, THEN THE TransferOrchestrator SHALL append the extra_info string to the context_message with a newline separator before passing it to `handle_error_and_notify`
5. WHEN `_notify_error` is called with `collect=False`, THE TransferOrchestrator SHALL pass `collect=False` to the underlying `handle_error_and_notify` call to trigger immediate notification instead of deferred collection

### Requirement 8: Reduce BaiduStorage to a Thin Orchestrator

**User Story:** As a developer, I want BaiduStorage (or its successor TransferOrchestrator) to be a thin coordination layer, so that it delegates to focused collaborators rather than containing all logic inline.

#### Acceptance Criteria

1. THE TransferOrchestrator SHALL delegate share loading to ShareLoader, candidate filtering to CandidateFilter, directory traversal to DirTreeTraverser, and result building to TransferResultBuilder
2. THE TransferOrchestrator SHALL retain responsibility for multi-share batch coordination, concurrency management, and top-level error handling while delegating all domain logic to collaborators
3. WHEN a public method of BaiduStorage is called with the same arguments and identical external service state, THE TransferOrchestrator SHALL produce identical observable behavior including the same return values, same side effects on remote storage, and same notification messages sent
4. THE TransferOrchestrator class SHALL contain no more than 500 lines of code, counted as non-blank source lines excluding import statements and docstrings
5. WHEN TransferOrchestrator is instantiated, THE TransferOrchestrator SHALL accept its collaborators (ShareLoader, CandidateFilter, DirTreeTraverser, TransferResultBuilder) via constructor parameters, so that each collaborator can be replaced with a test double independently
6. IF a collaborator raises an unhandled exception during a delegated operation, THEN THE TransferOrchestrator SHALL catch the exception at the coordination layer and propagate it through the existing top-level error handling path without exposing internal collaborator details to the caller

### Requirement 9: Preserve Backward Compatibility

**User Story:** As a developer, I want all existing tests and the CLI entry point to continue working without modification, so that the refactoring is a safe internal restructuring.

#### Acceptance Criteria

1. WHEN transfer_runner.py instantiates BaiduStorage with a cookies argument and an optional wechat_webhook argument, THE system SHALL accept the same two-parameter signature (cookies, wechat_webhook=None) and produce identical observable behavior for all public methods (transfer_share, transfer_multiple_shares, transfer_shares_from_text, is_valid, get_quota_info, set_notifier, parse_share_links_from_text)
2. THE refactored system SHALL pass all existing tests in tests/test_storage.py, tests/test_workflows.py, and tests/test_transfer_runner.py without modification to test code, producing zero test failures and zero test errors
3. WHEN external code imports module-level constants (RATE_LIMIT_WAIT_TIME, RENAME_DELAY, BATCH_SHARE_DELAY, TRANSFER_BATCH_SIZE, RENAME_CONCURRENCY, MULTI_SHARE_CONCURRENCY, TRANSFER_FAILED_RETRY_ATTEMPTS, TRANSFER_FAILED_RETRY_DELAY, STREAM_PRODUCER_JOIN_TIMEOUT, TRANSFER_PIPELINE_ENABLED) or compatibility aliases (_read_non_negative_float_env, _read_positive_int_env, _DirTreeFrame, TransferItem) from the storage module, THE system SHALL expose them at the same import path (from storage import <name>) with identical values
4. IF a deprecated import path is used, THEN THE system SHALL emit a DeprecationWarning with a message indicating the new recommended import path, and still resolve the import successfully without raising ImportError
5. WHEN legacy external code imports compatibility attributes from the storage module, THE system SHALL continue to expose those attributes; refactored internal modules (storage_rename, storage_streaming, storage_transfer_plan) SHALL import direct dependencies instead of using "import storage as storage_module"
