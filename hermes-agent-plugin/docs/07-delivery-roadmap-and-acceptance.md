# 07 交付路线、差距与验收

- 状态：执行基线
- 基线版本：1.0
- 更新日期：2026-08-02

## 1. 当前事实

`hermes-agent-plugin/` 当前是 Hermes Agent Plugin Foundation，不是完整商用
Connector。唯一导入包为 `hermes_agent_plugin`；旧导入包不再进入源码和发行物。
独立的 `hermes-connector/` 已具备本地 Supervisor、SQLite durable storage、
Connector WSS durable command lane、非持久 owner-control lane、设备身份和正式
pairing/proof/token 实现。Pairing 只证明 Connector 设备凭证已绑定到授权的
`workspace_id + agent_id`，不证明本机 Hermes runtime、Plugin 或 Local Gateway
已经接通。

`hermes-cloud/` 的当前稳定树已在本地完成 SQLite revision 11：会话投影使用
`tenant_id + agent_id + profile + session_key` 四元身份，WebSocket ticket 使用稳定
`session_id`，profile 隔离和 typed ORM migration 已闭环；PostgreSQL v10 非空源
在缺少外部身份对账时 fail closed。最终本地门禁为 Cloud `1452 passed`、根 Contract
`99 passed + 63 passed`、规范/质量 `PASS`。该候选尚未远程部署。

远端仍是旧 `20260731T103631Z` SQLite test-server release，并包含显式 seed
`android-agent`、`android-bootstrap` 和 `Hermes Cloud test session`。2026-07-31
Android emulator 登录、会话列表、配对和 `Control unavailable` 证据是该旧测试服务的
时间快照，不是 Android Agent、真实 Hermes 会话或 revision 11 远程验收。生产
`src/` 不得为这些 seed 名称设置特判。

当前仍不能宣称真实 Agent 控制闭环完成：Hermes 0.19 的公开 Plugin Host 缺少
`gateway-extension/1` 和权威 Owner Action Port。Plugin 在真实 0.19
`PluginContext` 中明确 fail closed，避免把网络发送或消息入队显示为 Agent 已执行。
生产 entry point 只允许绑定当前运行 Hermes Agent 的 PluginManager；历史独立 runtime
已封禁且不再是公开 API，测试 harness 位于发行物之外的 `tests/`。

### 1.1 已实现

| 能力 | 代码位置 | 测试证据 |
|---|---|---|
| Python 插件 Entry Point | `src/hermes_agent_plugin/__init__.py`、`bootstrap/registration.py`、`adapters/host/extension.py` | `tests/contract/plugin/test_registration.py` |
| Observer 平台无关语义/API | `src/hermes_agent_plugin/adapters/local_protocol/observer_relay.py` | 后端注入、订阅、取消和只读错误测试 |
| Observer macOS 私有 UDS 注册/发现与中继 | `src/hermes_agent_plugin/adapters/platform/macos/observer_relay.py` | 私有权限、清理、非法路径、快照和断线测试 |
| Control 平台无关语义/API | `src/hermes_agent_plugin/adapters/local_protocol/control_relay.py` | claims 校验、后端注入、方法错误测试 |
| Control macOS 私有 UDS 与 claims 绑定 | `src/hermes_agent_plugin/adapters/platform/macos/control_relay.py` | attach、路径、清理和跨进程中继测试 |
| Control 方法 fail closed | `src/hermes_agent_plugin/adapters/local_protocol/control_v1.py`、`src/hermes_agent_plugin/adapters/local_protocol/control_relay.py` | 不可用方法与错误码测试 |
| 单控制者租约 | `src/hermes_agent_plugin/domain/control_lease.py` | 竞争、续租、释放、过期、重连测试 |
| 进程内幂等账本 | `src/hermes_agent_plugin/application/control_commands.py` | 重复、冲突、并发、TTL/LRU 测试 |
| Frozen Control v1 对齐 | 根 `contracts/sources/mobile-control-v1.json` 为权威；`src/hermes_agent_plugin/contracts/generated/mobile-control-v1.json` 为正式生成副本 | 根 Contract 与 Plugin 兼容测试 |
| Connector WSS 会话 | `../hermes-connector/src/hermes_connector/application/cloud_wss_client.py`、`../hermes-connector/src/hermes_connector/adapters/cloud/` | hello/welcome、独立 heartbeat、resume/reconcile、并发 outbound sequence、真实 WSS/TLS 测试 |
| Connector SQLite durable state | `../hermes-connector/src/hermes_connector/adapters/persistence/sqlite/` | ORM repository、migration v2、Inbox/Outbox/cursor/restart 测试 |
| Connector durable command lane | `../hermes-connector/src/hermes_connector/application/command_lane.py` | `command.deliver`、显式 ACK、重复/断线/重启和副作用边界测试 |
| Connector owner-control lane | `../hermes-connector/src/hermes_connector/application/owner_control_lane.py`、`../hermes-connector/src/hermes_connector/adapters/platform/macos/plugin_control_relay.py` | transport scope、FIFO、bounded queue、断线清理、effect-unknown 和真实 UDS 测试 |
| Cloud SQLite ORM 单节点闭环 | `../hermes-cloud/src/hermes_cloud/platform/sqlalchemy/`、`../hermes-cloud/src/hermes_cloud/platform/sqlite/` | migration、ORM repository、command router、显式 seed、Business/Connector Gateway 与部署测试 |
| Cloud owner-control bridge | `../hermes-cloud/src/hermes_cloud/modules/control/` | UDS 私有边界、bounded transport/request、capability fail-closed 和端到端测试 |
| Android Cloud consumer 与会话 UI | `../hermes-android/app/`、`../hermes-android/core/protocol/` | JVM/模拟器覆盖 Realtime、流式 Transcript、Todo/Subagent、Approval/Clarify、工具结果和 Transcript minimap；2026-07-31 公网 emulator smoke 仅为旧 test-server seed 快照 |

### 1.2 未实现

| 目标能力 | 当前差距 | 首发等级 |
|---|---|---|
| Stable Host SPI | Hermes 0.19 无 Extension SPI；Plugin 已移除私有 Host import 并在真实 Host 预检中 fail closed；仍需 Hermes Core 实现 `gateway-extension/1` | P0 |
| Agent Extension Manager | 无签名 Bundle 安装、激活和回滚接口 | P0 |
| 跨平台 Local Gateway | 仅 macOS UDS 已验证；Linux/Windows adapter 当前 fail closed，Windows Named Pipe 尚未实现 | P0 |
| 独立 Connector daemon 发布 | Supervisor/service runner 已有；系统服务注册、签名安装和跨平台发布未闭环 | P0 |
| 统一安装和 Repair | 无一个安装器部署 Plugin + Connector 的闭环 | P0 |
| Device Identity/Pairing | macOS Device Key、pairing offer/proof/token、吊销与 Cloud 授权绑定已实现；系统服务安装、跨平台安全存储和完整产品化轮换仍待完成。Pairing 不代表 Agent runtime ready | P0 |
| Server signal wire 完整性 | command/control dispatch 已实现；`drain/revoked/update_required` 与正式设备生命周期仍未闭环 | P0 |
| Agent update reconciliation | 无完整 drain/generation/command 对账 | P0 |
| Remote Gateway/Command Store 商用扩展 | 当前 SQLite ORM revision 11 单节点实现仅本地完成；远端仍为旧 test-server release。PostgreSQL/NATS/Redis 多节点事实存储、路由和故障门禁仍待后续阶段 | Later / Commercial |
| Android 真实 Agent Control | Android→旧 test-server 的认证/seed 会话路径曾验证；真实 Observer/Control 仍依赖 Host SPI，当前不得宣称已完成 | P0 |
| 只读会话 catalog/history | 允许作为独立协议规划；标准 API Server GET 不能复用 Observer/Control envelope，HTTP adapter 只能在 Plugin，Connector 不得直连 | P1 / Separate capability |
| H5/PWA | 尚未创建；复用 Android 当前使用的 Cloud 公共边界 | Later / Commercial |
| 签名更新和回滚 | 无 | P0 |
| Windows Named Pipe | 无 | P1，Windows 首发则提升 P0 |
| 企业 Collaboration | 仅有规划，无协议制品 | Future |
| Bootstrap Manifest | 仅有规划，无协议制品 | Future |

### 1.3 API Server 只读边界

Hermes 0.19 标准 API Server 的 session/message GET 可以作为未来
catalog/history 同步的数据源，但只能形成独立能力，不能进入 Observer 或 Control
协议。当前响应缺少权威 `runtime_generation`、`runtime_session_id`、live
`running/status` 和 per-runtime `event_sequence`，因此不得补造这些字段，也不得用
message id、行数或结束时间推断。

若实现该能力，依赖方向固定为
`Connector → Local Gateway → Plugin → standard API Server`。HTTP endpoint、Bearer
key、响应 DTO 和字段裁剪只存在于 Plugin adapter；Connector 不直连 API Server，
不持有 key。同步记录必须标记 catalog provenance/cursor，只保存允许的历史字段，
丢弃 tool call 参数、工具输出、reasoning 和凭据。catalog 可用不改变
`AGENT_UNAVAILABLE` 的 Observer/Control 状态，不发布 `session.observe` 或
`session.control` capability，也不创建 lease。

当前 API Server key 不是 GET-only scope，因此该能力在独立 contract、固定 GET
allowlist、loopback endpoint 校验、响应上限、秘密处理和 TDD 门禁完成前保持
unavailable。允许独立规划不等于已实现或已获生产启用授权。

## 2. 目标代码边界

在当前目录继续实现时，建议结构：

```text
hermes-agent-plugin/
  docs/
  src/
    hermes_agent_plugin/         # 正式 Agent Plugin 分层实现

hermes-connector/                # 独立 Connector 兄弟项目

contracts/                       # 仓库根跨端唯一权威
  schemas/
  fixtures/
    collaboration/v1/
  tests/
    unit/
    contract/
    integration/
    compatibility/
    fault/
  packaging/
    macos/
    windows/
    linux/
```

如果独立发布、权限或团队所有权开始分离，应将 `hermes_connector` 拆为独立仓库。
Schema/Fixture 使用版本化制品共享，不靠复制粘贴。

## 3. Phase 0：冻结契约和 Host SPI

### 交付

- Local Gateway Protocol v1 JSON Schema；
- Connector Protocol v1 初版 Schema；
- Host SPI v1；
- Agent Extension Manager 管理契约；
- Plugin Bundle、Compatibility Manifest 和签名格式；
- capability 和 runtime descriptor；
- 当前 Mobile Control v1 的 available/reserved 对齐；
- 错误码、帧限制、日志字段和秘密分类；
- Python/Kotlin/TypeScript Golden Fixture。

### 实施

1. 先为 extension 安装和 Host 生命周期增加失败测试；
2. 由 Host 注入 dispatcher、observer、runtime 和 audit facade；
3. 保持 Plugin 无 `tui_gateway` 等私有 Host import；
4. 冻结 start/stop/disconnect/cancel 行为；
5. 加入运行时私有导入拦截与 Host 注入边界测试；
6. 建立 Host 管理的 Extension Store 和 install/activate/rollback/remove 接口；
7. 验证 Agent 更新不会删除或覆盖 Extension Store。

### 门禁

- Plugin 不导入 Agent 私有模块；
- 不兼容 Plugin 不阻止 Agent 启动；
- Schema 破坏性变更 CI 会失败；
- Android Fixture、Python 常量和文档一致；
- 所有敏感字段有 `persist/log/trace` 规则；
- Plugin 可以通过签名 Bundle 幂等安装、禁用和回滚；

## 4. Phase 1：独立 Connector 本地闭环

### 交付

- Connector Supervisor；
- Agent discovery 和 Local Gateway client；
- macOS/Linux UDS；
- SQLite Schema、migration、Inbox/Outbox/cursor；
- 状态机和本地诊断；
- 模拟 Cloud Gateway。
- 单一开发安装器：同时部署 Connector 与 Plugin Bundle；
- Repair/Uninstall 状态机。

### 垂直切片

1. 发现 Agent，读取 runtime/capability；
2. Observer 快照和事件写 Outbox；
3. 模拟命令先落 Inbox，再调用 Local Gateway；
4. 结果先落 Outbox，再 ACK；
5. Connector/Agent 任意崩溃后对账；
6. Agent 重启 generation 变化后旧控制失效。
7. 从空白环境一次安装后自动完成 Plugin 注册和本地握手；
8. Agent 缺失时进入 `WAITING_FOR_AGENT`，安装器仍成功结束。

### 门禁

- SQLite 写入前崩溃可安全重投；
- 写入后执行前崩溃不会重复执行；
- 执行边界后崩溃进入 `UNKNOWN`；
- Agent 不在线时 Connector 仍运行；
- Connector 不运行时 Agent 仍本地可用；
- 磁盘满/数据库损坏有安全降级。
- 用户无需手工安装 Python 包或修改 Agent venv；
- 重复运行安装器只检查/修复，不产生重复服务和 Plugin；
- 卸载程序不会删除 Agent 会话和配置。

## 5. Phase 2：设备与 Cloud WSS

当前已完成 Connector 侧 hello/welcome、heartbeat、帧限制、`max_in_flight`、
durable cursor、resume/reconciliation、重连、真实 WSS/TLS、`command.deliver`
和 `session.control` capability negotiation。Cloud/Connector 已完成 macOS Device
Key、正式 pairing/proof/token、吊销与精确 Agent/Device/Session ACL 的本地实现；
跨平台安全存储、系统服务安装、完整轮换以及 `drain/revoked/update_required` 仍待
完成。Cloud 中的 `agent_id` 是通用可选择身份和路由键；Agent A/B 切换只是选择
不同授权数据，不是切换代码。Pairing 只建立 `workspace_id + agent_id` 授权绑定，
Local Gateway/runtime readiness 仍须独立握手。File、A2A 和 Card 类型不得据此声明
业务已实现。

### 交付

- OS Device Key；
- 配对码、Challenge、短期令牌和吊销；
- Connector hello/welcome（Connector 侧已实现）；
- 心跳、帧限制、背压、重连和 resume cursor（Connector 侧已实现）；
- Gateway 模拟器和协议 Fuzz；
- Cloud/Local 状态组合。

### 门禁

- 私钥不进入文件、日志或诊断包；
- 吊销能关闭在线连接并阻止重连；
- 10,000 连接重连使用 jitter，不形成同步风暴；
- 未知字段兼容、未知类型拒绝；
- 超大帧、压缩炸弹和重放测试通过。

## 6. Phase 3：Remote Server 命令闭环

Android-first 所需的 Cloud 侧基础设施已经以 SQLite + SQLAlchemy ORM 在本地完成
revision 11、Command Store、Connector Gateway 和私有 owner-control bridge。
2026-07-31 Android 到公网旧 test-server 的认证、seed Session list/open 和 UI 状态
只保留为历史快照；当前候选未远程部署，也没有真实 Hermes Observer/Control。
下列 PostgreSQL、NATS 和 H5 条目仍是多节点商用目标。

### 交付

- Connector Gateway；
- Device/Tenant Service；
- PostgreSQL Command、Attempt、Outbox；
- NATS Core/JetStream；
- ACK/结果消费与对账；
- H5 最小设备中心和命令时间线。

### 门禁

- Command + Outbox 同事务；
- Gateway 无状态，可滚动替换；
- 已提交命令丢失为 0；
- 重复投递业务副作用为 0；
- UI 区分 CREATED/QUEUED/DISPATCHED/DELIVERED/EXECUTING/UNKNOWN/终态；
- Redis 不承担命令事实。

## 7. Phase 4：读取投影和完整控制

Android 已完成 Session Projection、Transcript、Observer/Control consumer、
Approval/Clarify 交互和 capability gate。公网证据来自旧 test-server seed，不是
真实 Hermes Observer。真实 prompt/interrupt/steer/approval/clarify 的权威效果仍被
Hermes 0.19 Host SPI 阻断；在 Core SPI 完成前不得把 Cloud projection、模拟器、seed
或 Fake Host 结果提升为真实 Agent Observer/Control 验收。

### 交付

- Session snapshot/delta/cursor；
- Projection Service；
- H5/PWA 会话观察；
- control lease UX；
- prompt/interrupt/steer/approval/clarify；
- 数据保留、导出和删除；
- Android 参考客户端协议回归。

### 门禁

- 序列缺口能自动快照恢复；
- Cloud 投影删除可验证；
- pending revision 冲突 fail closed；
- H5 不能调用 capability 未开放的方法；
- 敏感输入仍保持关闭，除非端到端密文门禁通过。

## 8. Phase 5：签名发布和商用运维

### 交付

- macOS/Windows/Linux 安装器；
- 一个用户入口同时管理 Connector 与 Plugin；
- 系统服务；
- 签名更新、双槽回滚；
- SBOM、制品摘要和发布清单；
- OpenTelemetry、Dashboard、告警、状态页；
- Runbook、备份、灾备和删除演练；
- 订阅、配额和诊断包。

### 门禁

- internal → 1% → 10% → 50% → 100% 灰度可执行；
- 更新失败自动回滚；
- 安装/更新不修改 Agent 用户数据；
- 新旧版本兼容矩阵通过；
- 值班人员可在不读取正文的情况下定位主要故障。
- Agent/Plugin/Connector 兼容组合可以原子升级和分别回滚；
- 企业静默安装、预注册和批量卸载经过验证。

## 9. Phase 6：企业扩展

在基础远程闭环稳定后增加：

- Agent Bootstrap Manifest；
- Work Event/Evidence 引用；
- Collaboration Request、Receipt 和 Work Item；
- Access Grant 和接收方重新鉴权；
- 企业 Knowledge/Skill 引用；
- Evolution Agent 候选数据闭环。

这些能力使用新的 capability 和消息类型，不把企业业务逻辑写入 Connector 核心。

## 10. 测试体系

### 10.1 单元测试

- canonical payload digest；
- TTL、LRU、lease、revision；
- 状态机合法转换；
- capability resolution；
- 配置优先级和 fail closed；
- 脱敏和日志字段。

### 10.2 契约测试

- JSON Schema 与 Golden Fixture；
- 未知字段和未知 capability；
- 错误码无冲突；
- Kotlin/Python/TypeScript 模型对齐；
- Local/Connector/Cloud API 版本兼容。

### 10.3 集成测试

- Plugin ↔ Agent Host；
- Connector ↔ Local Gateway；
- Connector ↔ Gateway；
- Command ↔ Outbox ↔ NATS ↔ Gateway；
- Projection ↔ H5；
- 配对、吊销、删除和更新。

### 10.4 故障注入

- Agent、Plugin、Connector、Gateway、Worker 随机终止；
- SQLite 写前/写后/执行前/执行后崩溃；
- PostgreSQL 提交和 Outbox 发布边界；
- NATS Leader/节点故障和重复投递；
- 网络丢包、延迟、分区和时钟漂移；
- 磁盘满、只读和数据库损坏；
- KMS/OS 安全存储不可用。

### 10.5 安全

- Tenant/Agent/Session 越权；
- Token/Message 重放和签名伪造；
- UDS/Named Pipe 跨用户访问；
- 日志、Trace、诊断和崩溃转储秘密扫描；
- 协议 Fuzz、洪泛、超大帧和压缩炸弹；
- 安装包/更新供应链。

### 10.6 容量与 Chaos

执行 10,000/20,000/100,000 连接、5,000 events/s、重连风暴、AZ 故障、RDS 切换和
JetStream quorum 测试，目标见部署文档。

## 11. Definition of Done

每个垂直切片必须：

1. 先有可观察的失败测试；
2. 实施最小行为；
3. 单元、契约和相关集成测试通过；
4. 协议和文档同步；
5. 日志完成秘密审查；
6. 指标、错误码和用户可执行修复齐全；
7. 支持降级、关闭和回滚；
8. 在实际安装制品而非仅源代码中验证；
9. 不破坏 Agent 本地使用；
10. 留下验收命令、结果和制品摘要。

## 12. 商用上线门禁

以下条件全部满足：

1. Agent/Plugin/Connector/Server 兼容矩阵通过；
2. 端到端、容量、Chaos 和升级回滚通过；
3. 跨 Tenant 越权为 0；
4. 已提交命令丢失为 0；
5. 重复投递导致重复业务效果为 0；
6. 无未关闭 P0/P1 安全问题；
7. 备份恢复、设备吊销和密钥轮换已演练；
8. 状态页、告警、值班和 Runbook 可用；
9. 投影导出、删除和账户关闭可完成；
10. 订阅/配额/账单异常流程经过验证；
11. 隐私、用户协议、备案和适用合规经过专业审查；
12. 公网 Dashboard 隧道从正式链路移除。

## 13. 文档维护

- 协议变更必须先更新 Schema/Fixture，再更新本文；
- 代码实现状态变化必须更新第 1 节差距表；
- 接受或推翻关键决策必须新增 ADR，不静默改写历史；
- 每个稳定 Release 更新兼容矩阵、能力清单和 Runbook；
- 父项目企业规划扩展不能反向破坏 Connector v1 核心边界。
