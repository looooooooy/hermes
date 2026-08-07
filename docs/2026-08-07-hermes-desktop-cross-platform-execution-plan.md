# Hermes Desktop 跨平台工程执行设计

- 状态：工程执行基线 / 待实现
- 日期：2026-08-07
- 目标平台：macOS / Windows / Linux
- 产品名称：Hermes Desktop + Hermes Managed Runtime
- 首要目标：普通客户在一台没有 Python、pip、uv、Git、Node、Homebrew 等开发环境的电脑上，只安装一个 Hermes 桌面软件即可运行真实 Hermes Agent 并连接 Hermes Cloud
- 关联设计：
  - `docs/2026-08-07-hermes-managed-runtime-customer-installation-design.md`
  - `docs/2026-07-28-hermes-connector-commercial-architecture-design.md`
  - `docs/staging-real-full-chain-operator-runbook.md`
  - `docs/runtime-identity-owner-control-release-runbook.md`
  - `hermes-connector/docs/macos-runtime.md`

---

## 1. 执行结论

Hermes Desktop 不应被实现为“给 Hermes Agent 加一个桌面 UI”，也不应让客户分别安装 Hermes Core、Plugin、Connector、Python 和运行环境。

目标产品必须是一个由我们整体发行、整体验证、整体更新、整体回滚的桌面 Agent Runtime：

```text
Hermes Desktop
│
├── Desktop Shell
│   └── Tauri 2 + Svelte
│
├── Hermes Runtime Manager
│   └── Rust native background supervisor
│
├── Hermes Managed Runtime
│   ├── Private CPython
│   ├── Pinned uv
│   ├── Hermes Core
│   ├── Hermes Agent Plugin
│   ├── Hermes Connector
│   ├── dependency wheelhouse
│   ├── release manifest
│   └── receipts
│
└── Platform Integration
    ├── macOS
    ├── Windows
    └── Linux
```

客户只认识：

```text
Hermes
```

客户不认识：

```text
Python
pip
uv
venv
wheel
Plugin
Connector
LaunchAgent
systemd
Task Scheduler
Named Pipe
UDS
WSS
Access Token
```

工程复杂度必须全部封装在产品内部。

---

# 2. 最重要的工程原则

## 2.1 Zero Host Dependency Principle

Hermes Desktop 不得要求客户预安装：

- Python；
- pip；
- uv；
- Node.js；
- npm / pnpm；
- Git；
- Homebrew；
- Chocolatey；
- Cargo；
- Rust toolchain；
- C/C++ build tools；
- 开发 SDK。

普通客户的最低假设只有：

```text
受支持的操作系统
+ 磁盘空间
+ 用户权限
+ 网络（首次登录和 Cloud 连接需要）
```

运行时自身必须由 Hermes 提供。

注意：Linux GUI 底层可能依赖发行版提供的桌面 WebView / GTK 运行组件。这里的原则是“客户不手工准备开发环境”，不是承诺在任意最小化 Linux 镜像上零系统库运行。正式支持的 Linux 发行版必须有明确兼容矩阵，安装器负责验证或声明系统运行依赖。

## 2.2 System Python Is Untrusted

不论客户电脑有没有 Python，都统一视为：

```text
系统 Python = 不存在
```

禁止使用：

```text
/usr/bin/python
/usr/local/bin/python
/opt/homebrew/bin/python
python.exe from PATH
pyenv
conda
用户 venv
```

Hermes Runtime 只能使用受 Manifest 绑定的 Private CPython。

## 2.3 One Product Release, One Qualified Runtime Set

不能让客户自由组合：

```text
Core A + Plugin B + Connector C + Python D
```

必须由我们发布：

```text
Hermes Desktop 1.4.0

Desktop Shell       1.4.0
Runtime Manager     1.4.0
Private CPython     3.13.x + digest
uv                   pinned + digest
Hermes Core         upstream commit + patch set + digest
Agent Plugin        1.4.0 + digest
Connector           1.4.0 + digest
Runtime Contract    vN
Release Manifest    immutable digest
```

Cloud 必须能够看到这组事实。

## 2.4 Runtime Manager Is the Lifecycle Authority

跨平台以后，不允许：

- Desktop UI 自己直接 kill/start Host；
- Updater 自己直接覆盖当前运行目录；
- Connector 自己决定升级；
- Plugin 自己修改安装；
- 用户脚本成为正式生命周期控制面。

唯一生命周期控制器：

```text
Hermes Runtime Manager
```

职责：

- 安装；
- 验证；
- 激活；
- 启动；
- 停止；
- 健康检查；
- 更新；
- 回滚；
- 恢复；
- 诊断；
- 卸载协调；
- 上报设备运行版本与健康证据。

## 2.5 Desktop Shell Is Not Agent Authority

用户关闭窗口：

```text
Hermes Desktop window closed
```

必须保持：

```text
Runtime Manager     RUNNING
Hermes Host         RUNNING
Hermes Connector    RUNNING
Cloud               CONNECTED
```

只有用户显式选择：

```text
停止 Hermes
```

才停止 Agent Runtime。

---

# 3. 技术选型基线

## 3.1 Desktop Shell：Tauri 2 + Svelte

建议统一桌面 UI 技术栈：

```text
Tauri 2
+ Svelte
+ TypeScript
+ shared design system
```

选择原因：

1. 一份 UI 代码覆盖 macOS / Windows / Linux；
2. Rust 与 Runtime Manager 的工程语言一致；
3. 可以对文件系统、进程、更新、系统集成能力进行显式权限收敛；
4. 官方提供 macOS、Windows、Linux 的发行工具链；
5. 官方 updater 能作为 Desktop Shell 自更新基础，但 Managed Runtime 更新仍由我们的 Runtime Manager 控制；
6. 能继续使用现有 Web UI 设计资产，而不用维护三套原生 UI。

外部实现参考：

- Tauri 2 distribution：`https://v2.tauri.app/distribute/`
- Tauri updater：`https://v2.tauri.app/plugin/updater/`
- Windows installer：`https://v2.tauri.app/distribute/windows-installer/`

## 3.2 Rust Native Runtime Manager

新增一个真正的平台无关生命周期控制模块：

```text
hermes-desktop/src-tauri/
或
hermes-runtime-manager/
```

建议最终抽成独立 Rust crate / binary：

```text
hermes-runtime-manager
```

原因：

- Runtime Manager 必须在 Desktop UI 未打开时持续运行；
- UI crash 不应该影响 Agent；
- Runtime Manager 必须能够独立做 update / rollback；
- 平台服务管理和凭证调用更适合原生实现；
- 不应依赖 Python 来安装 Python 自己；
- 可以成为 Zero Host Dependency 的 bootstrap authority。

## 3.3 Python 继续用于 Agent Runtime

本方案不要求重写 Hermes Core、Plugin、Connector。

保持：

```text
Rust：安装、生命周期、平台边界
Python：Hermes Core / Plugin / Connector
Svelte：用户 UI
Cloud：远程控制面
```

不要为了桌面跨平台把 Hermes Core 重写成 Rust。

---

# 4. 目标仓库结构

建议新增：

```text
hermes-desktop/
├── README.md
├── package.json
├── vite.config.*
├── src/
│   ├── routes/
│   ├── components/
│   ├── stores/
│   ├── lib/
│   │   ├── runtime-client/
│   │   ├── cloud-client/
│   │   └── platform/
│   └── app.*
│
├── src-tauri/
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs
│       ├── commands/
│       ├── deep_link/
│       ├── updater/
│       └── tray/
│
└── tests/
    ├── onboarding/
    ├── runtime-status/
    └── update/
```

新增 Runtime Manager：

```text
hermes-runtime-manager/
├── Cargo.toml
├── src/
│   ├── main.rs
│   ├── domain/
│   │   ├── release.rs
│   │   ├── activation.rs
│   │   ├── health.rs
│   │   ├── update.rs
│   │   ├── rollback.rs
│   │   ├── recovery.rs
│   │   ├── uninstall.rs
│   │   └── toolchain.rs
│   │
│   ├── ports/
│   │   ├── service_manager.rs
│   │   ├── secret_store.rs
│   │   ├── process_identity.rs
│   │   ├── local_ipc.rs
│   │   ├── install_layout.rs
│   │   ├── updater.rs
│   │   └── power_state.rs
│   │
│   └── adapters/
│       ├── macos/
│       ├── windows/
│       └── linux/
│
└── tests/
    ├── contract/
    ├── activation/
    ├── rollback/
    └── recovery/
```

现有：

```text
hermes-agent-plugin/
hermes-connector/
hermes-runtime/
hermes-cloud/
```

继续保持。

---

# 5. 三平台共享架构

## 5.1 共享逻辑占 80% 以上

三平台必须共享：

- Release Manifest schema；
- Toolchain Manifest；
- 版本比较；
- digest 校验；
- 签名策略抽象；
- staged install；
- current / previous；
- activation state machine；
- health gate；
- rollback state machine；
- crash recovery；
- update policy；
- Cloud fleet status；
- Desktop ↔ Manager IPC contract；
- Runtime Manager ↔ Host/Connector process model；
- 日志和 receipt schema；
- 诊断包 schema；
- uninstall state machine。

不允许三平台各复制一套 activation 逻辑。

## 5.2 平台层只负责 OS 事实

平台 adapter 只能负责：

```text
如何启动后台进程？
如何验证进程身份？
如何建立本地安全 IPC？
如何保存 Secret？
目录放哪里？
如何响应 sleep / wake / shutdown？
如何安装/删除 OS integration？
如何验证代码签名？
```

业务协议和 release 规则不进入平台目录。

---

# 6. Runtime Manager 核心 Ports

## 6.1 ServiceManager

统一接口：

```text
install_service()
uninstall_service()
start()
stop()
restart()
status()
wait_ready()
```

平台实现：

| 平台 | 实现 |
|---|---|
| macOS | LaunchAgent |
| Windows | 用户级 Runtime Manager background registration；首选 Task Scheduler / 用户登录触发的受控后台进程，避免把用户 Agent 放进 LocalSystem |
| Linux | systemd --user；无 systemd 的发行版首发不承诺正式支持 |

重要：OS 只需要自动启动 Runtime Manager。

Host 和 Connector 由 Runtime Manager 统一启动和监督，而不是未来继续为每个平台复制两套独立 OS unit。

迁移阶段 macOS 可以继续兼容现有 Host + Connector LaunchAgent 模式，但最终目标是：

```text
OS
 ↓
Runtime Manager
 ├─ Host
 └─ Connector
```

## 6.2 LocalIpc

Desktop Shell 与 Runtime Manager 之间：

```text
macOS   → UDS
Linux   → UDS
Windows → Named Pipe
```

禁止使用 localhost TCP 作为默认管理接口。

IPC 必须：

- 限定当前用户；
- 验证 peer identity；
- 有 protocol version；
- 有最大帧大小；
- 有严格 method allowlist；
- 不传模型 API Key 明文到日志；
- 支持 request_id；
- mutation 有幂等策略；
- unknown effect 不假装 success。

建议协议：

```text
hermes.runtime.manager.v1
```

首发可以用 length-prefixed JSON；如果未来需要高频状态流，再评估 protobuf。

## 6.3 SecretStore

统一抽象：

```text
put(secret_ref, bytes)
get(secret_ref)
delete(secret_ref)
list_metadata()
```

平台实现：

| 平台 | Secret Store |
|---|---|
| macOS | Keychain |
| Windows | Windows Credential Manager / DPAPI-bound protected store |
| Linux | Secret Service API（受支持桌面环境）；必须定义 fail-closed 和企业 fallback，不允许明文文件 |

Provider API Key、Device Credential、refresh authority 不进入：

- argv；
- shell；
- environment variable；
-普通日志；
- GitHub artifact；
- Cloud projection。

## 6.4 ProcessIdentity

统一事实：

```text
pid
start_time
executable_path
executable_device/inode or platform equivalent
release_id
runtime_generation
host_bundle_id
```

macOS/Linux 使用 kernel/process/filesystem evidence。

Windows 使用：

- process handle；
- executable path；
- creation time；
- binary identity；
- ACL/security token facts。

不能只信 PID。

## 6.5 InstallLayout

逻辑目录固定：

```text
releases/
toolchains/
state/
logs/
receipts/
diagnostics/
```

平台映射：

### macOS

```text
~/Library/Application Support/Hermes/
~/Library/Logs/Hermes/
```

### Windows

```text
%LOCALAPPDATA%\Hermes\
```

企业策略需要机器级安装时，再增加：

```text
%ProgramFiles%\Hermes\
%ProgramData%\Hermes\
```

但普通员工首发保持 user-scoped runtime，避免权限复杂化。

### Linux

遵循 XDG：

```text
$XDG_DATA_HOME/hermes
$XDG_STATE_HOME/hermes
$XDG_CACHE_HOME/hermes
```

缺省时落到：

```text
~/.local/share/hermes
~/.local/state/hermes
~/.cache/hermes
```

---

# 7. Hermes Toolchain 设计

## 7.1 Toolchain 是正式 Release 输入

新增：

```text
toolchain-manifest.json
```

建议字段：

```json
{
  "schema_version": 1,
  "platform": "macos",
  "arch": "arm64",
  "python_version": "3.13.x",
  "python_sha256": "...",
  "uv_version": "...",
  "uv_sha256": "...",
  "wheelhouse_sha256": "...",
  "build_id": "..."
}
```

同一个 Desktop release 可以有：

```text
macos-arm64
macos-x86_64
windows-x86_64
windows-arm64 (later)
linux-x86_64
linux-arm64 (later)
```

## 7.2 ReleaseBuilder 必须去 PATH 化

当前 `hermes_local_release.py` 调用：

```text
uv
```

未来必须改成：

```text
<verified-toolchain>/uv
```

并把 Private Python 明确传入。

目标：

```text
PATH 不影响构建
系统 Python 不影响构建
Homebrew 不影响构建
Conda 不影响构建
```

## 7.3 Full Runtime Bundle

客户首次安装使用：

```text
ManagedRuntime-<version>-<platform>-<arch>.bundle
```

包含：

```text
manifest/
toolchain/
  python/
  uv/
wheelhouse/
core/
plugin/
connector/
services/
receipts/
```

必须支持离线安装 Runtime。

网络只用于：

- 登录；
- Cloud pairing；
- Cloud WSS；
- Provider API 调用；
- 后续更新下载。

---

# 8. 统一 Release Manifest

新增跨平台字段：

```text
release_id
product_version
platform
arch
runtime_contract
core_upstream_commit
core_patch_digest
core_artifact_digest
plugin_version
plugin_digest
connector_version
connector_digest
python_version
python_digest
uv_version
uv_digest
wheelhouse_digest
minimum_os_version
installer_channel
signing_identity
issued_at
expires_at optional
release_digest
```

Cloud Fleet 必须保存：

```text
device_id
agent_id
product_version
platform
arch
release_id
release_digest
runtime_generation
health
last_seen
update_channel
```

---

# 9. Desktop Shell 设计

## 9.1 UI 只做用户操作

页面：

```text
Status
Agent
Sessions
Cloud
Models
Skills
Updates
Diagnostics
Settings
About
```

首屏：

```text
Hermes

Agent        ● Running
Cloud        ● Connected
Remote       ● Available
Version      1.0.0

[Open Web Console]
```

## 9.2 Menu Bar / Tray

macOS menu bar、Windows system tray、Linux tray 行为统一：

```text
Hermes
────────────
● Agent 正常
● Cloud 已连接

打开 Hermes
打开 Web 控制台
诊断
检查更新
────────────
停止 Hermes
退出界面
```

“退出界面”不能停止 Runtime。

## 9.3 First Run

统一用户流程：

```text
Install
  ↓
Open Hermes
  ↓
Welcome
  ↓
Enterprise login
  ↓
Device pairing
  ↓
Install/activate Managed Runtime
  ↓
Model provider setup
  ↓
Health check
  ↓
Ready
```

禁止出现：

```text
pip
python
venv
terminal
access token
launchctl
systemctl
PowerShell command
```

---

# 10. macOS 平台设计

## 10.1 支持范围

首发：

```text
Apple Silicon arm64: P0
Intel x86_64: P1 / 根据客户占比决定
```

最低 macOS 版本通过发布矩阵冻结，不在代码中随意变化。

## 10.2 Installer

消费者：

```text
Hermes.dmg
```

企业：

```text
Hermes.pkg
```

必须：

- Developer ID 签名；
- notarization；
- staple；
- 完整 nested executable signing；
- Runtime bundle digest 验证。

## 10.3 Background

目标：

```text
LaunchAgent
   ↓
hermes-runtime-manager
   ├─ hermes host
   └─ hermes connector
```

现有两 LaunchAgent 架构先保持兼容，再逐步迁移成 Manager 单入口。

## 10.4 Local Transport

```text
UDS
```

继续复用当前已经验证的：

- 0600 descriptor；
- same-file validation；
- peer PID；
- process identity；
- runtime_generation；
- fail-closed discovery。

## 10.5 Secrets

```text
macOS Keychain
```

当前 Connector Keychain 安全设计继续作为基线。

---

# 11. Windows 平台设计

## 11.1 支持范围

第一正式目标：

```text
Windows 11 x86_64
```

再根据企业客户情况评估：

```text
Windows 10 maintained editions
Windows on ARM
```

## 11.2 Installer

首发同时保留两种能力：

消费者/自助：

```text
HermesSetup.exe
```

企业：

```text
Hermes.msi
```

目标支持：

- quiet install；
- enterprise deployment；
- code signing；
- uninstall registration；
- repair；
- version detection。

## 11.3 Background Strategy

不要默认把用户 Agent 放进 `LocalSystem` Windows Service。

Hermes Agent 使用用户 workspace、用户 credentials 和用户 session，因此首发应保持 user-scoped execution authority。

推荐模型：

```text
User logon
   ↓
Hermes Runtime Manager
   ├─ Hermes Host
   └─ Hermes Connector
```

OS bootstrap 可以使用：

```text
Task Scheduler / user logon registration
```

要求：

- 不弹出终端窗口；
- crash restart；
- 有明确 user SID 绑定；
- 不跨用户接管；
- 升级切换时 Runtime Manager 本身仍稳定。

以后如果出现 Windows Server / 共享机器场景，再单独设计 machine-scoped service，不把两种身份模型混为一谈。

## 11.4 Local Transport

Windows 不模拟 Unix socket path。

正式实现：

```text
Named Pipe
```

需要：

- ACL 只允许绑定用户；
- pipe server identity；
- peer process identity；
- 最大消息尺寸；
- versioned handshake；
- runtime_generation 绑定；
- endpoint replacement 防护；
- stale process fail closed。

目标命名：

```text
\\.\pipe\hermes\<user-sid>\local\<generation>
\\.\pipe\hermes\<user-sid>\control\<generation>
\\.\pipe\hermes\<user-sid>\observer\<generation>
\\.\pipe\hermes\<user-sid>\manager\v1
```

实际命名需经过 Windows ACL/namespace spike 验证后冻结。

## 11.5 Secrets

首选：

```text
Windows Credential Manager
```

必要时配合：

```text
DPAPI user-bound encryption
```

禁止用：

```text
.env
registry plaintext
普通 JSON
environment variable
```

## 11.6 Process Identity

实现 Windows adapter：

- OpenProcess；
- creation time；
- executable path；
- file identity；
- signer/Authenticode evidence（作为发行验证，不与 runtime identity 混为一谈）；
- user SID / token identity。

## 11.7 Windows P0 验收

必须在一台干净 Windows VM：

```text
No Python
No Git
No Node
No uv
No dev tools
```

执行：

```text
HermesSetup.exe
→ login
→ pair
→ Runtime Ready
→ Host Ready
→ Connector Ready
→ Cloud Connected
→ real prompt.submit
→ reboot
→ auto recovery
→ update
→ rollback drill
→ uninstall
```

全程不打开 PowerShell。

---

# 12. Linux 平台设计

## 12.1 不承诺“所有 Linux”

Linux 必须采用支持矩阵，而不是一句“支持 Linux”。

首发建议：

```text
Ubuntu LTS x86_64
```

第二阶段：

```text
Debian stable
Fedora / RHEL-family
Ubuntu arm64
```

不同 libc、桌面环境、secret service、systemd 可用性必须纳入测试矩阵。

## 12.2 Installer

首发：

```text
.deb
```

第二阶段：

```text
.rpm
AppImage optional
```

如果 GUI 依赖 WebKitGTK/GTK 等系统运行库，安装包必须声明依赖并在支持矩阵验证，不让用户手工寻找开发包。

## 12.3 Background

桌面用户：

```text
systemd --user
    ↓
hermes-runtime-manager
    ├─ Host
    └─ Connector
```

首发正式支持要求：

```text
systemd user session available
```

没有 systemd 的发行版：

```text
fail closed / unsupported
```

不要在 P0 同时维护 OpenRC、runit、SysV init。

## 12.4 Local Transport

```text
UDS
```

复用 macOS 的 contract，但平台实现独立。

需要验证：

- SO_PEERCRED；
- UID；
- PID；
- process start identity；
- socket owner/mode；
- stale descriptor；
- symlink/path replacement；
- runtime_generation。

## 12.5 Secrets

Linux 是三平台里最需要谨慎的 Secret 问题。

首发桌面环境支持：

```text
Secret Service API
```

必须明确测试：

- GNOME Keyring；
- KDE wallet / Secret Service compatibility；
- headless/unavailable secret service。

如果 Secret Store 不可用：

```text
不要自动降级到明文文件
```

应该：

```text
local_secret_store_unavailable
```

并在 UI 中明确提示管理员。

企业 headless Linux 的凭证模型另开设计，不和桌面版混在一起。

## 12.6 Linux P0 验收

干净 Ubuntu LTS VM：

```text
No Python
No pip
No uv
No Node
No Git
```

安装 `.deb` 后：

```text
Hermes Desktop
→ Runtime Manager
→ Private Python
→ Core
→ Plugin
→ Connector
→ Cloud
→ real prompt.submit
→ reboot/login
→ auto start
→ update
→ rollback
→ uninstall
```

用户不执行 shell 安装命令以外的任何依赖准备。

企业软件中心自动部署时，安装本身也应无人工终端步骤。

---

# 13. Runtime Manager 状态机

## 13.1 Runtime State

统一：

```text
ABSENT
DOWNLOADED
VERIFIED
STAGED
ACTIVATING
ACTIVE
DEGRADED
ROLLING_BACK
FAILED
UNINSTALLING
```

禁止用一个布尔：

```text
installed=true
```

替代真实状态。

## 13.2 Activation

```text
download/stage
  ↓
verify manifest
  ↓
verify platform/arch
  ↓
verify toolchain
  ↓
verify artifacts
  ↓
prepare private release dir
  ↓
install runtime
  ↓
preflight
  ↓
quiesce old runtime
  ↓
activate candidate
  ↓
Host health
  ↓
Connector health
  ↓
Cloud handshake
  ↓
mark current
```

任何后半段失败：

```text
ROLLBACK
```

## 13.3 Rollback

必须保留：

```text
current
previous
```

升级不直接覆盖 current 内容。

正确：

```text
release-1.3.2 immutable
release-1.4.0 immutable
current → 1.4.0
previous → 1.3.2
```

失败：

```text
current → 1.3.2
```

## 13.4 Interrupted Update Recovery

必须模拟：

- 下载中断；
- 解包中断；
- activation 中断；
- Host 已停、candidate 未起；
- candidate Host 起、Connector 未起；
- Cloud handshake 前断电；
- rollback 中断。

Runtime Manager 重启后必须读取 receipt/state 恢复，而不是猜测。

---

# 14. Desktop App Update 与 Runtime Update 分离

这是跨平台设计中的重要边界。

## 14.1 Desktop Shell Update

Tauri updater 负责：

```text
Hermes Desktop Shell
```

它不直接更新：

```text
Hermes Core
Plugin
Connector
Private Python
```

## 14.2 Managed Runtime Update

由 Runtime Manager 负责：

```text
Managed Runtime Bundle
```

Cloud 下发的是：

```text
update policy / signed manifest / channel
```

而不是任意 shell command。

## 14.3 双层版本

设备上报：

```text
desktop_version
runtime_manager_version
managed_runtime_version
runtime_contract
```

这样可以做到：

```text
Desktop UI 更新
≠
Agent Runtime 强制切换
```

---

# 15. Cloud Fleet Update Control

Cloud 未来需要：

```text
Fleet
├── Devices
├── Platform
├── Architecture
├── Desktop Version
├── Runtime Version
├── Health
├── Update Channel
├── Last Update
└── Rollback State
```

发布策略：

```text
internal
 ↓
canary 1%
 ↓
5%
 ↓
20%
 ↓
50%
 ↓
100%
```

支持：

- pause rollout；
- block bad release；
- force critical security update；
- platform-specific release；
- architecture-specific release；
- tenant ring；
- device pin；
- rollback recommendation。

---

# 16. Provider / Model Configuration

模型配置是产品配置，不是环境变量教学。

统一 UI：

```text
Models

OpenAI
DeepSeek
Kimi
Anthropic
OpenAI Compatible
```

输入：

```text
API Key
Base URL optional
Model
```

保存：

```text
Platform SecretStore
```

运行时通过受控本地 secret reference 获取。

Cloud 只能知道：

```text
provider configured = true
provider type = deepseek
```

不拿到 API Key。

---

# 17. Existing Hermes Migration

首次运行检测：

```text
legacy Hermes installation
legacy ~/.hermes
legacy skills
legacy workspace
```

原则：

```text
import, do not mutate in place
```

流程：

```text
Discover
 ↓
Read-only analyze
 ↓
Show migration plan
 ↓
Copy approved data
 ↓
Validate
 ↓
Start Managed Runtime
```

旧安装保持可回退，直到用户明确清理。

---

# 18. Uninstall

卸载分两类：

## 18.1 Remove App Only

保留：

- Workspace；
- Skills；
- user content。

停止并删除：

- Runtime Manager registration；
- Managed Runtime binaries。

## 18.2 Full Device Removal

增加：

- delete logs；
- delete runtime state；
- delete device credentials；
- remove provider keys（用户明确选择）；
- revoke Cloud device；
- remove launch/task/systemd registration。

不得留下：

```text
ghost Agent
ghost Connector
stale scheduled task
stale LaunchAgent
stale systemd unit
active DeviceCredential
```

---

# 19. Diagnostics

用户点击：

```text
诊断
```

Runtime Manager 自动检查：

```text
Desktop
Manager
Toolchain
Python
Core
Plugin
Connector
Local IPC
Cloud DNS/TLS/WSS
Credential Store
Disk
Permissions
Update state
Current/Previous release
```

输出：

```text
Hermes Diagnostic Bundle
```

默认必须脱敏：

- token；
- provider key；
- prompt body；
- approval body；
- raw tool output；
- private path details where unnecessary。

客户支持人员应该拿一个诊断包，而不是让客户截图 Terminal。

---

# 20. Recovery Matrix

三个平台必须共同验收：

| 故障 | 期望 |
|---|---|
| Desktop UI crash | Runtime 不受影响 |
| Runtime Manager crash | OS 自动恢复 Manager；Manager reconcile children |
| Host crash | Manager 检测、按策略恢复 |
| Connector crash | Manager 检测、恢复，不重复已完成 effect |
| Cloud unavailable | 本地 Agent 可继续；Connector 重连 |
| Network change | 自动重连 |
| sleep/wake | authority revalidate + reconnect |
| OS reboot | 登录后恢复 |
| update download fail | current 不受影响 |
| activation fail | rollback previous |
| disk full | fail closed before corrupt current |
| secret store unavailable | 不降级明文 |
| corrupt release | 不激活 |
| manifest mismatch | 不激活 |
| architecture mismatch | 不激活 |

---

# 21. 三平台安全模型

## 21.1 通用

- signed release；
- immutable manifest；
- digest verification；
- no shell install execution from Cloud；
- least privilege；
- user-scoped identity；
- local IPC peer validation；
- secret store；
- audit receipts；
- fail closed。

## 21.2 macOS

- Developer ID；
- notarization；
- Keychain；
- UDS；
- LaunchAgent；
- file owner/mode/process evidence。

## 21.3 Windows

- Authenticode；
- user SID；
- Credential Manager / DPAPI；
- Named Pipe ACL；
- process token；
- installer signature。

## 21.4 Linux

- package/release signing；
- UID；
- Secret Service；
- UDS permissions + SO_PEERCRED；
- systemd user unit；
- package manager integrity。

---

# 22. Build Matrix

CI 最终需要：

```text
macos-arm64
macos-x86_64
windows-x86_64
linux-x86_64
```

后续：

```text
windows-arm64
linux-arm64
```

每个平台构建：

```text
Desktop Shell
Runtime Manager
Private Python toolchain
uv
Core wheel
Plugin bundle
Connector wheel
wheelhouse
Managed Runtime Bundle
Installer
SHA256SUMS
release manifest
SBOM
provenance
```

---

# 23. CI / CD Pipelines

建议新增：

```text
.github/workflows/
├── desktop-contracts.yml
├── desktop-macos-build.yml
├── desktop-windows-build.yml
├── desktop-linux-build.yml
├── managed-runtime-build.yml
├── managed-runtime-blank-machine-e2e.yml
├── managed-runtime-update-rollback.yml
└── desktop-release.yml
```

## 23.1 desktop-contracts

任何平台都跑：

- Rust unit；
- Svelte unit；
- Runtime Manager contract；
- manifest schema；
- activation state machine；
- rollback state machine；
- local IPC protocol；
- secret redaction。

## 23.2 Blank Machine E2E

这是 P0 中最重要的 CI。

创建没有开发环境的 VM：

```text
No Python
No Node
No Git
No uv
```

只安装正式 installer。

验收：

```text
installer
→ first run
→ runtime
→ local control
→ Cloud
→ live Session
```

---

# 24. 支持矩阵必须是事实，不是营销文案

建议引入：

```text
contracts/runtime/platform-support-v1.json
```

类似：

```json
{
  "macos": {
    "arm64": "production",
    "x86_64": "planned"
  },
  "windows": {
    "x86_64": "development"
  },
  "linux": {
    "ubuntu-lts-x86_64": "development"
  }
}
```

Connector 和 Plugin 的 availability 只能由真实能力切换。

不能因为目录存在就报告支持。

当前 Windows/Linux `available=False` 的做法是正确的；每个平台只有完整闭环通过后才切换为 true。

---

# 25. Platform Capability Gate

Windows/Linux 从 `available=False` 进入 production 必须逐项满足：

```text
[ ] local transport
[ ] peer identity
[ ] secret store
[ ] process identity
[ ] runtime manager bootstrap
[ ] background startup
[ ] shutdown/restart
[ ] release install
[ ] activation
[ ] rollback
[ ] connector pairing
[ ] Cloud WSS
[ ] live Session observe
[ ] live Session control
[ ] restart persistence
[ ] update
[ ] uninstall
[ ] blank-machine install
[ ] security scan
```

任何一项不满足：

```text
available=False
```

---

# 26. 工程执行阶段

## Phase 0 — Freeze Contracts

目标：不写三套逻辑。

任务：

```text
D0.1 Runtime Manager domain model
D0.2 platform ports
D0.3 local IPC v1
D0.4 toolchain manifest v1
D0.5 managed release manifest v1
D0.6 platform support contract
D0.7 status/health model
D0.8 update/rollback state machine
D0.9 diagnostic schema
```

验收：

- 纯 Rust domain test；
- 不依赖具体 OS；
- Linux/Windows/macOS adapter 可以替换。

## Phase 1 — Desktop Shell Foundation

任务：

```text
D1.1 create hermes-desktop
D1.2 Tauri 2 shell
D1.3 Svelte UI
D1.4 tray/menu abstraction
D1.5 deep link login return
D1.6 Desktop ↔ Runtime Manager IPC
D1.7 Status screen
D1.8 onboarding shell
D1.9 update screen
D1.10 diagnostics screen
```

验收：

三平台都能启动同一个 UI shell，即使 Runtime 暂不可用也必须显示真实 `unsupported/unavailable`，不能 fake green。

## Phase 2 — Zero Host Toolchain

任务：

```text
D2.1 pinned CPython acquisition/build provenance
D2.2 pinned uv
D2.3 offline wheelhouse
D2.4 toolchain manifest
D2.5 eliminate PATH uv
D2.6 eliminate system Python discovery
D2.7 toolchain integrity gate
D2.8 private runtime paths
D2.9 blank-host toolchain test
```

验收：

系统删除/不存在 Python 后仍能安装。

## Phase 3 — macOS Production Closure

任务：

```text
D3.1 Runtime Manager macOS adapter
D3.2 migrate activation engine contracts
D3.3 LaunchAgent manager bootstrap
D3.4 UDS manager IPC
D3.5 Keychain
D3.6 DMG
D3.7 PKG
D3.8 signing/notarization
D3.9 update/rollback
D3.10 blank Mac E2E
```

验收：

Mac Customer Installation Closure 完成。

## Phase 4 — Windows Platform Closure

任务：

```text
D4.1 Windows Runtime Manager bootstrap
D4.2 Named Pipe manager IPC
D4.3 Plugin Named Pipe Local/Control/Observer
D4.4 Connector Named Pipe discovery/client
D4.5 peer PID/SID identity
D4.6 Credential Manager/DPAPI SecretStore
D4.7 Windows process identity
D4.8 user logon startup
D4.9 EXE installer
D4.10 MSI installer
D4.11 code signing
D4.12 update/rollback
D4.13 reboot/recovery
D4.14 blank Windows E2E
```

完成后才允许：

```text
windows.available=True
```

## Phase 5 — Linux Platform Closure

任务：

```text
D5.1 Linux Runtime Manager adapter
D5.2 systemd --user
D5.3 UDS manager IPC
D5.4 Plugin Linux UDS Local/Control/Observer
D5.5 Connector Linux discovery/client
D5.6 SO_PEERCRED identity
D5.7 Secret Service adapter
D5.8 XDG layout
D5.9 .deb
D5.10 update/rollback
D5.11 reboot/login recovery
D5.12 blank Ubuntu E2E
```

完成后才允许：

```text
linux.available=True
```

## Phase 6 — Fleet Release Control

任务：

```text
D6.1 Cloud desktop/runtime version facts
D6.2 signed update manifest
D6.3 update channels
D6.4 staged rollout
D6.5 pause/kill switch
D6.6 bad release block
D6.7 forced security update
D6.8 fleet health dashboard
D6.9 rollback telemetry
```

---

# 27. 优先级

## P0

```text
shared Runtime Manager
zero-host toolchain
Desktop Shell
macOS full closure
Windows architecture spikes
Linux architecture spikes
```

## P1

```text
Windows production closure
Linux Ubuntu production closure
Fleet rollout
enterprise PKG/MSI/deb deployment
```

## P2

```text
Intel Mac if still needed
Windows ARM
Linux ARM
RPM
AppImage
additional Linux distributions
headless server SKU
```

---

# 28. Windows / Linux Spike 必须提前做

虽然正式顺序是 macOS → Windows → Linux，但以下风险不能等到 macOS 完成以后才发现。

立即做两个 2~3 天工程 spike：

## Windows Spike

证明：

```text
Rust manager
↔ Named Pipe
↔ Python test Host
```

同时证明：

```text
user SID ACL
peer PID
Credential Manager
user logon restart
```

## Linux Spike

证明：

```text
Rust manager
↔ UDS
↔ Python test Host
```

同时证明：

```text
SO_PEERCRED
systemd --user
Secret Service
.deb runtime dependencies
```

Spike 不把平台标记为支持，只提前消灭架构未知数。

---

# 29. Definition of Done：Desktop Platform Closure

每个平台只有同时满足以下条件才叫“支持”：

## Install

- 官方安装包；
- 不要求开发环境；
- Private Python；
- Private uv；
- offline runtime install；
- signed release；
- 正确 uninstall registration。

## First Run

- 企业登录；
- device pairing；
- model setup；
- real Agent；
- Cloud ready；
- 不使用 Terminal。

## Runtime

- Host real process；
- Plugin real platform transport；
- Connector independent process；
- Cloud WSS；
- live authoritative Session；
- prompt.submit；
- interrupt；
- approval；
- clarify。

## Reliability

- UI crash；
- manager crash；
- host crash；
- connector crash；
- network loss；
- sleep/wake；
- reboot；
- update interruption；
- disk full；
- Cloud restart。

## Upgrade

- download；
- verify；
- staged install；
- activate；
- health；
- rollback；
- previous remains bootable。

## Security

- signed installer；
- signed runtime manifest；
- SecretStore；
- local IPC peer identity；
- no plaintext token；
- no prompt body in default logs；
- no public local port；
- uninstall revokes device。

## Operations

- diagnostics；
- sanitized support bundle；
- Cloud version facts；
- fleet rollout；
- bad release block。

---

# 30. P0 任务树

建议把第一轮真正开发任务冻结为：

```text
EPIC DESKTOP-0  Cross-platform foundation
├── DESKTOP-001 hermes-desktop scaffold
├── DESKTOP-002 Tauri/Svelte shell
├── DESKTOP-003 hermes-runtime-manager crate/binary
├── DESKTOP-004 Runtime Manager domain state machine
├── DESKTOP-005 Platform ServiceManager port
├── DESKTOP-006 Platform SecretStore port
├── DESKTOP-007 Platform LocalIpc port
├── DESKTOP-008 Platform ProcessIdentity port
├── DESKTOP-009 InstallLayout port
├── DESKTOP-010 manager IPC v1
├── DESKTOP-011 Toolchain Manifest v1
├── DESKTOP-012 Managed Release Manifest v1
├── DESKTOP-013 remove PATH uv dependency
├── DESKTOP-014 Private CPython toolchain
├── DESKTOP-015 offline wheelhouse contract
├── DESKTOP-016 Desktop status UI
├── DESKTOP-017 onboarding UI
├── DESKTOP-018 diagnostics UI
├── DESKTOP-019 tray abstraction
└── DESKTOP-020 blank-machine test harness

EPIC DESKTOP-MAC  macOS closure
├── MAC-001 LaunchAgent Runtime Manager
├── MAC-002 Manager UDS
├── MAC-003 Keychain bridge
├── MAC-004 current activation migration
├── MAC-005 DMG
├── MAC-006 PKG
├── MAC-007 signing
├── MAC-008 notarization
├── MAC-009 updater/rollback
└── MAC-010 blank Mac full chain

EPIC DESKTOP-WIN  Windows closure
├── WIN-001 Named Pipe spike
├── WIN-002 SID/ACL spike
├── WIN-003 Credential Manager spike
├── WIN-004 process identity spike
├── WIN-005 Runtime Manager bootstrap
├── WIN-006 Plugin Named Pipe backend
├── WIN-007 Connector Named Pipe client
├── WIN-008 EXE/MSI
├── WIN-009 update/rollback
└── WIN-010 blank Windows full chain

EPIC DESKTOP-LINUX Linux closure
├── LINUX-001 UDS peer identity spike
├── LINUX-002 systemd-user spike
├── LINUX-003 Secret Service spike
├── LINUX-004 Runtime Manager adapter
├── LINUX-005 Plugin Linux UDS backend
├── LINUX-006 Connector Linux UDS client
├── LINUX-007 .deb
├── LINUX-008 update/rollback
└── LINUX-009 blank Ubuntu full chain
```

---

# 31. 第一轮不做什么

为了防止范围爆炸，首轮明确不做：

- 所有 Linux 发行版；
- Windows Server 多用户 service；
- root/system Agent runtime；
- Docker Desktop runtime；
- Kubernetes local Agent；
- 把 Agent 改写成 Rust；
- 把 Connector 合并进 Host；
- 允许 Cloud 下发 shell command 安装；
- 动态从 PyPI 拼装生产环境；
- 客户自己选择 Python；
- 客户自己选择 Core/Plugin/Connector 版本；
- 自动降级 Secret 到明文文件。

---

# 32. 当前代码差距

当前仓库已具备：

```text
macOS Plugin platform implementation
macOS Connector platform implementation
macOS immutable release builder
macOS LaunchAgent generation
macOS activation / rollback controller
Keychain credential path
Cloud/Connector WSS
real local staging evidence
```

当前明确缺失：

```text
Hermes Desktop project
cross-platform Runtime Manager
Private CPython formal toolchain
PATH-independent uv
Desktop ↔ Manager IPC
Windows local transport
Windows service/bootstrap
Windows secret store
Linux local transport
Linux service/bootstrap
Linux secret store
cross-platform installer CI
blank-machine CI
fleet desktop/runtime release model
```

当前 Windows/Linux Connector availability 是 fail-closed，这个状态必须保持到真实闭环完成。

---

# 33. 架构红线

以下决定不得在实现中被悄悄改变：

1. **客户不安装 Python。**
2. **系统 Python 永远不成为生产 Runtime 依赖。**
3. **Core + Plugin + Connector + Python 是我们限定的 Managed Runtime。**
4. **Runtime Manager 是唯一 lifecycle authority。**
5. **Desktop UI crash 不得停止 Agent。**
6. **Host 与 Connector 保持进程隔离。**
7. **Cloud 不成为 Agent execution authority。**
8. **Provider Secret 不离开执行主机。**
9. **Windows/Linux 没有真实平台能力前必须 fail closed。**
10. **升级失败必须能够回滚 previous release。**
11. **不在客户机器上从公网 PyPI 临时拼装生产环境。**
12. **三平台只共享 domain/contract，不共享错误的 OS 假设。**
13. **支持平台必须由 blank-machine full-chain evidence 证明。**

---

# 34. 最终用户体验验收

一个完全不了解 Python 的员工：

### macOS

```text
下载 Hermes.dmg
→ 拖到 Applications
→ 打开
→ 登录
→ 完成
```

### Windows

```text
下载 HermesSetup.exe
→ Next / Install
→ 打开
→ 登录
→ 完成
```

### Linux

```text
企业软件中心 / 正式 .deb 安装
→ 打开 Hermes
→ 登录
→ 完成
```

用户不应该知道系统内部存在：

```text
Private Python
Core
Plugin
Connector
UDS
Named Pipe
systemd
LaunchAgent
Task Scheduler
runtime_generation
```

而运维人员必须能够从 Cloud 和 Diagnostic Bundle 精确知道它们的真实状态。

这就是 Hermes Desktop 从工程项目进入企业级桌面产品的完成标准。
