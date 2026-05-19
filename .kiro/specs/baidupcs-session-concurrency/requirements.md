# Requirements Document

## Introduction

`transfershare` 已经在多个层面引入了基于 `concurrent.futures.ThreadPoolExecutor` 的并发：多链接转存（`TRANSFERSHARE_MULTI_SHARE_CONCURRENCY`）、扫描–转存双缓冲（`TRANSFERSHARE_TRANSFER_PIPELINE`）、本地扫描（`TRANSFERSHARE_LOCAL_SCAN_CONCURRENCY`）、批量重命名（`TRANSFERSHARE_RENAME_CONCURRENCY`）。这些并发路径共享同一个 `BaiduStorage` 实例，进而共享同一个 `baidupcs_py.baidupcs.pcs.BaiduPCS._session`（`requests.Session`）。

当前 `BaiduPCS._session` 直接使用 `requests.Session()` 默认构造，存在以下已知问题：

1. 默认 `HTTPAdapter` 仅维护 `pool_connections=10` / `pool_maxsize=10`，并发 ≥ 4 时会频繁出现 `Connection pool is full, discarding connection` 警告并丢弃 keep-alive 连接，每次请求重建 TCP/TLS，吞吐下降。
2. `_session.cookies.update(...)` 在 `access_shared` / `shared_paths` 等路径中被多线程调用，`http.cookiejar.CookieJar` 自带的 `RLock` 仅保证 jar 自身一致，但与 `BaiduPCS.cookies`（`get_dict()` 读取）之间没有更高层的原子语义。
3. `vendor/BaiduPCS-Py` 是 git submodule，历史约定是不直接修改其源码，所有运行时增强通过 `storage_client.BaiduClientAdapter` 注入（参考已有的 `_inject_timeout` / `_patch_request_methods`）。
4. GitHub Actions runner 网络抖动较大，已有 `MAX_RETRIES_GITHUB=5` 的差异化重试策略，本次 session 改造也必须按 GA 环境单独调参。

本特性的目标是在不修改 `vendor/BaiduPCS-Py` 源码、不引入新运行时依赖的前提下，通过运行时 monkey-patch 增强 `BaiduPCS._session`，使其连接池容量与并发层匹配、cookie 更新具备明确的线程安全语义，并保持与既有超时注入、重试封装、`access_shared` / `shared_paths` / `list_shared_paths` / `transfer_shared_paths` / `list` / `makedir` / `rename` / `quota` 等消费方完全的行为兼容。

## Glossary

- **BaiduPCS**: `baidupcs_py.baidupcs.pcs.BaiduPCS` 类，封装百度网盘底层 HTTP 协议；实例持有 `_session` 与 `_cookies`。
- **BaiduPCSApi**: `baidupcs_py.baidupcs.BaiduPCSApi`，对外暴露的高层 API，内部持有一个 `BaiduPCS` 实例（属性名为 `_pcs` / `pcs` / `baidupcs` / `_baidupcs` 之一）。
- **BaiduClientAdapter**: 本仓库 `storage_client.py` 中的适配器类，负责构造 `BaiduPCSApi`、注入超时、封装 `call_with_retry`，本特性的 patch 入口。
- **Session**: `requests.Session` 实例，即 `BaiduPCS._session`。
- **HTTPAdapter**: `requests.adapters.HTTPAdapter`，挂载在 Session 上，决定连接池大小（`pool_connections` / `pool_maxsize`）与底层 `urllib3` 重试策略。
- **ConnectionPool**: 由 HTTPAdapter 内部 `urllib3.PoolManager` 维护的、按 host 划分的 TCP/TLS keep-alive 连接池。
- **CookieJar**: `_session.cookies`（`requests.cookies.RequestsCookieJar`），底层使用 `RLock` 保护读写。
- **Cookie_Update_Lock**: 本特性新增的、由 `BaiduClientAdapter` 持有的 `threading.Lock`，用于把"更新 cookie + 同步 `_cookies` 字典"的复合操作变成原子段。
- **BDUSS / STOKEN / PTOKEN**: 百度网盘鉴权 cookie；其中 BDUSS、STOKEN 是 `BaiduClientAdapter.validate_cookies` 强制要求的两项。
- **ThreadPoolExecutor**: `concurrent.futures.ThreadPoolExecutor`，本仓库 `storage.py` / `storage_paths.py` / `transfer_runner.py` 用于多链接、扫描、重命名等并发执行的执行器。
- **GA_Environment**: `os.getenv("GITHUB_ACTIONS") == "true"` 的运行环境；本特性对 GA 与本地使用不同默认值。
- **Round_Trip_Patch**: 本特性的 patch 入口名称，约定形如 `_patch_session_pool` 与 `_patch_cookies_update`，仅在 `_init_client` 成功获得 `pcs_candidate` 之后执行一次，并以 `_session_concurrency_patched=True` 标记，幂等。

## Requirements

### Requirement 1: 连接池容量与并发层对齐

**User Story:** 作为运行多链接并发转存的运维者，我希望底层 `requests.Session` 的连接池容量足以容纳所有 worker 的并发请求，这样 keep-alive 连接不会被丢弃、TLS 握手次数最小化。

#### Acceptance Criteria

1. WHEN `BaiduClientAdapter._init_client` 成功初始化 `BaiduPCSApi`，THE BaiduClientAdapter SHALL 解析其 `pcs_candidate._session` 并对 `https://` 与 `http://` 两个前缀各挂载一个新的 `HTTPAdapter`，其 `pool_connections` 与 `pool_maxsize` 均不小于 `max(TRANSFERSHARE_PCS_POOL_MAXSIZE, MULTI_SHARE_CONCURRENCY * TRANSFERSHARE_PCS_POOL_FANOUT)`。
2. WHEN 环境变量 `TRANSFERSHARE_PCS_POOL_MAXSIZE` 未设置，THE BaiduClientAdapter SHALL 使用默认值 `32`。
3. WHEN 环境变量 `TRANSFERSHARE_PCS_POOL_FANOUT` 未设置，THE BaiduClientAdapter SHALL 使用默认值 `4`。
4. WHILE GA_Environment 为真，THE BaiduClientAdapter SHALL 把 `pool_maxsize` 的下限再叠加 `8`（用于补偿 runner 抖动导致的临时连接重开），最终值与 `TRANSFERSHARE_PCS_POOL_MAXSIZE` 显式覆盖值取较大者。
5. THE BaiduClientAdapter SHALL 在挂载新 `HTTPAdapter` 时把 `pool_block` 设为 `False`，使得短时间瞬时超额请求不会阻塞调用线程。
6. WHEN 调用方显式设置 `TRANSFERSHARE_PCS_POOL_MAXSIZE=0`，THE BaiduClientAdapter SHALL 跳过连接池替换、保留 `requests.Session` 默认行为，并在 DEBUG 级别记录"已禁用连接池调优"。
7. IF `pcs_candidate._session` 不是 `requests.Session` 的实例，THEN THE BaiduClientAdapter SHALL 跳过本 patch、在 WARNING 级别记录被跳过的原因，并继续完成 `_init_client` 的其余步骤。

### Requirement 2: Cookie 更新的原子性

**User Story:** 作为并发调用 `access_shared` / `shared_paths` / `list_shared_paths` 的开发者，我希望 cookie 在多线程下不会出现"更新到一半被读到旧值"或两个线程交错写入导致 `_cookies` 字典与 `_session.cookies` 不一致的情况。

#### Acceptance Criteria

1. THE BaiduClientAdapter SHALL 在初始化阶段创建一个 `threading.Lock` 实例 `Cookie_Update_Lock`，并通过 monkey-patch 替换 `pcs_candidate._cookies_update`，使被替换后的方法在 `Cookie_Update_Lock` 持有期间依次执行 `self._session.cookies.update(...)` 与 `self._cookies.update(...)`。
2. WHEN 被替换后的 `_cookies_update` 收到的入参不是 `dict` 也不是可迭代的键值对，THEN THE Patched_Cookies_Update SHALL 直接抛出 `TypeError` 而不是静默忽略。
3. WHEN 多个线程并发调用 `access_shared` 触发 `_cookies_update`，THE Patched_Cookies_Update SHALL 保证任意外部观察者（包括 `BaiduPCS.cookies` 属性的 `_session.cookies.get_dict()`）看到的状态要么是更新前、要么是更新后的完整集合，不会出现"只更新了一半的 key"的中间态。
4. THE Patched_Cookies_Update SHALL 保留与原方法一致的返回值（`None`）。
5. WHILE `Cookie_Update_Lock` 已被持有，IF 同一线程递归触发 `_cookies_update`，THEN THE Patched_Cookies_Update SHALL 不发生死锁（实现可使用 `RLock`，或保证调用链不会递归进入）。
6. THE BaiduClientAdapter SHALL 仅在 `pcs_candidate` 暴露 `_cookies_update` 属性时才执行该 patch；IF 该属性不存在，THEN THE BaiduClientAdapter SHALL 跳过本 patch 并在 DEBUG 级别记录"未找到 _cookies_update，跳过 cookie 原子化"。

### Requirement 3: 与既有 monkey-patch 的兼容

**User Story:** 作为维护者，我希望新增的 session/cookie patch 与已有的 `_inject_timeout` 不冲突，并且重复初始化客户端时不会被重复套层。

#### Acceptance Criteria

1. THE BaiduClientAdapter SHALL 在 `pcs_candidate` 上设置标记属性 `_session_concurrency_patched`，并在执行 patch 前先检查该标记；WHEN 该标记已为 `True`，THE BaiduClientAdapter SHALL 跳过本次 patch 并在 DEBUG 级别记录"会话并发 patch 已存在"。
2. THE Round_Trip_Patch SHALL 在 `_inject_timeout` 之后执行，使得超时注入完成的 `_request` / `_request_get` / `_request_post` 仍然走在被替换连接池之上。
3. WHEN 同一 `BaiduClientAdapter` 实例的 `_init_client` 被重试（现有循环 `for retry in range(3)`），THE BaiduClientAdapter SHALL 仅在最终成功时挂一次 HTTPAdapter，不会因为重试而注册多份 adapter。
4. THE Round_Trip_Patch SHALL 不替换、不包装 `pcs_candidate._request*` 方法本身（这些方法仍由 `_inject_timeout` 负责），仅作用于 `_session` 与 `_cookies_update`。

### Requirement 4: 行为兼容性

**User Story:** 作为 `storage.py` / `storage_shares.py` / `transfer_runner.py` 的调用方，我希望 patch 之后所有现有 API 的可观察行为完全不变。

#### Acceptance Criteria

1. THE BaiduClientAdapter SHALL 在 patch 完成后，保证 `quota` / `list` / `makedir` / `rename` / `access_shared` / `shared_paths` / `list_shared_paths` / `transfer_shared_paths` 的入参签名、返回值类型与异常类型与 patch 前一致。
2. WHEN `BaiduStorage` 在 patch 后调用 `client.cookies`（即 `BaiduPCS.cookies` 属性），THE BaiduPCS SHALL 返回与 `self._session.cookies.get_dict()` 完全相同的字典内容。
3. WHEN `call_with_retry` 因可重试错误进行重试，THE BaiduClientAdapter SHALL 复用同一 Session 与同一连接池，不会重新构造 Session 或丢弃既有 keep-alive 连接。
4. IF 任意 patch 步骤抛出异常，THEN THE BaiduClientAdapter SHALL 把异常转换为 WARNING 日志、回滚已替换的 adapter（恢复到 patch 前的 `_session.adapters` 快照），并继续完成 `_init_client`，不让初始化失败。

### Requirement 5: 可配置性与无新运行时依赖

**User Story:** 作为部署者，我希望通过环境变量调整连接池行为，且不需要新增 `pip install` 的依赖。

#### Acceptance Criteria

1. THE BaiduClientAdapter SHALL 仅使用 `requirements.txt` 中已存在的 `requests` 与其传递依赖 `urllib3` 来实现连接池替换，不引入任何新增第三方包。
2. THE BaiduClientAdapter SHALL 通过 `env_utils.read_positive_int_env` 读取 `TRANSFERSHARE_PCS_POOL_MAXSIZE` 与 `TRANSFERSHARE_PCS_POOL_FANOUT`，并通过 `env_utils.read_non_negative_int_env`（如缺失需先在 `env_utils` 中补充）读取允许 `0` 的 `TRANSFERSHARE_PCS_POOL_MAXSIZE` 禁用值。
3. WHEN 环境变量取值非法（非整数、负数），THE BaiduClientAdapter SHALL 回退到默认值并在 WARNING 级别记录"环境变量 X 解析失败，使用默认 Y"。
4. THE BaiduClientAdapter SHALL 不依赖 `urllib3.Retry`；连接级重试继续由 `call_with_retry` 在应用层负责，避免与既有指数退避策略叠加。

### Requirement 6: GA 与本地环境的差异化默认

**User Story:** 作为在 GitHub Actions 运行的 workflow，我希望默认池容量比本地稍大以吸收 runner 网络抖动，但不至于让本地调试浪费资源。

#### Acceptance Criteria

1. WHILE GA_Environment 为真且未显式设置 `TRANSFERSHARE_PCS_POOL_MAXSIZE`，THE BaiduClientAdapter SHALL 把 `pool_maxsize` 设为 `max(32, MULTI_SHARE_CONCURRENCY * 4) + 8`。
2. WHILE GA_Environment 为假且未显式设置 `TRANSFERSHARE_PCS_POOL_MAXSIZE`，THE BaiduClientAdapter SHALL 把 `pool_maxsize` 设为 `max(32, MULTI_SHARE_CONCURRENCY * 4)`。
3. WHEN 用户显式设置 `TRANSFERSHARE_PCS_POOL_MAXSIZE`，THE BaiduClientAdapter SHALL 以该值为准，不再叠加 GA 偏置。

### Requirement 7: 可观测性

**User Story:** 作为排查"为什么并发上去了吞吐没涨"的工程师，我希望能从日志里直接看到 patch 是否生效、池大小是多少、是否曾经被回滚。

#### Acceptance Criteria

1. WHEN Round_Trip_Patch 成功完成，THE BaiduClientAdapter SHALL 在 INFO 级别记录一条结构化日志，至少包含字段 `pool_maxsize`、`pool_connections`、`fanout`、`ga_environment`、`patched_cookies_update`（布尔）。
2. WHEN 环境变量 `TRANSFERSHARE_PCS_DEBUG_POOL=1`，THE BaiduClientAdapter SHALL 在每次 `call_with_retry` 完成时把当前 `_session.adapters['https://']._pool_connections` 与已用连接数（若 urllib3 暴露）写入 DEBUG 日志。
3. THE BaiduClientAdapter SHALL 暴露一个只读属性 `session_pool_info`，返回 `{"pool_maxsize": int, "pool_connections": int, "fanout": int, "patched": bool}`，便于单元测试与外部诊断脚本读取。
4. IF Round_Trip_Patch 因任意原因被回滚（见 Requirement 4 AC 4），THEN THE BaiduClientAdapter SHALL 在 WARNING 级别记录"会话并发 patch 已回滚: {reason}"，且 `session_pool_info["patched"]` 取值 `False`。

### Requirement 8: 可验证性

**User Story:** 作为测试编写者，我希望能在不发起真实 HTTP 请求的情况下，断言 patch 的副作用。

#### Acceptance Criteria

1. THE BaiduClientAdapter SHALL 允许通过依赖注入或类属性 hook（例如 `BaiduClientAdapter._pcs_factory`）替换 `BaiduPCSApi` 构造函数，使测试用例可以注入一个仅暴露 `_pcs._session = requests.Session()` 与 `_pcs._cookies_update` 的伪造对象。
2. WHEN 注入的伪造对象其 `_session` 为非默认 `requests.Session` 子类，THE BaiduClientAdapter SHALL 仍按 Requirement 1 AC 1–5 替换 adapter，并在 `session_pool_info` 中体现新的池容量。
3. THE BaiduClientAdapter SHALL 提供可被测试调用的 `_apply_session_patches(pcs_candidate)` 入口，使测试可以在不调用 `_init_client` 的前提下单独验证 patch 行为。
4. WHEN 单元测试在 patch 之后并发触发 `pcs_candidate._cookies_update`，THE Patched_Cookies_Update SHALL 在断言"任意时刻 `BaiduPCS.cookies` 字典是某次 update 的完整快照"时通过测试。

### Requirement 9: 失败回退与降级

**User Story:** 作为运维者，我希望即使新 patch 在某些 BaiduPCS-Py 版本上不兼容，主流程也能继续工作。

#### Acceptance Criteria

1. IF 替换 `HTTPAdapter` 抛出 `Exception`，THEN THE BaiduClientAdapter SHALL 恢复 patch 前的 `_session.adapters`、把 `_session_concurrency_patched` 保持为 `False`，并在 WARNING 级别记录原因。
2. IF 替换 `_cookies_update` 抛出 `Exception`，THEN THE BaiduClientAdapter SHALL 把 `_cookies_update` 恢复为原方法、并在 WARNING 级别记录原因，但不影响 HTTPAdapter 的替换结果。
3. WHEN 任一回退发生，THE BaiduClientAdapter SHALL 仍然完成 `_init_client`、`self.client` 与 `self._quota_info` 仍然被正常赋值。
4. THE BaiduClientAdapter SHALL 不在回退路径上抛出异常给调用方（除非原本就该抛出的鉴权类异常），保证 `BaiduStorage` 的构造行为与本特性引入前完全一致。
