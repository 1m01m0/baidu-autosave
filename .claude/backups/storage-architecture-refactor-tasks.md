# Implementation Plan: Storage Architecture Refactor

## Overview

Incrementally extract focused collaborator classes from the monolithic BaiduStorage God class, wiring them together via a thin TransferOrchestrator. Each extraction step is validated against the existing test suite to ensure backward compatibility throughout.

## Tasks

- [ ] 1. Extract StorageConstants module and fix import coupling
  - [ ] 1.1 Create `storage_constants.py` with all environment-derived constants
    - Move RATE_LIMIT_WAIT_TIME, RENAME_DELAY, RENAME_CONCURRENCY, BATCH_SHARE_DELAY, MULTI_SHARE_CONCURRENCY, TRANSFER_BATCH_SIZE, TRANSFER_FAILED_RETRY_ATTEMPTS, TRANSFER_FAILED_RETRY_DELAY, STREAM_PRODUCER_JOIN_TIMEOUT, TRANSFER_PIPELINE_ENABLED from storage.py
    - Import only from `env_utils` and stdlib
    - _Requirements: 3.1, 3.3, 3.4_

  - [ ] 1.2 Update `storage.py` to re-export constants from `storage_constants`
    - Replace local constant definitions with imports from `storage_constants`
    - Ensure `from storage import RATE_LIMIT_WAIT_TIME` etc. still works
    - _Requirements: 3.6, 9.3_

  - [ ] 1.3 Update `storage_transfer_plan.py` to import from `storage_constants` and `utils` directly
    - Replace `import storage as storage_module` with direct imports
    - Import constants from `storage_constants`, `handle_error_and_notify` from `utils`, `time` from stdlib
    - _Requirements: 4.1, 4.2_

  - [ ] 1.4 Update `storage_streaming.py` to import from `storage_constants`, `utils`, and `logger` directly
    - Replace `import storage as storage_module` with direct imports
    - Import constants from `storage_constants`, `handle_error_and_notify` from `utils`, `get_logger` from `logger`
    - _Requirements: 4.3_

  - [ ] 1.5 Update `storage_rename.py` to import from `storage_constants`, `storage_errors`, `utils` directly
    - Replace `import storage as storage_module` with direct imports
    - Import constants from `storage_constants`, `classify_storage_error` from `storage_errors`, `handle_error_and_notify` from `utils`
    - _Requirements: 4.4_

  - [ ]* 1.6 Write property test for StorageConstants environment-value equivalence
    - **Property 4: StorageConstants environment-value equivalence**
    - Use Hypothesis to generate env var dicts, monkeypatch os.environ, reimport both modules, compare values
    - **Validates: Requirements 3.5**

- [ ] 2. Checkpoint - Ensure all existing tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 3. Implement TransferResult dataclass and TransferResultBuilder
  - [ ] 3.1 Add TransferResult frozen dataclass to `storage_models.py`
    - Fields: success, partial, message, transferred_files, failed_files, skipped_count, error_details
    - Implement `__post_init__` validation (success=True + non-empty failed_files → ValueError)
    - Implement `to_dict()` and `from_dict()` class method
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6_

  - [ ] 3.2 Create TransferResultBuilder class in `storage_models.py`
    - Implement builder methods: add_transferred, add_failed, set_skipped, set_message, set_error
    - Implement `build()` method returning a TransferResult instance
    - _Requirements: 1.2_

  - [ ]* 3.3 Write property test for TransferResult serialization round trip
    - **Property 1: TransferResult serialization round trip**
    - Use Hypothesis `@composite` strategy to generate valid TransferResult instances
    - Verify `from_dict(to_dict(x)) == x` for all generated instances
    - **Validates: Requirements 1.4**

  - [ ]* 3.4 Write unit tests for TransferResult and TransferResultBuilder
    - Test construction with defaults
    - Test `__post_init__` ValueError on inconsistent state
    - Test `from_dict()` KeyError on missing keys
    - Test builder produces correct TransferResult
    - _Requirements: 1.1, 1.2, 1.5, 1.6_

- [ ] 4. Implement ProgressReporter
  - [ ] 4.1 Create ProgressReporter class in a new `storage_progress.py` module
    - Accept optional callback `(level: str, message: str) -> None`
    - Implement `report(level, message)` method: invoke callback if present, no-op otherwise
    - Propagate callback exceptions without catching
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6_

  - [ ]* 4.2 Write property tests for ProgressReporter
    - **Property 2: ProgressReporter null-object forwarding**
    - **Property 3: ProgressReporter exception propagation**
    - Use Hypothesis to generate (level, message) pairs and exception types
    - **Validates: Requirements 2.2, 2.3, 2.6**

- [ ] 5. Implement CandidateFilter
  - [ ] 5.1 Create CandidateFilter class in a new `storage_filter.py` module
    - Accept regex_pattern, regex_replace, exclude_folder_filter at construction
    - Extract logic from `_filter_transfer_candidates`, `_filter_transfer_candidates_core`, `_filter_planned_path_conflict`
    - Return `(list[TransferItem], Counter)` from `filter()` method
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 5.2 Write property test for CandidateFilter correct filtering
    - **Property 5: CandidateFilter correct filtering**
    - Generate candidate lists and local_files dicts with Hypothesis
    - Verify output invariants: items not in local_files, len matches counter
    - **Validates: Requirements 5.1, 5.2**

  - [ ]* 5.3 Write property test for CandidateFilter deduplication invariant
    - **Property 6: CandidateFilter deduplication invariant**
    - Generate lists with deliberate path collisions
    - Verify first-occurrence retention and existing_count increment
    - **Validates: Requirements 5.6**

- [ ] 6. Implement DirTreeTraverser
  - [ ] 6.1 Create DirTreeTraverser class in a new `storage_traverser.py` module
    - Extract logic from `_transfer_dir_tree_divide` and related helper methods
    - Accept transfer executor interface, StoragePathService, ProgressReporter, exclude_folder_filter, batch_size
    - Implement `traverse(share_context, target_dir)` returning result dict
    - Handle count-limit errors by pushing to stack; handle dir creation failures gracefully
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [ ]* 6.2 Write property test for DirTreeTraverser batch size invariant
    - **Property 7: DirTreeTraverser batch size invariant**
    - Generate file lists of varying sizes, verify batch constraints
    - **Validates: Requirements 6.6**

  - [ ]* 6.3 Write property test for DirTreeTraverser excluded folder skipping
    - **Property 8: DirTreeTraverser excluded folder skipping**
    - Generate directory trees with exclusion patterns, verify skipping and count
    - **Validates: Requirements 6.5**

  - [ ]* 6.4 Write unit tests for DirTreeTraverser
    - Test count-limit error handling (push to stack)
    - Test directory creation failure (increment failed_count, continue)
    - Test empty directory traversal
    - _Requirements: 6.7, 6.8_

- [ ] 7. Checkpoint - Ensure all existing tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Extract ShareLoader and wire TransferOrchestrator
  - [ ] 8.1 Create ShareLoader class in a new `storage_loader.py` module
    - Accept SharedPathService instance
    - Implement `load(share_url, pwd)` returning share context dict
    - _Requirements: 8.1_

  - [ ] 8.2 Add `_notify_error` method to the orchestrator class
    - Implement `_notify_error(error, context_message, extra_info=None, collect=True)`
    - Append extra_info to context_message with newline separator
    - Forward to `handle_error_and_notify` with self.wechat_notifier and self.config
    - _Requirements: 7.1, 7.4, 7.5_

  - [ ] 8.3 Replace all direct `handle_error_and_notify` calls in storage.py with `_notify_error`
    - Locate all ~20 call sites and convert them
    - Ensure identical notification arguments are passed
    - _Requirements: 7.2, 7.3_

  - [ ] 8.4 Refactor BaiduStorage into TransferOrchestrator with collaborator injection
    - Rename class to TransferOrchestrator
    - Add constructor parameters for ShareLoader, CandidateFilter, DirTreeTraverser, TransferResultBuilder
    - Delegate share loading, filtering, traversal, and result building to collaborators
    - Create `BaiduStorage = TransferOrchestrator` alias for backward compatibility
    - Target: ≤500 non-blank source lines in TransferOrchestrator
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [ ]* 8.5 Write unit tests for TransferOrchestrator
    - Test `_notify_error` argument forwarding
    - Test collaborator injection via constructor
    - Test that BaiduStorage alias works
    - _Requirements: 7.1, 8.5, 9.1_

- [ ] 9. Ensure backward compatibility
  - [ ] 9.1 Verify and fix all backward-compatible imports in `storage.py`
    - Ensure `from storage import BaiduStorage, RATE_LIMIT_WAIT_TIME, _DirTreeFrame, TransferItem` works
    - Ensure `_read_non_negative_float_env`, `_read_positive_int_env` aliases exist
    - Add DeprecationWarning for deprecated import paths if applicable
    - _Requirements: 9.1, 9.3, 9.4, 9.5_

  - [ ] 9.2 Verify no `import storage as storage_module` remains in refactored modules
    - Check storage_transfer_plan.py, storage_streaming.py, storage_rename.py
    - Run lint check to confirm no violations
    - _Requirements: 4.5, 4.6_

- [ ] 10. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation against the existing test suite
- Property tests use Hypothesis library and validate universal correctness properties from the design
- Unit tests validate specific examples and edge cases
- The refactoring is intentionally incremental: constants extraction first (breaking coupling), then data models, then collaborators, then wiring

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5"] },
    { "id": 2, "tasks": ["1.6", "3.1", "4.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "4.2"] },
    { "id": 4, "tasks": ["5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "6.1"] },
    { "id": 6, "tasks": ["6.2", "6.3", "6.4"] },
    { "id": 7, "tasks": ["8.1", "8.2"] },
    { "id": 8, "tasks": ["8.3", "8.4"] },
    { "id": 9, "tasks": ["8.5", "9.1", "9.2"] }
  ]
}
```
