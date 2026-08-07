# Hermes Managed Runtime：客户安装、发行、升级与生命周期闭环设计

- 状态：工程设计基线 / 待实现
- 日期：2026-08-07
- 适用阶段：macOS 商用首发
- 目标对象：Hermes Desktop、Hermes Core、Hermes Agent Plugin、Hermes Connector、Hermes Cloud
- 关联文档：
  - `docs/2026-07-28-hermes-connector-commercial-architecture-design.md`
  - `docs/staging-real-full-chain-operator-runbook.md`
  - `docs/runtime-identity-owner-control-release-runbook.md`
  - `docs/local-install-validation.md`
  - `hermes-connector/docs/macos-runtime.md`

## 1. 结论先行

Hermes 当前正在从“可运行的开源 Agent + 商用连接组件”进入“可规模化交付的企业桌面 Agent Runtime”阶段。下一阶段的关键工作不应继续让客户直接面对 Core、Plugin、Connector、Python、venv、LaunchAgent、Token、WSS、UDS 等工程概念，而应把这些能力收敛为一个完整、受控、可升级、可回滚的桌面产品。

正式产品定义：

> **Hermes Managed Runtime** 是由我们发行、验证、激活、升级和回滚的完整 Hermes 本地运行时；`Hermes.app` 是它的 Desktop Shell + Runtime Manager，而不是 Agent 执行进程本身。

客户最终只安装一个产品：

```text
Hermes.dmg
   ↓
Hermes.app
   ↓
首次运行 / 登录 / 配对
   ↓
自动安装并激活 Managed Runtime
   ↓
Hermes Host + Agent Plugin + Connector
   ↓
Hermes Cloud
```

客户不应该被要求安装或理解：

- Python、pip、uv、venv；
- Git 或源码仓库；
- Hermes Core 单独安装包；
- Hermes Agent Plugin 单独安装；
- Hermes Connector 单独安装；
- LaunchAgent plist；
- Nginx、PostgreSQL、SQLite migration 命令；
- Cloud access token、Connector token、WSS ticket；
- 手工 TLS、UDS、端口和守护进程配置。

Cloud、Nginx、数据库和公网 TLS 属于服务端基础设施，不属于普通客户电脑的安装内容。

---

## 2. 为什么这一步是产品闭环，而不是“打包优化”

如果客户的安装说明仍然类似：

```text
1. 安装 Hermes Agent
2. 安装 Python
3. pip install Plugin
4. pip install Connector
5. 配环境变量
6. 配 Cloud URL
7. 放 Token
8. 配 LaunchAgent
9. 启动 Agent
10. 启动 Connector
```

那么我们拥有的只是“工程团队可以部署的系统”，还不是“企业员工可以安装的产品”。

一旦进入企业规模部署，以下问题会迅速成为主要成本：

- Hermes Core、Plugin、Connector 的兼容矩阵失控；
- upstream Hermes 升级导致客户机器随机失配；
- Python 版本和本地包管理器差异；
- 客户终端中的环境变量、shell、PATH、venv 污染；
- Connector 没有启动、重复启动或连接到错误 Runtime；
- Plugin 已更新而 Core 没更新；
- 回滚只能依赖技术支持人员远程操作；
- 客户不知道“Cloud 在线”和“Agent 可执行”是两个不同状态；
- 升级失败后设备成为半安装状态；
- 卸载后残留守护进程、凭证和 Cloud DeviceCredential；
- 任何一个环节都可能产生大量无法标准化的售后工单。

因此，Managed Runtime 不是包装层，而是把已有架构转化为可商业交付能力的关键控制面。

---

## 3. 核心产品原则

### 3.1 客户只认识一个 Hermes

客户产品视图：

```text
Hermes
├── 状态
├── Agent
├── Cloud
├── 模型服务
├── 更新
├── 诊断
└── 设置
```

内部工程视图：

```text
Hermes Desktop
│
├── Desktop Shell / Runtime Manager
│
├── Hermes Host
│   └── Patched Hermes Core
│       └── Hermes Agent Plugin
│
├── Hermes Connector
│
└── Managed Runtime State
    ├── Release Manifest
    ├── Activation Receipt
    ├── Health Evidence
    ├── Current / Previous Release
    └── Update / Rollback State
```

这两个视图必须严格分离。用户体验层不暴露内部组件复杂度。

### 3.2 一个产品版本对应一组经过验证的组件版本

不允许客户自由组合：

```text
Core A + Plugin B + Connector C
```

而应由我们定义完整发行单元：

```text
Hermes Desktop 1.3.2

Hermes Core       = upstream 0.xx + pinned patch set
Hermes Plugin     = 1.3.2
Hermes Connector  = 1.3.2
Runtime Contract  = vN
Desktop Manager   = 1.3.2
Release Manifest  = immutable digest
```

Cloud 必须能够读取和审计该组合，而不是只看到一个模糊的 Agent 版本。

### 3.3 我们控制发行，但不永久 fork Hermes

长期正确路线：

```text
Hermes upstream
      ↓
Upstream Sync
      ↓
Compatibility / Patch Layer
      ↓
Plugin + Connector Integration
      ↓
Full-chain Qualification
      ↓
Hermes Managed Runtime Release
```

我们的长期资产应是：

- Patch / Compatibility Layer；
- Agent Plugin；
- Connector；
- Cloud；
- Runtime Manager；
- Release / Update / Rollback 控制面；
- Enterprise capabilities；
- Protocol / Contract；
- Security / Audit / Fleet Management。

不应把“维护整个 Hermes Core 的永久分叉”当作产品价值本身。

### 3.4 Agent 执行权威永远在客户本机

必须保持现有产品不变量：

- PC / Server 是唯一 Agent execution authority；
- Cloud 不成为第二个 Agent；
- Cloud 不直接持有模型 provider credentials；
- 模型 provider credentials 不离开 Hermes 执行主机；
- Connector 是独立本地服务，不导入 Agent 私有实现；
- H5 / Mobile 只能通过 Cloud 与 Connector 间接观察和控制 Agent。

### 3.5 关闭 UI 不等于关闭 Agent

`Hermes.app` 是管理界面，不应该承担核心 Agent 生命周期。

正确行为：

```text
关闭 Hermes.app 窗口
       ↓
Hermes Host 继续运行
Hermes Connector 继续运行
Cloud 仍可观察状态
```

用户需要显式选择“停止 Hermes Agent”或“退出所有 Hermes 服务”时，才停止后台服务。

---

## 4. 现有工程基础已经支持该方向

Managed Runtime 不是从零建设。当前仓库已经具备关键底座。

### 4.1 Immutable Local Release

`hermes-connector/packaging/common/hermes_local_release.py` 已经实现离线、不可变、本地 release 组装，能够将明确的 Core、Plugin、Connector 输入构造成版本化 release，并记录 digest、manifest、build command receipt 和输入 provenance。

目标本地布局已经自然接近：

```text
release/<release-id>/
├── host/
│   ├── venv/
│   └── project/
├── plugin/
│   ├── artifacts/
│   └── metadata/
├── connector/
│   ├── venv/
│   └── project/
├── services/
├── manifest/
└── receipts/
```

### 4.2 macOS LaunchAgent

`hermes-connector/packaging/macos/hermes_macos_launch_agents.py` 已经能够为 immutable release 生成版本绑定的 Host 和 Connector service 定义，包含：

- RunAtLoad；
- KeepAlive；
- 固定可执行文件路径；
- release-scoped logs；
- 私有 Umask；
- Plugin trust metadata；
- Connector `--release-id` 绑定。

因此客户不应手工启动 Connector，服务启动应由 Managed Runtime 管理。

### 4.3 Activation / Rollback Controller

`hermes-connector/packaging/macos/hermes_macos_activation.py` 已经包含一套 fail-closed activation orchestration，覆盖：

- activation lock；
- current authority validation；
- candidate validation；
- pending activation recovery；
- old receipt backup；
- service cutover；
- health gates；
- rollback；
- interrupted activation recovery；
- immutable release identity。

这实际上已经是 Updater / Runtime Manager 的核心执行引擎，而不是简单脚本。

### 4.4 Plugin 和 Connector 已有独立包边界

- `hermes-agent-plugin` 使用正式 Hermes plugin entry point；
- `hermes-connector` 是独立 package 和 CLI；
- `hermes-runtime-control` 维护 runtime-owned control-plane primitives。

因此，不需要把所有代码强行合并成一个进程。应保留进程和权限隔离，仅把“安装和生命周期控制”统一到 Desktop Manager。

---

## 5. 目标桌面架构

```text
┌─────────────────────────────────────────────┐
│                 Hermes.app                  │
│                                             │
│  Onboarding / Status / Settings / Update    │
│  Diagnostics / Provider UI / Fleet Link     │
└──────────────────────┬──────────────────────┘
                       │ local management IPC
                       ▼
┌─────────────────────────────────────────────┐
│           Hermes Runtime Manager            │
│                                             │
│ Release → Verify → Install → Activate       │
│ Health → Update → Rollback → Uninstall      │
└───────────────┬─────────────────────┬───────┘
                │                     │
                ▼                     ▼
┌────────────────────────┐   ┌────────────────────────┐
│      Hermes Host       │   │    Hermes Connector    │
│                        │   │                        │
│ Patched Hermes Core    │◄─►│ UDS / Local Protocol   │
│ Agent Plugin           │   │ SQLite / Keychain      │
└────────────────────────┘   └────────────┬───────────┘
                                         │ WSS / TLS 443
                                         ▼
                               ┌──────────────────────┐
                               │     Hermes Cloud     │
                               └──────────────────────┘
```

### 5.1 Desktop Shell 职责

Desktop Shell 负责：

- 首次运行体验；
- 企业账号登录；
- 设备配对；
- 模型服务配置；
- 本地状态展示；
- 检查更新；
- 安装进度；
- 升级/回滚提示；
- 诊断报告；
- 日志查看入口；
- 打开 Web 控制台；
- 显式停止/重启 Agent；
- 卸载入口。

Desktop Shell **不直接承担**：

- Agent execution authority；
- Cloud WSS Connector transport；
- Agent Plugin runtime；
- SessionDB authority；
- 长时间后台任务的进程生存责任。

### 5.2 Runtime Manager 职责

Runtime Manager 是客户本地安装闭环的真正控制平面，应提供明确状态机和幂等操作：

```text
UNINSTALLED
   ↓
DOWNLOADED
   ↓
VERIFIED
   ↓
BUILT
   ↓
ACTIVATING
   ↓
HEALTH_CHECKING
   ↓
ACTIVE
```

异常路径：

```text
ACTIVATING / HEALTH_CHECKING
   ↓ failure
ROLLING_BACK
   ↓
ACTIVE(previous)
```

无法安全恢复时：

```text
BLOCKED
```

必须给出机器可读 fault category 和用户可理解说明，不能用“未知错误，请重装”掩盖状态。

---

## 6. 客户安装体验：目标为“下载 → 登录 → 完成”

### 6.1 安装介质

macOS V1 推荐：

```text
Hermes.dmg
```

用户操作：

```text
打开 DMG
   ↓
拖动 Hermes.app 到 Applications
   ↓
打开 Hermes.app
```

不要求客户运行 shell script。

### 6.2 First Run 四步模型

#### Step 1：欢迎

用户只看到产品价值，不看到内部组件。

```text
欢迎使用 Hermes

让 AI Agent 安全运行在自己的电脑上，
并可以从 Web 和手机远程使用。

[开始]
```

#### Step 2：企业登录 / 设备授权

推荐采用 browser-based login / device authorization，而不是让用户复制 access token。

```text
连接到 Hermes Cloud

[使用浏览器登录]
```

浏览器完成企业身份认证后，用户确认当前设备：

```text
设备：Loy's MacBook Pro
企业：Example Corp

[确认连接]
```

#### Step 3：后台自动准备 Runtime

用户可以看到进度，但看不到技术细节：

```text
正在准备 Hermes...

✓ 验证安装包
✓ 安装 Agent
✓ 安装本地连接服务
✓ 创建安全设备身份
✓ 连接 Hermes Cloud
✓ 检查运行状态
```

内部真实流程：

```text
Fetch release manifest
      ↓
Download immutable bundle
      ↓
Signature / SHA verification
      ↓
ReleaseBuilder
      ↓
Host venv + patched Core
      ↓
Plugin trust/install
      ↓
Connector venv
      ↓
LaunchAgent render/install
      ↓
ActivationController
      ↓
Device pairing / Keychain
      ↓
Cloud WSS
      ↓
Full local health gate
```

#### Step 4：Ready

```text
Hermes 已准备就绪

本机 Agent       ● 运行中
Cloud            ● 已连接
远程控制         ● 可用
版本             1.x.x

[打开 Hermes]
```

首发验收目标：普通非技术用户在不打开终端的前提下完成安装。

---

## 7. Provider / Model Credential 体验

禁止把 provider 配置做成“环境变量教程”。

错误体验：

```text
export DEEPSEEK_API_KEY=...
export OPENAI_API_KEY=...
```

目标体验：

```text
模型服务

○ OpenAI
○ DeepSeek
○ Kimi
○ Anthropic
○ OpenAI-Compatible
```

选中 provider 后：

```text
API Key
[ sk-************************ ]

Base URL
[ 默认 ]

[测试连接]
```

安全原则：

- provider credential 只存本机 Keychain；
- Cloud 不接收 provider credential；
- 不进入 argv；
- 不进入 shell history；
- 不进入环境变量日志；
- 不进入上传诊断包；
- UI 默认不可恢复显示明文；
- 删除 credential 需要显式动作。

Connector 已经使用 Keychain 管理设备 credential，这一设计应推广到模型 provider credential。

---

## 8. Release Model：我们整体控制版本

### 8.1 Release Manifest

每个 Managed Runtime 必须有 machine-readable manifest：

```json
{
  "desktop_version": "1.3.2",
  "release_id": "hermes-desktop-1.3.2-macos-arm64",
  "platform": "macos",
  "arch": "arm64",
  "core": {
    "upstream_version": "0.xx",
    "upstream_commit": "...",
    "patchset_digest": "...",
    "artifact_sha256": "..."
  },
  "plugin": {
    "version": "1.3.2",
    "artifact_sha256": "..."
  },
  "connector": {
    "version": "1.3.2",
    "artifact_sha256": "..."
  },
  "runtime_contract": "vN",
  "minimum_cloud_protocol": "vN",
  "release_digest": "..."
}
```

Cloud fleet inventory 至少应投影：

- Desktop version；
- release_id；
- Core upstream version / patch identity；
- Plugin version；
- Connector version；
- Runtime contract；
- runtime_generation；
- platform / arch；
- health state；
- update channel；
- last successful activation；
- current / previous release identity。

### 8.2 禁止组件独立自更新破坏兼容矩阵

Managed Runtime 模式下：

- Core 不应独立 self-update；
- Plugin 不应脱离 Runtime release 独立升级；
- Connector 不应自动跳到未经 qualification 的版本；
- Desktop Manager 必须把一个 release 视为原子兼容单元。

服务端 Cloud 可以独立部署，但必须保持协议兼容窗口和 capability negotiation。

---

## 9. Upstream Hermes 更新流程

Hermes upstream 发布新版本后，不直接推给客户。

推荐 pipeline：

```text
Upstream Hermes release / commit
        ↓
Automated upstream sync
        ↓
Patch applicability / drift check
        ↓
Core unit + Host SPI tests
        ↓
Plugin integration tests
        ↓
Connector integration tests
        ↓
Cloud protocol / WSS tests
        ↓
Real full-chain qualification
        ↓
Desktop packaging tests
        ↓
Signed + notarized candidate
        ↓
Internal channel
        ↓
Canary 5%
        ↓
Progressive rollout 20% / 50% / 100%
```

任何一个 P0 gate 失败，版本不得进入 stable channel。

---

## 10. 更新策略：先下载、后验证、任务安全点切换

客户无感更新的正确目标：

```text
发现 1.4.0
   ↓
后台下载
   ↓
完整签名 / digest 验证
   ↓
构建 immutable release
   ↓
等待安全切换窗口
   ↓
停止旧 Connector
   ↓
停止旧 Host
   ↓
激活新 Host
   ↓
检查 Plugin / Local Gateway
   ↓
启动新 Connector
   ↓
检查 Cloud handshake / health
   ↓
提交 current release
```

这里必须特别强调：

**更新不能简单等价于“替换应用文件”。**

它是一个带执行权威、Session、Connector、Cloud presence、runtime_generation 和 rollback 的受控切换过程。

### 10.1 安全切换窗口

V1 可采用保守策略：

- 无活动 mutation；
- 无待确认 approval / clarification；
- 无 `effect_unknown` command；
- Agent 不处于不可中断关键阶段；
- Connector backlog 在阈值内；
- Cloud 可记录“draining / updating”；
- 达到最长等待时间后由用户显式选择“稍后更新”或“现在重启”。

未来再支持更细粒度的 session checkpoint / resume。

---

## 11. Rollback 是核心产品能力，不是故障脚本

升级成功标准不是“新进程启动了”，而是完成完整健康闭环。

新版本 activation 后必须至少验证：

- Host PID 与 release identity 一致；
- Plugin loaded / enabled；
- Local / Control / Observer descriptor 完整且可信；
- runtime_generation 有效；
- Connector 与该 authority 一致；
- Connector process identity 正确；
- Keychain credential 可用；
- Cloud WSS negotiation 成功；
- Agent presence 可被 Cloud 正确认知；
- 不存在第二 controller；
- 未产生敏感日志；
- backlog 不异常增长。

失败：

```text
new release failed
      ↓
ActivationController rollback
      ↓
restore previous service definitions
      ↓
restart previous Host
      ↓
restart previous Connector
      ↓
verify previous health
      ↓
report update_failed_rolled_back
```

用户看到：

```text
更新未完成，Hermes 已恢复到上一稳定版本。
```

只有 rollback 本身失败时才进入 BLOCKED 状态，并要求人工介入。

---

## 12. 本地目录基线

推荐将产品 UI 和 Runtime data 分离。

应用：

```text
/Applications/Hermes.app
```

用户级 Runtime：

```text
~/Library/Application Support/Hermes/
├── releases/
│   ├── <release-id-A>/
│   ├── <release-id-B>/
│   └── <release-id-C>/
├── state/
├── profiles/
├── receipts/
├── diagnostics/
└── logs/
```

`current` / `previous` 不一定必须使用符号链接；如果安全模型禁止 symlink，应采用经过验证的 receipt / pointer 文件或等价的不可变引用机制。

LaunchAgent：

```text
~/Library/LaunchAgents/
├── com.hermes.host.plist
└── com.hermes.connector.plist
```

Secret：

```text
macOS Keychain
```

对运行时目录继续维持私有权限、no-follow、安全 ownership 和 immutable release 约束。

---

## 13. Existing Hermes Migration

不建议 V1 原地修改用户已有的 Hermes 安装。

如果检测到已有 Hermes 数据，应采用“导入”而不是“就地 patch”。

```text
Existing Hermes
      ↓ read-only discovery
Migration Preview
      ↓ user confirmation
Managed Runtime
```

可导入对象：

- Skills；
- Workspace；
- 用户级配置；
- 非秘密偏好；
- 经明确支持的数据资产。

不应自动迁移：

- 未识别的 executable / plugin；
- 不受信任的 site-packages；
- 未知 `.pth` 注入；
- 不受信任的 shell hook；
- 明文 token；
- 无来源证明的私有 patch。

迁移原则：

1. 原安装保持不变；
2. 迁移前生成 preview；
3. 每个迁移对象有明确 source / destination；
4. 敏感信息单独确认；
5. 导入失败不破坏原安装；
6. 导入过程生成 receipt。

---

## 14. Menu Bar 是核心日常入口

Hermes.app 可提供完整窗口，但 macOS 日常产品入口建议以菜单栏为主。

示意：

```text
🦦 Hermes
────────────────
● Agent 正常
● Cloud 已连接

活动 Session    3
当前任务        2

打开 Hermes
打开 Web 控制台
查看诊断
检查更新
────────────────
停止 Agent
退出 Hermes
```

需要区分：

- 关闭窗口；
- 退出 Desktop UI；
- 停止 Agent；
- 停止所有 Hermes service；

不能让用户误操作导致后台 Agent 意外下线。

---

## 15. Recovery Closure：把真实客户机器当作不可靠环境

商业版本必须对以下场景有定义明确的恢复行为：

| 场景 | 期望行为 |
|---|---|
| macOS 重启 | Host / Connector 自动恢复，并重新完成 authority + Cloud negotiation |
| 休眠 / 唤醒 | Connector 检测 stale transport 并安全重连 |
| 网络中断 | 不重复业务效果；恢复后按 durable state reconcile |
| Cloud 暂不可用 | 本地 Agent 可继续本地工作；远程状态明确降级 |
| Connector crash | 重启后不重执行 completed command |
| Host crash | runtime_generation / authority 重新建立，旧绑定失效 |
| Activation 中断 | pending activation recovery 或 rollback |
| 磁盘满 | fail closed；不宣称 update 成功 |
| Keychain 不可用 | 明确 credential unavailable，不回退到明文文件 |
| Release 文件损坏 | digest / manifest 验证失败，禁止启动候选版本 |
| Cloud token 到期 | 设备 challenge / renewal，不暴露长期 bearer secret |
| Plugin 不可加载 | Runtime unhealthy，不伪造 Agent ready |

这些都应成为自动化或 staged acceptance test，而不是仅存在于文档。

---

## 16. Uninstall Closure

卸载不是删除 `/Applications/Hermes.app`。

应提供受控卸载流程：

```text
卸载 Hermes

☑ 删除 Agent Runtime
☑ 删除 Hermes 本地日志与诊断
☐ 保留 Workspace / Skills
☐ 删除模型服务凭证
☑ 从 Cloud 注销此设备
```

需要处理：

- stop / bootout LaunchAgents；
- 删除 Host / Connector service definitions；
- 删除 releases；
- 删除 local state / SQLite；
- 删除 Connector device credential；
- 可选删除 provider credential；
- revoke / deactivate Cloud DeviceCredential；
- 清理 temporary / pending activation；
- 保留或删除 Workspace 必须明确区分；
- 输出 uninstall receipt。

不能留下“幽灵 Agent”：Cloud 显示在线记录但本机已经没有 Runtime。

---

## 17. 安全与供应链要求

客户安装闭环必须包含软件供应链闭环。

### 17.1 macOS Distribution Gate

稳定发行至少必须完成：

```text
Build
  ↓
Bundle
  ↓
Artifact hash
  ↓
Code Sign
  ↓
Notarize
  ↓
Staple
  ↓
DMG
  ↓
Fresh-machine install
  ↓
First-run smoke
```

需要同时覆盖 Apple Silicon 和 Intel 时，应明确采用 universal binary、双 artifact 或分平台 release，不允许运行时再下载未签名可执行内容绕过发行边界。

### 17.2 Release Trust

Runtime Manager 只信任：

- 受信 release signing key；
- exact manifest；
- exact release digest；
- exact component hashes；
- exact platform / arch；
- compatible contract range。

错误 release id 重用、digest 冲突、signature 失效、manifest 不完整全部 fail closed。

### 17.3 Secret Boundary

禁止进入默认日志或上传诊断包：

- provider API keys；
- access / refresh token；
- Connector bearer token；
- pairing private key；
- approval / clarification 原始 payload；
- tool arguments / raw tool output；
- secret path content。

诊断包应执行 deterministic redaction + sensitive-pattern scan 后才允许导出。

---

## 18. Fleet / Cloud Control Plane

Managed Runtime 使 Cloud 从“连接到一台 Connector”升级为“管理企业 Agent Fleet”。

Cloud 后续应支持：

### 18.1 Device Inventory

```text
Device
├── tenant
├── user
├── platform / arch
├── desktop version
├── release_id
├── core identity
├── plugin version
├── connector version
├── runtime contract
├── runtime_generation
├── current health
├── last seen
├── update channel
└── rollout cohort
```

### 18.2 Update Policy

企业管理员可设置：

- Stable；
- Early Access；
- Internal；
- Freeze window；
- Maintenance window；
- Mandatory security update；
- staged rollout cohort。

### 18.3 Fleet Health

Cloud 要能区分：

```text
Desktop Installed
Connector Online
Agent Runtime Available
Agent Ready
Session Available
Controller Active
Update Pending
Updating
Rollback Active
Blocked
```

不能把 `Connector WSS connected` 等价为“Agent 可执行”。

---

## 19. 平台路线

### 19.1 Phase 1：macOS

当前 macOS 是唯一生产化 composition，应优先把它做到完整商业闭环：

- signed/notarized Hermes.app；
- DMG；
- LaunchAgent；
- UDS；
- Keychain；
- Menu Bar；
- Managed Runtime；
- First Run；
- Update/Rollback；
- Uninstall；
- Fleet integration。

### 19.2 Phase 2：Linux

目标体验可分两类：

Desktop Linux：

```text
.deb / .rpm / AppImage + systemd user service
```

Server Linux：

```text
signed repository / package manager install
systemd service
headless device pairing
```

不要把“开发机 pip install”作为正式 Linux commercial distribution。

### 19.3 Phase 3：Windows

Windows 需要独立平台设计：

- signed installer；
- Windows Service 或合适的 per-user background service；
- Credential Manager / DPAPI；
- Named Pipe；
- ACL；
- update service；
- code signing / SmartScreen reputation。

不得简单移植 macOS plist/UDS 假设。

---

## 20. 从 Runtime P0 扩展到客户交付六大闭环

当前仓库已经把大量精力投入 Runtime full-chain gate。下一阶段应明确六类 closure，不允许用其中一个代替其他五个。

### Closure A — Runtime Closure

链路：

```text
Cloud
  ↓
Connector
  ↓
Plugin
  ↓
Core
  ↓
Authoritative Live Session
  ↓
Receipt / Observer State
```

必须完成 real macOS Agent host、真实 owner actions、fault injection、runtime generation rollover、rollback 等现有 P0 验收。

### Closure B — Installer Closure

链路：

```text
Source / Release Inputs
  ↓
Managed Runtime Bundle
  ↓
Hermes.app
  ↓
Code Sign / Notarize
  ↓
DMG
  ↓
Fresh Mac install
  ↓
Background services healthy
```

### Closure C — First Run Closure

链路：

```text
Install
  ↓
Launch
  ↓
Enterprise login
  ↓
Device pairing
  ↓
Runtime activation
  ↓
Provider setup
  ↓
Real Agent ready
```

目标：非技术客户无需终端。

### Closure D — Upgrade Closure

链路：

```text
Stable N
  ↓
Download N+1
  ↓
Verify
  ↓
Activate
  ↓
Health
  ↓
Stable N+1
```

并证明：

```text
N+1 failure
  ↓
Rollback
  ↓
Stable N
```

### Closure E — Recovery Closure

覆盖：

- reboot；
- sleep/wake；
- network loss；
- Cloud loss；
- Connector crash；
- Host crash；
- partial activation；
- credential failure；
- disk full；
- corrupted release。

### Closure F — Uninstall Closure

证明：

- service 全停；
- runtime 可清；
- credential 按策略清；
- Cloud device revoke；
- workspace 保留策略正确；
- 无残留进程和幽灵设备。

---

## 21. Mac Customer Installation Closure：建议新 P0

建议把以下内容作为新的正式 P0，而不是普通 UX backlog。

### 21.1 用户验收标准

一个从未安装过 Hermes、不会 Python、不会终端的 macOS 用户，应能够：

1. 下载官方 `Hermes.dmg`；
2. 完成系统允许的标准安装动作；
3. 打开 `Hermes.app`；
4. 登录企业账号；
5. 授权当前设备；
6. 配置至少一个模型 provider；
7. 看到 Agent / Cloud / Remote Control 全部 Healthy；
8. 从 Web/H5 看到真实 Agent；
9. 发起一个真实 benign prompt 并收到完整输出；
10. 重启 Mac 后无需手工操作自动恢复。

全程不允许：

- 打开 Terminal；
- 手工复制 access token；
- 手工执行 pip；
- 手工编辑 plist；
- 手工设置 PATH；
- 手工启动 Connector。

### 21.2 工程验收标准

必须证明：

- clean macOS installation；
- app signing valid；
- notarization valid；
- release digest valid；
- Host + Plugin + Connector 安装来源可审计；
- LaunchAgent installation safe；
- Device pairing safe；
- Keychain state safe；
- First Run 状态机幂等；
- 重复启动不会重复安装；
- activation failure 自动 rollback；
- update failure 自动 rollback；
- reboot 自动恢复；
- uninstall 无幽灵进程/设备；
- exported diagnostics 无 secret。

---

## 22. 建议仓库新增模块

建议后续建立明确目录，而不是把 Desktop 安装逻辑塞进 Connector：

```text
hermes-desktop/
├── app/
├── runtime-manager/
├── updater/
├── onboarding/
├── diagnostics/
├── packaging/
│   └── macos/
└── tests/
```

其中 Runtime Manager 可以复用现有：

- `hermes_local_release.py`；
- `hermes_macos_launch_agents.py`；
- `hermes_macos_activation.py`；
- Plugin trust / bundle logic；
- Connector pairing / Keychain logic；
- existing health contracts。

长期应把“通用 release/activation primitives”逐步抽到明确的 runtime packaging library，避免 Desktop 直接依赖 Connector application internals。

---

## 23. 推荐实施顺序

### P0.1 Runtime Gate 收尾

先把现有 real macOS Agent full-chain P0 完成，不降低现有 fail-closed 标准。

### P0.2 Desktop Skeleton

交付：

- `Hermes.app`；
- Menu Bar；
- Runtime status；
- Start/Stop/Restart；
- local management IPC。

### P0.3 Installer / Managed Runtime

交付：

- release fetch；
- signature/hash verification；
- immutable install；
- LaunchAgent；
- activation；
- rollback；
- installation receipt。

### P0.4 First Run

交付：

- browser login；
- device pairing；
- Cloud connection；
- provider setup；
- real-session smoke。

### P0.5 Distribution

交付：

- code sign；
- notarize；
- DMG；
- fresh machine E2E；
- artifact provenance。

### P0.6 Update / Rollback

交付：

- release channel；
- background download；
- safe activation；
- rollback；
- rollout cohort；
- Cloud fleet status。

### P0.7 Recovery / Uninstall

交付：

- reboot；
- sleep/wake；
- crash；
- network interruption；
- interrupted activation；
- uninstall / revoke。

---

## 24. 非目标

首版不要同时解决所有问题。

明确非目标：

1. V1 不同时追求 macOS / Linux / Windows 三个平台商业闭环；
2. V1 不支持客户任意组合 Core / Plugin / Connector 版本；
3. V1 不允许客户直接修改 Runtime release 内文件；
4. V1 不做“原地修改所有已有 Hermes 安装”的复杂兼容工程；
5. V1 不把 Cloud、Nginx、数据库安装到普通客户端；
6. V1 不把 provider key 上传到 Cloud；
7. V1 不把 Desktop UI 生命周期与 Agent runtime 生命周期绑定；
8. V1 不因为安装体验简化而削弱当前 runtime identity、receipt、idempotency 和 fail-closed 约束。

---

## 25. 架构决策记录

### ADR-1：采用 Hermes Managed Runtime

**决定：** 我们发行完整、版本锁定、经过 qualification 的 Hermes Runtime，而不是要求客户先自行安装 upstream Hermes 再附加我们的 Plugin / Connector。

**原因：** 控制兼容矩阵、供应链、升级、回滚和企业运维成本。

### ADR-2：Hermes.app 是 Shell / Manager，不是 Agent Authority

**决定：** Agent Host 与 Connector 作为后台服务独立生存。

**原因：** 关闭 UI 不应让企业 Agent 下线；后台运行和 UI 生命周期必须分离。

### ADR-3：整体 release 原子升级

**决定：** Core + Plugin + Connector 按 Managed Runtime release 统一 qualification 和 activation。

**原因：** 避免跨组件版本漂移。

### ADR-4：保持 Connector 独立进程

**决定：** 不为了“一键安装”而把 Connector 嵌回 Agent。

**原因：** 继续保持 Cloud transport、credential、local control 和 Agent authority 的安全边界。

### ADR-5：不永久 fork upstream Hermes

**决定：** 保持 upstream sync + bounded patch layer + qualification pipeline。

**原因：** 降低长期维护成本，把差异化资产集中在商用控制面和企业能力。

### ADR-6：Cloud/Nginx 不进入普通客户端安装

**决定：** 客户本地只安装 Agent Runtime 与 Connector。

**原因：** Cloud 是服务端控制平面，客户端只需要出站 TLS/WSS。

### ADR-7：Rollback 是发布协议的一部分

**决定：** 任何升级都必须有 previous release 和可验证 rollback。

**原因：** 对企业 Agent 来说，半升级状态不可接受。

---

## 26. 最终产品目标

后台可以很复杂：

```text
Core
Host SPI
Plugin
UDS
Connector
WSS
Lease
Receipt
Runtime generation
Immutable release
Keychain
Activation
Rollback
```

客户体验必须极简：

```text
下载 Hermes
     ↓
安装
     ↓
登录
     ↓
完成
```

这不是隐藏工程问题，而是由我们承担工程问题。

当这个闭环完成后，Hermes 的产品形态将从“需要技术人员部署的 Agent 软件”转变为：

> **企业 AI Agent Runtime + Cloud Control Plane + Fleet Lifecycle Management**。

Hermes Core 是底层 Engine；真正的商业产品是我们围绕它建立的 Managed Runtime、Connector、Cloud、Runtime Manager、Fleet、企业权限和可审计执行体系。

这是下一阶段从工程能力走向规模化企业交付的核心路线。
