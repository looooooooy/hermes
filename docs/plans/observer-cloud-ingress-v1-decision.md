# Observer Cloud Ingress v1 契约决策单

状态：`IMPLEMENTED — root contract frozen, Cloud ORM ingress/subscription complete`

本决策单只审计真实 `Plugin → Connector → Connector Gateway → Cloud → Android/Web`
Observer 的 snapshot、replay、live event 上行链路。七项设计选择已由既有权威文档
消解，并已落地为根契约、Cloud ORM 投影与业务回执语义。

## 1. 审计结论

根 `contracts/message-types-v1.json` 已冻结 Observer 数据与控制面：

- Connector 上行：`session.snapshot`、`session.event`；
- Cloud 下行：`session.observe.open`、`session.observe.close`、`stream.ack`、`stream.nack`。

六类消息均具备根 payload schema、valid/invalid fixture、catalog effect、消费者
同步和契约测试。Cloud 在认证 identity binding 后执行严格解码，只在 ORM 事务
提交 projection、专用 Observer inbox、outbox 和 audit 后发送业务 `stream.ack`。
gap 或投影冲突发送 `stream.nack`；ACK/NACK 只要成功送达，Gateway 都推进全局
Connector transport cursor，避免同一坏帧永久阻塞连接，但 NACK 绝不推进单会话
projection event cursor。心跳中的 transport cursor 不允许代替该业务回执。

## 2. 当前实现边界

| 层 | 已实现能力 | 后续边界 |
|---|---|---|
| Plugin local observer | snapshot-first、严格 replay/live sequence、gap fail closed；事件类型与外部 realtime allowlist 已对齐 | 真实 Hermes Host SPI production caller 仍是独立阻断 |
| Connector local runtime | 已实现显式 `profile/session_key/runtime_generation` Observer UDS、snapshot-first、ready/response authority 复核、generation/sequence/gap 门禁、ORM durable outbox、固定 identity replay、ACK/NACK 消费 | Linux/Windows Observer transport 仍需各自平台适配器 |
| Root Connector contract | Envelope、握手、心跳、command/control 与六类 Observer 消息全部冻结 | 新语义只能通过根契约演进 |
| Connector Gateway | 认证、identity binding、resume cursor、观察流严格解码、ORM commit 后 ACK、gap/conflict NACK | 持续保持 transport cursor 与业务回执分离 |
| Cloud ORM projection | SQLAlchemy ORM snapshot/event journal、authority-bound inbox、outbox/audit 同事务、严格 cursor/generation 校验、Tenant KEK/随机 DEK 信封加密、SQLite v3 migration | PostgreSQL/NATS 只替换存储与分发适配器，不改业务语义 |
| Cloud realtime | 生产 composition 已注入 ORM projection repository、profile-bound bounded polling event source，以及带 lease/ref-count/retry 的 durable observe open/close 路由 | 可在不改事实源的前提下增加 commit 后唤醒 |

## 3. 已冻结的最小根契约

根 catalog 已新增两个 `connector_to_cloud`、`frozen` 类型：

| message type | effect |
|---|---|
| `session.snapshot` | `authoritative_projection_replace` |
| `session.event` | `authoritative_projection_append` |

### 3.1 `session.snapshot` 最小语义

Payload 应复用 `cloud-realtime-v1.json` 已冻结的 observer snapshot 结构，并只增加
Connector ingress 必需的来源字段：

- `profile`；
- `runtime_generation`；
- `session_key`；
- `runtime_session_id`；
- `running`、`status`；
- `event_sequence`、`snapshot_event_sequence`；
- `messages`、`inflight`、`replay_events`。

本地 `subscription_id` 是单一 UDS transport 的临时句柄，禁止上传 Cloud。
Tenant、Device、Workspace、Agent、User 或 ACL 不得由 payload 自报；Cloud 必须从
已认证 Connector identity 和权威 Device lifecycle 推导。

语义门禁：

- `snapshot_event_sequence <= event_sequence`；
- replay 必须从 snapshot cursor 连续到 head；
- replay 的 session 必须与 snapshot 一致，并继承 snapshot 的 runtime generation；
- 同一 snapshot identity 加相同 digest 为幂等；相同 identity 加不同 digest 冲突；
- ORM 事务提交且 ACK 成功送达前不得推进成功帧的 Connector transport cursor；
- 超过 256 KiB 的 snapshot 必须 fail closed，不允许静默截断。

### 3.2 `session.event` 最小语义

Payload 应复用外部 realtime `session_event` 的关闭事件目录与 payload schema，并增加
`profile` 和 `runtime_generation`：

- `type`；
- `session_id`（值等于 `runtime_session_id`）；
- `session_key`；
- `profile`；
- `runtime_generation`；
- 可选 `event_sequence_start`；
- `event_sequence`；
- `payload`。

语义门禁：

- 事件类型和 payload 必须通过根 allowlist，禁止透传任意本地事件；
- 首个 live range 必须从已提交 head 的下一位开始；
- stale exact duplicate 不产生第二次业务效果；
- gap 不推进 projection event cursor；NACK 成功送达后推进 transport cursor，Connector
  必须以新的 transport sequence 重新取得并发送权威 snapshot；
- 只有根契约声明 mergeable 的类型可以携带 sequence range；
- 持久化成功后才能向外部 realtime 发布。

### 3.3 Capability 与 cursor

建议继续使用已冻结的 `session.observe` capability，不新增同义 capability。Envelope
`sequence` 仍是 Connector 方向的全局 durable transport cursor；payload
`event_sequence` 是单一 runtime session 的 observer cursor，两者不得混用。

Transport resume 必须使用 Cloud 唯一持久 authority：只有 tenant/device、Connector
instance、runtime generation、上一连接和双向 cursor 全部精确匹配时才返回
`resumed`。同一 instance 和 generation 仅发生连接或 cursor 偏差时，返回
`reset_required` 及 Cloud 已提交的 authoritative pair，Connector 在同一 transport
epoch 内从该 pair 重放 journal 中原 pending/sent/settled attempt identity。若
ACK/NACK 已到 Connector、但 Cloud transport
cursor commit 失败，本地已结算 frame 也必须在 reset 恢复窗口内重放。重放范围从
Cloud authoritative Connector cursor 开始，以重连前本地 durable checkpoint 为排他
上界；Observer 与 command/control 共用的全局 sequence 必须跨 lane 连续合并，缺口或
重复占位一律 fail closed。正常 send、精确 resume 和常规 pending 查询不扫描已结算
记录。Cloud `fresh`、本地 `fresh_epoch_required`、instance/runtime generation 替换
或本地 authority 丢失才允许旋转新 epoch，并从 `(0, 0)` 重新握手；
`reset_required` 不得旋转 epoch。新 epoch 不得继承旧 runtime 的传输尝试，
但未获 Cloud transport receipt 的 owner-control 终态业务结果必须以新帧重新
envelope，不得重复执行本地效果。

WebSocket send 成功不等于 cursor commit 成功。ACK 或 durable subscription intent
发生 post-send/pre-commit 故障时，重连允许传输级重复投递，但业务侧必须按 durable
identity 幂等。重复 active observe-open 不再次调用 Host subscribe/prepare，只以 active
subscription 的 cached authoritative snapshot 确保交付：pending 或 sent-unacked 记录
复用原 `message_id` 和 Connector sequence；只有记录缺失或已处于 terminal 状态时才
创建新 attempt。

## 4. 已批准并落地的决策包

以下选择均可由既有架构约束唯一推出。`仍需用户决定` 均为“无”；主 Agent 仍需完成
根契约的正式批准、fixture 和 consumer sync，但不需要把已约定的产品边界再次交给
用户选择。

### 4.1 Snapshot 存储形态

**已有证据**：

- `01-product-boundary-and-responsibilities.md` 明确 Hermes Agent 是 Session 正文、
  执行和工具结果的权威，Cloud 只保存近期读取投影；
- `05-security-and-data-governance.md` 明确 Cloud Projection 不是 SessionDB 备份；
- `architecture.md` 禁止客户端或适配器推导不同类型的 Session/sequence ID；
- 当前 local snapshot 没有 normalized message ORM 所要求的 message UUID、message
  sequence 和 created_at。

**推荐冻结值**：Cloud 使用 SQLAlchemy ORM 保存一个经过根 schema 验证的当前
Observer Snapshot，以及按 runtime cursor 排序的 Observer Event Journal。Snapshot
中的 messages/inflight 保持读取投影结构，不伪造缺失的 message identity/time，也不
写成 Agent SessionDB 的权威副本。REST transcript 和 realtime adapter 从该读取投影
生成外部表示。未来只有 Host contract 提供稳定 message identity 后，才可增量生成
normalized message projection。

**仍需用户决定**：无。选择 normalized message 写入会违反“不得推导 ID”和
“Cloud 不是 SessionDB 权威”两项既有约束。

### 4.2 Runtime string 到 Cloud UUID 的映射

**已有证据**：`11-local-and-cloud-data-protocols.md` 已冻结 `session_key` 为跨重启稳定
的会话谱系根，`runtime_session_id` 和 `runtime_generation` 均不跨重启稳定；
`architecture.md` 明确这些 ID 互不等价。Cloud 当前 projection 主键则是 UUID。

**推荐冻结值**：

- Cloud 为 `(tenant_id, agent_id, profile, session_key)` 生成并保持一个稳定内部
  `session_id` UUID；
- 另存 runtime binding：`(cloud_session_id, runtime_generation,
  runtime_session_id)`；
- `device_id` 是认证 ingress provenance，不是 durable session identity；重新配对或
  Connector 更换不能创建第二个会话谱系；
- Tenant、Agent、Workspace 和 Device 全部从认证 Device lifecycle 推导，禁止 wire
  payload 自报；
- 禁止把任意 runtime string 强转、哈希冒充或命名 UUID 化为 Cloud session UUID。

**仍需用户决定**：无。稳定 lineage 与易失 runtime identity 的区分已经冻结。

### 4.3 状态词汇

**已有证据**：`cloud-realtime-v1.json` 已冻结 `running/working/streaming` 为 running
statuses，并要求 `running` 当且仅当 status 属于该集合。Cloud normalized projection
使用另一套 catalog lifecycle 词汇，现有 Observer contract 没有权威 terminal
session lifecycle event。

**推荐冻结值**：Observer Snapshot/Event 原样保存并输出经过验证的 `status +
running` 对。只为列表 catalog 派生非终态：`running=true → active`，
`running=false → waiting`。首次 snapshot 前可以是 `created`；不得从 observer delta
推导 `completed/failed/cancelled`。终态只能由后续冻结的权威 session lifecycle
message 设置。

**仍需用户决定**：无。任何更丰富映射都会猜测当前 contract 没有表达的终态。

### 4.4 Snapshot replace、压缩与 gap 恢复

**已有证据**：Host SPI patch map、Plugin sequence guard 和外部 realtime contract
共同要求 snapshot/replay 从 snapshot cursor 连续到 head，gap 必须获取新 snapshot；
`03-protocols-and-reliability.md` 允许高频 delta 合并或丢弃并回退 snapshot；Cloud
Projection 明确高频 delta 合并后保存。

**推荐冻结值**：

- 一个有效 snapshot 在单一 ORM 事务内替换同 durable identity 的当前 read model，
  并原子写入 snapshot 携带的 replay range；
- 事务提交后，`<= snapshot_event_sequence` 的 live delta journal 可压缩，当前 snapshot
  成为恢复基线；durable/audit 事实不借 Observer journal 保存，进入各自已定义通道；
- 同 identity/cursor/内容 digest 为幂等，不同 digest 为冲突；
- live gap、跨 generation 或非法 range 均拒绝且不推进 projection cursor；NACK
  成功送达后消费该 transport sequence，由新 snapshot 使用下一 transport sequence 恢复；
- snapshot replacement 不删除或修改 Agent 的权威 SessionDB。

**仍需用户决定**：无。保留旧 live delta 作为第二份 Session 历史与已批准的
projection/compaction 边界冲突。

### 4.5 正文与 retention

**已有证据**：`05-security-and-data-governance.md` 允许业务正文按保留策略进入 Cloud
读取投影，明确默认近期保留、基线 30 天、Tenant 可关闭/缩短/按套餐延长、Tenant
DEK 信封加密、用户可导出/清除；同一文档和 Host SPI patch map 默认禁止 token、
完整 approval、tool args、完整 tool output 和未脱敏错误。

**推荐冻结值**：Cloud 可以持久化 Host/Plugin 已裁剪并通过根 event payload schema
的 user/assistant message text 与 bounded display-safe tool delta，用于多端近期读取；
默认 `retention_until = source_time + 30 days`，Tenant policy 可以关闭、缩短或授权
延长。来源时间超过 5 分钟未来偏差时拒绝；允许偏差内按当前可信时间封顶。新事件只
能延长、不能反向缩短活跃会话保留期。每个字段使用随机 256-bit DEK 加密，DEK 由
Tenant 当前版本 KEK 包装，AAD 固定绑定 tenant/agent/profile/session/field/schema，
历史 KEK 仅用于解密轮换前数据。生产只能从权限受限 credential file 注入 keyring，
缺失即 fail closed。过期由 ORM 删除账本、metadata-only audit 和有界退避重试清理。
日志、Trace、指标、普通 audit/outbox payload 不保存正文；完整原始工具输出始终不进入
该投影。

**仍需用户决定**：无。30 天是已批准基线；Tenant 的具体覆盖值是运行策略，不阻断
v1 wire contract。

### 4.6 Profile 与 Session identity

**已有证据**：Core Host SPI、Plugin Controller 和 command binding 均使用
`profile + durable_session_key + runtime_generation`；external observe subscribe 已
允许可选 `profile`；Cloud control ticket 强制 `session_key + profile`。仅使用
`session_key` 会丢失既有 Host authority scope。

**推荐冻结值**：Connector ingress 的 snapshot/event 都必须携带 `profile`，Cloud
durable identity 使用 `(tenant_id, agent_id, profile, session_key)`。外部 observer
请求可继续兼容省略 profile，但只有 session_key 在调用者可见范围内唯一时才能解析；
歧义时 fail closed，不选择“第一个”匹配项。Control 继续强制 profile。

**仍需用户决定**：无。是否全局唯一未被冻结，因此最小权限实现必须保留 profile。

### 4.7 ORM Outbox 与 realtime event source

**已有证据**：推进计划 REQ-006 要求 Transactional Outbox、Consumer Inbox、至少
一次投递和业务效果幂等；`AtomicWriteCoordinator` 已定义 business fact、outbox、
audit 在同一事务提交；`ProjectionEventSourcePort` 已是 realtime 的正式读取端口；
当前单节点阶段明确使用 SQLite ORM，NATS 属于后续扩展。

**推荐冻结值**：

- Gateway ingress 在同一 SQLAlchemy ORM 事务内提交 projection fact、去重 inbox、
  无正文 outbox notification 和必要的无正文 audit；成功帧提交后发送 ACK，拒绝帧发送
  NACK，只有对应回执成功送达后才推进 Connector transport cursor；
- P0 SQLite 的 `ProjectionEventSourcePort` 使用 ORM Event Journal bounded polling，
  可加 commit 后进程内 wake-up 降低延迟，但 wake-up 不是事实源；
- reconnect/catch-up 始终从 ORM cursor 读取；进程重启不会丢事件；
- 后续 PostgreSQL/NATS 由 outbox publisher 替换唤醒/分发适配器，不改变 ingress
  contract、ORM 事实或 realtime cursor 语义；
- outbox/audit 只携带 tenant/workspace/session/cursor/digest 等元数据，不复制正文。

**仍需用户决定**：无。纯内存 publish 会违反既有 Outbox 和服务器权威约束；当前
SQLite 阶段也没有理由提前引入 NATS。

## 5. 落地顺序

1. 根 catalog、payload schema、错误语义、valid/invalid/N-1 fixture 与同步门禁；
2. Cloud codec 对两个新 payload 的严格解码；
3. Cloud ingress application port 与认证 Device → workspace/agent resolution；
4. SQLAlchemy ORM model/migration/repository；SQLite v3 原子加密 v2 plaintext，并
   新建 Observer inbox、删除账本与 subscription authority 表；
5. Connector Gateway 在同一 cursor 临界区内执行“验证 → ORM commit → cursor advance”；
6. ORM-backed `ProjectionEventSourcePort` 与 snapshot query；
7. Connector Observer outbound lane（已完成）；
8. Plugin → Connector → Cloud → Android/Web 的 snapshot/replay/live/gap/reconnect E2E。

所有业务数据库读取和写入必须使用 SQLAlchemy ORM。直接 SQL、`text()` 和
`exec_driver_sql()` 不得进入 Repository/Application；仅集中式 SQLite 连接策略中的
固定 PRAGMA 例外保持不变。

## 6. 当前验收结论

- 根契约、fixture、catalog、consumer copies 已同步；
- Cloud Gateway 已实现“验证 → ORM 事务提交 → 业务 ACK → transport cursor 推进”；
- gap/conflict 会回 NACK 并保持 projection cursor；NACK 送达后推进 transport cursor，
  gap 明确要求以新 transport sequence 重取 snapshot；
- SQLite v3 migration 保持 v2 schema/checksum 不变，原子加密既有正文并创建 Observer
  authority/subscription/retention 表；SQLite migration runner 与 Business/Connector
  production composition 使用同一严格 keyring 路径，keyring 缺失、权限错误或内容错误均
  在服务启动/迁移前 fail closed；业务数据库读写全部通过 SQLAlchemy ORM；
- Business API 生产 composition 已使用 ORM projection repository 与 event source；
- Observer subscribe 省略 `profile` 时由 ORM 按 tenant/agent/session_key 解析唯一
  authority；0 个返回 not found，多个明确判定歧义，显式 profile 保持精确匹配；
- 每个 Connector 默认最多 32 个不同 active target；历史 target reopen 与首次创建使用
  同一事务容量门禁和数据库行锁，共享 active ref 不重复占容量，最后关闭释放容量；
- Connector macOS production composition 已消费 `session.observe.open/close` 作为唯一
  target authority，并以 SQLAlchemy ORM `observer_outbox` 保存 payload digest、固定
  message ID/Connector sequence 和完整 envelope；只有精确 `stream.ack` 才结算，
  `stream.nack` 保留拒绝事实并按合同重快照或停流；
- Cloud transport authority 在发送 welcome 和注册业务 router 前原子取得 ownership；
  fresh insert/update 和 resume replacement 均使用完整 CAS，旧连接的 commit/disconnect
  不得覆盖新 owner。断连 authority 清理采用有界重试，永久失败记录脱敏计数和待协调
  状态，取消信号直接传播；
- post-send/pre-commit 故障 E2E 已验证 ACK 与 durable observe-open 两条链路：同一
  instance/runtime 从 Cloud authoritative pair reset/replay，Observer inbox、projection
  和 Host prepare 均无重复业务效果，复用的 snapshot outbox identity 最终由 ACK 结算；
- Connector 已通过真实 WebSocket-over-UDS snapshot-first 集成测试及 100 次订阅生命周期；
- 本轮只有本地代码、部署制品与自动化测试更新；未部署、未重启远程服务、未执行远程
  migration，也未修改 Hermes 用户配置。Android/Web 的最终闭环不在本决策单的实施范围。
