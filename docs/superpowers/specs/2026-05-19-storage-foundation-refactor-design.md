# Storage Foundation Refactor Design

## Context

`storage.py` still contains the main `BaiduStorage` orchestration class and many transfer-specific helpers. Earlier work has already extracted constants and several helper modules, but future extraction of filtering, traversal, and orchestration logic needs stable foundation types first.

This design covers a small, low-risk step: add structured transfer-result and progress-reporting primitives while preserving the existing public dictionary-based API.

## Goals

- Add a typed `TransferResult` model for transfer outcomes.
- Add a `TransferResultBuilder` for incremental result construction.
- Add a `ProgressReporter` null-object wrapper around optional progress callbacks.
- Preserve all current public return values and call signatures.
- Use only existing `unittest`-based testing; do not add new test dependencies.

## Non-Goals

- Do not extract `CandidateFilter` in this step.
- Do not extract `DirTreeTraverser` in this step.
- Do not rename `BaiduStorage` or introduce `TransferOrchestrator` in this step.
- Do not change public methods to return dataclasses.
- Do not perform a broad replacement of every `if progress_callback` guard.

## Components

### TransferResult

Location: `storage_models.py`

`TransferResult` is a frozen dataclass with these fields:

- `success: bool`
- `partial: bool = False`
- `message: str = ""`
- `transferred_files: list = field(default_factory=list)`
- `failed_files: list = field(default_factory=list)`
- `skipped_count: int = 0`
- `error_details: str | None = None`

Rules:

- `success=True` with non-empty `failed_files` is invalid and raises `ValueError` in `__post_init__`.
- `to_dict()` returns the existing result dictionary shape used by current storage code.
- `from_dict()` reconstructs a `TransferResult` from that dictionary shape.
- `from_dict()` raises `KeyError` when required keys are absent.

Dictionary output keys:

- `success`
- `partial`
- `message`
- `transferred_files`
- `transfer_failed_files`
- `transfer_failed_count`
- `rename_failed_files`, derived as an empty list in this foundation step
- `rename_failed_count`, derived as `0` in this foundation step
- `completed_count`
- `transfer_success_count`
- `skipped_count`
- `error`, only when `error_details` is not `None`

### TransferResultBuilder

Location: `storage_models.py`

`TransferResultBuilder` provides chainable methods:

- `add_transferred(file_info)`
- `add_failed(file_info)`
- `set_skipped(count)`
- `set_message(message)`
- `set_error(details)`
- `set_partial(partial)`
- `build()`

Default success inference:

- If `error_details` is set, the result is unsuccessful.
- If `failed_files` is non-empty and `partial` is false, the result is unsuccessful.
- If `partial` is true, the result is partial and unsuccessful, while still carrying transferred and failed file lists.
- Otherwise the result is successful.

### ProgressReporter

Location: `storage_progress.py`

`ProgressReporter` wraps the existing optional callback pattern:

- Constructor accepts `callback=None`.
- `report(level, message)` does nothing when callback is missing.
- `report(level, message)` calls the callback with exactly the same positional arguments when callback exists.
- Callback exceptions are not caught or suppressed.

This preserves the behavior of direct callback invocation while allowing later code to remove repeated `if progress_callback` checks safely.

## Integration Strategy

This step should keep public behavior unchanged.

- Existing public `BaiduStorage` methods continue returning dictionaries.
- `TransferResult` may be used internally only at low-risk result construction points.
- If an internal method uses `TransferResult`, it must convert back through `to_dict()` before returning to existing callers.
- The first implementation can add the models and tests without wiring them deeply into the transfer flow.

This approach creates reusable primitives for later refactors without increasing the current blast radius.

## Error Handling

- `TransferResult.__post_init__` validates only obvious inconsistent state.
- `TransferResult.from_dict()` fails fast with `KeyError` for missing required keys.
- `ProgressReporter` intentionally propagates callback exceptions because the existing direct-callback style would do the same.

## Testing

Add `unittest` coverage without new dependencies.

### `tests/test_storage_models.py`

Cover:

- `TransferResult` default and explicit fields.
- `success=True` plus failed files raises `ValueError`.
- `to_dict()` emits the compatible result shape.
- `from_dict()` reconstructs the model from compatible dictionaries.
- `from_dict()` raises `KeyError` on missing required keys.
- `TransferResultBuilder` builds success, failure, and partial results.
- Builder chain methods return the builder instance.

### `tests/test_storage_progress.py`

Cover:

- Missing callback is a no-op.
- Existing callback receives exactly `(level, message)`.
- Callback exceptions propagate unchanged.

## Verification

Run targeted tests first:

```bash
python -m unittest tests.test_storage_models tests.test_storage_progress
```

Then run the full existing test suite:

```bash
python -m unittest discover -s tests -p "test_*.py" -b
```

## Compatibility Requirements

- No public return type changes.
- No public method signature changes.
- No new runtime or test dependencies.
- Existing imports from `storage_models.py` continue to work.
- Existing tests should pass unchanged except for newly added tests.

## Follow-Up Path

After this foundation lands, the next safe refactor is to extract `CandidateFilter`, because it can use `TransferItem`, `TransferResultBuilder`, and later `ProgressReporter` without changing public behavior.
