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

## 同一个身份，连接多个平台

GitHub 和 LinuxDO 是登录身份，不绑定某一个签到平台。同一个身份可以分别登录所有**已适配、且实际提供对应登录入口**的平台；目前已适配 AnyRouter 与 AgentRouter。

1. 添加账号时先选择「使用 GitHub 继续」或「使用 LinuxDO 继续」，再勾选一个或多个目标平台。
2. 码粮按选择顺序打开各平台的授权窗口；请在每个窗口使用你希望关联的同一个身份完成授权。
3. 每成功连接一个平台，账号列表中都会新增一条该平台的独立账号记录，可分别签到、查看余额和重新授权。
4. 已有 GitHub / LinuxDO 授权账号可通过「连接其他平台」继续添加平台，不必重复填写备注与登录方式。

**会话不跨站复用**：每个平台签发的 Cookie、站内用户 ID 均独立加密保存，不复制给其他平台，也不会仅凭相同备注或登录方式自动合并账号。本应用不保存 GitHub / LinuxDO 的密码或上游登录会话。

授权窗口使用独立临时浏览器，不共享日常 Chrome 的登录状态；不需要提前在日常浏览器登录 LinuxDO，请在本次授权窗口里完成 LinuxDO 登录与授权，网站验证也需本人手动完成。仅检测到 Cookie 不等于已登录，应用会在返回目标平台后验证账号。

AnyRouter 首次登录页若未显示第三方入口，应用会尝试点击站内「注册 → 登录」链接，再查找对应授权按钮；该过程只导航页面，不填写或提交注册信息。AgentRouter 继续直接使用现有登录入口，不走这段导航。

如果当前页面仍未显示可识别的入口，本次授权会明确提示未完成，而非断言平台完全不支持；你可以重试或跳过该平台继续连接其他平台。取消剩余授权不会删除已成功添加的账号。邮箱密码与手动 Session Cookie 仍一次添加一个平台。

> 平台是否开放 GitHub / LinuxDO 登录，以实际登录页为准；“已适配”不等于保证该平台当前启用所有登录方式。跨平台连接不代表登录一次即可自动获得所有平台的授权或额度。

## 技术栈

Python 3.11+ · FastAPI · Uvicorn · SQLite · cryptography(Fernet) · Playwright · pywebview · PyInstaller

## 下载安装

到 [Releases](https://github.com/weiguang1017/codefuel/releases) 下载：

| 平台 | 文件 |
|---|---|
| macOS（Apple Silicon，M 系列芯片） | `CodeFuel-macOS-arm64.dmg` |
| macOS（Intel 芯片） | `CodeFuel-macOS-x86_64.dmg` |
| Windows 10/11 | `CodeFuel-*-setup.exe` |

仅分发 DMG 和 EXE 安装包，不再提供 ZIP 便携包。

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
uv run python tests/ui_platform_login.py  # 多平台授权界面回归：模拟 API，不登录真实站点
```

多平台界面回归使用本机 Chrome 和仅监听 `127.0.0.1` 的临时静态服务，覆盖逐平台授权、部分失败、取消、重试与认证方式切换。运行会写入账号的旧版 `tests/ui_check.py` 时，必须显式指定隔离的 `CODEFUEL_DATA_DIR` 与本地测试服务 `CODEFUEL_TEST_URL`，不要指向日常使用的数据或服务。

## 打包

```bash
bash build/build_macos.sh        # macOS：产出 .app、.dmg（使用当前 Mac 架构）
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
