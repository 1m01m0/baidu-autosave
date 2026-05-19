# Implementation Plan

## Overview

按 design.md 定义的三阶段顺序实现 BaiduPCS-Py 会话并发增强：先在 `env_utils` / `storage_client.py` 中铺设零副作用的基础设施（Phase 1），再加入 `_patch_session_pool` 与回滚（Phase 2），最后接入 `_patch_cookies_update` 与可观测性（Phase 3）。每个阶段都给出独立的测试验收，并通过 `_session_concurrency_patched` 幂等标记保证多次初始化安全。

## Tasks

### Phase 1 — 基础设施（无副作用）

- [ ] 1. 在 `env_utils.py` 新增 `read_non_negative_int_env(name, default)` 解析器
  - 复用现有 `read_positive_int_env` 的实现风格
  - 区别：值 `0` 合法返回；负数 / 非整数回退到 `default`
  - 不修改现有 `read_positive_int_env` / `read_non_negative_float_env` 行为
  - _Requirements: 5.2, 5.3_

- [ ] 1.1 在 `tests/test_storage.py` 中追加 `test_read_non_negative_int_env_*` 单元测试
  - 覆盖：合法 `0` / 合法正数 / 负数回退 / 非整数回退 / 未设置回退
  - _Requirements: 5.3_

- [ ] 2. 在 `storage_client.py` 顶部新增 3 个环境变量常量
  - `TRANSFERSHARE_PCS_POOL_MAXSIZE`（用 `read_non_negative_int_env`，默认哨兵值 `-1` 表示未显式设置，由 `_compute_pool_maxsize` 内部决定 GA / 本地默认）
  - `TRANSFERSHARE_PCS_POOL_FANOUT`（用 `read_positive_int_env`，默认 `4`）
  - `TRANSFERSHARE_PCS_DEBUG_POOL`（用 `read_positive_int_env`，默认 `0`）
  - 注释里写清楚每个变量的用途与默认值
  - _Requirements: 1.2, 1.3, 1.6, 5.1, 7.2_

- [ ] 3. 在 `BaiduClientAdapter` 类新增类属性 `_pcs_factory = staticmethod(BaiduPCSApi)`
  - 修改 `_init_client` 中 `BaiduPCSApi(cookies=cookies_dict)` 为 `type(self)._pcs_factory(cookies=cookies_dict)`
  - 行为不变；目的是让测试可以 patch 工厂注入 fake
  - _Requirements: 8.1_

- [ ] 4. 在 `BaiduClientAdapter.__init__` 新增 3 个状态字段
  - `self._session_cookie_lock: Optional[threading.Lock] = None`
  - `self._session_pool_info: dict = {"pool_maxsize": 0, "pool_connections": 0, "fanout": 0, "patched": False}`
  - `self._session_patches_applied: bool = False`
  - 顺序：放在 `_client_lock` 之后、`_init_client` 调用之前
  - _Requirements: 7.3_

- [ ] 5. 在 `BaiduClientAdapter` 新增 `session_pool_info` 只读属性
  - `@property` 返回 `dict(self._session_pool_info)` 的副本
  - 兼容老测试：用 `getattr(self, "_session_pool_info", {...默认...})` 兜底，避免 `__new__` 跳过 `__init__` 的测试失败
  - _Requirements: 7.3_

- [ ] 6. 实现 `BaiduClientAdapter._compute_pool_maxsize()` 方法
  - 输入：读 `os.environ` 与 `read_positive_int_env("TRANSFERSHARE_MULTI_SHARE_CONCURRENCY", 1)`（避免与 storage.py 的循环 import）
  - 返回 `(pool_maxsize, pool_connections, fanout)` 三元组
  - 决策矩阵（design.md Requirement 6 表 + Requirement 1）：
    - 显式 `MAXSIZE=0` → 返回 `(0, 0, fanout)`，调用方据此跳过 mount
    - 显式 `MAXSIZE>0` → `pool_maxsize = pool_connections = MAXSIZE`，不叠加 GA 偏置
    - 未显式设置 GA=true → `max(32, MULTI * fanout) + 8`
    - 未显式设置 GA=false → `max(32, MULTI * fanout)`
  - 任意 env 解析失败回退默认并 `logger.warning`
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 5.3, 6.1, 6.2, 6.3_

- [ ] 6.1 单元测试 `test_compute_pool_maxsize_*`
  - matrix 覆盖：`(GA, MULTI, env_override, fanout_override) → expected`
  - 至少 6 组 case：本地默认 / GA 默认 / 显式 MAXSIZE / 禁用 0 / 非法 fanout 回退 / 大 MULTI
  - 用 `assertLogs("transfershare", level="WARNING")` 验证 fallback 日志
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 5.3, 6.1, 6.2, 6.3, 7.3_

- [ ] 6.2 单元测试 `test_session_pool_info_is_a_copy`
  - 调用 `adapter.session_pool_info` 后修改返回字典，断言 `adapter._session_pool_info` 不变
  - _Requirements: 7.3_

### Phase 2 — `_patch_session_pool` + 回滚

- [ ] 7. 实现 `BaiduClientAdapter._patch_session_pool(pcs_candidate) -> bool`
  - 步骤：
    1. `if not isinstance(pcs_candidate._session, requests.Session):` → log WARNING，返回 `False`（Requirement 1.7）
    2. `pool_maxsize, pool_connections, fanout = self._compute_pool_maxsize()`
    3. `if pool_maxsize == 0:` → log DEBUG（Requirement 1.6），更新 `_session_pool_info["fanout"]=fanout, "patched"=False`，返回 `False`
    4. `snapshot = dict(pcs_candidate._session.adapters)`
    5. `try:` 构造 `HTTPAdapter(pool_connections=..., pool_maxsize=..., pool_block=False)` 各一份，`session.mount("https://", adapter_https); session.mount("http://", adapter_http)`
    6. 成功后写 `_session_pool_info`，返回 `True`
    7. `except Exception as exc:` 还原 `pcs_candidate._session.adapters.clear(); pcs_candidate._session.adapters.update(snapshot)`，log WARNING（含原因），`_session_pool_info["patched"]=False`，返回 `False`
  - 不抛异常给调用方
  - _Requirements: 1.1, 1.5, 1.6, 1.7, 4.4, 9.1, 9.3_

- [ ] 8. 实现 `BaiduClientAdapter._apply_session_patches(pcs_candidate) -> None`
  - 步骤：
    1. `if getattr(pcs_candidate, "_session_concurrency_patched", False):` → log DEBUG，return（Requirement 3.1）
    2. `pool_ok = self._patch_session_pool(pcs_candidate)`
    3. _Phase 3 才加_ `cookie_ok = self._patch_cookies_update(pcs_candidate)`，**Phase 2 暂时跳过**
    4. 任意一项成功就 `pcs_candidate._session_concurrency_patched = True; self._session_patches_applied = True`
    5. INFO 日志：`"会话并发 patch 已启用: pool_maxsize={...} pool_connections={...} fanout={...} ga_environment={...} patched_cookies_update={...}"`（Phase 2 时 `patched_cookies_update=false`）
  - _Requirements: 3.1, 3.2, 3.3, 7.1_

- [ ] 9. 在 `_init_client` 中接入 `_apply_session_patches`
  - 位置：`self._inject_timeout()` 之后、`self._quota_info = self.client.quota()` 之前
  - 调用方式：`pcs_candidate = self._get_pcs_candidate(); if pcs_candidate is not None: self._apply_session_patches(pcs_candidate)`
  - 任意异常被 `_apply_session_patches` 内部吞掉，不会让 `_init_client` 失败（Property 1）
  - _Requirements: 3.2, 3.3, 9.3, 9.4_

- [ ] 10. 单元测试 `test_patch_session_pool_replaces_https_and_http_adapters`
  - 用真实 `requests.Session()` + `SimpleNamespace(_session=session)`
  - `BaiduClientAdapter.__new__` 构造 adapter，注入必要字段（`is_github_actions`、`_session_pool_info` 等）
  - 调用后断言 `session.adapters["https://"]` 与 `session.adapters["http://"]` 都是新实例，`pool_maxsize` 等于 `_compute_pool_maxsize` 返回值
  - 通过 `adapter.poolmanager.connection_pool_kw["maxsize"]` 取实际值
  - _Requirements: 1.1, 1.5_

- [ ] 11. 单元测试 `test_patch_session_pool_rolls_back_on_mount_failure`
  - 用 `mock.patch.object(session, "mount", side_effect=[None, RuntimeError("boom")])` 让第二次 mount 抛异常
  - 断言 `session.adapters` 的 keys 与 mock 调用之前完全一致
  - 断言 `adapter.session_pool_info["patched"] is False`
  - 用 `assertLogs(..., level="WARNING")` 验证日志包含原因
  - _Requirements: 4.4, 9.1, 9.3_

- [ ] 12. 单元测试 `test_apply_session_patches_skips_when_session_is_not_requests_session`
  - `pcs_candidate = SimpleNamespace(_session=object())`
  - 调用 `_apply_session_patches` 不抛异常
  - 断言 `session_pool_info["patched"] is False` + WARNING 日志
  - _Requirements: 1.7_

- [ ] 13. 单元测试 `test_apply_session_patches_skips_when_already_patched`
  - 预先 `pcs_candidate._session_concurrency_patched = True`
  - 调用 `_apply_session_patches` 后断言 `session.mount` 未被调用、DEBUG 日志
  - _Requirements: 3.1_

- [ ] 14. 单元测试 `test_apply_session_patches_is_idempotent`
  - 同一 `pcs_candidate` 调用 `_apply_session_patches` 两次
  - 用 `mock.patch.object(session, "mount", wraps=session.mount)` 断言第一次后两次 mount = https + http；第二次因为 `_session_concurrency_patched=True` 跳过，总 `mount.call_count == 2`
  - _Requirements: 3.1, 3.3_

- [ ] 15. 集成测试 `test_init_client_keeps_working_when_session_pool_patch_fails`
  - 用 `BaiduClientAdapter._pcs_factory` 注入 fake：返回的对象暴露 `_pcs._session`，且 `_session.mount` 抛异常
  - 完整跑 `BaiduClientAdapter(cookies)`，断言：
    - `self.client is not None`
    - `self._quota_info` 已设置（fake 的 `quota()` 返回固定值）
    - `self.session_pool_info["patched"] is False`
    - 不向上抛异常
  - _Requirements: 9.3, 9.4_

### Phase 3 — `_patch_cookies_update` + 完整可观测性

- [ ] 16. 实现 `BaiduClientAdapter._patch_cookies_update(pcs_candidate) -> bool`
  - 步骤：
    1. `original = getattr(pcs_candidate, "_cookies_update", None)` → 为 `None` 时 log DEBUG，返回 `False`（Requirement 2.6）
    2. `lock = threading.Lock()`
    3. 构造闭包 `def patched(cookies, *args, **kwargs):` —— 闭包内 `with lock: return original(cookies, *args, **kwargs)`
    4. `try: setattr(pcs_candidate, "_cookies_update", patched); self._session_cookie_lock = lock; return True`
    5. `except Exception as exc:` log WARNING，`self._session_cookie_lock = None`，`setattr(pcs_candidate, "_cookies_update", original)`（容错性 setattr，再失败就吞掉），返回 `False`
  - _Requirements: 2.1, 2.4, 2.5, 2.6, 9.2, 9.3_

- [ ] 17. 在 `_apply_session_patches` 中接入 `_patch_cookies_update`
  - 在 `_patch_session_pool` 之后调用
  - 拿到 `cookie_ok`，更新 INFO 日志中的 `patched_cookies_update={cookie_ok}`
  - 任意一项成功就标记 `_session_concurrency_patched=True`
  - _Requirements: 3.1, 3.2, 7.1_

- [ ] 18. 单元测试 `test_patch_cookies_update_serializes_concurrent_updates`
  - 真实 `requests.Session()`，原 `_cookies_update` 实现为：`time.sleep(0.005); self._session.cookies.update(cookies)`（模拟非原子写）
  - 用 `threading.Barrier(N)` 让 N=4 个线程同时 update 不同 key 集合
  - 断言每次外部观察到的 `_session.cookies.get_dict()` 都是某次 update 的完整快照（实现：每个 worker update 形如 `{f"K{i}_a": ..., f"K{i}_b": ...}` 后再读，必须看到自己写的两个 key 完整存在）
  - _Requirements: 2.3, 4.2_

- [ ] 19. 单元测试 `test_patch_cookies_update_rolls_back_when_setattr_fails`
  - 用 `Mock` 配置 `__setattr__` 在某次调用时抛异常（例如包一层 read-only descriptor）
  - 断言 `pcs_candidate._cookies_update` 仍是原方法、`adapter._session_cookie_lock is None`
  - WARNING 日志
  - _Requirements: 9.2, 9.3_

- [ ] 20. 单元测试 `test_patch_cookies_update_skipped_when_method_missing`
  - `pcs_candidate = SimpleNamespace(_session=requests.Session())`（无 `_cookies_update`）
  - 调用 `_patch_cookies_update` 返回 `False`、不抛异常、DEBUG 日志
  - _Requirements: 2.6_

- [ ] 21. 在 `call_with_retry` 内增加可选 DEBUG 池状态日志
  - `if read_positive_int_env("TRANSFERSHARE_PCS_DEBUG_POOL", 0) >= 1 and self._session_patches_applied:`
  - 在每次 `func(...)` 成功返回前，读 `pcs_candidate._session.adapters["https://"].poolmanager.pools` 的长度
  - 用 try/except 兜底——urllib3 不暴露时跳过
  - 日志：`"会话池状态: scheme=https:// num_pools={...}"`
  - _Requirements: 7.2_

- [ ] 22. 在 README 的 "性能调优" 段落（如不存在则新增一节）补充新 env 变量说明
  - `TRANSFERSHARE_PCS_POOL_MAXSIZE`、`TRANSFERSHARE_PCS_POOL_FANOUT`、`TRANSFERSHARE_PCS_DEBUG_POOL`
  - 推荐值：`MAXSIZE=32~64`、`FANOUT=4`，并发越高建议同步上调
  - _Requirements: 5.1_

- [ ] 23. 验证既有测试套件零破坏
  - `python -m unittest discover -s tests -p "test_*.py" -b`
  - `python -m ruff check . --exclude vendor --select E9,F63,F7,F82`
  - `python -m compileall -q -x 'vendor/' .`
  - _Requirements: 4.1, 4.2, 4.3_

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "description": "Phase 1 基础设施：env 解析器与零副作用常量、字段、属性",
      "tasks": ["1", "1.1", "2", "3", "4", "5", "6", "6.1", "6.2"]
    },
    {
      "wave": 2,
      "description": "Phase 2：实现 _patch_session_pool 与回滚，接入 _init_client",
      "tasks": ["7", "8", "9"]
    },
    {
      "wave": 3,
      "description": "Phase 2 测试：替换 / 回滚 / 跳过 / 幂等 / 集成",
      "tasks": ["10", "11", "12", "13", "14", "15"]
    },
    {
      "wave": 4,
      "description": "Phase 3：cookie 原子化 patch 与编排接入",
      "tasks": ["16", "17"]
    },
    {
      "wave": 5,
      "description": "Phase 3 测试：并发 cookie / 回滚 / 缺失方法",
      "tasks": ["18", "19", "20"]
    },
    {
      "wave": 6,
      "description": "可观测性、文档与全套回归",
      "tasks": ["21", "22", "23"]
    }
  ]
}
```

## Notes

- **不修改 vendor/BaiduPCS-Py**：所有改动通过 `storage_client.py` 在初始化阶段做运行时 monkey-patch，与已有 `_inject_timeout` 风格一致。
- **避免循环 import**：`storage_client.py` 不直接 `from storage import MULTI_SHARE_CONCURRENCY`；改为重新读 `TRANSFERSHARE_MULTI_SHARE_CONCURRENCY` 环境变量，与 `storage.py` 共用同一来源。
- **既有测试兼容**：`BaiduClientAdapterRetryTests` / `BaiduClientAdapterTests` 用 `BaiduClientAdapter.__new__` 构造对象，新方法访问 `self._session_pool_info` 等字段时统一用 `getattr` 兜底。
- **回滚是硬契约**：任何 patch 失败都不让 `_init_client` 抛异常给 `BaiduStorage`；adapter 替换、cookie 包裹两条路径独立回滚，互不影响（Requirement 9.1 / 9.2）。
- **百度限频策略不变**：本特性只动连接池容量与 cookie 锁，不动 `call_with_retry` 的重试逻辑（Requirement 5.4）。
- **Phase 2 验收门槛**：本地或 mock GA 环境日志可见 `会话并发 patch 已启用: ...`，并发 4 链接转存观察 `Connection pool is full` 警告消失。
- **Phase 3 验收门槛**：`unittest -v` 多次跑 `test_patch_cookies_update_serializes_concurrent_updates` 无 flake；README "性能调优" 段落包含全部新 env 变量。
