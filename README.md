# TransferShare

中文 | [English](README.en.md)

TransferShare 将百度网盘分享链接中的文件保存到自己的网盘，支持本地运行或 GitHub Actions 定时执行。本仓库派生自 [Jack261108/transfershare](https://github.com/Jack261108/transfershare)。

## 运行

### 从源码运行

需要 Python 3.9+，以及构建 BaiduPCS 子模块所需的工具。

```sh
git clone --recurse-submodules https://github.com/1m01m0/baidu-autosave.git
cd baidu-autosave
python -m pip install -r requirements.txt
./scripts/build_baidupcs_submodule.sh
cp config.example.json config.json
```

编辑 `config.json`，填写 Cookies 和分享链接后运行：

```sh
./scripts/run_transfer_task.sh
```

也可直接运行 `python transfer_runner.py`。程序优先读取根目录的 `config.json`，不存在时回退到环境变量。配置字段、过滤、重命名和并发设置见 [CONFIG_GUIDE.md](CONFIG_GUIDE.md)。

### GitHub Actions

在仓库 Settings → Secrets and variables → Actions 中设置以下 Secrets，并启用 [转存工作流](.github/workflows/baidu-transfer.yml)：

| Secret | 说明 |
| --- | --- |
| `BAIDU_COOKIES` | `BDUSS=...; STOKEN=...` |
| `SHARE_URLS` | 分享链接，每行一个 |
| `SAVE_DIR` | 可选保存目录，默认 `/AutoTransfer` |
| `WECHAT_WEBHOOK` | 可选企业微信通知地址 |
| `TRANSFERSHARE_STATE_KEY` | 可选失败清单加密密钥；使用 `openssl rand -base64 32` 生成 |

获取 Cookies 的浏览器辅助脚本：

```sh
python -m pip install -r requirements-playwright.txt
python -m playwright install
python save_baidu_cookies.py --repo 1m01m0/baidu-autosave
```

脚本会打开浏览器，并在登录后提取 Cookies、写入指定仓库的 GitHub Secrets。用于其他 fork 时，请替换 `--repo`。也可以在百度网盘网页版开发者工具中手动取得 `BDUSS` 和 `STOKEN`。工作流支持手动触发；定时表达式见工作流文件。

## 配置与限制

分享链接使用 `https://pan.baidu.com/s/xxxxx?pwd=xxxx` 格式，可在链接后添加空格和目标目录。高级对象格式见配置指南。Cookies 是账号凭据，不应提交到 Git；运行前确认网盘空间充足、链接有效且有权转存。遇到频率限制时降低并发或增加延迟，Cookies 失效时重新获取。

## 开发

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -p 'test_*.py'
```

## 许可证

[MIT](LICENSE)
