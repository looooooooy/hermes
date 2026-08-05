# 13 全链路逻辑与详细时序

- 状态：实现级规范
- 基线版本：1.0
- 更新日期：2026-07-30

## 1. 全链路组件图

```mermaid
flowchart LR
    subgraph UX["Experience"]
      H5["H5 / PWA"]
      AND["Android Reference"]
    end

    subgraph CLOUD["Remote Server"]
      EDGE["WAF / ALB"]
      API["H5 API / Realtime"]
      CGW["Connector Gateway"]
      DEVICE["Identity / Tenant / Device"]
      CMD["Command Service"]
      PROJ["Projection Service"]
      COLLAB["Collaboration Gateway"]
      OUT["Outbox Worker"]
      NATS["NATS Core / JetStream"]
      PG[("PostgreSQL")]
      OBJ[("Object Storage")]
    end

    subgraph LOCAL["User Device"]
      CONN["Hermes Connector"]
      SQL[("SQLite")]
      KEY["OS Secure Store"]
      PLUGIN["Agent Plugin"]
      AGENT["Hermes Agent"]
      SESSION[("SessionDB")]
    end

    H5 --> EDGE
    AND --> EDGE
    EDGE --> API
    EDGE --> CGW
    API --> DEVICE
    API --> CMD
    API --> PROJ
    API --> COLLAB
    CMD --> PG
    COLLAB --> PG
    CMD --> OUT
    COLLAB --> OUT
    OUT --> NATS
    CGW <--> NATS
    PROJ --> PG
    PROJ --> OBJ
    CGW <-->|"WSS/TLS 443"| CONN
    CONN --> SQL
    CONN --> KEY
    CONN <-->|"UDS / Named Pipe"| PLUGIN
    PLUGIN <-->|"Stable Host SPI"| AGENT
    AGENT --> SESSION
```

## 2. 全链路责任分层

```mermaid
flowchart TB
    INTENT["User intent / operation"] --> UX["H5: collect and display"]
    UX --> AUTH["Remote: identity, tenant, policy, command fact"]
    AUTH --> DELIVERY["Connector: durable local delivery"]
    DELIVERY --> LOCALAUTH["Plugin: runtime, role, lease, pending verification"]
    LOCALAUTH --> EXEC["Agent: authoritative execution"]
    EXEC --> LOCALRESULT["Plugin: safe local result/event"]
    LOCALRESULT --> DURABLE["Connector: Outbox and cursor"]
    DURABLE --> CLOUDRESULT["Remote: command/projection/audit"]
    CLOUDRESULT --> UXRESULT["H5: factual status and content"]
```

任何上层“允许”都不替代下一层校验；任何下层执行结果都必须逐层传播后才能在 H5
显示为最终状态。

## 3. 本地组件安装全链路

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant I as Unified Installer
    participant E as Agent Extension Manager
    participant S as OS Service Manager
    participant C as Connector Slot
    participant P as Plugin Bundle
    participant A as Hermes Agent

    U->>I: Run signed installer
    I->>I: Verify release signature/digest/SBOM
    I->>E: Inspect Agent and Host API
    alt Agent absent
        E-->>I: Host unavailable
        I->>C: Install Connector and stage Plugin
        I->>S: Register/start service
        C-->>I: WAITING_FOR_AGENT
        I-->>U: Installed; waiting for Hermes Agent
    else Agent present and compatible
        E-->>I: Host API and safe activation window
        I->>C: Install Connector inactive slot
        I->>E: Install signed Plugin Bundle
        E->>P: Verify manifest/signature
        alt Agent busy and restart required
            E-->>I: PENDING_ACTIVATION
            I-->>U: Installed; activate after current task
        else can activate
            E->>P: Activate atomically
            E-->>I: Plugin active
            I->>S: Register/start Connector service
            C->>P: Local capability handshake
            P->>A: Host runtime descriptor
            A-->>P: Capabilities and generation
            P-->>C: Ready
            C-->>I: Local health passed
            I-->>U: Ready for Cloud pairing
        end
    end
```

### 安装失败逻辑

```mermaid
flowchart TD
    FAIL["Install step fails"] --> CLASS{"Failure class"}
    CLASS -->|Signature/digest| ABORT["Abort; never execute payload"]
    CLASS -->|Host incompatible| KEEP["Keep Agent running; Plugin disabled"]
    CLASS -->|Connector health| ROLLC["Rollback Connector slot"]
    CLASS -->|Plugin activation| ROLLP["Extension Manager rollback Plugin"]
    CLASS -->|Service registration| REPAIR["Leave staged artifacts; offer Repair"]
    ABORT --> REPORT["Display safe actionable reason"]
    KEEP --> REPORT
    ROLLC --> REPORT
    ROLLP --> REPORT
    REPAIR --> REPORT
```

## 4. Agent 与 Plugin 启动全链路

```mermaid
sequenceDiagram
    autonumber
    participant A as Hermes Agent
    participant E as Extension Manager
    participant P as Agent Plugin
    participant R as Endpoint Registry
    participant C as Connector

    A->>E: Discover extension bundles
    E->>E: Verify active Plugin signature
    E->>P: register(versioned context)
    P->>A: Check Host API
    alt incompatible
        P-->>E: DISABLED(reason)
        E-->>A: Continue Agent startup
        C->>R: Discover endpoints
        R-->>C: No compatible endpoint
        C-->>C: AGENT_UNAVAILABLE
    else compatible
        P->>A: Register lifecycle listener
        P->>R: Publish private Observer endpoint
        P->>R: Publish private Control endpoint
        P->>A: Resolve capability/runtime generation
        P-->>E: READY
        C->>R: Discover endpoints
        R-->>C: Candidate endpoints
        C->>P: gateway.runtime.describe
        P-->>C: Descriptor and capabilities
        C->>C: Persist agent_runtime
        C->>C: Start reconciliation
    end
```

## 5. Connector 进程启动全链路

```mermaid
flowchart TD
    START["OS starts Connector"] --> LOCK{"Acquire single-instance lock?"}
    LOCK -->|No| EXIT["Exit duplicate instance"]
    LOCK -->|Yes| CONFIG["Load managed policy/config"]
    CONFIG --> DB["Open/migrate/integrity-check SQLite"]
    DB --> DBOK{"SQLite safe?"}
    DBOK -->|No| DEG["DEGRADED: stop durable receive; diagnostics"]
    DBOK -->|Yes| KEY["Load Device Key metadata"]
    KEY --> PAR["Start Local discovery and Cloud connection in parallel"]
    PAR --> LOCAL["Local Gateway handshake"]
    PAR --> CLOUD["Cloud handshake if paired"]
    LOCAL --> JOIN{"Required sides ready?"}
    CLOUD --> JOIN
    JOIN -->|No| WAIT["WAITING/CONNECTING/DEGRADED"]
    JOIN -->|Yes| RECON["Reconcile runtime, commands, cursors"]
    RECON --> READY{"No blocking ambiguity?"}
    READY -->|Yes| RUN["READY: accept work"]
    READY -->|No| HOLD["DEGRADED/UNKNOWN visible"]
```

## 6. 设备配对与认证全链路

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant C as Connector
    participant K as OS Secure Store
    participant H as H5/PWA
    participant D as Device Service
    participant G as Connector Gateway

    U->>C: Start pairing
    C->>K: Get or create Device Key
    K-->>C: Key handle + public key
    C->>D: Create pairing session(public key, device metadata)
    D-->>C: One-time code, fingerprint, expiry
    C-->>U: Display QR/code/fingerprint
    U->>H: Confirm while authenticated
    H->>D: Bind tenant/user/device/agent scope
    D->>C: Random challenge
    C->>K: Sign challenge via key handle
    K-->>C: Signature
    C->>D: Submit signature
    D->>D: Verify and activate device
    D-->>C: Short-lived connection token
    C->>G: Open WSS with device-bound token
    G-->>C: Authenticated connection
```

失败边界：

- Code 过期：只重新开始 pairing；
- 指纹不一致：用户拒绝；
- Secure Store 不可用：不降级保存私钥；
- Device suspended/revoked：关闭 WSS，不自动重新配对；
- Tenant policy 拒绝：显示管理员可执行原因。

## 7. Connector Cloud 握手全链路

```mermaid
sequenceDiagram
    autonumber
    participant C as Connector
    participant G as Connector Gateway
    participant D as Device Service
    participant P as Presence/Connection Index

    C->>G: WSS/TLS + short-lived token
    G->>D: Validate device/token/revocation
    D-->>G: Tenant/realm/device context
    G->>C: Request/accept connector.hello
    C->>G: Versions, agent state, capabilities, cursors
    G->>G: Negotiate protocol/window/frame/heartbeat
    alt incompatible or blocked
        G-->>C: protocol/update/device error
        C->>C: Stop business traffic; keep repair path
    else accepted
        G->>P: Register live connection
        G-->>C: server.welcome + resume cursors
        C->>C: Start heartbeat/send/receive
        C->>C: Trigger Cloud reconciliation
    end
```

Tenant/Realm 由 Gateway 认证上下文注入，不能信任 Hello 中的同名客户端字段。

## 8. 会话观察全链路

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant H as H5
    participant API as Realtime API
    participant G as Connector Gateway
    participant C as Connector
    participant P as Agent Plugin
    participant A as Agent

    U->>H: Open session
    H->>API: Authorize session observation
    API->>G: session.observe.open
    G->>C: Durable/live observe command
    C->>C: Validate and persist required control state
    C->>P: session.observe.subscribe
    P->>A: Open authoritative observer
    A-->>P: Snapshot(sequence=104)
    P->>P: Sanitize projection
    P-->>C: Snapshot + runtime generation
    C->>G: session.snapshot
    G->>API: Authorized projection
    API-->>H: Render snapshot
    loop Runtime events
        A-->>P: Raw runtime event
        P->>P: Sanitize + sequence
        P-->>C: Local session.event
        C->>C: Durable/live classification
        C-->>G: Connector session.event
        G-->>API: Authorized realtime event
        API-->>H: Update UI
    end
```

### 观察缺口恢复

```mermaid
flowchart LR
    E104["Have sequence 104"] --> E106["Receive sequence 106"]
    E106 --> GAP["Detect gap 105"]
    GAP --> PAUSE["Pause incremental application"]
    PAUSE --> REQUEST["Request authoritative snapshot"]
    REQUEST --> SNAP["Snapshot at sequence 110"]
    SNAP --> REPLACE["Replace projection"]
    REPLACE --> RESUME["Resume from 111"]
```

H5、Remote、Connector 都不能根据缓存猜测缺失事件。

## 9. Prompt Submit 全链路

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant H as H5
    participant API as Command API
    participant DB as PostgreSQL
    participant O as Outbox/NATS
    participant G as Connector Gateway
    participant C as Connector
    participant L as SQLite
    participant P as Agent Plugin
    participant A as Agent

    U->>H: Submit prompt
    H->>API: command.prompt.submit + client IDs
    API->>API: Identity/Tenant/Session/Lease/Policy/TTL
    API->>DB: Transaction: Command CREATED + Outbox
    DB-->>API: command_id
    API-->>H: CREATED(command_id)
    O->>DB: Read unpublished Outbox
    O->>G: Publish durable command
    G->>C: Command envelope
    C->>C: Schema/integrity/expiry/capability
    C->>L: Insert Inbox(message_id,digest)
    L-->>C: Durable
    C-->>G: DELIVERED
    G-->>API: Update Command DELIVERED
    C->>P: prompt.submit with local lease/scope
    P->>P: Role/runtime/session/lease/idempotency
    P->>A: Stable owner action
    A-->>P: Accepted + server_turn_id
    P-->>C: accepted
    C->>L: Save result into Outbox
    C-->>G: EXECUTING/accepted
    G-->>API: Update Command
    A-->>P: Completion event/result
    P-->>C: Final safe result
    C->>L: Persist final Outbox
    C-->>G: SUCCEEDED/FAILED
    G-->>API: Final Command state
    API-->>H: Realtime status/result
    H-->>U: Display factual completion
```

关键事实：

- API 返回 CREATED 不代表送达；
- Connector ACK DELIVERED 不代表 Agent 受理；
- Agent accepted 不代表任务完成；
- 只有 Agent 最终结果传播到 Command Service 后显示 SUCCEEDED/FAILED。

## 10. Interrupt/Steer 全链路差异

```mermaid
flowchart TD
    ACTION["User action"] --> TYPE{"Type"}
    TYPE -->|Queue Prompt| QUEUE["May queue <= 5m; creates client_turn_id"]
    TYPE -->|Steer| STEER["Current execution only; no client_turn_id; no offline queue"]
    TYPE -->|Interrupt| STOP["Current execution only; TTL 10s; no offline queue"]
    TYPE -->|Redirect| REDIR["Reserved; Target v1 unavailable"]
    QUEUE --> COMMON["Remote authorization + Connector Inbox + Plugin lease"]
    STEER --> COMMON
    STOP --> COMMON
    REDIR --> DENY["method_not_allowed"]
```

## 11. Approval/Clarify 全链路

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent
    participant P as Plugin
    participant C as Connector
    participant R as Remote Projection/Command
    participant H as H5

    A->>P: Pending request enqueued
    P->>P: Select oldest + redact + revision++
    P->>C: session.control.state
    C->>R: Durable pending metadata
    R->>H: Show server-provided choices
    H->>R: Response(request_id, revision, client_request_id)
    R->>R: User/Tenant/TTL/command fact
    R->>C: approval/clarify command
    C->>C: Inbox persist
    C->>P: Local mutation
    P->>P: Lease + exact request + revision + choice
    alt valid
        P->>A: Resolve exact pending entry
        A-->>P: Accepted
        P->>P: revision++
        P-->>C: Accepted + next pending snapshot
    else stale/invalid
        P-->>C: 4208/4211/4213
    end
    C->>R: Final command status
    R->>H: Replace pending state
```

## 12. 网络中断与命令恢复

```mermaid
stateDiagram-v2
    [*] --> CREATED
    CREATED --> QUEUED
    QUEUED --> DISPATCHED
    DISPATCHED --> DELIVERED: Connector Inbox durable
    DELIVERED --> EXECUTING: Agent accepted
    EXECUTING --> SUCCEEDED
    EXECUTING --> FAILED
    DISPATCHED --> QUEUED: Gateway lost before Connector ACK
    DELIVERED --> DELIVERED: Redelivery returns saved state
    EXECUTING --> UNKNOWN: Connection lost after side-effect boundary
    UNKNOWN --> SUCCEEDED: Reconciliation proves success
    UNKNOWN --> FAILED: Reconciliation proves failure
```

### 恢复决策

```mermaid
flowchart TD
    LOST["Connection lost"] --> FACT["Read Server Command Fact + SQLite Inbox"]
    FACT --> S1{"State before DELIVERED?"}
    S1 -->|Yes and TTL valid| RESEND["Redeliver same message/idempotency"]
    S1 -->|Expired| EXPIRE["Mark EXPIRED"]
    S1 -->|No| S2{"Connector has terminal result?"}
    S2 -->|Yes| REPLAY["Replay saved result"]
    S2 -->|No| S3{"Agent may have crossed side-effect boundary?"}
    S3 -->|No| SAFE["Resume controlled delivery"]
    S3 -->|Yes| QUERY["Query Agent action/turn"]
    QUERY --> KNOWN{"Authoritative result known?"}
    KNOWN -->|Yes| CLOSE["Close SUCCEEDED/FAILED"]
    KNOWN -->|No| UNKNOWN["Keep UNKNOWN; human decision"]
```

## 13. Agent 重启/更新全链路

```mermaid
sequenceDiagram
    autonumber
    participant S as Remote Server
    participant C as Connector
    participant P1 as Plugin Runtime N
    participant A1 as Agent N
    participant A2 as Agent N+1
    participant P2 as Plugin Runtime N+1

    A1->>P1: runtime.draining
    P1-->>C: agent.state=draining
    C->>C: Stop new Control; flush state
    C-->>S: connector online / agent draining
    A1->>P1: runtime.stopped
    P1->>P1: Close endpoints; invalidate lease/subscriptions
    P1-->>C: Local disconnect
    C-->>S: agent unavailable
    A2->>P2: Load compatible Plugin
    P2->>P2: Publish new endpoints/generation
    C->>P2: Discover and describe
    P2-->>C: New generation/capabilities
    C->>C: Invalidate old local bindings
    C->>P2: Snapshot + command status
    C->>S: Reconcile commands/cursors
    S-->>C: Server facts/resume
    C-->>S: agent ready + reconciliation report
```

旧 generation 的 Lease、Pending、Subscription 和实时事件全部失效。

## 14. Connector 更新全链路

```mermaid
sequenceDiagram
    autonumber
    participant S as Server Update Policy
    participant U as Connector Updater
    participant H as Agent Extension Manager
    participant C1 as Current Connector
    participant C2 as New Connector

    S->>U: Signed release manifest
    U->>U: Verify and download inactive slots
    U->>H: Stage compatible Plugin
    H-->>U: Activated/deferred/rejected
    alt Plugin ready or unchanged
        U->>C1: Drain
        C1->>C1: Flush Outbox and close WSS
        U->>C2: Start with compatible SQLite
        C2->>H: Local health
        C2->>S: Cloud health
        alt health pass
            U->>U: Commit new active slot
        else health fail
            U->>C2: Stop
            H->>H: Rollback Plugin if needed
            U->>C1: Restore
        end
    else rejected
        U->>U: Keep current version; report incompatibility
    end
```

## 15. Remote Gateway 滚动发布

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant G1 as Gateway Old
    participant C as Connector
    participant G2 as Gateway New
    participant N as NATS / Connection Index

    O->>G1: Enter drain
    G1->>C: server.drain(reconnect_after)
    G1->>N: Stop accepting new connection ownership
    C->>C: Jittered reconnect
    C->>G2: WSS hello + resume cursor
    G2->>N: Register new connection
    G2-->>C: welcome/resume
    C->>G2: Replay unacked Outbox by same message IDs
    G2->>N: Deduplicate and route
    O->>G1: Terminate after drain deadline
```

Gateway 无最终业务状态，因此发布不会要求 Connector 清空本地队列。

## 16. Device 吊销全链路

```mermaid
sequenceDiagram
    autonumber
    participant U as User/Admin
    participant H as H5
    participant D as Device Service
    participant G as Connector Gateway
    participant C as Connector
    participant K as OS Secure Store

    U->>H: Revoke device
    H->>D: Authorized revoke request
    D->>D: Device state REVOKED + audit
    D->>G: Close active device sessions
    G-->>C: device.revoked
    C->>C: Stop business traffic
    C->>C: Clear unexecuted sensitive messages
    C->>K: Delete/retire Device Key by policy
    C-->>D: Local revoke receipt if reachable
    D-->>H: Final device state
```

本地只删除 Key 而不通知 Server 不等于吊销；Server 仍必须将设备状态改为
`REVOKED`。

## 17. Cloud 投影删除全链路

```mermaid
flowchart TD
    USER["User/Tenant deletion request"] --> AUTH["Authorize and create deletion job"]
    AUTH --> PG["Delete/tombstone PostgreSQL projection"]
    PG --> OBJ["Delete object derivatives"]
    OBJ --> SEARCH["Remove search/vector index"]
    SEARCH --> CACHE["Invalidate Gateway/cache"]
    CACHE --> CONN["Notify Connector cache policy"]
    CONN --> LOCAL["Delete eligible local projection/outbox payload"]
    LOCAL --> REVIEW["Invalidate dependent Knowledge/Skill references"]
    REVIEW --> AUDIT["Record completion/failures without deleted content"]
```

Agent SessionDB 的删除由 Agent/用户本地事实流程单独控制，Cloud 删除不能隐式删除
本地权威会话。

## 18. Repair 全链路

```mermaid
flowchart TD
    START["User selects Repair"] --> READ["Read-only inspection"]
    READ --> AGENT["Agent/Host API/version"]
    READ --> PLUGIN["Plugin bundle/active slot/endpoints"]
    READ --> CONN["Service/version/config"]
    READ --> DB["SQLite integrity/queue"]
    READ --> CLOUD["Device/WSS/certificate/proxy"]
    AGENT --> PLAN["Build bounded repair plan"]
    PLUGIN --> PLAN
    CONN --> PLAN
    DB --> PLAN
    CLOUD --> PLAN
    PLAN --> SHOW["Show actions and data impact"]
    SHOW --> APPLY["Repair only owned artifacts"]
    APPLY --> VERIFY["Local handshake + Cloud handshake + reconciliation"]
    VERIFY --> RESULT["Ready or precise remaining blocker"]
```

Repair 禁止：

- 删除整个 `HERMES_HOME`；
- 清空 SessionDB；
- 重置所有用户设置；
- 清空 SQLite 后自动重发未知命令；
- 下载未签名制品。

## 19. 完整命令各组件内部逻辑

### 19.1 H5

```text
collect intent
-> show target Agent/Session
-> require explicit confirmation when needed
-> submit client_request_id/client_turn_id
-> render authoritative state timeline
-> never infer success from WebSocket send
```

### 19.2 Remote Server

```text
authenticate user
-> resolve Tenant/device/Agent/session
-> Policy/lease/risk/TTL
-> transaction Command + Outbox
-> publish durable delivery
-> consume ACK/result idempotently
-> expose state and audit
```

### 19.3 Connector

```text
validate envelope
-> check expiry/capability
-> SQLite Inbox
-> DELIVERED ACK
-> Local RPC
-> SQLite Outbox result
-> Cloud result
-> retain/query UNKNOWN
```

### 19.4 Agent Plugin

```text
validate Local RPC
-> immutable role/claims
-> method allowlist
-> generation/session
-> lease/pending revision
-> runtime idempotency
-> Host owner action
-> safe result/event
```

### 19.5 Hermes Agent

```text
authoritative action preconditions
-> accept/reject
-> execute
-> persist session/action fact
-> emit runtime events
-> expose final status
```

## 20. Future：Agent-to-Agent 协作全链路

```mermaid
sequenceDiagram
    autonumber
    participant A as Work Agent A
    participant CA as Connector A
    participant R as Collaboration Gateway
    participant DB as Message Store/Work Item
    participant CB as Connector B
    participant B as Work Agent B
    participant HB as Employee B

    A->>CA: collaboration.send(structured refs, delegation)
    CA->>R: WSS message + idempotency
    R->>R: Identity/Delegation/Policy/TTL/loop budget
    R->>DB: Persist message + Outbox
    R-->>CA: ACCEPTED_BY_GATEWAY
    alt B offline
        DB->>DB: QUEUED until Delivery Session
    else B online
        R->>CB: collaboration.available/message
        CB->>B: Local structured request
        B->>B: Re-authorize every resource reference
        alt human gate required
            B->>HB: Request confirmation
            HB-->>B: Confirm/modify/reject
        end
        B-->>CB: receipt/status/deliverable
        CB->>R: collaboration.receipt
        R->>DB: Update authoritative state
        R-->>CA: collaboration.status
    end
```

Connector 只传结构化消息和引用，不理解具体 Skill，不建立设备间 P2P。

## 21. 全链路状态展示

```mermaid
flowchart LR
    CREATED["CREATED<br/>Server transaction"] --> QUEUED["QUEUED<br/>Delivery layer"]
    QUEUED --> DISPATCHED["DISPATCHED<br/>Gateway"]
    DISPATCHED --> DELIVERED["DELIVERED<br/>Connector SQLite"]
    DELIVERED --> EXECUTING["EXECUTING<br/>Agent accepted"]
    EXECUTING --> SUCCEEDED["SUCCEEDED<br/>Agent final"]
    EXECUTING --> FAILED["FAILED<br/>Agent final"]
    EXECUTING --> UNKNOWN["UNKNOWN<br/>Needs reconciliation"]
```

UI 文案必须匹配事实：

| 状态 | 可以显示 | 不能显示 |
|---|---|---|
| CREATED | 已创建 | 已发送给 Agent |
| QUEUED | 等待投递 | 对方已收到 |
| DISPATCHED | 正在投递 | Connector 已保存 |
| DELIVERED | 已送达设备 | Agent 正在执行 |
| EXECUTING | Agent 已受理 | 已完成 |
| UNKNOWN | 结果待确认 | 执行失败/可以重试 |
| SUCCEEDED | 已完成 | — |
| FAILED | 执行失败 | 命令未发送 |

## 22. 全链路验收场景

必须以真实安装制品验证：

1. 空白机器一次安装；
2. Agent 缺失后再安装；
3. Agent 正忙时 Plugin 延迟激活；
4. 首次配对、拒绝、过期和吊销；
5. Observer 快照、实时流和 gap；
6. Prompt、Interrupt、Steer、Approval、Clarify；
7. Connector 在 Inbox 写前/写后崩溃；
8. Agent 在执行边界前/后重启；
9. Gateway drain 和重连；
10. NATS 重投和 PostgreSQL Outbox 补发；
11. Connector/Plugin 兼容升级与回滚；
12. SQLite 磁盘满/损坏；
13. Cloud 投影删除；
14. Repair 不破坏用户数据；
15. 重复投递无重复业务效果；
16. `UNKNOWN` 全程不自动重做。
