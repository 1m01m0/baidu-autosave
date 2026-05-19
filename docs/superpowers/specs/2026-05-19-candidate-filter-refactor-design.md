# Candidate Filter Refactor Design

## Context

The `.kiro/specs/storage-architecture-refactor/` plan proposes splitting the monolithic `BaiduStorage` class into focused collaborators. The foundation step has already added `TransferItem`, `TransferResult`, `TransferResultBuilder`, `DirTreeFrame`, storage constants, and `ProgressReporter` while preserving the existing dictionary-based public API.

This design covers the next small extraction: move transfer-candidate analysis and filtering out of `storage.py` into a focused `CandidateFilter`, and use `ProgressReporter` only in that candidate-analysis path. The change keeps existing method names and return shapes available through `BaiduStorage` wrappers.

## Goals

- Add a focused `CandidateFilter` in `storage_filter.py`.
- Move candidate preparation, filtering, planned-path conflict checks, warning sampling, and summary reporting logic out of `BaiduStorage`.
- Use `ProgressReporter` for candidate-analysis progress messages.
- Preserve existing `BaiduStorage` private wrapper methods used by tests and `storage_streaming.py`.
- Preserve current public behavior, return values, summary counters, warning messages, and callback invocation order.
- Add `unittest` coverage for the extracted class without adding dependencies.

## Non-Goals

- Do not extract `DirTreeTraverser` in this step.
- Do not introduce or rename `TransferOrchestrator` in this step.
- Do not change public method signatures or public result dictionaries.
- Do not rewrite `storage_streaming.py` beyond what is required to keep its current calls working.
- Do not replace every `if progress_callback` guard in `storage.py`.
- Do not add Hypothesis, pytest-only behavior, or other new test dependencies.
- Do not move folder-exclusion logic into `CandidateFilter`; folder exclusion remains traversal/share-loading behavior for a later step.

## Architecture

`BaiduStorage` remains the orchestration class. It keeps compatibility wrappers for the existing candidate-related private methods, but each wrapper instantiates or delegates to `CandidateFilter`.

```text
storage.py / storage_streaming.py
        |
        v
BaiduStorage compatibility wrappers
        |
        v
CandidateFilter
        |
        +--> storage_rules.apply_regex_rules_detail
        +--> storage_models.TransferItem
        +--> storage_progress.ProgressReporter
```

`CandidateFilter` depends only on a path-normalization service and optional progress reporter. It does not depend on `BaiduStorage`, Baidu client state, WeChat notification state, or transfer execution code.

## Components

### `CandidateFilter`

Location: `storage_filter.py`

Constructor:

```python
class CandidateFilter:
    def __init__(self, path_service, progress=None):
        self.path_service = path_service
        self.progress = progress or ProgressReporter()
```

The broader Kiro design describes regex options as constructor configuration. This step keeps `regex_pattern` and `regex_replace` as method arguments because the current `BaiduStorage` wrappers and callers already pass them per transfer call. This avoids storing per-call state on a reusable filter object and preserves the current call signatures.

Methods:

- `candidate_parent_dirs(*paths)` returns parent directories needed for local scan narrowing.
- `prepare_candidates(shared_files_info, shared_paths, target_dir, regex_pattern=None, regex_replace=None)` returns `(candidates, summary, relative_dirs)`.
- `add_warning_sample(warning_samples, message, max_samples=5)` preserves the existing warning-sample cap.
- `is_verified_same_file(src_md5, local_md5)` preserves the existing MD5 comparison semantics.
- `existing_conflict_message(path, src_md5, local_md5, prefix)` preserves current warning text.
- `filter_planned_path_conflict(item, local_files_dict, planned_paths, summary, warning_samples)` preserves current duplicate/planned-path behavior.
- `filter_candidates_core(candidates, local_files_dict, summary, warning_samples, planned_paths=None)` returns `list[TransferItem]`.
- `report_summary(summary, warning_samples)` emits the existing summary and warning messages through `ProgressReporter`.
- `filter_candidates(candidates, local_files_dict, summary)` emits the existing step message, calls `filter_candidates_core()`, reports the summary, and returns `list[TransferItem]`.
- `build_transfer_list(shared_files_info, shared_paths, target_dir, local_files_dict, regex_pattern=None, regex_replace=None)` composes preparation and filtering for the legacy wrapper.

### `BaiduStorage` wrappers

Location: `storage.py`

Keep these method names available and route them to `CandidateFilter`:

- `_candidate_parent_dirs`
- `_prepare_transfer_candidates`
- `_add_warning_sample`
- `_is_verified_same_file`
- `_existing_conflict_message`
- `_filter_planned_path_conflict`
- `_filter_transfer_candidates_core`
- `_report_transfer_candidate_summary`
- `_filter_transfer_candidates`
- `_build_transfer_list`

`_scan_local_files_dict()` stays in `BaiduStorage` for now because it is scan/I/O oriented and not part of pure candidate filtering. Existing callers that scan first and then call `_filter_transfer_candidates_core()` continue to work.

## Data Flow

### Existing wrapper flow

```text
BaiduStorage._build_transfer_list(...)
        |
        v
CandidateFilter.prepare_candidates(...)
        |
        v
CandidateFilter.filter_candidates(...)
        |
        v
list[TransferItem]
```

### Streaming flow

`storage_streaming.py` continues to call the same methods on the `storage` object:

```text
storage._prepare_transfer_candidates(...)
storage._filter_transfer_candidates_core(...)
storage._report_transfer_candidate_summary(...)
```

Those calls still resolve through `BaiduStorage`, but the implementation is delegated to `CandidateFilter`. This keeps the streaming pipeline stable while reducing `storage.py` responsibility.

## Progress Reporting

Only the candidate-analysis path uses `ProgressReporter` in this step. Callback behavior remains identical:

- `filter_candidates()` emits `("info", "【步骤3/4】准备转存: 对比文件和准备目录")` before filtering.
- `report_summary()` emits the existing `"候选分析完成：..."` info message.
- `report_summary()` emits each warning sample as `("warning", message)` in the same order.
- Missing callbacks are no-ops through `ProgressReporter`.
- Callback exceptions propagate unchanged.

## Filtering Semantics to Preserve

- Regex filtering uses `apply_regex_rules_detail()`.
- Regex-unmatched files increment `regex_unmatched_count` and `regex_filtered_count`.
- Unsafe regex replacement targets increment `unsafe_regex_replace_count` and `regex_filtered_count`.
- `candidate_count` and `rename_candidate_count` keep their current meanings.
- Existing local paths with matching MD5 count as existing and are skipped.
- Existing local paths with missing or mismatched MD5 count as conflicts and produce warning samples.
- Rename candidates are skipped when their source path already exists locally to avoid duplicate copying.
- Rename targets that already exist are skipped or warned using the current MD5 rules.
- Planned-path conflicts within one batch keep first occurrence and skip later duplicates.
- Planned rename targets are recorded so later candidates targeting the same path are skipped.

## Error Handling

No new error handling layer is added.

- Invalid regex behavior remains whatever `apply_regex_rules_detail()` currently returns.
- Path normalization errors, callback exceptions, and unexpected programming errors propagate as they do today.
- `ProgressReporter` does not catch callback exceptions.
- Warning sampling remains bounded by the existing `max_samples=5` default.

## Testing

Add `tests/test_storage_filter.py` using `unittest` only.

Cover `CandidateFilter` directly:

- Empty input returns an empty transfer list and zero-valued summary counters.
- Regex filtering separates unmatched files from unsafe replacement targets.
- Existing same-path file with the same MD5 is skipped as already existing.
- Existing same-path file with different or missing MD5 is counted as a conflict and records a warning sample.
- Rename candidate is skipped when the source path already exists locally.
- Rename target that already exists locally preserves current skip/conflict behavior.
- Duplicate planned paths keep only the first transfer item.
- Rename targets are recorded as planned paths.
- `ProgressReporter` receives the existing step, summary, and warning messages.

Keep existing storage tests unchanged. They verify the wrapper compatibility from `BaiduStorage`.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_filter
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_filter
```

Full verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p "test_*.py" -b
```

## Compatibility Requirements

- No public API changes.
- No public return type changes.
- No changes required in `transfer_runner.py`.
- Existing imports from `storage.py` continue to work.
- Existing tests pass unchanged except for the new `tests/test_storage_filter.py`.
- `storage_streaming.py` continues using the current method calls on the `storage` object.

## Follow-Up Path

After this lands, the next safe refactor is `DirTreeTraverser`. It can use the already extracted `TransferItem`, `DirTreeFrame`, `TransferResultBuilder`, `ProgressReporter`, and the smaller candidate-filtering boundary created here.
