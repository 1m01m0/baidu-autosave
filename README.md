# Baidu AutoSave · 百度网盘自动转存

通过 GitHub Actions 或本地 Python 任务，将百度网盘分享文件保存到自己的网盘，支持逐链接目录、正则过滤、重命名、重复文件检查和企业微信结果通知。

本仓库基于 [TransferShare](https://github.com/Jack261108/transfershare) 维护，底层使用 [BaiduPCS-Py 子模块](.gitmodules)。本文的克隆与工作流入口指向当前仓库，上游署名及许可保留在 [LICENSE](LICENSE)。

[中文使用说明](#快速开始github-actions) · [English overview](#english-overview) · [完整配置](CONFIG_GUIDE.md) · [工作流记录](https://github.com/1m01m0/baidu-autosave/actions)

## 功能

| 能力 | 行为 |
| --- | --- |
| 定时转存 | 工作流声明每 6 小时运行，也可手动触发 |
| 批量分享 | 接受带提取码的分享链接，可逐链接指定网盘保存目录 |
| 过滤与重命名 | 文件／文件夹正则、排除规则、捕获组重命名 |
| 重复检查 | 结合目标路径与可用 MD5 判断；冲突或缺少校验信息时不会保证自动合并 |
| 失败处理 | 单次工作流重试；可选加密保存跨运行失败清单 |
| 通知 | 可选企业微信机器人 Webhook |
| 并发 | 可配置多链接、重命名等并发；默认保守运行 |

这是分享文件转存工具，不是双向同步或完整备份系统。分享有效性、账号状态、可用空间、网络和服务端限频都会影响结果。

## 快速开始：GitHub Actions

### 1. Fork 并启用工作流

Fork 当前仓库，在自己的仓库中启用 Actions。任务定义位于 [baidu-transfer.yml](.github/workflows/baidu-transfer.yml)，使用 Python 3.9，并自动检出及构建子模块。

### 2. 配置 Secrets

进入仓库的 **Settings → Secrets and variables → Actions**，添加：

| Secret | 必填 | 内容 |
| --- | --- | --- |
| `BAIDU_COOKIES` | 是 | `BDUSS=实际值; STOKEN=实际值` |
| `SHARE_URLS` | 是 | 分享链接，每行一个；格式见下方 |
| `SAVE_DIR` | 否 | 默认网盘目录，未设置时为 `/AutoTransfer` |
| `WECHAT_WEBHOOK` | 否 | 企业微信机器人 Webhook |
| `TRANSFERSHARE_STATE_KEY` | 否 | 用于工作流跨运行失败清单加密的密钥；未配置时跳过该持久化流程 |

`SHARE_URLS` 最小示例（把占位链接换成真实链接）：

```text
https://pan.baidu.com/s/xxxxxx?pwd=abcd
https://pan.baidu.com/s/yyyyyy?pwd=efgh /课程/数学
```

目录是百度网盘中的路径。复杂逐链接规则请使用 [配置指南](CONFIG_GUIDE.md) 中的格式，不要把多行 JSON 对象混入逐行链接示例。

可在本机用 `openssl rand -base64 32` 生成随机状态密钥，再保存为 Secret。保留该密钥以便后续运行解密已有状态。

### 3. 获取登录 Cookie

可以在已登录的百度网盘网页开发者工具中复制 `BDUSS` 和 `STOKEN`，组合后存入 `BAIDU_COOKIES`。

仓库也提供可选浏览器助手。先克隆仓库并在虚拟环境中安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-playwright.txt
python -m playwright install
python save_baidu_cookies.py --repo YOUR_USERNAME/baidu-autosave
```

将 `YOUR_USERNAME/baidu-autosave` 换成自己的仓库。写入 Secrets 需要安装 GitHub CLI 并预先登录具有目标仓库权限的账户；省略 `--repo` 可只写本地 env 文件。

助手会保留浏览器登录目录 `~/.baidu_pan_profile`，并写入 `baidu_cookies.env`。这些文件及 Cookie 都属于登录凭证，不应分享或提交。

### 4. 首次手动运行

在 **Actions → Baidu Transfer Task → Run workflow** 触发任务。检查依赖构建、转存日志和目标网盘目录，确认首次成功后再依赖定时执行。

当前计划为 UTC **00:17、06:17、12:17、18:17**，即 UTC+8 的 **08:17、14:17、20:17、次日 02:17**。计划时间不保证准点开始；以 Actions 实际运行记录为准。首次尝试失败时，工作流会再尝试一次。

## 本地运行

需要 Git、Python、Bash 和能编译 Cython 扩展的本机工具链。仓库 CI 定义了 Python 3.9／3.12 测试矩阵；依赖以锁定文件为准。

```bash
git clone --recurse-submodules https://github.com/1m01m0/baidu-autosave.git
cd baidu-autosave
python3 -m venv .venv
source .venv/bin/activate
bash scripts/install_dependencies.sh runtime
cp config.example.json config.json
```

编辑 `config.json`，填入自己的 Cookie 和链接。程序优先读取仓库根目录的 `config.json`，文件不存在时回退到环境变量；已存在但无效的配置不会被环境变量自动修复。

```json
{
  "cookies": "BDUSS=YOUR_BDUSS; STOKEN=YOUR_STOKEN",
  "save_dir": "/AutoTransfer",
  "share_urls": [
    {
      "share_url": "https://pan.baidu.com/s/xxxxxx?pwd=abcd",
      "save_dir": "/课程",
      "regex_pattern": "\\.(pdf|epub)$"
    }
  ]
}
```

先校验配置，再执行真实转存：

```bash
python validate_config.py
bash scripts/run_transfer_task.sh
```

包装脚本负责构建 `BaiduPCS-Py` 的 Cython 扩展并设置 `PYTHONPATH`。如果最初没有递归克隆，先运行 `git submodule update --init --recursive`。配置校验只检查结构与规则，不能证明 Cookie 或分享链接在线有效。

## 高级配置

完整字段、继承规则和样例见 [CONFIG_GUIDE.md](CONFIG_GUIDE.md) 及 [config.example.json](config.example.json)。常用字段包括：

- `folder_filter`、`exclude_folder_filter`：文件夹匹配和排除。
- `regex_pattern`：文件名匹配。
- `regex_replace`：结合正则捕获组生成目标名称。
- `save_dir`：全局或逐链接的目标目录。

`TRANSFERSHARE_MULTI_SHARE_CONCURRENCY` 和 `TRANSFERSHARE_RENAME_CONCURRENCY` 默认均为 `1`。在少量链接验证规则和结果后再调整并发；高并发不保证更快，可能增加限频和失败。

## 数据与失败状态

Cookie 和本地 `config.json` 属于敏感配置。工作流使用 Secrets 注入凭证；失败清单在配置状态密钥后加密写入 Actions cache，在任务运行时解密为本地文件。该机制帮助重试，不是永久存储或独立备份保证。

提交日志前检查 Cookie、分享提取码、Webhook 和私人文件名。通知会向配置的企业微信服务发送任务结果。仅转存自己有权保存的文件，并在执行前确认目标目录和剩余空间。

## 排查

| 现象 | 处理方向 |
| --- | --- |
| Cookie 无效／登录失效 | 重新登录并更新 Secret 或本地配置 |
| 子模块不存在 | 执行递归初始化；确认能访问子模块仓库 |
| Cython 编译失败 | 检查编译工具链和锁定依赖，使用虚拟环境安装 |
| 文件被跳过 | 检查正则、目标路径冲突、MD5 信息及日志原因 |
| 限频或 `-65` | 降低并发，稍后重试，避免反复密集触发 |
| 历史失败状态未恢复 | 检查状态密钥、cache 是否存在和解密日志 |
| 通知失败 | 检查 Webhook 及机器人所在群聊 |

## 开发与验证

```bash
bash scripts/install_dependencies.sh test
bash scripts/build_baidupcs_submodule.sh
PYTHONPATH=vendor/BaiduPCS-Py python -m unittest discover -s tests -p 'test_*.py'
```

CI 与质量检查定义见 [test-on-push.yml](.github/workflows/test-on-push.yml)。文档中的命令不代表当前分支测试已通过，应查看实际运行结果。

欢迎通过 [Issues](https://github.com/1m01m0/baidu-autosave/issues) 或 PR 提交可复现问题，附上 Python 版本、配置字段的脱敏示例和最小日志。

## English overview

Baidu AutoSave saves files from Baidu Pan share links to your own drive using GitHub Actions or a local Python task. This repository is based on TransferShare and retains its MIT license and upstream attribution.

For Actions, fork this repository, enable the workflow, and configure `BAIDU_COOKIES` and `SHARE_URLS` as repository Secrets. Optional Secrets are `SAVE_DIR`, `WECHAT_WEBHOOK`, and `TRANSFERSHARE_STATE_KEY`. Run **Baidu Transfer Task** manually first and verify the destination files. The workflow schedules four runs daily at 00:17, 06:17, 12:17, and 18:17 UTC; actual start times may vary.

For local use, clone recursively, create a Python virtual environment, install dependencies with `bash scripts/install_dependencies.sh runtime`, and edit `config.json`. Run `python validate_config.py`, then `bash scripts/run_transfer_task.sh`. The wrapper builds the Cython submodule and sets its import path.

Per-link paths, filtering and renaming are documented in [CONFIG_GUIDE.md](CONFIG_GUIDE.md). Transfers depend on valid credentials, live shares, available storage and service limits. Keep cookies, login profiles and notification credentials private. Optional encrypted retry state is a workflow convenience, not a durable backup.

## 许可证与来源

[MIT License](LICENSE)。感谢 [TransferShare](https://github.com/Jack261108/transfershare) 和 [BaiduPCS-Py](https://github.com/Jack-261108/BaiduPCS-Py) 的原始实现与维护者。
