# 码粮 CodeFuel

> 为代码添粮，让灵感续航。

面向开发者的桌面签到与额度控制台，自动完成 **AnyRouter** 与 **AgentRouter** 的每日签到，集中展示额度、运行记录与调度状态。仅支持 macOS 与 Windows 桌面端。

## 🎁 注册即多得 $50.00 额度

还没注册账号？通过下面的专属链接注册，**可额外获得 $50.00 奖励额度**：

| 平台 | 专属注册链接 |
|---|---|
| **AgentRouter** | <https://agentrouter.org/register?aff=MBCp> |
| **AnyRouter** | <https://anyrouter.top/register?aff=x73u> |

注册后把账号加进码粮，每日签到即可持续累积额度。

## 特性

- **自动签到**：按账号设定间隔在后台执行，休眠或关机后的到期任务会在下次启动时补做。
- **来源真实**：签到结果严格区分 `success / already_checked / synced / manual_required / failed / interrupted`，不把"查询成功"当作"已领取额度"。
- **多种登录**：GitHub 授权、LinuxDO 授权、邮箱密码、已有 Session；授权过程中不接触你的密码。
- **本地优先**：只监听 `127.0.0.1`，凭据用 Fernet 加密后落在本机 SQLite，不上传任何服务器。
- **可追溯**：每次执行留存站点真实响应摘要，日志可导出 CSV（已做公式注入防护）。

## 技术栈

Python 3.11+ · FastAPI · Uvicorn · SQLite · cryptography(Fernet) · Playwright · pywebview · PyInstaller

## 下载安装

到 [Releases](https://github.com/weiguang1017/codefuel/releases) 下载：

| 平台 | 文件 |
|---|---|
| macOS（Apple Silicon） | `CodeFuel-macOS-arm64.dmg` 或 `.zip` |
| Windows 10/11 | `CodeFuel-*-setup.exe` 或免安装的 `CodeFuel-Windows-portable-*.zip` |

安装包未做代码签名。macOS 首次打开需右键 →「打开」；Windows 遇 SmartScreen 点「更多信息」→「仍要运行」。

**运行前提**：本机需已安装 **Google Chrome**，签到复用系统浏览器，不额外下载 Chromium。

## 开发

```bash
uv sync --dev          # 安装依赖
uv run pytest -q       # 运行测试
uv run desktop.py      # 启动桌面窗口
```

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

推送 `v*` 标签会触发 GitHub Actions 自动构建两个平台的安装包并发布 Release。

## 数据位置

| 平台 | 路径 |
|---|---|
| macOS | `~/Library/Application Support/CodeFuel` |
| Windows | `%APPDATA%\CodeFuel` |

从旧版本升级时，`DailyQuota` 目录下的数据库与密钥会在首次启动自动复制过来，旧数据保留不删。也可用环境变量 `CODEFUEL_DATA_DIR` 指定其它位置。

## 免责声明

本项目仅用于管理你自己账号的每日额度，不绕过任何站点的访问控制。签到结果以站点真实响应为准；遇到验证码或风控，请到官网手动处理。

## 许可

签到逻辑部分遵循 BSD-2-Clause，原始许可证保留在 `vendor/full-upstream/LICENSE`。
