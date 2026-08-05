---
goal: Hermes Connector、云端服务、企业数据、AI 卡片、文件交换与 A2A 商用闭环实施计划
version: 1.2
date_created: 2026-07-30
last_updated: 2026-08-02
owner: Hermes Platform
status: 'In Progress'
tags: [feature, connector, cloud, enterprise-data, ai-card, postgresql, redis, a2a, file-exchange, observability]
---

# Introduction

![Status: In Progress](https://img.shields.io/badge/status-In%20Progress-yellow)

本计划用于在保持 Hermes Agent 独立升级的前提下，完成 Agent Plugin、Hermes
Connector、Remote Server、Enterprise Data Gateway、AI UI Card、H5/PWA、
文件交换、A2A 协作以及商用监控运维闭环。

执行遵循“主 Agent 统一契约和集成，子 Agent 按独占目录并行开发”。当前里程碑已
由用户明确调整为 H5/PWA 优先、Android 暂停、远程 Cloud 使用 SQLite；PostgreSQL、Redis、
NATS、对象存储和企业数据能力保留为后续商用扩展，不得用未来目标否定当前单节点
闭环，也不得在未单独授权时连接或迁移这些外部资源。

## 0. 当前执行快照

以下状态只描述当前 H5 首闭环，不代表 Android、文件交换、A2A、企业数据、AI Card
或多节点商用阶段已经完成。

| 工作流 | 状态 | 当前证据与边界 |
|---|---|---|
| 仓库与平台目录 | DONE | Plugin、Connector、Cloud、Android 为独立兄弟目录；Plugin 和 Connector 的 macOS/Linux/Windows adapter 分离；Android 代码、测试、脚本和 6 份 HTML 设计均位于 `hermes-android/` |
| Core Contract | DONE | 根 `contracts/` 是唯一权威；Plugin、Connector、Cloud、Android、Web 消费同一契约；H5 当前使用规范化 v1 目录与精确 logout 合同 |
| Agent Plugin | PARTIAL / FAIL CLOSED ON HERMES 0.19 | Control lease、幂等账本、Observer/Control UDS、严格 sequence/gap/replay、snapshot-first `prepare → activate → close`、激活总期限、资源限制和发行物已完成本地测试。真实 Hermes 0.19 缺少 `gateway-extension/1` 及 production caller，Plugin 继续 fail closed，不得声明已接通权威 Agent |
| Connector | IMPLEMENTED LOCALLY / AGENT UNAVAILABLE | SQLite durable command lane、非持久 owner-control lane、macOS Keychain Ed25519 身份、正式 pairing offer/proof/token 和 Cloud WSS 已实现；OS 安装服务仍属于后续发布阶段。Pairing 只建立 Cloud 设备授权，不证明本机 Local Gateway 或 Hermes runtime 已接通 |
| Cloud | REVISION 11 + H5 AUTH/DIRECTORY INCREMENT LOCALLY / NOT DEPLOYED | 在 revision 11 基线上增加 cookie-auth `GET /api/v1/agents`、`GET /api/v1/agents/{agent_id}/sessions`、精确 logout 和旧 access/ticket 权威失效；旧未版本化目录仅保留兼容别名。SQLite/PostgreSQL 共享 mapped ORM predicate；本里程碑只有 SQLite 真实事务执行证据，PostgreSQL 仅有方言结构/编译检查；当前候选尚未远程部署 |
| H5/PWA | BACKEND VERTICAL SLICE COMPLETE LOCALLY / FULL AGENT CHAIN PENDING | 当前生产 auth/catalog client 已通过真实 Vite proxy 接入真实 Cloud ASGI + 临时 SQLite ORM：login → v1 Agent 目录 → v1 scoped session 目录 → ticket → logout，并验证旧 access/ticket 拒绝。`integration-agent` / `Integration session` 是测试夹具，不是实际 Hermes 输出；Connector/Plugin/Hermes 与远程部署仍待闭环 |
| Android | PAUSED / HISTORICAL TEST-SERVER SLICE ONLY | 按当前指令暂停。2026-07-31 emulator 记录只证明旧测试服务客户端路径，不证明存在 Android Agent、真实 Hermes 会话或当前 revision 11 已部署 |
| 真实 Agent 闭环 | PARTIAL / BLOCKED AT TRANSPORT | Stage 3 Core 补丁已在固定 commit 的隔离 archive 重放验证；真实 Plugin UDS opener、Connector client、临时安装及端到端效果仍待独立门禁。禁止用私有 import、反射、hook 或 `inject_message` 伪造成功 |
| 物理设备验收 | PAUSED | 当前 H5 优先；Android 恢复后再执行真机验收，不作为本轮 H5 后端门禁 |

Cloud 与 Connector 的业务查询、写入和迁移必须继续使用 ORM 或 typed migration
operations。唯一允许的直接数据库语句是集中式 SQLite 连接策略中经过专项测试的固定
PRAGMA；不得将该例外扩散到 Repository、Application、migration、seed 或部署脚本。

本地 SQLite revision 11 使用 typed Alembic Operations 和 ORM 账本，并把
`tenant_id + agent_id + profile + session_key` 冻结为会话投影唯一身份；WebSocket
ticket 绑定稳定 `session_id`，不再用可歧义的 `session_key` 代替行身份。SQLite v10
只有在权威 Observer 证据能唯一补齐 `agent_id + profile` 时才可升级；命名为测试 seed
也不是权威证据。PostgreSQL v10 非空会话/票据数据必须 fail closed，待外部对账后再
迁移。截至 2026-08-02 的本地候选回归范围（未部署）是 Cloud 全量
`1513 passed`、根 Contract `101 passed + 67 subtests passed`、H5 application
`327 passed`、H5 process-lifecycle `15 passed`，且 H5 typecheck、lint、production
build 通过。该快照不包含远程、Connector、Plugin 或真实 Hermes 链路验收。

远端仍是旧 `20260731T103631Z` SQLite release，保留
`20260731T084500Z` 历史回滚材料，并运行显式 test-server seed。`android-agent`、
`android-bootstrap`、`Hermes Cloud test session` 只是测试数据，不是 Android Agent，
也不是 Hermes runtime 输出；生产 `src/` 不得按这些名字分支。该远端没有部署本地
revision 11 候选，不能作为四元身份、稳定 ticket、profile 隔离或真实 Hermes 会话的
验收证据。

2026-07-31 的公网登录、配对、Observer、control bridge、服务重启、旧 token 拒绝和
ORM 清理记录继续保留为旧 release 的时间快照。它只证明当时的 test-server REST/WSS
和 UI 测试路径，不能提升为当前候选远程部署、真实 Agent Observer 或 Control 闭环。

2026-08-02 的 H5 本地集成门禁使用真实 Cloud ASGI、真实 Vite proxy 和临时 SQLite
ORM 数据库，但其中 `integration-agent` 与 `Integration session` 仍是确定性测试夹具。
它证明 H5→Cloud 的鉴权、目录、ticket 与 logout 失效语义，不证明
Cloud→Connector→Plugin→Hermes 已经接通。

## 1. Requirements & Constraints

### 1.1 功能要求

- **REQ-001**：Agent Plugin 必须通过 Stable Host SPI 与 Hermes Agent 集成，不得
  导入 Agent 私有模块。
- **REQ-002**：Connector 必须作为独立 OS 服务运行，拥有独立 Python Runtime、
  锁文件、SQLite 和发布版本。
- **REQ-003**：Remote Server 首期必须由 Connector Gateway、Business API、
  Async Worker 和 File Gateway 四个独立入口组成；Business API 内部保持模块化
  单体。
- **REQ-004**：PostgreSQL 必须保存命令、身份、权限、审计、文件和 A2A 的云端
  业务事实。
- **REQ-005**：Redis 只能用于 Presence、限流、可重建 Gateway 路由缓存和短锁，
  不得保存命令、权限、A2A Mailbox、文件或审计事实。
- **REQ-006**：核心消息链路必须采用 Transactional Outbox、Consumer Inbox、
  至少一次投递和业务效果幂等。
- **REQ-007**：文件交换必须支持授权、分片、连续 offset、摘要校验、断点续传、
  配额、扫描、隔离、保留和删除。
- **REQ-008**：A2A 必须支持持久 Mailbox、Delegation、Access Grant、Receipt、
  Work Item、TTL、预算、人工 Gate、最大 hop/depth 和循环阻断。
- **REQ-009**：H5/PWA 必须展示真实命令、文件和 A2A 状态，不得把网络发送成功
  显示为 Agent 执行成功。
- **REQ-010**：调试、日志、Trace、指标、诊断、告警和 Runbook 必须从第一个实现
  切片进入，不得延期到上线前补做。

### 1.2 代码与注释要求

- **COD-001**：所有行为变更必须先添加可观察的失败测试，再实施最小代码。
- **COD-002**：Domain、Application、Ports、Adapters、Bootstrap 必须遵守向内
  依赖规则。
- **COD-003**：每个包含状态变更的 Domain 模块，必须在 transition table 或
  transition function 上方使用 ASCII 字符画描述完整状态机。
- **COD-004**：ASCII 图旁必须存在可执行的允许转换表和参数化测试；注释不能替代
  代码约束。
- **COD-005**：公共 Port 和跨进程接口必须通过 docstring 说明输入单位、deadline、
  幂等键、副作用边界、返回状态和错误。
- **COD-006**：注释只解释不变量、状态转换和失败恢复原因，不逐行复述代码。
- **COD-007**：禁止无界队列、无 deadline 网络/数据库调用、`max_size=None` 和
  无生命周期所有者的后台任务或线程。
- **COD-008**：Connector 本地 SQLite 的业务模型、查询和写入统一使用
  SQLAlchemy 2.x ORM；Schema 演进使用 Alembic Operations 或版本化
  SQLAlchemy DDL API。Repository 和 Application 层禁止 `text()`、
  `exec_driver_sql()` 及手拼 SQL。仅允许在单一 Infrastructure 连接策略中执行
  SQLite `WAL`、`foreign_keys`、`synchronous`、`busy_timeout` PRAGMA，并要求
  专项测试、固定值和代码审查。

状态机注释采用以下格式：

```text
# State machine:
#   NEW -> PERSISTED -> DISPATCHING -> EXECUTING -> SUCCEEDED
#                              |            |
#                              |            +-> FAILED
#                              +----------------> UNKNOWN
#
# Invariants:
#   - UNKNOWN is never retried automatically.
#   - DELIVERED means the Connector inbox is durable.
_ALLOWED_TRANSITIONS = {...}
```

### 1.3 输入、容量与错误要求

- **ERR-001**：Local Gateway 和 Connector WSS JSON 帧默认最大 256 KiB；超限必须
  在完整解析前拒绝。
- **ERR-002**：JSON 单字符串最大 128 KiB，并限制对象深度、数组长度、字段数量和
  解压后字节数。
- **ERR-003**：所有文本协议使用严格 UTF-8；非法字节、BOM 策略冲突、NUL、
  lone surrogate 和不可编码值必须映射为稳定错误。
- **ERR-004**：Local connect 默认 2 秒，Local RPC 默认 3 秒；Cloud 心跳默认
  20 秒。其他 deadline 在契约中显式配置，禁止无限等待。
- **ERR-005**：超时必须区分副作用边界前和边界后。边界前可以安全失败；边界后
  无法确定时必须进入 `UNKNOWN`。
- **ERR-006**：重复 ID 加相同 digest 返回已有状态；重复 ID 加不同 digest 必须
  安全拒绝并产生告警。
- **ERR-007**：乱序必须使用 `sequence/revision/generation` 判断；缺口触发快照或
  对账，不得无限缓存。
- **ERR-008**：磁盘满、SQLite 损坏或只读时，Connector 必须停止接受新的 durable
  command/file，不得发送虚假的 durable ACK。
- **ERR-009**：PostgreSQL 连接池耗尽或不可用时，写请求必须 fail closed；
  Redis 不可用时只能丢失加速能力，不能丢失业务事实。
- **ERR-010**：NATS/JetStream 不可用时，PostgreSQL Outbox 必须保留待发布事件，
  禁止回滚已提交业务事实。

### 1.4 文件交换安全约束

- **FIL-001**：Plugin 不得提供 `file.read(path)`、`file.write(path)` 或任意目录
  浏览能力，只能处理 Host 明确授权的 source/sink handle。
- **FIL-002**：`file_ref`、`local_file_ref` 和 `transfer_id` 必须是不透明且不可
  互相推导的 ID。
- **FIL-003**：Local Gateway base64 chunk 默认不超过 128 KiB；Cloud 大文件走
  File Gateway 流式分片，不走 WSS 或 Business API 内存中转。
- **FIL-004**：Connector 和 H5 不接触 OSS AccessKey、Bucket 内部结构或 OSS
  SDK；File Gateway 是稳定数据面，Server Adapter 负责对象存储。
- **FIL-005**：文件传输 capability 默认关闭，只有租户策略明确配置单文件上限、
  总配额、允许 MIME、保留期和扫描策略后才能开启。
- **FIL-006**：文件名只作为显示信息，不得参与磁盘路径或对象 key 生成；拒绝绝对
  路径、`..`、路径分隔符、Windows drive/UNC、NUL 和控制字符。
- **FIL-007**：临时文件使用专用 `0700` 目录、随机 `0600` 文件、no-follow open
  和 open 后 `fstat`；拒绝 symlink、目录、FIFO、device 和 socket。
- **FIL-008**：chunk 必须按连续 offset 写入，完成 byte count、chunk digest 和
  final SHA-256 校验后才能 commit。
- **FIL-009**：v1 默认不对文件内容压缩；归档内容扫描必须限制展开大小、压缩比、
  嵌套层数和 CPU 时间。
- **FIL-010**：A2A 消息只能携带已授权的 `file_ref`，接收方必须按自身身份、Purpose
  和 ACL 重新鉴权。

### 1.5 A2A 安全约束

- **A2A-001**：Server PostgreSQL 是 Message、Receipt、Work Item、Delegation、
  Access Grant 和预算使用的权威。
- **A2A-002**：Connector 只负责可靠传输、短期 SQLite Inbox/Outbox 和对账，不
  理解 Skill，不做最终授权，不建立设备 P2P。
- **A2A-003**：Plugin 只通过公开 Host SPI 交付结构化 Work Item 并回传 Receipt/
  Result，不得实现企业业务规则。
- **A2A-004**：每条消息必须包含 sender、recipient、Tenant、Purpose、Policy
  version、delegation/access-grant ID、TTL、correlation、causation 和幂等键。
- **A2A-005**：每条委托必须限制最大 hop、delegation depth、执行次数、时间、
  Token/费用预算和 fan-out。
- **A2A-006**：同一 causation chain 返回已访问 Agent、预算耗尽、委托过期或权限
  变化时必须 fail closed 并审计。
- **A2A-007**：Agent 不得自行扩大资源、动作、TTL、预算或转委托权限。

### 1.6 可观测与调试约束

- **OBS-001**：所有单元使用 OpenTelemetry 语义，传播 `trace_id`、`message_id`、
  `command_id`、`transfer_id`、`work_item_id` 和 `runtime_generation`。
- **OBS-002**：Span attribute、日志和指标禁止包含 token、私钥、prompt、审批
  正文、文件正文、Personal Memory 和完整工具输出。
- **OBS-003**：Plugin、Connector、Gateway、Business API、Worker、File Gateway
  必须分别暴露受限的 liveness、readiness 和 component health。
- **OBS-004**：Connector 诊断只能通过本机受认证接口生成；Cloud 诊断端点必须
  只读且需要运维权限。
- **OBS-005**：Debug 模式必须由签名策略启用、限时自动关闭并继续脱敏；禁止远程
  Shell 和任意文件读取。
- **OBS-006**：诊断包必须先执行 secret scan，设置大小和保留期上限，只包含版本、
  capability、制品摘要、队列计数、migration、错误类别和权限诊断。
- **OBS-007**：每个 P0/P1 告警必须绑定 Runbook、只读诊断、缓解、恢复、对账和
  回滚步骤。

### 1.7 数据库和秘密约束

- **SEC-001**：用户提供的 PostgreSQL/Redis 连接串不得写入仓库、Markdown、命令行、
  日志、Trace、测试快照或诊断包。
- **SEC-002**：本地/临时环境使用 `HERMES_POSTGRES_DSN_FILE` 和
  `HERMES_REDIS_DSN_FILE` 指向仓库外权限为 `0600` 的文件。
- **SEC-003**：预发/生产使用 Secret Manager/KMS/RAM Role 或等价 Secret
  reference，不以普通环境变量保存长期凭据。
- **SEC-004**：Migration Role 和 Runtime Role 应分离；Runtime Role 不拥有 DDL、
  超级用户或跨数据库权限。
- **SEC-005**：执行任何迁移前必须确认 `environment_id`、数据库标识、TLS、Tenant
  Realm 和备份状态，生产环境默认只允许只读探测。
- **SEC-006**：测试、开发、预发和生产必须使用不同数据库、Redis namespace/ACL、
  签名根、设备 Realm 和对象存储空间。

### 1.8 架构约束

- **CON-001**：一个权威 Hermes Agent，可有多个 Observer，同一实时会话最多一个
  显式远程 Controller。
- **CON-002**：Agent、Plugin、Connector、Server 和 H5 使用独立版本、依赖和发布
  列车。
- **CON-003**：Connector 不读取 SessionDB，不直连 PostgreSQL、Redis、NATS、
  OSS 或 KMS。
- **CON-004**：Remote Server 持久层保存云端业务事实（当前单节点为 SQLite ORM，
  商用扩展为 PostgreSQL）；Agent 保存会话事实；Connector SQLite 保存本地交付
  事实；消息系统不是最终事实源。
- **CON-005**：系统采用至少一次投递和业务效果幂等；`UNKNOWN` 不自动重试。
- **CON-006**：H5/PWA 只连接 Remote Server，不直接连接本机 Agent/Plugin/
  Connector。
- **CON-007**：`/contracts` 是跨进程数据交换的唯一权威；Plugin、Connector、
  Cloud、Android、Web 和未来客户端都是消费者，任何端侧 Fixture、生成代码或实现
  都不得反向定义或修改核心语义。
- **CON-008**：核心 Envelope 和状态语义不得包含 Android、Web、iOS、Desktop 等
  平台字段；平台差异通过 capability negotiation、renderer profile 和 adapter
  解决。
- **CON-009**：端侧缺少 required capability 时必须在副作用前 fail closed；缺少
  optional capability 时必须显式降级并报告 unavailable capability，不得创建端侧
  私有协议分支。
- **CON-010**：实验或消费者专属元数据只能放入经过命名空间约束的 `extensions`；
  extension 不得改变核心字段含义、携带凭据、扩大授权或绕过 Policy。
- **CON-011**：每个消费者必须运行同一组 valid、invalid、N-1 和 capability
  degradation Fixture；CI 的依赖方向固定为 Core Contract 到 Consumer Adapter。
- **CON-012**：`agent_id` 是 Cloud 通用可选择身份与路由键。Agent A/B 切换只允许
  改变被授权和被选择的数据，不得形成不同服务实现、接口、客户端构建或生产源码
  特判。
- **CON-013**：pairing 只把 Connector 设备凭证绑定到授权的
  `workspace_id + agent_id`；它不证明本机 Hermes runtime、Plugin 注册、Local
  Gateway handshake、Observer 或 Control 已可用。
- **CON-014**：Hermes API Server 的标准 `GET` 会话/消息接口不得映射为 Observer
  或 Control。允许后续定义独立的只读 catalog/history 同步协议，但 HTTP adapter
  只能位于 Plugin，Connector 只能经 Local Gateway 接收规范化记录，并且不得据此
  合成 runtime generation、running/status、event sequence 或 controller lease。
- **CON-015**：`android-agent`、`android-bootstrap` 和
  `Hermes Cloud test session` 只允许作为 `deploy/test_server` 的显式 seed；生产
  `src/` 不得按这些标识分支，测试记录不得称为 Android Agent 或真实 Hermes 会话。
- **PAT-001**：Plugin 是 Host SPI 防腐层。
- **PAT-002**：Connector 是六边形、单进程、模块化、有状态边缘服务。
- **PAT-003**：Remote Server 使用 Gateway + 模块化单体 + Worker，使用 CQRS-lite，
  不采用完整 Event Sourcing。

### 1.9 企业数据和 AI Card 约束

- **DAT-001**：企业数据中心接入必须使用独立 Enterprise Data Gateway，不得复用
  员工设备上的 Hermes Connector。
- **DAT-002**：Enterprise Data Gateway 部署在企业内网或 VPC，只向 Cloud 发起
  mTLS 443 出站连接；源系统凭据不得离开企业控制域。
- **DAT-003**：源业务系统仍是业务事实权威；Hermes 保存 Data Product、查询、
  授权、血缘、质量、快照和 Card 投影，不反向取代源系统。
- **DAT-004**：Source Connector SDK 必须分离 `QueryConnector`、
  `ExtractConnector`、`CdcConnector` 和默认关闭的 `ActionConnector`。
- **DAT-005**：Agent 和模型禁止直接连接生产数据库、生成任意 SQL、任意 URL 或
  任意 API 调用；只允许访问有 Owner、Schema、权限、质量、新鲜度、停用和删除
  合同的 Data Product。
- **DAT-006**：每次读取、模型使用、Card 渲染、导出、共享和 Action 必须绑定
  Server 派生的 Tenant、Workspace、Human、Agent、Device、Session、Purpose、
  Policy Version、Delegation 和 Grant。
- **DAT-007**：授权是 RBAC、ABAC/Purpose、来源 ACL、行级策略和列级策略的交集，
  默认拒绝；管理员和运维身份不自动获得业务正文。
- **DAT-008**：字段裁剪和 deny/mask/tokenize/generalize/aggregate-only 必须在
  模型调用前完成，禁止只在 Card UI 层做视觉脱敏。
- **DAT-009**：Query Broker 只接受版本化 Metric、Dimension、Filter 和 Query
  Template；同步查询默认 15 秒、硬上限 30 秒、5,000 行/8 MiB，超限转换为异步
  查询或受控导出。
- **DAT-010**：数据结果必须携带 Data Product/Contract/Semantic Version、来源
  引用、watermark、`as_of`、新鲜度、质量、血缘、Policy Decision 和
  partial/estimated 标记。
- **DAT-011**：AI Card 是可删除、可过期的读取投影，不是业务事实；Card Action
  必须通过版本化 Action Contract 和源系统正式 API 回写。
- **DAT-012**：Hermes AI Card Protocol 使用受限声明式组件 Catalog，提供 A2UI
  v0.9.1 兼容 Profile；默认 Card 禁止任意 HTML、JavaScript、远程脚本和自定义
  网络访问。
- **DAT-013**：MCP Apps 仅作为复杂地图、编辑器、富媒体或重型 Dashboard 的独立
  sandbox lane；不能替代默认 Card DSL，所有 tool/action 仍需服务端鉴权、确认和
  审计。
- **DAT-014**：AG-UI 只允许作为可选 Agent↔UI 事件传输适配，不作为 Card 组件
  Schema 或权限模型。
- **DAT-015**：Card Manifest 必须包含 stable ID/revision、renderer capability、
  Data Result/Resource Reference、来源、新鲜度、质量、分类、脱敏说明、血缘、
  expiry 和签名 `action_ref`。
- **DAT-016**：Card Manifest 最大 128 KiB、最多 25 个组件；内联数据最多 200 行
  且必须使完整协议帧低于 256 KiB，超过限制必须使用受权 `resource_ref`。
- **DAT-017**：Card 打开、刷新、离线恢复、共享和 Action 时必须重新鉴权；
  `RENDERED` 不等于拥有 Action 权限，STALE/DEGRADED Card 默认关闭高风险 Action。
- **DAT-018**：业务数据、网页、文件和工具结果一律是不可信数据；来源文本不能
  成为系统指令、Action 定义、Tool Allowlist 或自动外发依据。
- **DAT-019**：A2A 只能分享不透明 `resource_ref/card_ref/evidence_ref`；接收方
  在交付、解引用、渲染和 Action 时使用自己的身份与 Purpose 重新鉴权。
- **DAT-020**：来源更正、删除、Schema/ACL/Policy/Consent/Grant/成员关系变化必须
  传播到快照、Card、H5、Connector、Agent Cache、搜索/向量和派生物，并形成完成
  审计。

## 2. Implementation Steps

### 2.1 目标目录和代码所有权

确认后采用以下兄弟项目边界：

```text
/Users/apple/hermesmobile/
  contracts/                 # 跨端 Schema、Fixture、生成工具
  hermes-agent-plugin/       # Agent Plugin
  hermes-connector/          # 独立本地服务、SQLite、安装器协作
  hermes-enterprise-gateway/ # 企业数据中心出站 Data Gateway 与 Source SDK
  hermes-cloud/              # Gateway、Business API、Worker、File Gateway
  hermes-web/                # H5/PWA
  operations/                # OTel、Dashboard、Alert、Runbook、部署模板
  tests/e2e/                 # 跨项目端到端与故障注入
```

`hermes-agent-plugin/src/` 只保留 `hermes_agent_plugin` 正式包；历史
`hermes_mobile_gateway` 导入包不再进入源码或新发行物。外部升级事务仍识别旧发行
版并支持失败回滚。Mobile Control Contract 只同步到 canonical generated 路径。

主 Agent 独占：

- 根目录共享配置、`.gitignore`、CI 和 release manifest 索引；
- `contracts/` 的最终 Schema 版本和错误码合并；
- 所有 PostgreSQL migration 编号；
- 各项目 `pyproject.toml`、锁文件和共享依赖变更；
- `docs/`、计划状态、兼容矩阵和最终验收证据；
- 子 Agent worktree、集成顺序、完整测试和回滚。

子 Agent 只修改任务分配的独占目录。需要修改共享文件时提交变更申请，由主 Agent
统一完成。

#### 2.1.1 跨平台目录与模块边界

三个 Python 运行单元不得把 Domain、Application、协议适配、数据库适配、操作系统
差异和打包脚本平铺在同一目录。目标目录如下；迁移允许按垂直切片渐进完成，但所有
新增代码必须直接进入目标目录，禁止继续扩大现有平铺结构。

```text
hermes-agent-plugin/
  src/hermes_agent_plugin/
    domain/
    application/
    ports/
    adapters/
      host/
      local_protocol/
      platform/
        macos/
        windows/
        linux/
    bootstrap/
    contracts/generated/
  packaging/
    common/
    macos/
    windows/
    linux/
  tests/
    unit/
    contract/
    integration/
    platform/
      macos/
      windows/
      linux/
    packaging/

hermes-connector/
  src/hermes_connector/
    domain/
    application/
    ports/
    adapters/
      cloud/
      local_gateway/
      persistence/
      identity/
      platform/
        macos/
        windows/
        linux/
    bootstrap/
  packaging/
    common/
    macos/
    windows/
    linux/
  tests/
    unit/
    contract/
    integration/
    platform/
      macos/
      windows/
      linux/
    packaging/

hermes-cloud/
  src/hermes_cloud/
    modules/
      identity/
      tenant/
      device/
      command/
      projection/
      authorization/
      collaboration/
      audit/
    platform/
      postgres/
      messaging/
      object_storage/
      kms/
      observability/
    entrypoints/
      business_api/
      connector_gateway/
      worker/
      file_gateway/
    bootstrap/
  deploy/
    test_server/
      systemd/
      nginx/
    aliyun/
      ack/
  tests/
    unit/
    contract/
    integration/
    migration/
    entrypoints/
```

目录边界是强制工程约束：

- Domain、Application 和 Ports 禁止出现 `sys.platform`、操作系统 API 或平台专用
  依赖；平台选择只允许在 Bootstrap/Composition Root 完成。
- macOS、Windows、Linux 分别拥有自己的本地传输、路径/权限、安全存储、服务管理、
  安装升级和诊断实现；即使 macOS 与 Linux 都使用 UDS，也不得把 launchd/systemd、
  Keychain/Secret Service 或平台权限策略混入同一 `posix_*` 文件。
- `common/` 只保存三平台行为完全一致且有共享测试证明的制品；不得成为无法归类代码
  的堆放目录。
- Plugin 与 Connector 的 `packaging/` 独立于运行时代码；安装器、服务清单、签名、
  升级和回滚脚本不得进入 Domain/Application。
- Cloud 不按服务器操作系统拆业务代码，而按领域模块、基础设施平台和四个独立入口
  拆分；测试服务器的 systemd/Nginx 制品与阿里云 ACK 生产制品必须分目录管理。
- 测试目录必须镜像源码边界。平台实现至少具备平台专属契约测试；无法在本机执行的
  平台测试必须在对应 CI Runner 执行，不得以当前开发机平台代替三平台门禁。
- 子 Agent 每次只获得一个领域目录或一个平台目录及其镜像测试目录的写权限；共享
  Contract、Bootstrap、依赖锁和跨平台基类仍由主 Agent 统一集成。
- 迁移旧文件时先建立失败测试，再移动最小垂直切片并保留公开导入兼容层；禁止一次性
  全目录改名或以目录整理为由重写已验证业务行为。
- 项目、Python 分发包、导入包、Entry Point、公开类、服务名和日志模块名必须表达
  当前职责，禁止沿用已经失效的 `mobile`、`dashboard`、`tunnel` 等原型命名。
  Plugin 的正式导入包固定为 `hermes_agent_plugin`，正式公开类使用
  `HermesAgentPlugin*` 命名；`Local Gateway` 只作为 Plugin 对 Connector 暴露的
  本地协议端口名称，不作为整个 Plugin 的产品名。
- 已冻结的 v1 Contract 标识或文件名不得原地改名；若名称已不准确，先登记弃用和
  兼容映射，再发布语义清晰的后继 Contract，避免破坏已发布消费者。
- Distribution、Entry Point 或服务名改名必须定义升级事务和回滚测试。旧、新
  Distribution 不得在同一环境共同拥有相同兼容模块；Plugin 应优先在干净 Extension
  Store 槽位安装并验证新 Bundle，再由 Agent Host 切换激活。Host 槽位能力完成前，
  只允许在停止 Plugin 后按“卸载旧包 → 安装并验证新包 → 失败时恢复旧包”的顺序
  升级，不得把开发机手工清理 metadata 当作商用迁移方案。任何破坏性卸载前必须
  校验旧/新制品的 Distribution、版本和 SHA-256，将回滚制品复制到持久事务目录，
  写入可在进程重启后读取的 receipt；候选安装后必须通过依赖一致性、关键导入和唯一
  Entry Point 校验才算成功。Receipt 必须在每个破坏性阶段前持久化状态，并允许从
  `prepared`、`in_progress` 和 `completed` 阶段幂等恢复旧包；底层安装/卸载函数
  不得作为绕过停服门禁的公共 API。摘要只用于完整性检测，不能替代发布签名验证。

### 2.2 父子 Agent 执行波次

最多同时运行三个子 Agent，主 Agent 保留一个协调槽位：

```text
ROOT
 |
 +-- Wave 1: Contract-Local
 |           Contract-Cloud/File
 |           Contract-A2A/Telemetry
 |
 +-- Wave 2: Plugin Foundation
 |           Connector Foundation
 |           Cloud Foundation
 |
 +-- Wave 3: Local/Cloud Adapters
 |           Command/Outbox/Gateway
 |           Observability/Fault Harness
 |
 +-- Wave 4: Plugin File Port
 |           Connector File Transfer
 |           Cloud File Gateway
 |
 +-- Wave 5: A2A Server
 |           A2A Local Transport
 |           A2A H5
 |
 +-- Wave 6: Enterprise Data Gateway/SDK
 |           Data Product/Query/Governance
 |           AI Card/Action/Renderer
 |
 +-- Wave 7: Packaging/Updater
             Security/Chaos/Capacity
             H5/PWA Core
```

每个波次的固定集成顺序：

```text
Schema/Fixture
  -> Generated Types
  -> Domain State Machine
  -> Application/Ports
  -> Adapters
  -> Integration Tests
  -> Docs/Manifest
  -> Full Regression
```

### Implementation Phase 0

- GOAL-000：建立可追溯开发基线和安全环境入口。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-000 | 主 Agent 审核当前未跟踪文件，排除 `.venv/`、cache、`__pycache__/`、本地 `dist/` 和秘密；在用户明确允许本地提交后建立基线提交 | PARTIAL：secret 已忽略并扫描；未获提交授权 | 2026-07-31 |
| TASK-001 | 运行 Plugin 完整测试，记录 Python、依赖锁、测试数量和制品状态；修正文档中已经过时的 `tui_gateway` fallback 和 extension CURRENT 描述 | DONE AT 2026-07-31 SNAPSHOT：真实 0.19 Host 契约与发行物门禁通过；数量只保留在当次测试日志，不作为当前全库计数 | 2026-07-31 |
| TASK-002 | 创建兄弟项目目录、各自 `pyproject.toml`/lock 边界和独立测试入口，不增加业务行为 | DONE | 2026-07-31 |
| TASK-003 | 实现 secret reference 配置模型，只接受 DSN file/Secret Manager reference；增加日志与异常 secret scan 测试 | DONE FOR CURRENT SQLITE SLICE | 2026-07-31 |
| TASK-004 | 对用户提供的非生产 PostgreSQL/Redis 执行只读 capability probe，确认版本、TLS、ACL、时区、连接上限、Redis eviction 和环境标识，不写表或 key | LATER：用户已指定当前远程 Cloud 使用 SQLite | 2026-07-31 |
| TASK-005 | 建立主 Agent/子 Agent worktree 和目录所有权检查；CI 拒绝子分支修改非授权目录 | PARTIAL：已按目录分工；未跟踪基线下未建立 worktree/CI gate | 2026-07-31 |
| TASK-006 | 审计项目名、Python 包、Entry Point、公开类、服务名、配置键、日志模块和文档术语；为失效的 `mobile`/`dashboard`/`tunnel` 原型命名建立测试先行的弃用、兼容与迁移清单 | DONE：正式源码和新发行物只保留 `hermes_agent_plugin` | 2026-07-31 |

完成门禁：

- 当前 Plugin 测试可复现；
- 仓库不包含密钥、虚拟环境、缓存或临时 Socket；
- worktree 可包含完整基线；
- 未确认环境不会执行数据库迁移。

### Implementation Phase 1

- GOAL-100：冻结全部并行开发依赖的 Contract Packet。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-100 | Contract-Local 子 Agent 创建 Host SPI v1、Local Gateway handshake/runtime/observer/control/file Schema、256 KiB 帧限制、128 KiB 字符限制、错误码和 invalid Fixture | PARTIAL：runtime/observer/control 与 Core Host SPI Stage 3 已冻结并隔离验证；file 待后续 | 2026-08-02 |
| TASK-101 | Contract-Cloud/File 子 Agent创建 Connector hello/welcome/envelope/command/status/file-transfer Schema、Cloud OpenAPI、File Gateway API、chunk/resume/commit 规则 | PARTIAL：Android-first REST/Realtime/Connector command-control 已冻结；file 待后续 | 2026-07-31 |
| TASK-102 | Contract-A2A/Telemetry 子 Agent创建 Collaboration Message、Receipt、Work Item、Delegation、Access Grant、budget/hop/loop、trace context 和字段 persist/log/trace 分类 | PENDING / LATER |  |
| TASK-103 | 主 Agent 解决当前 available/reserved 方法和 Plugin ID 漂移，冻结唯一 capability manifest | DONE FOR CURRENT SLICE | 2026-07-31 |
| TASK-104 | 以 `/contracts` 为唯一输入生成 Python/Kotlin/TypeScript 类型和 valid/invalid/N-1 Fixture；建立只允许 Core Contract→Consumer Adapter 的同步与一致性门禁，增加 canonical digest、breaking change、错误码唯一性和跨语言测试 | PARTIAL FOR CURRENT CONSUMERS：Python/Kotlin 同步；Web 已消费 Cloud OpenAPI 与生成的实时 Schema，但完整 TypeScript API codegen 仍待补齐 | 2026-08-02 |
| TASK-105 | 冻结命令、Plugin、Connector、Cloud、文件、A2A 状态机；为每个状态机提供 ASCII 注释模板、允许转换表和测试向量 | PARTIAL：当前 command/control/lifecycle 完成；file/A2A 待后续 | 2026-07-31 |
| TASK-106 | 在 Host/Connector capability 中预留版本化 `view.card`、`view.interaction`、`enterprise.data` 和 `mcp.app` namespace；完整 Schema 在 Phase 6 冻结 | PENDING / LATER |  |
| TASK-107 | 为标准 Hermes API Server GET 定义独立只读 catalog/history contract、字段分类、provenance/cursor、Plugin HTTP adapter 与 Local Gateway capability；证明其不能发布 Observer/Control capability | PENDING：允许独立规划，当前不得复用 Observer/Control envelope |  |

完成门禁：

- 所有 Schema 示例通过；
- 未知可选字段兼容，未知类型 fail closed；
- Android/Web/Connector/Cloud 的能力差异只能形成 adapter/profile，不得改变核心
  Envelope、状态或错误语义；
- 所有状态、错误、TTL、deadline、帧限制和敏感字段分类有唯一权威；
- 后续子 Agent 不需要猜测字段。

### Implementation Phase 2

- GOAL-200：三个子 Agent 并行完成 Plugin、Connector 和 Cloud Foundation。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-200 | Plugin 子 Agent在 `hermes-agent-plugin/src/hermes_agent_plugin/` 建立 Domain/Application/Ports/Adapters/Bootstrap，完成历史错误命名包退出、旧发行版升级识别、Host SPI、install/start/drain/stop、runtime generation、capability、确定性资源清理 | PARTIAL：Plugin adapter 与 Hermes Core `gateway-extension/1` Stage 3 已完成；真实 Plugin endpoint opener/UDS lifecycle 待跨边界实现 | 2026-08-02 |
| TASK-201 | Plugin 子 Agent为 relay 增加严格 UTF-8、帧/JSON 限制、有界 pending/executor、deadline、profile/UID/symlink 校验和安全错误映射，移除 `max_size=None` | DONE | 2026-07-31 |
| TASK-202 | Connector 子 Agent在 `hermes-connector/` 实现 Python 3.11+ Supervisor、`TaskGroup`、配置、单实例锁、健康状态、Local/Cloud Port 和结构化日志 | DONE FOR MACOS RUNTIME：真实 `build_service_runner` 连续 100 轮后 task、thread、FD/socket、flock、SQLite writer/engine 和 UDS peer 均回到基线 | 2026-07-31 |
| TASK-203 | Connector 子 Agent使用 SQLAlchemy 2.x ORM 与 Alembic/版本化 DDL API 实现 SQLite migration v1、WAL、单写者、有界写队列、Inbox/Outbox/cursor、digest 唯一约束和崩溃测试 hook；除集中式 SQLite PRAGMA 外禁止原始 SQL | DONE | 2026-07-31 |
| TASK-204 | Cloud 子 Agent在 `hermes-cloud/` 建立 FastAPI/ASGI Business API、Connector Gateway、Async Worker、File Gateway 四个 entrypoint 和模块化单体边界 | DONE LOCALLY FOR SQLITE SINGLE NODE：revision 11 与本地门禁已完成；远端仍是旧 `20260731T103631Z` test-server release，未部署当前候选 | 2026-08-02 |
| TASK-205 | Cloud 子 Agent实现 PostgreSQL platform adapter、Alembic migration、runtime/migration role、transaction helper、Outbox/Inbox repository 和审计基础设施 | LATER：当前远程里程碑按用户要求使用 SQLite ORM | 2026-07-31 |
| TASK-206 | 各子 Agent在所属组件接入 OTel、liveness/readiness、错误分类和队列/连接/状态指标；Operations 配置由后续专属 Agent 汇总 | PARTIAL：health/error/queue gates 已有；统一 OTel/Operations 待后续 | 2026-07-31 |

Cloud 初始 PostgreSQL 模块和表：

```text
identity:       tenants, users, memberships, roles
workspace:      workspaces, workspace_memberships
device:         agents, devices, device_credentials, pairing_sessions
command:        commands, command_attempts, command_transitions
authorization:  policies, access_grants
data_catalog:   purpose_catalog, purpose_bindings, data_products,
                data_product_versions, data_fields, consent_records
data_access:    query_runs, resource_snapshots, resource_grants,
                access_decisions, quality_results, lineage_edges
experience:     card_instances, card_revisions, action_attempts
file_exchange:  file_assets, file_transfers, file_parts, file_acl, scan_results
collaboration:  a2a_messages, receipts, work_items, delegations, budget_usage
retention:      retention_jobs, deletion_tombstones
audit:          audit_events
platform:       outbox_events, inbox_messages
```

首个 Android 云端闭环采用独立的 Cloud 兼容适配层，不把 Android 变成 Connector
Protocol 或 Local Gateway Protocol 参与方：

- REST 权威为 `/contracts/openapi/cloud-api-v1.json`；
- 外部 Realtime 权威为 `/contracts/cloud-realtime-v1.json`；
- P0 先完成 Basic 登录、刷新、Session Projection、Transcript 和 Observer；
- 当前闭环继续补齐 Control；客户端只能看到 Cloud 级不透明控制绑定，禁止暴露
  Plugin 本地 Lease；
- 远程单节点阶段统一使用 SQLite 与 SQLAlchemy ORM；未来 PostgreSQL 迁移必须另行
  编号并经过跨租户、UUID 外键和回滚门禁，不得把 SQLite 细节泄漏进领域层。
- Cloud 部署并通过外部验收后，Android 子 Agent 必须将 consumer 适配器收紧到
  Realtime v1：observer ticket/ready role、exact event allowlist、必填 Session/Sequence、
  RPC error catalog、subscribe/unsubscribe 结果、running/status 一致性、UTF-8 字节限制和
  one-frame-one-document；不得继续用未知事件推进游标。

完成门禁：

- 三个项目可以独立启动和停止；
- 100 次 Plugin/Connector 生命周期循环无任务、线程、Socket 泄漏；
- SQLite/PostgreSQL migration 可从空库执行并从最近两个版本升级；
- Redis/NATS/对象存储不可用不影响 Foundation 启动诊断；
- Domain/Application 没有基础设施反向依赖。

### Implementation Phase 3

- GOAL-300：完成设备配对、远程命令和 Observer 的第一条真实纵向闭环。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-300 | Plugin 完成 Observer sequence/gap/snapshot、Control lease/generation、Command Guard、pending revision 和 owner action Host Adapter | PARTIAL：Core 已完成真实 `prepare/activate/close`、output-parity v2 producer、catalog 与受限 owner production caller；Plugin 侧已有严格 projector/sequence/lease，尚缺真实 UDS opener 的 write-success→activate、断开 close 与 shared-authority rollover 集成 | 2026-08-02 |
| TASK-301 | Connector 完成 Agent discovery、Local Gateway client、Device Identity/OS Secret Store、pairing、Cloud WSS hello/welcome/heartbeat/backoff/resume | PARTIAL：macOS Keychain identity、正式 pairing/proof/token、transport/runtime、Discovery executor 所有权、取消安全关闭和 `run/finally + stop` 单飞 socket 清理完成并通过独立复审；OS install service 待 Phase 8 | 2026-07-31 |
| TASK-302 | Cloud 完成 Tenant/Device/Pairing、Gateway WSS 认证、连接窗口、慢消费者、设备吊销和 Redis Presence/限流/路由缓存 | PARTIAL：pairing/proof/revocation、精确 ACL 与 WSS auth/limits 已完成本地实现；`20260731T103631Z` 只保留旧 test-server 时间快照，当前 revision 11 尚未远程部署；Redis 与多节点路由待后续 | 2026-08-02 |
| TASK-303 | Cloud 完成 Command + Outbox 同事务、NATS/JetStream 发布、Gateway dispatch、Connector ACK/Result Consumer Inbox 和 Reconciler | PARTIAL：SQLite ORM command router/dispatch 完成；NATS 与多节点对账待后续 | 2026-07-31 |
| TASK-304 | Connector 完成命令先落 Inbox 再 `DELIVERED`、Local RPC、结果先落 Outbox、Cloud ACK 和 Agent/Cloud 重启对账 | PARTIAL：Connector Inbox/Outbox/ACK 与本地 transport 已实现；Core Host SPI 阻断已解除，真实 Local RPC 权威效果和 Agent 重启对账现受 Plugin opener/临时端到端集成阻断 | 2026-08-02 |
| TASK-305 | Observability 子 Agent在 `operations/` 建立 OTel Collector、Dashboard、P0/P1/P2 告警和安全诊断包；跨层 Trace 证明 Command 可端到端关联 | PENDING / LATER |  |
| TASK-306 | E2E 子 Agent覆盖断线、重复、乱序、旧 generation、超时、连接池耗尽、磁盘满、Redis/NATS/PostgreSQL 故障和 `UNKNOWN` | PARTIAL：当前 command/control/SQLite/UDS/WSS 故障门禁完成；商用依赖 chaos 待后续 | 2026-07-31 |

Redis 固定用法：

```text
presence:{tenant_id}:{device_id}       TTL，可重建
route:{tenant_id}:{device_id}          Gateway instance + connection epoch，TTL
rate:{tenant_id}:{principal}:{action}  Token bucket
lock:{purpose}:{opaque_id}             短锁，业务写入仍由 PostgreSQL 约束
```

完成门禁：

- Command + Outbox 原子提交；
- 已提交命令丢失为 0；
- 重复投递导致重复业务副作用为 0；
- Redis 清空或不可用后业务事实完整；
- UI/诊断能区分 CREATED、QUEUED、DISPATCHED、DELIVERED、EXECUTING、
  `UNKNOWN` 和终态；
- `UNKNOWN` 只对账或人工处理，不自动重做。

### Implementation Phase 4

- GOAL-400：完成安全文件交换闭环。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-400 | Plugin File 子 Agent实现 HostFilePort、Export/Import Grant、source/sink handle、generation/lease/principal/transport 绑定和 file JSON-RPC |  |  |
| TASK-401 | Connector File 子 Agent实现 transfer state、SQLite migration v2、spool quota、连续 chunk、fsync/cursor/ACK、resume、SHA-256、清理和磁盘故障降级 |  |  |
| TASK-402 | Cloud File 子 Agent实现 file metadata/ACL/upload session、File Gateway 流式 multipart、对象存储 Adapter、final hash/size/MIME 校验、扫描/隔离和保留删除 |  |  |
| TASK-403 | Web 子 Agent实现文件 offer/accept/progress/abort/download 状态；不暴露对象 key、本地路径或长期签名 URL |  |  |
| TASK-404 | Security 子 Agent实施非法文件名、路径穿越、symlink/TOCTOU、特殊文件、chunk 重复/乱序、hash mismatch、压缩炸弹、配额和 `ENOSPC` 测试 |  |  |
| TASK-405 | 主 Agent完成 File Gateway、Connector spool、Host Grant 三方对账和删除传播 E2E |  |  |

文件状态代码注释必须包含：

```text
OFFERED -> AUTHORIZING -> STAGING -> TRANSFERRING -> VERIFYING
   |           |            |            |              |
   |           |            |            v              v
   |           |            +------> PAUSED <------ CORRUPT
   |           |                         |
   |           +-> REJECTED              +--resume----> TRANSFERRING
   +-> EXPIRED

VERIFYING -> SCANNING -> COMMITTING -> AVAILABLE
                 |            |
                 v            +-> FAILED_COMMIT
             QUARANTINED
```

完成门禁：

- 未授权本地文件泄露字节数为 0；
- 未完整 durable 的 chunk ACK 数为 0；
- hash/扫描未通过的文件 AVAILABLE 数为 0；
- 文件名和 `file_ref` 无法改变 staging/object 目标；
- 中断后只能从 Server、SQLite 和 Host Grant 都认可的最小安全 offset 恢复。

### Implementation Phase 5

- GOAL-500：完成 A2A 组织协作基础闭环。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-500 | A2A Server 子 Agent实现 PostgreSQL Mailbox、Message/Receipt/Work Item、Delegation、Access Grant、预算、Policy、Transactional Outbox 和离线投递 |  |  |
| TASK-501 | A2A Server 子 Agent实现 hop/depth/fan-out、时间/Token/费用预算、causation loop guard、委托撤销和人工 Gate |  |  |
| TASK-502 | A2A Local 子 Agent实现 Connector collaboration feature、SQLite 交付状态、Receipt/Result、重连对账和 Generic Work Item Host Port |  |  |
| TASK-503 | Plugin 子 Agent实现公开 Host SPI capability 和结构化 Work Item 适配，不引入具体 Skill 或企业业务规则 |  |  |
| TASK-504 | A2A H5 子 Agent实现收件箱、委托范围、预算、人工接受/修改/拒绝、工作状态和审计时间线 |  |  |
| TASK-505 | Reliability 子 Agent覆盖跨 Tenant、委托伪造/扩大/撤销、循环、预算耗尽、fan-out 风暴、离线、重复、乱序、过期、NATS redelivery 和 `UNKNOWN` |  |  |
| TASK-506 | File/A2A 集成只允许传已扫描且接收方重新授权的 `file_ref`；引用撤销或过期立即阻止后续读取 |  |  |

A2A 状态代码注释必须包含：

```text
CREATED -> POLICY_CHECKED -> QUEUED -> AVAILABLE -> DELIVERED -> ACKED
    |             |            |           |           |
    +-> REJECTED  +-> EXPIRED  +-----------+----------> EXPIRED
                                                        |
                                                        v
                                                   EXECUTING
                                                     |  |  |
                                                     |  |  +-> UNKNOWN
                                                     |  +----> FAILED
                                                     +-------> SUCCEEDED

any active state -- cancel/revoke/budget/loop --> CANCELLED_OR_BLOCKED
```

完成门禁：

- Server Mailbox 状态不依赖 Redis；
- 设备离线、Connector 重启和 NATS 重投后 Receipt 可收敛；
- 跨 Tenant 或无委托资源访问成功数为 0；
- 循环、hop、depth、fan-out 和预算超限全部终止并审计；
- A2A 状态、预算和人工 Gate 有完整 OTel 指标。

### Implementation Phase 6

- GOAL-600：完成首个受治理业务 Data Product、AI Card 展示、Action 回写与失效
  传播闭环。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-600 | Contract 子 Agent冻结 Enterprise Gateway、Data Product、Semantic Query、Data Result、Resource Reference、AI Card、Action、Purpose、Policy Decision、Quality、Freshness、Lineage 和删除事件 Schema |  |  |
| TASK-601 | 在 `hermes-enterprise-gateway/` 建立独立 Python 运行时、出站 mTLS enrollment、Supervisor、OS Secret Store、本地 checkpoint/spool 和受控诊断 |  |  |
| TASK-602 | 建立 Source Connector SDK，分别定义 Query/Extract/CDC/Action Port、参数绑定、Schema、ACL、checkpoint、限流、超时、删除和停用合同 |  |  |
| TASK-603 | Cloud Data 子 Agent实现 Data Product Registry、Purpose Catalog、Semantic Layer、Metric/Dimension/Filter、Owner、分类、质量/新鲜度 SLO 和版本停用 |  |  |
| TASK-604 | Governance 子 Agent实现 Policy Decision/Enforcement：RBAC、ABAC/Purpose、来源 ACL、PostgreSQL FORCE RLS、列级 deny/mask/tokenize/generalize、行/字节/聚合限制 |  |  |
| TASK-605 | Query 子 Agent实现 Query Broker：查询规范化、授权预算、物化优先、策略作用域缓存、live Gateway、异步转换和 cancellation/deadline |  |  |
| TASK-606 | Ingestion 子 Agent实现分页抽取、Arrow/Parquet batch、staging、Schema/质量校验、watermark、lineage、Outbox 和 ACK 后 checkpoint |  |  |
| TASK-607 | CDC 子 Agent实现 snapshot→catch-up→streaming、`(source, partition, position)` 幂等、gap/乱序/Schema breaking 对账和删除/撤权事件 |  |  |
| TASK-608 | Data Access 子 Agent实现最小字段裁剪、行过滤、列脱敏、Data Result Envelope、授权指纹、资源 Grant、加密 Snapshot 和缓存失效 |  |  |
| TASK-609 | Security 子 Agent实现数据与指令隔离、DLP/分类/Prompt Injection 标签、安全 Markdown/HTML 处理和模型路由；来源文本不能产生 Tool/Action 权限 |  |  |
| TASK-610 | View 子 Agent实现 Hermes AI Card Registry/View Compiler、受信组件 Catalog、Manifest 签名、来源/新鲜度/质量/血缘展示、STALE/REVOKED/QUARANTINED 状态 |  |  |
| TASK-611 | 实现 A2UI v0.9.1 兼容 Profile：Hermes card↔surface、block↔component、payload↔data model、interaction↔action，并进行 catalog capability 协商 |  |  |
| TASK-612 | 在 Agent Host 新增 View SPI，并基于 `/Users/apple/.hermes/hermes-agent/ui-tui/src/sdk/` 的现有 Widget Catalog/Registry 增加受限 AI Card renderer；Plugin/Connector 只透明适配 Card envelope/interaction，Android 会话增加可原位更新的 `AI_CARD` section |  |  |
| TASK-613 | 在 `hermes-web/` 实现同一 Hermes Card Schema 的 React Renderer；数据超限时只使用受权 `resource_ref`，不复制完整结果 |  |  |
| TASK-614 | Action 子 Agent实现服务端签名 Action Catalog、点击和副作用边界双重再鉴权、实体 revision、风险等级、人工确认、幂等、Outbox 和 `UNKNOWN` 对账 |  |  |
| TASK-615 | A2A 扩展支持 `card_ref/resource_ref`，完成发送方 Share 校验、接收方解引用/渲染/Action 再鉴权和每跳范围单调收窄 |  |  |
| TASK-616 | 实现 Policy/Consent/Grant/成员关系/来源更正删除到 Snapshot、Card、H5、Connector、Agent Cache、搜索/向量和派生物的可重试失效/清除账本 |  |  |
| TASK-617 | 选择一个低风险、只读、Internal 级 Data Product 和一张 Card 完成灰度；治理验收前禁止接入 Confidential/Restricted 或生产库任意 SQL |  |  |
| TASK-618 | MCP Apps 仅建立独立 capability 与 sandbox lane，用于复杂富应用；默认关闭外域、设备权限和 tool 写操作，不影响普通 Card renderer |  |  |
| TASK-619 | Operations/E2E 子 Agent覆盖 Gateway、Query、CDC、质量、Card、Action、删除与 A2A 的 Trace、指标、告警、故障注入和容量测试 |  |  |

Enterprise Data Gateway、Query 和 Card 状态代码注释必须包含：

```text
Gateway:
UNENROLLED -> ENROLLING -> CONNECTING -> READY
CONNECTING -> BACKOFF -> CONNECTING
READY -> DEGRADED -> READY
READY/DEGRADED -> DRAINING -> STOPPED
any -> BLOCKED

Data access and Card:
REQUESTED -> CONTEXT_BOUND -> AUTHORIZING
AUTHORIZING -> DENIED
AUTHORIZING -> FETCHING -> INSPECTING
FETCHING/INSPECTING -> FAILED
INSPECTING -> QUARANTINED
INSPECTING -> SNAPSHOT_READY -> CARD_READY -> RENDERED

RENDERED -> STALE -> REAUTHORIZING
REAUTHORIZING -> FETCHING / DENIED / REVOKED

any active state --policy/grant/source/TTL change-->
INVALIDATED -> PURGE_PENDING -> PURGED

Card Action:
PROPOSED -> REAUTHORIZING -> AUTHORIZED
AUTHORIZED -> CONFIRMATION_REQUIRED -> HUMAN_CONFIRMED
AUTHORIZED/HUMAN_CONFIRMED -> PERSISTED -> DISPATCHED -> SOURCE_EXECUTING
SOURCE_EXECUTING -> SUCCEEDED / FAILED / UNKNOWN
UNKNOWN -> RECONCILING -> SUCCEEDED / FAILED / NEEDS_HUMAN
```

完成门禁：

- Agent/模型直接连接业务数据库或执行自由 SQL 的路径为 0；
- 跨 Tenant/Workspace/Purpose、行级和列级越权成功数为 0；
- 未授权正文进入模型、Card、缓存、日志、Trace 或诊断的测试字节数为 0；
- 所有 Card 都显示来源、`as_of`、新鲜度、质量、脱敏和失效状态；
- `RENDERED` 不授予 Action，STALE Card 高风险 Action 全部关闭；
- 不可信来源数据直接触发 Tool、Action、权限扩大或外发的次数为 0；
- 删除、更正和撤权在 SLO 内传播，失败任务可观测、重试和人工接管；
- 低风险 Data Product 的读取→Card→可补偿 Action→来源事件→Card 刷新闭环通过。

### Implementation Phase 7

- GOAL-700：完成 H5/PWA 统一操作体验。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-700 | 在 `hermes-web/` 建立 React + TypeScript + Vite + Workbox 特性切片架构、生成 API Client、统一鉴权和 Realtime Client | PARTIAL：React/TypeScript/Vite 特性切片、cookie auth、目录和 realtime client 已落地；Workbox 与完整 API codegen 未完成 | 2026-08-02 |
| TASK-701 | 实现设备中心、配对/吊销、Connector/Agent/Enterprise Gateway 组合状态和兼容性提示 |  |  |
| TASK-702 | 实现 Session Observer、Command 时间线、Control Lease、Prompt/Interrupt/Steer/Approval/Clarify 的 capability gate | PARTIAL LOCALLY：Web 投影、lease 和 capability gate 已实现；真实 Connector/Plugin/Hermes 动作闭环与远程验收未完成 | 2026-08-02 |
| TASK-703 | 集成 AI Card Workspace、文件中心和 A2A Inbox/Work Item/Delegation/Budget，不缓存文件正文或 Personal Memory |  |  |
| TASK-704 | Service Worker 只缓存静态资源和明确允许的只读 GET；控制、审批、权限、Card Action、文件 commit 和 A2A 副作用不得后台重放 |  |  |
| TASK-705 | Confidential/Restricted Card 不进入通用 Service Worker 或长期 IndexedDB；离线恢复必须重新鉴权 |  |  |
| TASK-706 | 接入 Web Vitals、前端错误、API/WSS 状态、Card/Action 和命令关联；禁止采集输入或业务正文 |  |  |

完成门禁：

- H5 不能调用未开放 capability；
- 离线草稿和 Card 恢复后重新鉴权并由用户确认；
- UI 状态与 Server/Connector/Agent/Data Product 事实一一对应；
- iOS Safari、Android Chrome、桌面 Chrome/Edge 的核心流程通过。

### Implementation Phase 8

- GOAL-800：完成统一安装、升级、调试和商用运维。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-800 | 实现签名 Plugin Bundle、Compatibility Manifest、Agent Extension Store 和 Host install/activate/rollback/remove；涉及 `/Users/apple/.hermes/hermes-agent` 的修改必须单独获准 |  |  |
| TASK-801 | 实现一个用户安装入口、Connector OS Service、Plugin staged slot、Repair/Uninstall 和 Agent 缺失 `WAITING_FOR_AGENT` |  |  |
| TASK-802 | 实现 Connector 双槽、签名更新、drain、SQLite expand/migrate/contract、健康检查和自动回滚 |  |  |
| TASK-803 | Operations 子 Agent完成 Alibaba Cloud 与企业 Data Gateway 环境模板、OTel/ARMS 或等价后端、日志、Dashboard、告警、状态页和 Runbook |  |  |
| TASK-804 | 实现受控 Debug policy、限时诊断、secret scan、诊断包过期和客户可执行 Repair 建议，禁止远程 Shell |  |  |
| TASK-805 | 生成 SBOM，执行依赖漏洞、许可证、镜像、Bundle、安装器、Gateway 制品签名和供应链验证 |  |  |

完成门禁：

- 用户无需安装 Python 包或修改 Agent venv；
- Agent、Plugin、Connector 任一更新失败不破坏 Agent 本地使用；
- Repair/Uninstall 不删除 Session、配置、Connector durable state 或整个
  `HERMES_HOME`；
- Enterprise Data Gateway 不开放企业入站端口，源凭据不离开企业控制域；
- 值班人员不读取正文即可定位主要故障；
- 每个 P0/P1 告警均可按 Runbook 缓解、恢复和对账。

### Implementation Phase 9

- GOAL-900：完成商用发布验证。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-900 | 执行 N/N-1/N-2 Agent/Plugin/Connector/Enterprise Gateway/Server/H5/Card 兼容矩阵 |  |  |
| TASK-901 | 执行 10,000/20,000/100,000 Connector、5,000 events/s、重连风暴、慢消费者、Query/Card、文件和 A2A 配额压测 |  |  |
| TASK-902 | 执行 Agent/Plugin/Connector/Enterprise Gateway/Gateway/Worker/File Gateway 随机终止、RDS 切换、NATS 故障、Redis 清空、对象存储/来源超时和时钟漂移 |  |  |
| TASK-903 | 执行备份恢复、Tenant/Workspace 删除、来源更正删除、Card 失效、文件删除、设备吊销、密钥轮换、Delegation 撤销和审计导出演练 |  |  |
| TASK-904 | 执行 internal → 1% → 10% → 50% → 100% 灰度；错误预算耗尽时自动暂停 |  |  |
| TASK-905 | 更新全部 `[CURRENT]`、Data Product/Card Catalog、兼容矩阵、Runbook、制品摘要和发布证据 |  |  |

完成门禁：

- 已提交命令、A2A Work Item、Data Action 和已完成文件事实丢失为 0；
- 重复投递导致重复业务副作用为 0；
- 跨 Tenant/Workspace/Purpose、行级和列级越权为 0；
- 不可信数据直接触发副作用为 0；
- 无未关闭 P0/P1 安全问题；
- 安装、更新、回滚、灾备、数据源停用、删除和密钥轮换全部通过。

## 3. Alternatives

- **ALT-001**：把 Connector 合并进 Agent。未采用，因为会把网络、SQLite、更新和
  云端故障放大到 Agent 本地执行。
- **ALT-002**：首期全微服务。未采用，因为领域边界尚未通过真实流量验证，会增加
  分布式事务、部署和运维成本。
- **ALT-003**：使用 Redis Streams 保存命令和 A2A Mailbox。未采用，因为 Redis
  故障或淘汰策略不能影响业务事实。
- **ALT-004**：Connector 直接连接 NATS/PostgreSQL/Redis/OSS。未采用，因为会泄露
  云内部拓扑、扩大本地攻击面并阻碍 Server 演进。
- **ALT-005**：大文件通过 WSS base64 传输。未采用，因为会放大内存、带宽和重连
  成本；大文件使用独立 File Gateway。
- **ALT-006**：A2A 设备间 P2P。未采用，因为离线投递、Tenant ACL、委托撤销、
  审计和循环治理必须由 Server 控制。
- **ALT-007**：首期完整 Event Sourcing。未采用，因为恢复、治理和调试复杂度高于
  收益；采用当前事实表、状态历史、审计和 Outbox。
- **ALT-008**：让员工 Agent 或 Hermes Connector 直连数据中心数据库。未采用，
  因为会扩散源凭据、绕过来源 ACL，并把员工端故障域带入企业数据面。
- **ALT-009**：允许模型生成自由 SQL。未采用，因为无法稳定约束字段权限、查询
  成本、语义口径、删除更正和来源审计。
- **ALT-010**：AI Card 直接下发任意 HTML/JavaScript。未采用，因为远端代码会
  扩大 Agent Host 攻击面并破坏跨端原生渲染。
- **ALT-011**：用 A2UI 全量替换 Hermes 内部 View、权限和审计语义。未采用；首期
  保留 Hermes 权威合同并提供 A2UI v0.9.1 Profile，等待 v1 与各端 Renderer
  稳定后再评估。
- **ALT-012**：所有 Card 都使用 MCP Apps iframe。未采用；MCP Apps 只服务复杂
  富应用，普通经营卡片使用无任意代码的声明式 Renderer。

## 4. Dependencies

- **DEP-001**：用户确认本计划及兄弟项目目录布局。
- **DEP-002**：用户明确允许建立本地 Git 基线提交和开发分支；不包含 push、deploy
  或生产迁移授权。
- **DEP-003**：PostgreSQL 非生产连接通过 `HERMES_POSTGRES_DSN_FILE` 安全提供。
- **DEP-004**：Redis 非生产连接通过 `HERMES_REDIS_DSN_FILE` 安全提供。
- **DEP-005**：NATS/JetStream 测试环境；正式环境在 Remote 内部部署，不对 Connector
  暴露。
- **DEP-006**：对象存储、KMS/Secret Manager、病毒/内容扫描服务和 File Gateway
  域名/TLS。
- **DEP-007**：OpenTelemetry Collector 和指标/日志/Trace 后端。
- **DEP-008**：Hermes Agent Stable Host SPI 与 Extension Manager 的修改权限；
  当前源码位置为 `/Users/apple/.hermes/hermes-agent`。
- **DEP-009**：macOS Keychain、Windows DPAPI、Linux Secret Service 的测试环境。
- **DEP-010**：Android、Python、TypeScript Golden Fixture 生成与回归工具链。
- **DEP-011**：一个具有 Data Owner、稳定主键、Schema、ACL、删除更正路径和质量/
  新鲜度 SLO 的低风险只读 Data Product。
- **DEP-012**：企业内网或 VPC 中可部署 Enterprise Data Gateway 的主机、出站
  mTLS 443、Secret Store 和源系统最小权限服务账号。
- **DEP-013**：源系统官方 API/Webhook/CDC 或受控查询模板；缺少来源幂等和状态
  查询的写操作不得开放。
- **DEP-014**：A2UI v0.9.1 Fixture/Renderer 兼容测试；MCP Apps sandbox lane
  作为独立可选 capability。
- **DEP-015**：Data Owner、Security 和业务 Owner 对 Purpose、字段策略、模型用途、
  Action 风险、保留删除和 Kill Switch 的联合验收。

## 5. Files

- **FILE-001**：`/Users/apple/hermesmobile/contracts/`，新增跨端协议、错误码、限制、
  telemetry、file 和 collaboration Schema/Fixture。
- **FILE-002**：`/Users/apple/hermesmobile/hermes-agent-plugin/src/hermes_agent_plugin/`，
  渐进迁移 Plugin 六边形结构、Host SPI Adapter、Control/Observer/File/A2A Port。
- **FILE-003**：`/Users/apple/hermesmobile/hermes-connector/`，新增独立 Connector
  项目、SQLite migrations、Local/Cloud/File/A2A Adapter、Updater 和测试。
- **FILE-004**：`/Users/apple/hermesmobile/hermes-cloud/`，新增 Gateway、Business
  API、Worker、File Gateway、PostgreSQL/Redis/NATS/Object Storage Adapter 和测试。
- **FILE-005**：`/Users/apple/hermesmobile/hermes-web/`，新增 H5/PWA。
- **FILE-006**：`/Users/apple/hermesmobile/operations/`，新增 OTel、Dashboard、
  Alert、Runbook 和部署模板。
- **FILE-007**：`/Users/apple/hermesmobile/tests/e2e/`，新增跨项目 E2E、Fault、
  Security、Compatibility 和 Capacity 测试。
- **FILE-008**：`/Users/apple/.hermes/hermes-agent`，仅在获得单独修改授权后实施
  Stable Host SPI、View SPI、Extension Manager，以及
  `ui-tui/src/sdk/`/`ui-tui/src/components/` 的受信 AI Card Catalog/Renderer。
- **FILE-009**：`/Users/apple/hermesmobile/hermes-agent-plugin/docs/`，由主 Agent
  同步 CURRENT、目录布局、协议和验收证据。
- **FILE-010**：`/Users/apple/hermesmobile/hermes-enterprise-gateway/`，新增内网
  出站 Gateway、Source Connector SDK、checkpoint/spool、mTLS、诊断和测试。
- **FILE-011**：`/Users/apple/hermesmobile/hermes-cloud/src/hermes_cloud/modules/`，
  新增 enterprise_gateway、data_catalog、semantic、query_broker、data_ingestion、
  data_quality、lineage、view 和 action 模块。
- **FILE-012**：`/Users/apple/hermesmobile/hermes-android/app/src/main/kotlin/app/hermesmobile/sessions/ai_card/`，
  新增 Android AI Card section、原位 revision 更新和交互事件。
- **FILE-013**：`/Users/apple/hermesmobile/operations/enterprise-data/`，新增 Data
  Gateway、Query、CDC、Quality、Card 和 Action 的 Dashboard、Alert 与 Runbook。

## 6. Testing

- **TEST-001**：Plugin Domain/Application 单元测试覆盖 lifecycle、lease、
  generation、pending revision、command guard、observer sequence 和 file grant。
- **TEST-002**：Connector 单元测试覆盖 Supervisor、queue、SQLite state、Inbox/
  Outbox、Reconciler、WSS、Device Identity、file transfer 和 A2A delivery。
- **TEST-003**：Cloud 单元测试覆盖 Tenant、Device、Command、Authorization、
  File、A2A、Outbox/Inbox、Redis degradation 和审计。
- **TEST-004**：Contract 测试覆盖 Schema、Golden Fixture、错误码、帧限制、严格
  UTF-8、未知字段和 N/N-1/N-2。
- **TEST-005**：Integration 测试覆盖 Plugin↔Host、Connector↔Local Gateway、
  Connector↔Gateway、Command↔PostgreSQL↔NATS、File Gateway↔Object Storage。
- **TEST-006**：Fault 测试覆盖每个持久化/副作用边界的崩溃、超时、重复、乱序、
  断线、磁盘满、数据库损坏、连接池耗尽和依赖不可用。
- **TEST-007**：File Security 测试覆盖路径穿越、symlink/TOCTOU、特殊文件、非法
  UTF-8、超大内容、hash mismatch、压缩炸弹、扫描和配额。
- **TEST-008**：A2A Security 测试覆盖跨 Tenant、委托伪造/扩大、循环、预算、
  fan-out、离线、撤销、过期和人工 Gate。
- **TEST-009**：Observability 测试证明 Trace 连续、状态指标齐全、告警可触发且
  日志/Trace/诊断无秘密和正文。
- **TEST-010**：Packaging 测试覆盖干净系统安装、Agent 缺失、Repair、卸载、升级、
  回滚、签名、SBOM 和不修改 Agent 用户数据。
- **TEST-011**：H5 E2E 覆盖弱网、断线、状态时间线、capability gate、文件和 A2A，
  并证明 Service Worker 不重放副作用。
- **TEST-012**：Capacity/Chaos 覆盖目标在线连接、事件吞吐、重连风暴、RDS/NATS/
  Redis/Object Storage 故障和版本灰度。
- **TEST-013**：Tenant/Workspace/Purpose/RBAC/ABAC/来源 ACL 组合矩阵、伪造上下文、
  Policy 版本变化和默认拒绝。
- **TEST-014**：PostgreSQL `FORCE RLS`、Runtime Role 无 `BYPASSRLS`、连接池上下文
  残留、事务复用和跨租户复合外键。
- **TEST-015**：列级 deny/mask/tokenize/generalize 在模型调用前生效；模型、Card、
  缓存、日志和错误中未授权原文字节为 0。
- **TEST-016**：恶意 HTML/Markdown/CSV、间接 Prompt Injection、伪造 Action/Tool
  指令和外发诱导；自动工具调用与业务副作用为 0。
- **TEST-017**：Card 打开/刷新、切换 Workspace、Purpose/Grant/Consent 过期、
  角色撤销、实体 revision 冲突、重复点击、CSRF、离线重放和 `UNKNOWN`。
- **TEST-018**：来源延迟、质量失败、Schema/ACL 漂移、更正和删除；Card 正确进入
  DEGRADED、STALE、QUARANTINED、INVALIDATED 和 PURGED。
- **TEST-019**：Enterprise Gateway mTLS、源凭据、Query/Extract/CDC checkpoint、
  gap、重复、乱序、磁盘 spool 水位和来源限流/超时。
- **TEST-020**：A2A `card_ref/resource_ref` 无权限接收、离线期间撤权、多跳扩大
  字段/Purpose/TTL、复制正文绕过、过期引用和接收方 Action。
- **TEST-021**：Hermes Card 与 A2UI v0.9.1 双向 Fixture、Catalog capability、
  Android/H5 渲染一致性、Card 尺寸/组件限制和未知组件降级。
- **TEST-022**：MCP Apps sandbox、CSP、permission、tool-call proxy 和无 capability
  客户端文本降级；sandbox 不能访问 Host DOM、cookie 或任意本地能力。

截至 2026-08-02 可引用的本地候选回归快照为：Cloud 全量 `1513 passed`，根
Contract `101 passed + 67 subtests passed`，H5 application `327 passed`、
process-lifecycle `15 passed`，且 H5 typecheck、lint、production build 通过。旧文档
中的更小测试数只表示其标注日期的阶段性运行，不得覆盖该快照。范围仅为本地
Cloud/H5/contract 候选，不构成远程部署、Connector/Plugin 或真实 Hermes 链路验收。

每个子 Agent 返回：

```text
scope:
files_changed:
tests_added:
tests_run:
failure_evidence_before:
success_evidence_after:
state_diagrams_added:
timeouts_and_limits:
known_risks:
```

主 Agent 必须独立检查 diff、目录所有权、ASCII 状态机、错误门禁和完整测试，不直接
采信子 Agent 的完成声明。

## 7. Risks & Assumptions

- **RISK-001**：`hermes-agent-plugin/` 当前整体未跟踪，未建立基线提交前无法安全
  使用 worktree 并行开发。
- **RISK-002**：Stable Host SPI 和 Extension Manager 需要修改独立 Hermes Agent
  源码；如果不授权，只能完成 Plugin 侧 Port 和 Fake Host。
- **RISK-003**：用户提供的 PostgreSQL/Redis 若为生产资源，只能先执行只读探测；
  开发和 migration 必须等待独立非生产环境。
- **RISK-004**：当前尚未提供 NATS/JetStream、对象存储、KMS、扫描服务和 OTel
  后端，真实云端闭环依赖这些资源。
- **RISK-005**：文件内容扫描与端到端加密存在能力冲突；v1 采用 Server 可扫描的
  受控加密存储，E2EE 文件需单独 ADR。
- **RISK-006**：A2A 若早于身份、授权、命令和文件引用稳定上线，会产生权限扩张和
  循环执行风险，因此只提前冻结契约，功能在核心闭环后启用。
- **RISK-007**：macOS、Windows、Linux 安装和安全存储差异会扩大验证矩阵；首条
  开发闭环先在 macOS 验证，商用门禁覆盖三平台。
- **RISK-008**：多 Agent 同时修改共享 Schema、migration、锁文件或文档会产生
  隐蔽冲突，因此这些文件只能由主 Agent 合并。
- **RISK-009**：来源 ACL 与 Hermes Policy 漂移可能过度授权；来源事实优先，使用
  版本绑定、契约测试、短 TTL 和 fail closed。
- **RISK-010**：只在 Card 层脱敏会让原文先进入模型；字段裁剪与脱敏必须位于
  Data Access Gateway。
- **RISK-011**：Prompt Injection 扫描存在漏报；结构化数据/指令隔离、Tool/Action
  Allowlist 和再鉴权是强边界，扫描只作辅助。
- **RISK-012**：RLS 连接池上下文残留会造成跨 Tenant 泄露；使用事务级上下文、
  FORCE RLS、连接归还清理和专门故障测试。
- **RISK-013**：离线 Card 和 Agent Cache 可能在撤权后继续显示；敏感 Card 不进入
  通用离线缓存，并使用短 TTL、撤权推送和打开时再鉴权。
- **RISK-014**：来源缺少稳定 ID、删除事件、字段 ACL 或幂等 Action；此类数据源
  只能只读或聚合接入，不能宣称完整写回闭环。
- **RISK-015**：A2UI v1 和跨端 Renderer 仍在演进；内部 Hermes Card 保持版本化，
  A2UI 作为兼容 Profile，避免锁定候选协议。
- **RISK-016**：MCP Apps 允许 HTML/JS，错误 CSP 或权限代理会扩大攻击面；它必须
  保持独立 sandbox lane、最小 capability 和服务端 Action 再鉴权。
- **RISK-017**：Hermes 0.19 API Server 的同一 bearer key 同时覆盖读取与变更接口，
  不是只读作用域凭证。即使未来只调用 GET，Plugin 仍必须固定 loopback endpoint、
  方法 allowlist、超时/响应上限和敏感字段裁剪，并在独立安全门禁通过前不发布
  catalog capability；Connector 绝不持有该 endpoint 或 key。
- **ASSUMPTION-001**：PostgreSQL 和 Redis 连接支持 TLS、独立 ACL 和非生产测试。
- **ASSUMPTION-002**：Remote Server 首期部署在阿里云，应用代码保持 OTel、S3/
  Object Storage Port 和 Secret Port 抽象，不把厂商 SDK泄露到 Domain。
- **ASSUMPTION-003**：当前首个可验收里程碑是“本地 H5 生产 client + revision 11
  Cloud 候选的鉴权、Agent/session 目录、ticket 和 logout 失效闭环”，不含真实
  Hermes Observer/Control、Connector/Plugin/Hermes 全链路或当前候选远程部署。
  PostgreSQL 命令事实、Redis
  Presence、NATS、多节点对账和统一 OTel Operations 属于后续商用里程碑，必须在
  独立资源与迁移授权后接入；文件和 A2A 再接入同一可靠性底座。
- **ASSUMPTION-004**：用户确认计划不等于授权 push、deploy、生产数据库迁移、
  购买云资源或修改 `/Users/apple/.hermes/hermes-agent`；这些动作分别确认。
- **ASSUMPTION-005**：第一条企业数据闭环只选择低风险只读 Data Product、一张
  声明式 Card 和一个具有幂等/查询/补偿能力的 Action，不一次接入全部业务系统。

## 8. Related Specifications / Further Reading

- [产品边界与责任](../../hermes-agent-plugin/docs/01-product-boundary-and-responsibilities.md)
- [目标架构](../../hermes-agent-plugin/docs/02-target-architecture.md)
- [协议与可靠性](../../hermes-agent-plugin/docs/03-protocols-and-reliability.md)
- [Agent 集成与升级](../../hermes-agent-plugin/docs/04-agent-integration-and-upgrade.md)
- [安全与数据治理](../../hermes-agent-plugin/docs/05-security-and-data-governance.md)
- [部署与运维](../../hermes-agent-plugin/docs/06-deployment-observability-and-operations.md)
- [交付路线与验收](../../hermes-agent-plugin/docs/07-delivery-roadmap-and-acceptance.md)
- [企业协作扩展](../../hermes-agent-plugin/docs/08-enterprise-extension-and-agent-collaboration.md)
- [Local/Cloud 数据协议](../../hermes-agent-plugin/docs/11-local-and-cloud-data-protocols.md)
- [功能责任边界矩阵](../../hermes-agent-plugin/docs/12-feature-and-responsibility-boundary-matrix.md)
- [全链路逻辑](../../hermes-agent-plugin/docs/13-end-to-end-logical-flows.md)
- [软件架构与工程约束](../../hermes-agent-plugin/docs/14-software-architecture-and-engineering-constraints.md)
- [A2UI v0.9.1 当前规范](https://a2ui.org/specification/v0.9.1-a2ui/)
- [A2UI Renderer 路线](https://a2ui.org/roadmap/)
- [AG-UI 与 Generative UI 的关系](https://docs.ag-ui.com/concepts/generative-ui-specs)
- [MCP Apps 官方概览](https://modelcontextprotocol.io/extensions/apps/overview)
