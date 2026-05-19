# Design Document: Storage Architecture Refactor

## Overview

This design decomposes the monolithic `BaiduStorage` class (~2000 lines) into focused collaborator classes, each with a single responsibility. The refactoring follows an incremental extraction strategy: new classes are introduced alongside the existing code, wired together via a thin orchestrator, and validated against the full existing test suite at each step.

The key architectural change is moving from a God-class pattern (BaiduStorage does everything) to a composition pattern (TransferOrchestrator delegates to focused collaborators). This enables independent testing, clearer dependency graphs, and removal of the problematic `import storage as storage_module` coupling.

## Architecture

```mermaid
graph TD
    CLI[transfer_runner.py] --> TO[TransferOrchestrator]
    TO --> SL[ShareLoader]
    TO --> CF[CandidateFilter]
    TO --> DTT[DirTreeTraverser]
    TO --> TRB[TransferResultBuilder]
    TO --> PR[ProgressReporter]
    TO --> NE[_notify_error]

    DTT --> TE[Transfer Executor Interface]
    TE --> STP[storage_transfer_plan]
    TE --> SS[storage_streaming]

    CF --> SR[storage_rules]
    SL --> SPS[SharedPathService]
    DTT --> SPS
    TO --> PAS[StoragePathService]

    SC[storage_constants.py] --> STP
    SC --> SS
    SC --> RN[storage_rename]

    subgraph External Dependencies
        BC[BaiduClientAdapter]
        WN[WeChatNotifier]
    end

    TO --> BC
    TO --> WN
```

**Dependency flow** (acyclic):
- `storage_constants.py` ← `env_utils.py` ← stdlib only
- `storage_transfer_plan.py` ← `storage_constants`, `storage_errors`, `utils`
- `storage_streaming.py` ← `storage_constants`, `storage_errors`, `utils`, `logger`
- `storage_rename.py` ← `storage_constants`, `storage_errors`, `utils`
- `storage.py` (TransferOrchestrator) ← all of the above + collaborators

## Components and Interfaces

### 1. StorageConstants Module (`storage_constants.py`)

A new module containing all environment-derived constants, breaking the circular dependency.

```python
# storage_constants.py
from env_utils import read_non_negative_float_env, read_positive_int_env

RATE_LIMIT_WAIT_TIME = 10
RENAME_DELAY = read_non_negative_float_env("TRANSFERSHARE_RENAME_DELAY", 0)
RENAME_CONCURRENCY = read_positive_int_env("TRANSFERSHARE_RENAME_CONCURRENCY", 1)
BATCH_SHARE_DELAY = read_non_negative_float_env("TRANSFERSHARE_BATCH_SHARE_DELAY", 0)
MULTI_SHARE_CONCURRENCY = read_positive_int_env("TRANSFERSHARE_MULTI_SHARE_CONCURRENCY", 1)
TRANSFER_BATCH_SIZE = read_positive_int_env("TRANSFERSHARE_TRANSFER_BATCH_SIZE", 999)
TRANSFER_FAILED_RETRY_ATTEMPTS = read_positive_int_env("TRANSFERSHARE_TRANSFER_FAILED_RETRY_ATTEMPTS", 2)
TRANSFER_FAILED_RETRY_DELAY = read_non_negative_float_env("TRANSFERSHARE_TRANSFER_FAILED_RETRY_DELAY", 5)
STREAM_PRODUCER_JOIN_TIMEOUT = read_non_negative_float_env("TRANSFERSHARE_STREAM_PRODUCER_JOIN_TIMEOUT", 5)
TRANSFER_PIPELINE_ENABLED = read_positive_int_env("TRANSFERSHARE_TRANSFER_PIPELINE", 1) >= 1
```

### 2. TransferResult Dataclass (`storage_models.py`)

```python
@dataclass(frozen=True)
class TransferResult:
    success: bool
    partial: bool = False
    message: str = ""
    transferred_files: list = field(default_factory=list)
    failed_files: list = field(default_factory=list)
    skipped_count: int = 0
    error_details: Optional[str] = None

    def __post_init__(self):
        if self.success and self.failed_files:
            raise ValueError("Inconsistent state: success=True with non-empty failed_files")

    def to_dict(self) -> dict: ...
    
    @classmethod
    def from_dict(cls, d: dict) -> "TransferResult": ...
```

### 3. TransferResultBuilder

```python
class TransferResultBuilder:
    def __init__(self):
        self._transferred = []
        self._failed = []
        self._skipped = 0
        self._message = ""
        self._error_details = None

    def add_transferred(self, file_info: dict) -> "TransferResultBuilder": ...
    def add_failed(self, file_info: dict) -> "TransferResultBuilder": ...
    def set_skipped(self, count: int) -> "TransferResultBuilder": ...
    def set_message(self, msg: str) -> "TransferResultBuilder": ...
    def set_error(self, details: str) -> "TransferResultBuilder": ...
    def build(self) -> TransferResult: ...
```

### 4. ProgressReporter (Null-Object Pattern)

```python
class ProgressReporter:
    def __init__(self, callback: Optional[Callable[[str, str], None]] = None):
        self._callback = callback

    def report(self, level: str, message: str) -> None:
        if self._callback is not None:
            self._callback(level, message)
```

### 5. CandidateFilter

```python
class CandidateFilter:
    def __init__(
        self,
        regex_pattern: Optional[str] = None,
        regex_replace: Optional[str] = None,
        exclude_folder_filter: Optional[list] = None,
    ): ...

    def filter(
        self,
        candidates: list,
        local_files: dict,
    ) -> tuple[list[TransferItem], Counter]:
        """Returns (transfer_items, summary_counter).
        
        Summary counter keys: existing_count, conflict_count, 
        transfer_needed_count, rename_needed_count.
        """
        ...
```

### 6. DirTreeTraverser

```python
class DirTreeTraverser:
    def __init__(
        self,
        transfer_executor,      # interface with execute_batch() and transfer_directory()
        path_service: StoragePathService,
        progress: ProgressReporter,
        exclude_folder_filter: Optional[list] = None,
        batch_size: int = TRANSFER_BATCH_SIZE,
    ): ...

    def traverse(
        self,
        share_context: dict,
        target_dir: str,
    ) -> dict:
        """Returns result dict with keys matching _transfer_dir_tree_divide."""
        ...
```

### 7. ShareLoader

```python
class ShareLoader:
    def __init__(self, share_service: SharedPathService): ...

    def load(self, share_url: str, pwd: Optional[str] = None) -> dict:
        """Returns share context dict with uk, share_id, bdstoken, shared_paths."""
        ...
```

### 8. TransferOrchestrator (replaces BaiduStorage)

```python
class TransferOrchestrator:
    def __init__(
        self,
        cookies: str,
        wechat_webhook: Optional[str] = None,
        *,
        share_loader: Optional[ShareLoader] = None,
        candidate_filter: Optional[CandidateFilter] = None,
        dir_tree_traverser: Optional[DirTreeTraverser] = None,
        result_builder: Optional[TransferResultBuilder] = None,
    ): ...

    def transfer_share(self, share_url, ...): ...
    def transfer_multiple_shares(self, share_configs, ...): ...
    # All other public methods preserved...
    
    def _notify_error(self, error, context_message, extra_info=None, collect=True): ...
```

`BaiduStorage` becomes an alias for `TransferOrchestrator` for backward compatibility.

## Data Models

### TransferResult

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| success | bool | (required) | Whether the overall operation succeeded |
| partial | bool | False | Whether some files transferred but others failed |
| message | str | "" | Human-readable status message |
| transferred_files | list[dict] | [] | List of successfully transferred file info dicts |
| failed_files | list[dict] | [] | List of files that failed to transfer |
| skipped_count | int | 0 | Number of files skipped (already exist, filtered) |
| error_details | str \| None | None | Error description when success=False |

### to_dict() Key Mapping

| TransferResult field | Dict key |
|---------------------|----------|
| success | "success" |
| partial | "partial" |
| message | "message" |
| transferred_files | "transferred_files" |
| failed_files | "transfer_failed_files" |
| len(failed_files) | "transfer_failed_count" |
| (derived) | "rename_failed_files" |
| (derived) | "rename_failed_count" |
| (derived) | "completed_count" |
| (derived) | "transfer_success_count" |
| error_details | "error" (only when not None) |

### TransferItem (existing, unchanged)

Frozen dataclass with fields: `fs_id`, `dir_path`, `clean_path`, `final_path`, `need_rename`, `src_md5`.

### DirTreeFrame (existing, unchanged)

Mutable dataclass used as a stack frame during directory traversal.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: TransferResult serialization round trip

*For any* valid TransferResult instance (where success/failed_files are consistent), calling `to_dict()` then `from_dict()` on the resulting dictionary SHALL produce a TransferResult where all fields compare equal to the original instance.

**Validates: Requirements 1.4**

### Property 2: ProgressReporter null-object forwarding

*For any* `(level, message)` pair where level is one of "info", "warning", "error", "success" and message is any string: when constructed with a callback, `report(level, message)` SHALL invoke the callback with exactly `(level, message)`; when constructed without a callback, `report(level, message)` SHALL complete without error or side effects.

**Validates: Requirements 2.2, 2.3**

### Property 3: ProgressReporter exception propagation

*For any* exception type raised by the provided callback during a `report()` invocation, the ProgressReporter SHALL propagate that exact exception to the caller without catching or wrapping it.

**Validates: Requirements 2.6**

### Property 4: StorageConstants environment-value equivalence

*For any* set of environment variable values applied to the process, importing `storage_constants` SHALL produce identical constant values as the current `storage.py` module-level definitions would produce for the same environment state.

**Validates: Requirements 3.5**

### Property 5: CandidateFilter correct filtering

*For any* list of transfer candidates and local files dictionary, the CandidateFilter SHALL return a tuple of (transfer_items, summary) where: transfer_items contains only items not present in local_files (by normalized path), summary is a Counter with keys existing_count, conflict_count, transfer_needed_count, rename_needed_count, and `len(transfer_items) == summary["transfer_needed_count"]`.

**Validates: Requirements 5.1, 5.2**

### Property 6: CandidateFilter deduplication invariant

*For any* list of candidates where multiple items resolve to the same normalized path, the CandidateFilter SHALL retain only the first occurrence in the output list and increment existing_count for each subsequent duplicate.

**Validates: Requirements 5.6**

### Property 7: DirTreeTraverser batch size invariant

*For any* directory containing N file children, the DirTreeTraverser SHALL flush them to the transfer executor in batches where each batch contains at most `TRANSFER_BATCH_SIZE` items, and the total number of items across all batches equals N.

**Validates: Requirements 6.6**

### Property 8: DirTreeTraverser excluded folder skipping

*For any* directory tree and exclude_folder_filter list, the DirTreeTraverser SHALL never process (descend into or transfer) any directory whose name matches the exclusion filter, and SHALL increment skipped_dir_count by exactly the number of matched directories encountered.

**Validates: Requirements 6.5**

## Error Handling

### Strategy by Layer

| Layer | Error Handling |
|-------|---------------|
| TransferOrchestrator | Catches all collaborator exceptions; routes through `_notify_error`; returns error TransferResult |
| CandidateFilter | Pure logic — raises ValueError for invalid construction args; no I/O errors possible |
| DirTreeTraverser | Catches transfer executor errors; distinguishes count-limit (retry via stack push) from fatal (increment failed_count, continue) |
| ProgressReporter | Propagates callback exceptions without catching |
| TransferResult | Raises ValueError on inconsistent construction (success=True + failed_files) |
| TransferResultBuilder | Raises ValueError on build() if state is inconsistent |

### _notify_error Pattern

```python
def _notify_error(self, error, context_message, extra_info=None, collect=True):
    msg = context_message
    if extra_info:
        msg = f"{context_message}\n{extra_info}"
    handle_error_and_notify(error, msg, self.wechat_notifier, self.config, collect=collect)
```

This replaces ~20 scattered `handle_error_and_notify(e, msg, self.wechat_notifier, self.config)` calls.

## Testing Strategy

### Property-Based Tests (using Hypothesis)

Each correctness property maps to a single Hypothesis test with `@given(...)` and minimum 100 examples:

1. **TransferResult round trip** — Generate arbitrary TransferResult via `@composite` strategy, verify `from_dict(to_dict(x)) == x`
2. **ProgressReporter forwarding** — Generate random `(level, message)` pairs, verify callback invocation
3. **ProgressReporter exception propagation** — Generate random exceptions, verify propagation
4. **StorageConstants equivalence** — Parametrize with env var dicts, compare values
5. **CandidateFilter filtering** — Generate candidate lists + local_files dicts, verify output invariants
6. **CandidateFilter deduplication** — Generate lists with deliberate path collisions, verify first-wins
7. **DirTreeTraverser batching** — Generate file lists of varying sizes, verify batch size invariant
8. **DirTreeTraverser folder exclusion** — Generate directory trees with exclusion patterns, verify skipping

Tag format: `# Feature: storage-architecture-refactor, Property N: <property text>`

### Unit Tests (example-based, pytest)

- TransferResult construction with defaults
- TransferResult `__post_init__` ValueError on invalid state
- TransferResult `from_dict()` KeyError on missing keys
- ProgressReporter construction with/without callback
- CandidateFilter with empty input returns empty output
- DirTreeTraverser handles count-limit error by pushing to stack
- DirTreeTraverser handles directory creation failure gracefully
- TransferOrchestrator `_notify_error` argument forwarding
- TransferOrchestrator collaborator injection via constructor
- Backward compatibility: `from storage import BaiduStorage, RATE_LIMIT_WAIT_TIME, _DirTreeFrame`

### Integration Tests

- Full transfer_share flow with mock BaiduClientAdapter produces same result dict as before refactoring
- All existing tests in test_storage.py, test_workflows.py, test_transfer_runner.py pass unchanged
- `import storage as storage_module` pattern still works (backward compat window)

### Smoke Tests

- StorageConstants module imports without error
- No `import storage as storage_module` in refactored sub-modules
- TransferOrchestrator class body is ≤ 500 non-blank source lines
- All named constants importable from `storage` module

### Test Configuration

- PBT library: **Hypothesis** (standard for Python property-based testing)
- Minimum iterations: 100 per property (Hypothesis default `max_examples=100`)
- Each property test tagged with: `# Feature: storage-architecture-refactor, Property {N}: {text}`
