# DirTreeTraverser Refactor Design

## Context

The storage architecture refactor has already extracted storage constants, transfer result models, progress reporting, and candidate filtering. `storage.py` still contains the directory divide-and-conquer transfer logic. That logic is complex, stack-based, and tightly coupled to transfer execution, directory creation, share traversal, progress callbacks, and error notification.

This design covers the next incremental extraction: move only the directory-tree divide-and-conquer core into `DirTreeTraverser`, while keeping `BaiduStorage` wrappers and public behavior unchanged.

## Goals

- Add `DirTreeTraverser` in `storage_traverser.py`.
- Move directory divide-and-conquer traversal, batching, child handling, and result construction out of `BaiduStorage`.
- Preserve current result dictionaries, messages, counters, and warning/error notification behavior.
- Preserve existing `BaiduStorage` private wrapper methods used by tests and internal callers.
- Use `ProgressReporter` only in the extracted directory traversal path.
- Add `unittest` coverage without new dependencies.

## Non-Goals

- Do not move `_try_transfer_dir_fast_path()` in this step.
- Do not extract `ShareLoader` in this step.
- Do not introduce or rename `TransferOrchestrator` in this step.
- Do not change public method signatures or public result dictionaries.
- Do not change `transfer_runner.py` or CLI behavior.
- Do not replace progress callback handling outside the directory traversal path.
- Do not add Hypothesis, pytest-only behavior, or other new test dependencies.

## Architecture

`BaiduStorage` remains the top-level class. The existing fast-path logic still decides when to fall back to directory divide-and-conquer. When it calls `_transfer_dir_tree_divide()`, the wrapper delegates to `DirTreeTraverser`.

```text
BaiduStorage._try_transfer_dir_fast_path()
        |
        v
BaiduStorage._transfer_dir_tree_divide(args)  # compatibility wrapper
        |
        v
DirTreeTraverser.traverse(args)
        |
        +--> path_service.ensure_dir_exists()
        +--> share_service.iter_shared_dir_children()
        +--> transfer_executor.execute_transfer_plan()
        +--> transfer_executor.transfer_group()
        +--> ProgressReporter.report()
        +--> error_notifier(error, context_message, collect=True)
```

`DirTreeTraverser` owns traversal state and directory-tree rules. `BaiduStorage` still owns the broader orchestration flow and supplies dependencies through a small adapter.

## Components

### `DirTreeTraverser`

Location: `storage_traverser.py`

Constructor:

```python
class DirTreeTraverser:
    def __init__(
        self,
        path_service,
        share_service,
        transfer_executor,
        progress=None,
        error_notifier=None,
        batch_size=TRANSFER_BATCH_SIZE,
    ):
        self.path_service = path_service
        self.share_service = share_service
        self.transfer_executor = transfer_executor
        self.progress = progress or ProgressReporter()
        self.error_notifier = error_notifier
        self.batch_size = batch_size
```

Public traversal method:

```python
def traverse(
    self,
    shared_dir,
    target_dir,
    context,
    share_url,
    exclude_folder_filter=None,
):
    """Return the same result dictionary shape as BaiduStorage._transfer_dir_tree_divide()."""
```

Internal methods map directly to the existing `BaiduStorage` helpers:

- `new_stats()` replaces `_new_dir_tree_divide_stats()`.
- `build_result(stats)` replaces `_build_dir_tree_divide_result()`.
- `flush_file_batch(file_transfer_list, target_dir, context, share_url, stats)` replaces `_flush_dir_tree_file_batch()`.
- `initialize_frame(frame, context, stats)` replaces `_initialize_dir_tree_frame()`.
- `finish_frame(frame, context, share_url, stats)` replaces `_finish_dir_tree_frame()`.
- `handle_iter_error(frame, context, share_url, stats, exc)` replaces `_handle_dir_tree_iter_error()`.
- `handle_file_child(frame, child, context, share_url, stats)` replaces `_handle_dir_tree_file_child()`.
- `handle_dir_child(stack, frame, child, context, share_url, exclude_folder_filter, stats)` replaces `_handle_dir_tree_dir_child()`.
- `collect(shared_dir, target_dir, context, share_url, exclude_folder_filter, stats)` replaces `_transfer_dir_tree_divide_collect()`.

### Transfer executor adapter

Location: `storage.py`

`BaiduStorage` provides a small adapter around existing methods:

```python
class BaiduStorageDirTransferExecutor:
    def __init__(self, storage, progress_callback=None):
        self.storage = storage
        self.progress_callback = progress_callback

    def execute_transfer_plan(
        self,
        file_transfer_list,
        share_url,
        uk,
        share_id,
        bdstoken,
        target_dir,
    ):
        return self.storage._execute_transfer_plan(
            file_transfer_list,
            share_url,
            uk,
            share_id,
            bdstoken,
            target_dir,
            self.progress_callback,
        )

    def transfer_group(self, dir_path, fs_ids, share_url, uk, share_id, bdstoken):
        return self.storage._transfer_group(
            dir_path,
            fs_ids,
            share_url,
            uk,
            share_id,
            bdstoken,
            self.progress_callback,
        )
```

The exact adapter can be implemented as a small class or per-call object. The important boundary is that `DirTreeTraverser` calls `execute_transfer_plan()` and `transfer_group()` instead of calling `BaiduStorage` directly.

### `BaiduStorage` wrappers

Location: `storage.py`

Keep these method names available:

- `_new_dir_tree_divide_stats`
- `_build_dir_tree_divide_result`
- `_flush_dir_tree_file_batch`
- `_initialize_dir_tree_frame`
- `_finish_dir_tree_frame`
- `_handle_dir_tree_iter_error`
- `_handle_dir_tree_file_child`
- `_handle_dir_tree_dir_child`
- `_transfer_dir_tree_divide_collect`
- `_transfer_dir_tree_divide`

Wrappers may instantiate `DirTreeTraverser` and delegate. For helper wrappers that existing tests call directly, wrappers preserve the old signatures and translate `progress_callback` into `ProgressReporter(progress_callback)`.

## Data Flow

```text
BaiduStorage._transfer_dir_tree_divide(args)
        |
        v
DirTreeTraverser.traverse(args)
        |
        v
stack[DirTreeFrame]
        |
        +-- file child -> TransferItem -> frame.file_transfer_list
        |                  |
        |                  v
        |             flush_file_batch()
        |
        +-- dir child  -> excluded? skipped_dir_count++
                       -> transfer_group()
                       -> count-limit? push DirTreeFrame
                       -> fatal? failed_count++ + notify
        |
        v
build_result(stats)
```

The traversal stays iterative and stack-based. This preserves the current protection against deep directory recursion failures.

## Progress Reporting

Only directory traversal progress moves to `ProgressReporter` in this step. Message levels and text remain unchanged:

- `info`: `目录分治扫描: {shared_dir_path}`
- `info`: `目录分治扫描完成: {shared_dir_path}，处理 {child_count} 个子项`
- `info`: `跳过排除目录: {folder_name}`
- `warning`: `子目录超量，继续拆分: {folder_name}`
- `error`: `创建目录失败: {target_dir}`
- `error`: `转存子目录失败: {folder_name} - {error_message}`
- final result messages emitted by `build_result()`

`ProgressReporter` does not catch callback exceptions, preserving direct-callback behavior.

## Error Handling

Error handling preserves the current semantics:

- Directory creation failure increments `failed_count`, reports `error`, and skips that frame.
- Share directory iteration failure increments `failed_count`, flushes pending file batches when appropriate, and calls `error_notifier` with the same message used today.
- Child directory transfer count-limit errors report `warning` and push the child directory frame onto the stack for divide-and-conquer traversal.
- Other child directory transfer errors increment `failed_count`, report `error`, classify the storage error for the progress message, and call `error_notifier`.
- File batch transfer delegates to `transfer_executor.execute_transfer_plan()` and applies the current success/failure count updates.
- `build_result()` returns the same dictionary keys and message semantics as the current `_build_dir_tree_divide_result()`.

## Compatibility Requirements

- No public API changes.
- No public result shape changes.
- `_try_transfer_dir_fast_path()` remains in `BaiduStorage`.
- Existing `storage.py` private helper names remain available for current tests and internal callers.
- Existing tests in `tests/test_storage.py` continue to pass unchanged.
- `storage_streaming.py` remains unchanged.
- New code must not import `storage.py` from `storage_traverser.py`.

## Testing

Add `tests/test_storage_traverser.py` using `unittest` only.

Cover `DirTreeTraverser` directly:

- Deep directory traversal uses an explicit stack and does not rely on Python recursion.
- File children flush in batches of at most `TRANSFER_BATCH_SIZE`.
- A full file batch flushes before the child generator is exhausted.
- Child file metadata preserves `md5` in the generated `TransferItem`.
- Excluded folders are skipped and increment `skipped_dir_count`.
- Count-limit directory transfer errors push the child directory onto the traversal stack.
- Directory creation failure increments `failed_count` and returns the current failure/skipped result shape.
- Progress reporter receives scan, completion, warning, and error messages with the current text.

Keep existing storage tests unchanged. They verify `BaiduStorage` wrapper compatibility.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_traverser
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_traverser
```

Full verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p "test_*.py" -b
```

## Follow-Up Path

After this lands, the next safe extraction is `ShareLoader`, because share loading can then be separated from both candidate filtering and directory traversal. The final `TransferOrchestrator` rename remains a later step after the major collaborators are stable.
