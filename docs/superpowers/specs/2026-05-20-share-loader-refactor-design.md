# ShareLoader Refactor Design

## Context

The storage architecture refactor has already extracted storage constants, transfer result models, progress reporting, candidate filtering, and directory divide-and-conquer traversal. `BaiduStorage` still owns share-loading helpers that access a share link, build the share context, and optionally load the full shared file list.

This design covers the next incremental extraction: move only the share-entry and shared-file-list loading wrappers into `ShareLoader`, while preserving `BaiduStorage` wrapper methods and public behavior.

## Goals

- Add `ShareLoader` in `storage_loader.py`.
- Move `_load_share_entries()`, `_load_share_files()`, and `_load_share_context()` logic out of `BaiduStorage`.
- Preserve current progress messages, error notification behavior, return dictionaries, and exception propagation.
- Preserve existing `BaiduStorage` private wrapper methods used by tests and internal callers.
- Add focused `unittest` coverage without new dependencies.

## Non-Goals

- Do not change `transfer_share()` public signature or public result dictionaries.
- Do not change `get_share_folder_name()` in this step.
- Do not change `storage_streaming.py` or its producer behavior.
- Do not change `SharedPathService` traversal/listing behavior.
- Do not introduce or rename `TransferOrchestrator` in this step.
- Do not centralize `_notify_error` in this step.
- Do not replace progress callback handling outside the share-loading wrapper path.
- Do not add Hypothesis, pytest-only behavior, or other new test dependencies.

## Architecture

`BaiduStorage` remains the top-level coordinator. Existing public flows continue to call `BaiduStorage._load_share_entries()`, `_load_share_files()`, and `_load_share_context()`. Those wrappers instantiate `ShareLoader` and delegate.

```text
BaiduStorage.transfer_share()
        |
        v
BaiduStorage._load_share_entries(args)  # compatibility wrapper
        |
        v
ShareLoader.load_entries(args)
        |
        +--> share_service.load_shared_paths()
        +--> ProgressReporter.report()
        +--> error_notifier(error, context_message, collect=True)

BaiduStorage._load_share_files(args)    # compatibility wrapper
        |
        v
ShareLoader.load_files(args)
        |
        +--> share_service.list_shared_files()
        +--> ProgressReporter.report()
```

`ShareLoader` owns share-loading rules. `BaiduStorage` supplies `share_service`, progress reporting, and error notification through a small factory.

## Components

### `ShareLoader`

Location: `storage_loader.py`

Constructor:

```python
class ShareLoader:
    def __init__(self, share_service, progress=None, error_notifier=None):
        self.share_service = share_service
        self.progress = progress or ProgressReporter()
        self.error_notifier = error_notifier
```

Public methods:

```python
def load_entries(self, share_url, pwd=None):
    """Return the same base context shape as BaiduStorage._load_share_entries()."""


def load_files(self, context, folder_filter=None, exclude_folder_filter=None):
    """Return a copied context with shared_files_info added."""


def load_context(
    self,
    share_url,
    pwd=None,
    folder_filter=None,
    exclude_folder_filter=None,
):
    """Return the same context shape as BaiduStorage._load_share_context()."""
```

### `BaiduStorage` factory and wrappers

Location: `storage.py`

`BaiduStorage` adds a small factory:

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
```

Keep these method names and signatures available:

- `_load_share_entries(share_url, pwd, progress_callback=None)`
- `_load_share_files(context, folder_filter, progress_callback=None, exclude_folder_filter=None)`
- `_load_share_context(share_url, pwd, folder_filter, progress_callback=None, exclude_folder_filter=None)`

Wrappers delegate to `ShareLoader`. Existing tests and internal code can still monkeypatch the wrappers because `transfer_share()` continues to call `BaiduStorage` methods, not `ShareLoader` directly.

## Data Flow

### `load_entries()`

```text
share_url, pwd
    |
    +-- mask share URL for progress text
    +-- report info: 【步骤1/4】访问分享链接: {masked_share_url}
    +-- if pwd: report info: 使用密码访问分享链接
    +-- shared_paths = share_service.load_shared_paths(share_url, pwd)
    |
    +-- empty shared_paths
    |       +-- error_notifier(ValueError("获取分享文件列表失败"), "获取分享文件列表失败", collect=True)
    |       +-- return None
    |
    +-- return {
            "shared_paths": shared_paths,
            "uk": shared_paths[0].uk,
            "share_id": shared_paths[0].share_id,
            "bdstoken": shared_paths[0].bdstoken,
        }
```

### `load_files()`

```text
context, folder_filter, exclude_folder_filter
    |
    +-- report info: 开始获取共享文件列表
    +-- shared_files_info = share_service.list_shared_files(
            context["shared_paths"],
            folder_filter,
            progress.report,
            exclude_folder_filter=exclude_folder_filter,
        )
    +-- report info: 获取到 {len(shared_files_info)} 个共享文件
    +-- copied_context = dict(context)
    +-- copied_context["shared_files_info"] = shared_files_info
    +-- return copied_context
```

### `load_context()`

```text
load_entries(share_url, pwd)
    |
    +-- None -> return None
    |
    v
load_files(context, folder_filter, exclude_folder_filter)
```

## Progress Reporting

Only the share-loading wrapper path moves to `ProgressReporter` in this step. Message text and ordering remain unchanged:

- `info`: `【步骤1/4】访问分享链接: {masked_share_url}`
- `info`: `使用密码访问分享链接`
- `info`: `开始获取共享文件列表`
- `info`: `获取到 {len(shared_files_info)} 个共享文件`

`ShareLoader` passes `self.progress.report` to `share_service.list_shared_files()`, preserving existing positional callback behavior. `ProgressReporter` does not catch callback exceptions.

## Error Handling

Error handling preserves current semantics:

- Empty `shared_paths` creates `ValueError("获取分享文件列表失败")`, calls `error_notifier(error, "获取分享文件列表失败", collect=True)`, and returns `None`.
- Exceptions raised by `share_service.load_shared_paths()` propagate to the caller.
- Exceptions raised by `share_service.list_shared_files()` propagate to the caller.
- `load_context()` returns `None` when `load_entries()` returns `None`, and does not call `load_files()`.
- Error notification centralization is out of scope for this step and stays in Kiro task 7.

## Compatibility Requirements

- No public API changes.
- `transfer_share()` keeps calling `BaiduStorage._load_share_entries()` and therefore existing wrapper monkeypatch behavior remains valid.
- `_load_share_files()` and `_load_share_context()` keep their old signatures and return shapes.
- `get_share_folder_name()` remains unchanged, including its direct `share_service.load_shared_paths()` call.
- `storage_streaming.py` remains unchanged.
- `SharedPathService` remains unchanged.
- `storage_loader.py` must not import `storage.py`.

## Testing

Add `tests/test_storage_loader.py` using `unittest` only.

Cover `ShareLoader` directly:

- `load_entries()` returns `shared_paths`, `uk`, `share_id`, and `bdstoken` on success.
- `load_entries()` reports masked share URL progress.
- `load_entries()` reports password progress when `pwd` is provided.
- `load_entries()` returns `None` and calls `error_notifier(ValueError("获取分享文件列表失败"), "获取分享文件列表失败", collect=True)` when no shared paths are returned.
- `load_files()` calls `share_service.list_shared_files()` with `context["shared_paths"]`, `folder_filter`, `progress.report`, and `exclude_folder_filter`.
- `load_files()` returns a copied context and does not mutate the original context.
- `load_context()` does not call `list_shared_files()` when entry loading fails.
- Progress reporter receives the current Chinese progress messages.

Keep existing storage tests unchanged. They verify `BaiduStorage` wrapper compatibility and monkeypatch behavior.

Targeted verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage_loader
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest tests.test_storage tests.test_storage_loader
```

Full verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p "test_*.py" -b
```

## Follow-Up Path

After this lands, the next safe extraction is centralized error notification. `_notify_error(error, context_message, extra_info=None, collect=True)` can then replace direct `handle_error_and_notify(error, message, notifier, config, collect=collect)` calls in small batches with focused argument-forwarding tests.
