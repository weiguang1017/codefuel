# 码粮 CodeFuel

> 为代码添粮，让灵感续航。

面向开发者的桌面签到与额度控制台，自动完成 **AnyRouter** 与 **AgentRouter** 的每日签到，集中展示额度、运行记录与调度状态。仅支持 macOS 与 Windows 桌面端。

## 特性

- **自动签到**：按账号设定间隔在后台执行，休眠或关机后的到期任务会在下次启动时补做。
- **来源真实**：签到结果严格区分 `success / already_checked / synced / manual_required / failed / interrupted`，不把"查询成功"当作"已领取额度"。
- **多种登录**：GitHub 授权、LinuxDO 授权、邮箱密码、已有 Session；授权过程中不接触你的密码。
- **本地优先**：只监听 `127.0.0.1`，凭据用 Fernet 加密后落在本机 SQLite，不上传任何服务器。
- **可追溯**：每次执行留存站点真实响应摘要，日志可导出 CSV（已做公式注入防护）。

## 技术栈

Python 3.11+ · FastAPI · Uvicorn · SQLite · cryptography(Fernet) · Playwright · pywebview · PyInstaller

## 开发

```bash
uv sync --dev          # 安装依赖
uv run pytest -q       # 运行测试
uv run desktop.py      # 启动桌面窗口
```

浏览器能力复用系统已安装的 Google Chrome（通过 `CLOAKBROWSER_BINARY_PATH` 指定），无需额外下载 Chromium。

### 常用自检

```bash
uv run desktop.py --selftest          # 仅启动本地服务，不打开窗口
uv run desktop.py --browser-selftest  # 用本地页面验证浏览器驱动，不联网
uv run desktop.py --smoke             # 打开真实窗口读取页面后退出
```

## 打包

```bash
bash build/build_macos.sh        # macOS：产出 .app、.zip、.dmg
build\build_windows.bat          # Windows：产出 dist\CodeFuel\CodeFuel.exe
```

产物写入 `dist/`，不进版本库。

## 数据位置

| 平台 | 路径 |
|---|---|
| macOS | `~/Library/Application Support/DailyQuota` |
| Windows | `%APPDATA%\DailyQuota` |

目录名沿用 `DailyQuota` 是为了让改名前的账号与密钥继续可用。可用环境变量 `CODEFUEL_DATA_DIR` 覆盖。

## 免责声明

本项目仅用于管理你自己账号的每日额度，不绕过任何站点的访问控制。签到结果以站点真实响应为准；遇到验证码或风控，请到官网手动处理。

## 许可

`vendor/full-upstream/` 下的签到逻辑派生自 [millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in)，遵循 BSD-2-Clause，原始许可证保留在 `vendor/full-upstream/LICENSE`。
