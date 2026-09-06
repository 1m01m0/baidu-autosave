# TransferShare

[中文](README.md) | English

TransferShare saves files from Baidu Pan share links into your own Baidu Pan storage. It can run locally or on a GitHub Actions schedule. This repository is a fork of [Jack261108/transfershare](https://github.com/Jack261108/transfershare).

## Run

### Run from source

Requires Python 3.9+ and the tools needed to build the BaiduPCS submodule.

```sh
git clone --recurse-submodules https://github.com/1m01m0/baidu-autosave.git
cd baidu-autosave
python -m pip install -r requirements.txt
./scripts/build_baidupcs_submodule.sh
cp config.example.json config.json
```

Edit `config.json` with your Cookies and share links, then run:

```sh
./scripts/run_transfer_task.sh
```

You can also run `python transfer_runner.py` directly. The runner prefers root `config.json` and falls back to environment variables when that file is absent. See [CONFIG_GUIDE.md](CONFIG_GUIDE.md) for configuration, filtering, renaming, and concurrency settings.

### GitHub Actions

Configure the following Secrets under Settings → Secrets and variables → Actions, then enable the [transfer workflow](.github/workflows/baidu-transfer.yml):

| Secret | Purpose |
| --- | --- |
| `BAIDU_COOKIES` | `BDUSS=...; STOKEN=...` |
| `SHARE_URLS` | One share link per line |
| `SAVE_DIR` | Optional destination; defaults to `/AutoTransfer` |
| `WECHAT_WEBHOOK` | Optional WeCom notification webhook |
| `TRANSFERSHARE_STATE_KEY` | Optional failed-item state encryption key; generate with `openssl rand -base64 32` |

To use the browser helper for Cookies:

```sh
python -m pip install -r requirements-playwright.txt
python -m playwright install
python save_baidu_cookies.py --repo 1m01m0/baidu-autosave
```

The helper opens a browser, extracts Cookies after login, and writes GitHub Secrets to the specified repository. Change `--repo` for another fork. You can also obtain `BDUSS` and `STOKEN` manually through Baidu Pan browser developer tools. The workflow supports manual runs; its schedule is defined in the workflow file.

## Configuration and limitations

Use share links in the form `https://pan.baidu.com/s/xxxxx?pwd=xxxx`, optionally followed by a space and destination directory. Advanced object syntax is described in the configuration guide. Cookies are account credentials and must not be committed. Confirm storage capacity, link validity, and permission to save the content. Reduce concurrency or increase delays when rate-limited; refresh expired Cookies.

## Development

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -p 'test_*.py'
```

## License

[MIT](LICENSE)
