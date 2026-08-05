# Hermes 员工 Agent-to-Agent 协作网络落地设计

**版本：** v1.0  
**状态：** 实施设计草案（用于协议冻结、研发拆解与试点验收）  
**日期：** 2026-07-31  
**适用范围：** Hermes 企业工作台、Remote Server、Connector、Agent Local Gateway、员工 Hermes Work Agent  
**核心对象：** 员工 Agent ↔ 员工 Agent 的交流、任务协作、交付、人工门禁、证据与审计  

> 本文件以现有 Hermes 九份设计文档为基线，保留其术语、边界和事实归属。凡现有文档未冻结的实现细节，均明确标记为“本设计新增”或“待冻结”，不把推断伪装为既有结论。

## 版本记录

| 版本 | 日期 | 状态 | 主要变化 |

|---|---|---|---|

| v1.0 | 2026-07-31 | 实施设计草案 | 收敛内部员工 Agent A2A 的协议、服务、状态机、数据、可靠性、安全、试点和验收 |

## 目录

1. 执行摘要  
2. 设计依据与权威层级  
3. 目标、范围与非目标  
4. 关键架构决策  
5. 总体架构  
6. 身份、委托、Bootstrap 与自主等级  
7. Agent 目录、Capability Profile 与发现  
8. 四类交流对象与业务语义  
9. Hermes Collaboration Contract v1  
10. 消息类型与协议分层  
11. 端到端在线与离线链路  
12. 状态机与责任边界  
13. 路由、可靠性、幂等与恢复  
14. 循环、预算、跳数与并发控制  
15. Resource Reference、Access Grant 与权限重判  
16. Action、Approval 与外部承诺  
17. Evidence、Outcome、Trace 与 Audit  
18. 服务边界与首版部署单元  
19. PostgreSQL 数据模型、索引与约束  
20. NATS / JetStream 内部主题与流  
21. Connector 与 Agent Local Gateway 改造  
22. REST / WSS / Local RPC 接口  
23. 错误码与客户端可执行恢复  
24. Workbench 交互与移动端边界  
25. 安全威胁模型与控制  
26. 可观测性、SLO 与经营指标  
27. 四种部署模式与数据驻留  
28. 版本、兼容、发布与回滚  
29. 测试矩阵与上线门禁  
30. 分阶段实施计划（Gate 0–3）  
31. 首个试点：跨境订单非标交付可行性评估  
32. 已冻结、需新增冻结与待决事项  
附录 A–F：JSON、SQL、事件、错误码、验收样本、来源映射

# 1. 执行摘要

Hermes 的内部 A2A 第一优先级不是“让很多 Agent 互相聊天”，而是让每个员工的 Hermes Work Agent 在明确身份、员工委托、业务目的、权限、任务和责任边界下完成真实协作。

员工 Agent 之间**可以交流**，但交流被分成四个层级：

- `Conversation`：讨论、解释、澄清、草拟，不改变正式业务状态；
- `Collaboration Request`：向另一个员工或其 Agent 发起有目标、输入、输出、期限和验收标准的正式请求；
- `Work Item`：承载责任人、参与者、依赖、交付物、审批、异常、Evidence 和最终结果；
- `Action / Approval`：修改正式业务状态、对外发送或形成承诺，必须使用受控 Action Contract 和人工门禁。

正式员工 Agent-to-Agent 消息全部通过服务端 `Collaboration Gateway` 路由。设备上的 Agent 不直接 P2P，不互相开放端口。PostgreSQL 保存权威消息、请求、Work Item、Receipt 和审计状态；NATS Core / JetStream 只负责在线路由、交付、重放和消费者恢复；Connector 只认识 Hermes WSS 与本地协议，不直接连接 NATS、Redis、PostgreSQL 或对象存储。

本设计建议第一版采用“模块化单体 + 独立契约”的实现方式：服务职责必须分离，但无需立即拆成大量微服务。先完成只读协作和结构化交付，再开放可回滚的低风险 Action，最后才建设能力发现、Task Contract 模板和 Skill 复用。

## 1.1 一句话定义

> Hermes 员工 A2A 是一套“员工专属 Agent 的企业协作网络”：Agent 代表员工在 Purpose-bound Delegation 下交流、请求、交付和升级；业务效果落在 Work Item、Action、Evidence 与 System of Record，而不是落在聊天文本里。

## 1.2 第一版成功标准

| 维度 | 成功标准 |

|---|---|

| 正确性 | 发送方、接收方、代表员工、Purpose、权限、状态和交付物均可追溯 |

| 可靠性 | 接收方离线、Connector 重连或服务滚动发布时，已持久化请求不丢失、不重复产生业务效果 |

| 责任 | Agent 接受任务不等于员工形成承诺；外部承诺与高风险动作必须由有权人员确认 |

| 安全 | 接收方对每个 Resource Reference 使用自身身份重新鉴权，Personal Memory 不进入跨员工网络 |

| 经营价值 | 责任人匹配、交接时延、等待时间、返工或端到端周期获得可测改善 |

| 可维护性 | 新增员工 Agent 不增加点对点接口；通过稳定 Contract、Capability 和 Registry 扩展 |

# 2. 设计依据与权威层级

本文件不替代现有设计，而是在其上补齐员工 Agent A2A 的实施细节。发生冲突时采用以下权威顺序：

1. 已标记为“Approved / 已确认设计基线 / frozen contract”的文件；
2. `architecture.md` 中的 canonical architecture 与部署边界；
3. 企业工作模式、数据治理和投资 Gate 文档；
4. 本文件中标记为“本设计新增”的内容；
5. 本文件中的“待冻结”项不得直接进入生产。

## 2.1 继承的核心基线

| 编号 | 来源 | 本设计继承的关键边界 |

|---|---|---|

| B1 | Hermes Agent 原生企业工作模式设计 v2.4 | 员工 Work Agent、Bootstrap、Collaboration Request、Work Item、Action、Evidence、服务端路由与状态机 |

| B2 | Hermes Connector 商用首发架构设计 | Remote Server / WSS / Connector / Local Gateway 边界、至少一次投递、业务效果幂等、PostgreSQL 事实源、NATS 交付层 |

| B3 | Hermes Agent 原生企业数据治理与 AI 数据平台技术设计 | 引用优先、接收方重新鉴权、Access Grant、权限传播、Model Gateway、控制面与数据面 |

| B4 | Hermes 企业 AI 工作台扩展设计 | Work Event、企业空间、Knowledge / Skill / Work Item、权限感知与四种部署形态 |

| B5 | Hermes AI 原生企业工作台投资论证 | Gate 0–5、内部试点、Agent 网络增量价值和验收指标 |

| B6 | Hermes Mobile Control Contract v1 | Observer / Control 分离、控制租约、幂等、UNKNOWN 不自动重发、4200–4219 错误码已占用 |

| B7 | Hermes Agent Output Parity Contract v1 | 移动端是 Inspect + Operate Agent console，不是社交聊天 UI；稳定事件顺序与状态 |

| B8 | architecture.md | Remote Server 不是第二个 Agent；Connector 独立发布且不读 SessionDB / NATS / PG |

| B9 | 老板经营心智与组织进化理论 | 横向 Work Agent 协作、纵向 Evolution Agent 能力沉淀；不建设全知超级 Agent |

## 2.2 已发现的契约冲突点

现有 Connector 文档同时出现“根对象严格拒绝未知字段”和“同一 Major 未知字段必须忽略”的不同表述。为避免兼容性歧义，本设计提出以下冻结建议：

- 根信封字段采用严格 Schema；
- 扩展字段只能进入命名空间化 `extensions`；
- 同一 Major 新增可选字段时，接收方必须忽略自己不认识的 `extensions` 子项；
- 根级未知字段一律拒绝；
- 该规则必须写入 Connector Protocol 的正式兼容章节并由 Golden Fixture 固化。

这是“本设计新增的冲突收敛建议”，在协议冻结前不能当作现有实现事实。

# 3. 目标、范围与非目标

## 3.1 目标

1. 支持员工 A 的 Work Agent 与员工 B 的 Work Agent 讨论、请求、分工、查询、交付和升级；
2. 保留 Agent 身份与代表员工的双身份，不能把 Agent 行为伪装成员工本人行为；
3. 让请求在接收方离线、网络闪断、Connector 重连和 Agent 重启后可恢复；
4. 让每次正式协作具备 Purpose、TTL、预算、最大轮次、验收标准和人工接管；
5. 让数据交换采用 Resource Reference，接收方重新鉴权；
6. 让正式动作进入 Action / Approval，不从自然语言聊天直接产生业务效果；
7. 让结果形成 Evidence，并可被 Evolution Agent 在治理后提出 Skill / Template 候选；
8. 保持 Runtime、Connector、Remote Server、Capability 和 Experience 独立升级。

## 3.2 第一版范围

| 范围 | 包含 |

|---|---|

| 内部主体 | 同一 Tenant 内，经组织、Workspace、项目和 Policy 允许的员工 Work Agent |

| 消息类型 | Conversation、Task Request、Clarification、Status、Delivery、Access Request 引用、Approval Request 引用 |

| 执行 | 只读数据获取、生成结构化交付物、创建/更新 Hermes Work Item；Gate 2 后开放 R1/R2 Action |

| 终端 | H5/PWA、Android、Desktop 通过公开 Cloud API / realtime 契约观察和操作 |

| 部署 | Shared SaaS、Dedicated Data Plane、Connected Private、Offline Private 共用同一领域契约 |

## 3.3 非目标

- 不建设设备间 Agent P2P 网络；
- 不让员工 Agent 互相读取 Personal Memory；
- 不让 Agent 因对方 Agent 的权限而获得隐式权限；
- 不把 Conversation 中的自然语言“同意”当成业务批准；
- 不在第一阶段开放外部客户、供应商或跨厂商 Agent 联邦；
- 不复用 Mobile Control 的 owner-control lane 作为业务协作消息通道；
- 不让 Connector 理解具体 Skill、业务对象或审批逻辑；
- 不用 Agent 数、消息量、在线时长或提示词数量评价员工价值；
- 不把 NATS / Redis / Search / Vector 作为正式消息、权限或业务状态事实源。

# 4. 关键架构决策

| ID | 决策 | 状态 | 理由 |

|---|---|---|---|

| ADR-A2A-001 | 内部员工 Agent 使用 Hermes Collaboration Contract；外部跨组织/跨厂商后续经 A2A Federation Gateway | 继承基线 | 内部需要更强的组织、委托、Policy、Work Item、Evidence 语义 |

| ADR-A2A-002 | 正式 Agent-to-Agent 消息必须经服务端 Collaboration Gateway | 继承基线 | 统一身份、权限、离线、TTL、循环、撤回和审计 |

| ADR-A2A-003 | PostgreSQL 是协作事实源，JetStream 是交付层 | 继承基线 | 支持恢复、查询、幂等与审计 |

| ADR-A2A-004 | Connector 只认识版本化 Hermes WSS；不直连 NATS / PG / Redis | 继承基线 | 隔离基础设施变化和终端升级 |

| ADR-A2A-005 | Conversation 不改变业务状态；正式工作进入 Collaboration Request / Work Item / Action | 继承基线 | 避免自然语言歧义和责任失真 |

| ADR-A2A-006 | Resource Reference 优先，接收方用自身身份重新鉴权 | 继承基线 | 防止发送方权限传播 |

| ADR-A2A-007 | 当前 reserved `a2a.message` 不启用；新增 `collaboration.*` wire contract | 新增冻结建议 | 避免把保留无效果类型私自变成生产协议 |

| ADR-A2A-008 | 首版为模块化单体，不立即拆分微服务 | 新增实施建议 | 降低首期运维复杂度，同时保持契约和事实归属 |

| ADR-A2A-009 | Agent Capability Profile 为请求者视角的权限裁剪投影，不暴露真实全部权限 | 新增 | 支持发现，同时避免能力与权限侧信道 |

| ADR-A2A-010 | 协作错误码建议使用 4300–4349，发布前机械碰撞扫描 | 待冻结 | 4200–4219 已被 Mobile Control v1 占用 |

# 5. 总体架构

[[FIG:overall]]

架构由六层构成：

1. **体验层**：H5/PWA、Android、Desktop 展示协作线程、请求、Work Item、审批和 Agent 状态；
2. **公开服务边界**：Remote Server 终止 HTTPS/WSS，校验用户、Tenant、设备和会话；
3. **协作控制层**：Collaboration Gateway、Identity / Delegation / Policy、Work Item、Evidence、Audit；
4. **可靠交付层**：PostgreSQL + Transactional Outbox + NATS Core / JetStream；
5. **边缘连接层**：Connector 的 WSS、SQLite Inbox / Outbox、游标与 Local Gateway 发现；
6. **执行层**：员工 Hermes Work Agent 使用其 Bootstrap Manifest、Skill、Data Product 和 Action 完成任务。

未来外部 A2A 只接入 Collaboration Gateway 之前的 Federation Adapter，不绕过内部身份、Policy、Work Item、Evidence 和 Action 体系。

## 5.1 控制面与数据面

| 控制面（应该如何运行） | 数据面（本次协作正在发生什么） |

|---|---|

| Agent / Human / Device Identity | Conversation Message |

| Bootstrap Manifest 与激活状态 | Collaboration Request |

| Role Capability Package | Work Item / Delivery |

| Agent Delegation Policy | Receipt / Cursor / Delivery Attempt |

| Capability / Schema Registry | Resource Reference / Query Result |

| Policy / Classification / Purpose | Evidence / Outcome / Trace |

| 版本、签名、灰度、配额 | Action Command / Approval |

## 5.2 事实归属

| 对象 | 权威事实源 | 明确不作为事实源 |

|---|---|---|

| 员工、组织、岗位 | Identity & Org Service | Agent Prompt / Personal Memory |

| Agent 激活与 Manifest | Bootstrap Service | Connector 缓存 |

| 协作消息与请求状态 | PostgreSQL Collaboration Store | JetStream / Redis / H5 缓存 |

| Work Item 当前状态 | Collaboration Service | Conversation 文本 |

| 业务对象状态 | 对应 System of Record | Hermes 搜索与投影 |

| 权限政策 | Policy Registry；一次决策在 Decision Log | Agent 自行判断 |

| 正式交付与证据 | Object Store + Evidence Metadata | 模型隐藏推理 |

| Agent 执行过程 | Trace Store | 审计账本之外的普通日志 |

# 6. 身份、委托、Bootstrap 与自主等级

[[FIG:identity]]

## 6.1 双身份不可合并

每条 Agent 消息、每个 Action 和每条 Trace 至少保存：

```json
{
  "actor": {"type": "agent", "id": "agt_work_01"},
  "on_behalf_of": {"type": "human", "id": "usr_01"},
  "tenant_id": "ten_01",
  "workspace_id": "wsp_sales_cn",
  "purpose": "customer_delivery",
  "delegation_ref": "dlg_01",
  "bootstrap_manifest_id": "abm_01",
  "policy_context_version": "pcv_109"
}
```

界面必须明确标记“由某员工的 Hermes Agent 发起”；审计不能把 Agent 操作写成员工亲自操作，也不能只记录一个匿名系统账号。

## 6.2 Purpose-bound Delegation Token

Delegation Token 为短期、不可转授权的执行凭证，建议绑定：

- Tenant、Human、Agent、Device / Runtime Attestation；
- Workspace、Project、Purpose；
- 可访问的资源选择器；
- 允许的 Collaboration Authority；
- 允许的 Action 与风险上限；
- 最大跳数、最大轮次与预算引用；
- 是否要求逐次确认；
- `issued_at`、`expires_at`、`not_before`、`jti`；
- Policy Context Version 与签名 Key ID。

员工离职、项目退出、设备吊销、Manifest 撤销或委托撤销后必须立即失效。接收方 Agent 不继承发送方的 Token；它使用自己的身份和本次协作上下文重新申请权限决策。

## 6.3 Bootstrap Manifest 对 A2A 的要求

Manifest 必须新增或确认以下 capability：

```json
{
  "runtime": {
    "required_capabilities": [
      "purpose_token",
      "collaboration_v1",
      "collaboration_receipt",
      "work_item_resume"
    ]
  },
  "collaboration": {
    "policy_ref": "acp_sales_02",
    "maximum_hops": 2,
    "maximum_rounds": 4,
    "maximum_parallel_tasks": 3,
    "default_request_ttl_seconds": 86400,
    "budget_ref": "cbg_sales_standard"
  },
  "autonomy": {
    "default_level": "L1",
    "maximum_level": "L2",
    "human_gate_action_classes": ["R3", "R4"]
  }
}
```

Manifest 不携带数据库密码、长期模型密钥、第三方 Refresh Token、知识正文、客户数据正文或其他员工 Personal Memory。

## 6.4 自主等级与接收策略

| 等级 | A2A 可自动执行 | 必须由员工处理 |

|---|---|---|

| L0 Draft | 生成未发送的请求草稿 | 发送、接受、交付、承诺 |

| L1 Inform | 回复已发布共享事实；发送 inform / propose | 接受新任务、改变 Work Item 状态 |

| L2 Collaborate | 接受和推进低风险 Work Item；发起澄清和交付草稿 | 资源冲突、目标变化、外部承诺 |

| L3 Act | 执行已授权 R1/R2 Action | R3/R4 Action 与高风险异常 |

| L4 Commit | 默认关闭 | 逐次人工批准后才可形成正式承诺 |

## 6.5 Agent 可代理状态

对其他 Agent 只暴露以下 display-safe 状态：`AVAILABLE_FOR_ROUTINE_REQUESTS`、`REQUIRE_OWNER_CONFIRMATION`、`FOCUS_MODE`、`OUT_OF_OFFICE_WITH_DELEGATION`、`UNAVAILABLE`。状态不得泄露私人日程、位置、健康或未响应原因。

# 7. Agent 目录、Capability Profile 与发现

## 7.1 为什么需要目录

员工 Agent 网络不能依赖硬编码“销售 Agent 调供应链 Agent”。发送方应按业务关系、责任、Capability、Schema、可用状态和 Policy 找到合适接收方。目录用于减少寻找责任人和跨部门打听，但不能把所有员工能力、权限和个人信息公开成全公司黄页。

## 7.2 Internal Agent Capability Profile（本设计新增）

Capability Profile 是请求者视角的动态投影，而不是 Agent 的全部真实权限。建议结构：

```json
{
  "profile_id": "acp_agt_supply_01_for_usr_sales_01",
  "agent_id": "agt_supply_01",
  "owner": {
    "human_id": "usr_supply_01",
    "display_name": "供应链负责人",
    "organization_unit": "supply-chain"
  },
  "agent_type": "hermes_work_agent",
  "availability": "REQUIRE_OWNER_CONFIRMATION",
  "capabilities": [
    {
      "capability_id": "delivery.feasibility.review",
      "input_schema": "schema://delivery/feasibility-request/v1",
      "output_schema": "schema://delivery/feasibility-result/v1",
      "supported_authorities": ["inform", "propose", "request"],
      "maximum_risk": "R2",
      "requires_shared_object": "sales.order|project.delivery"
    }
  ],
  "protocol": {"collaboration": "1.0"},
  "expires_at": "2026-07-31T12:00:00Z"
}
```

禁止暴露：个人记忆、完整权限列表、客户清单、私人日程、模型配置、Prompt、密钥、未发布 Skill、隐藏的 Access Grant。

## 7.3 发现顺序

1. 先按当前 Work Item、Business Object 和组织责任找到正式 Owner；
2. 再按 Capability / Schema 兼容性筛选；
3. 再检查双方 Tenant、Workspace、项目和 Purpose 关系；
4. 再检查 Agent 可代理状态、自主等级、容量和 SLA；
5. 无合适 Agent 时返回员工、团队队列或人工协调入口；
6. 不允许模型通过聊天内容或 Personal Memory 推断“谁最能干”。

## 7.4 发现 API

```http
POST /v1/agent-capabilities:resolve
Content-Type: application/json

{
  "requester": {"agent_id": "agt_sales_01", "on_behalf_of": "usr_sales_01"},
  "tenant_id": "ten_01",
  "workspace_id": "wsp_order_01",
  "purpose": "customer_delivery",
  "business_object_refs": ["sales.order:SO-2026-001"],
  "required_capability": "delivery.feasibility.review",
  "input_schema": "schema://delivery/feasibility-request/v1"
}
```

响应只返回经过 Policy 裁剪的候选；没有权限看到的 Agent 不能通过“候选数量”“错误差异”被侧信道推断。

# 8. 四类交流对象与业务语义

## 8.1 Conversation

用途：解释、提问、澄清、草拟、非正式提醒。允许自然语言，但 `authority` 只能是 `inform`、`propose` 或 `question`。Conversation 可以关联 Work Item，但不创建或修改正式业务状态。

当对话出现以下任一条件时，Agent 必须建议或自动生成 Collaboration Request 草稿：有明确交付物、有截止时间、需要他人承担责任、需要读取受限资源、需要更新 Work Item、涉及 Action 或外部承诺。

## 8.2 Collaboration Request

请求是正式协作合同，必须包含目标、输入引用、期望输出 Schema、验收标准、期限、Purpose、分类、责任、人工门禁、预算与循环限制。请求被接受后必须创建或关联 Work Item。

## 8.3 Work Item

Work Item 是跨终端、跨 Agent、跨会话的连续性载体，至少保存目标、Owner、参与者、输入、依赖、状态、Action、审批、交付物、异常、Evidence、Outcome 和复盘。会话可删除或过期，但正式 Work Item 仍能恢复完整工作状态。

## 8.4 Action / Approval

任何修改 System of Record、外发、报价、交期、合同、付款、删除或高影响配置的行为必须进入 Action Contract。`prepare` 生成影响预览和 Commit Token，`commit` 才产生业务效果。Conversation 中的“同意”“可以”“就这样做”不能被解释为批准。

## 8.5 七种经营互动

| 互动 | 协议对象 | Agent 可自主 | 人类责任 |

|---|---|---|---|

| 发现 | Capability Resolve | 推荐责任人/能力 | 不能根据私人内容推断能力 |

| 查询 | Conversation / Inform | 返回已有共享事实 | 不能形成新承诺 |

| 请求 | Collaboration Request | 生成结构化请求和引用 | 确认目标和业务目的 |

| 分工 | Request + Work Item Dependency | 低风险子任务与跟踪 | 优先级和资源冲突 |

| 交付 | Delivery + Evidence Ref | 按 Schema 交付 | 重大口径与最终接受 |

| 升级 | NEEDS_HUMAN / Decision Record | 组织证据和冲突 | 作出取舍和承诺 |

| 学习 | Evolution Candidate | 发现重复模式和生成候选 | 发布、权限扩大与停用 |

# 9. Hermes Collaboration Contract v1

## 9.1 公共信封（本设计新增冻结建议）

```json
{
  "contract": "hermes.collaboration",
  "contract_version": "1.0",
  "message_id": "msg_01H...",
  "message_type": "task_request",
  "idempotency_key": "idem_01H...",
  "tenant_id": "ten_01",
  "realm_id": "cn-mainland-1",
  "workspace_id": "wsp_01",
  "thread_id": "thr_01",
  "request_id": "crq_01",
  "work_item_id": "wrk_01",
  "sender": {
    "type": "agent",
    "id": "agt_sales_01",
    "on_behalf_of": "usr_sales_01",
    "device_id": "dev_01",
    "runtime_generation": "rtg_20260731_01"
  },
  "recipients": [
    {"type": "agent", "id": "agt_supply_01", "owner_id": "usr_supply_01"}
  ],
  "authority": "request",
  "purpose": "customer_delivery",
  "classification": "confidential",
  "delegation_ref": "dlg_01",
  "policy_context_version": "pcv_109",
  "sequence": 108,
  "sent_at": "2026-07-31T02:00:00Z",
  "expires_at": "2026-08-01T02:00:00Z",
  "payload_hash": "sha256:...",
  "payload": {},
  "extensions": {}
}
```

字段规则：

- `message_id` 全局稳定，用于投递去重；
- `idempotency_key` 表示发送业务意图的幂等作用域；
- `request_id` 在同一正式协作生命周期内稳定；
- `sequence` 是特定 Delivery Session 方向内游标，不可从 message_id 推导；
- `payload_hash` 对 canonical JSON 计算；同幂等键不同摘要返回冲突；
- 根级未知字段拒绝，兼容扩展进入 `extensions`；
- `authority=commit` 必须携带有效 human approval / enterprise authorization reference。

## 9.2 authority 枚举

| authority | 含义 | 能否形成业务效果 |

|---|---|---|

| inform | 提供事实或状态 | 否 |

| question | 请求解释或澄清 | 否 |

| propose | 提出方案或草稿 | 否 |

| request | 请求对方完成工作 | 创建/推进 Work Item，但不形成外部承诺 |

| delegate | 在有效委托内分配子任务 | 仅限授权范围和风险上限 |

| approve_request | 请求有权人员批准 | 否，直到 Approval 事实生效 |

| commit | 正式承诺或确认 | 是；必须有有效人工或企业授权 |

## 9.3 Task Request payload

```json
{
  "task_type": "delivery_feasibility_review",
  "objective": "判断 SO-2026-001 能否在 2026-09-15 前完成交付",
  "business_object_refs": [
    {"type": "sales.order", "id": "SO-2026-001", "version": "18"}
  ],
  "input_refs": [
    {
      "resource_id": "order://SO-2026-001/lines",
      "version": "18",
      "acl_ref": "acl_order_01",
      "required_freshness": "PT15M"
    }
  ],
  "expected_output": {
    "schema": "schema://delivery/feasibility-result/v1",
    "delivery_mode": "structured"
  },
  "acceptance_criteria": [
    "给出最早可承诺日期",
    "列出产能、物料、物流和质量风险",
    "所有结论附 Evidence Reference"
  ],
  "human_gate": "required_before_external_commitment",
  "reply_policy": {
    "max_agent_turns": 4,
    "max_hops": 2,
    "max_parallel_tasks": 3,
    "allow_partial": true,
    "no_progress_rounds": 2
  },
  "budget": {
    "token_budget": 40000,
    "cost_budget_ref": "cbg_order_review_standard",
    "deadline": "2026-07-31T18:00:00+08:00"
  }
}
```

## 9.4 Delivery payload

```json
{
  "request_id": "crq_01",
  "delivery_id": "dlv_01",
  "status": "partial|complete|blocked",
  "output_schema": "schema://delivery/feasibility-result/v1",
  "output": {
    "earliest_delivery_date": "2026-09-20",
    "available_quantity": 500,
    "confidence": "medium",
    "risks": [
      {"type": "ocean_schedule", "severity": "high", "summary": "目标日期存在 5 天缺口"}
    ]
  },
  "evidence_refs": ["evd_inventory_01", "evd_capacity_01", "evd_schedule_01"],
  "open_questions": ["是否接受拆单或替代包装版本"],
  "requires_human": true,
  "generated_by": {"agent_id": "agt_supply_01", "on_behalf_of": "usr_supply_01"}
}
```

## 9.5 Receipt payload

```json
{
  "message_id": "msg_01H...",
  "request_id": "crq_01",
  "receipt_type": "accepted_by_gateway|delivered_to_connector|received_by_agent|seen_by_human|accepted|rejected",
  "receiver_agent_id": "agt_supply_01",
  "delivery_session_id": "ads_01",
  "sequence": 109,
  "resource_version": 7,
  "reason_code": null,
  "occurred_at": "2026-07-31T02:00:01Z"
}
```

Receipt 表示不同层级事实。`accepted_by_gateway` 只能证明服务端已校验并持久化，不能显示成“对方已收到”或“对方已确认”。

# 10. 消息类型与协议分层

## 10.1 领域消息类型

| message_type | 用途 | 是否要求 Work Item |

|---|---|---|

| conversation_message | 讨论、解释、提问、草拟 | 可选 |

| task_request | 请求对方完成明确工作 | 接受后必须 |

| task_acceptance | 接受、拒绝、要求 Owner 确认 | 是 |

| clarification_request | 请求补充输入或边界 | 是 |

| clarification_response | 提供结构化补充 | 是 |

| status_update | 进度、阻塞、恢复、预计完成时间 | 是 |

| delivery | 提交结构化交付物和 Evidence | 是 |

| verification_result | 验收通过、退回或差异 | 是 |

| access_request_ref | 引用 Access Request，不含受限正文 | 是 |

| approval_request_ref | 引用 Business Approval，不直接携带批准结果 | 是 |

| cancellation | 撤回仍允许取消的请求 | 是 |

## 10.2 Connector WSS wire 类型

继承企业工作模式文档提出的 WSS 类型：

- `collaboration.send`：Connector A → Server，提交带幂等键的消息；
- `collaboration.accepted`：Server → Connector A，服务端已持久化；
- `collaboration.available`：Server → Connector B，存在待收消息；
- `collaboration.pull`：Connector B → Server，按游标拉取；
- `collaboration.message`：Server → Connector B，交付结构化消息；
- `collaboration.receipt`：Connector B → Server，持久化/接收/处理回执；
- `collaboration.status`：Server → 双方，状态变化；
- `collaboration.cancel`：Connector A → Server，在允许状态下取消。

**重要：** 当前 Connector Protocol v1 中 `a2a.message` 仍为 `reserved/effect=none`。第一版不得复用或私自激活它。`collaboration.*` 必须通过正式 contract update、Schema、Golden Fixture、capability negotiation 和生产门禁后才能产生业务效果。

## 10.3 capability negotiation

建议 Connector Hello 增加可选 capability（实际字段位置需按根契约冻结）：

```json
{
  "optional_capabilities": [
    "collaboration.v1.send",
    "collaboration.v1.receive",
    "collaboration.v1.receipt",
    "collaboration.v1.cursor_resume"
  ]
}
```

只有 Server、Connector、Local Gateway 和 Agent Runtime 全部通过兼容检查，且 Bootstrap Manifest 激活 `collaboration_v1` 时，Gateway 才广告可用。任一环节失败，必须 fail-closed，界面显示“协作能力不可用”，不能降级成普通 prompt 代发。

# 11. 端到端在线与离线链路

## 11.1 在线链路

[[FIG:online]]

关键确认点：

1. Agent A 在本地先生成 message_id、idempotency_key 和 payload_hash；
2. Connector A 将待发消息写入本地 Outbox 后再发送；
3. Collaboration Gateway 在同一 PostgreSQL 事务写入 message、request 状态和 outbox；
4. `collaboration.accepted` 只在事务提交后返回；
5. Connector B 收到消息后先落 SQLite Inbox，再回 `DELIVERED_TO_CONNECTOR`；
6. Local Gateway 验证目标 Agent、runtime generation、Manifest capability 和本地 allowlist；
7. Agent B 返回 `RECEIVED_BY_AGENT`，随后按自主等级自动接受、请求 Owner 确认或拒绝；
8. 发送方 UI 依次显示“服务端已接收 / 等待对方 / 已送达设备 / Agent 已接收 / 已接受”等真实状态。

## 11.2 离线链路

[[FIG:offline]]

离线请求必须具备 TTL、优先级、最大积压、重复投递幂等、毒消息隔离和员工可见失败状态。管理员可查看积压数量、年龄、错误码和消息哈希，但不默认读取正文。

请求超过 TTL 后进入 `EXPIRED`。接收方上线时不得自动复活；发送方必须重新确认目标、资源版本和期限后创建新请求或显式重开。

## 11.3 澄清回路

Agent B 缺少输入时发送 `clarification_request`，必须引用原 request_id，并说明缺失字段的 Schema 路径。Agent A 可以自动补充已有共享事实；若需要新权限、目标变化或员工判断，则进入 `NEEDS_HUMAN`。澄清不允许悄悄改变原目标、Purpose 或验收标准；变更必须生成 request revision 并由 Owner 确认。

## 11.4 权限不足回路

Agent B 无权读取引用时：

1. 返回 `ACCESS_REQUIRED`，不泄露资源正文、标题或敏感元数据；
2. 系统生成 Access Request 草稿；
3. Data Owner 只可授权指定资源/字段、Purpose、接收主体和有效期；
4. Grant 生效后 Agent B 使用自己的身份重新读取；
5. 原请求继续执行或由发送方取消；
6. Grant 撤销时，未完成任务立即重新鉴权并按风险暂停。

# 12. 状态机与责任边界

## 12.1 Collaboration Request 状态机

[[FIG:states]]

每次状态转换都必须保存：操作者、Agent 身份、代表员工、原因、时间、资源版本、Policy Decision、Manifest 版本和 Trace ID。状态转换使用乐观锁 `resource_version`，客户端不得根据本地 UI 猜测成功。

## 12.2 Delivery 状态机（本设计新增）

```text
DRAFT
  -> SUBMITTED
  -> SCHEMA_VALIDATED
  -> DELIVERED
  -> ACCEPTED / RETURNED
  -> SUPERSEDED

SUBMITTED / SCHEMA_VALIDATED
  -> FAILED / EXPIRED
```

交付物被退回必须包含结构化差异或验收项；重新交付创建新 revision，不覆盖历史版本。

## 12.3 Work Item 状态机

```text
PLANNED -> READY -> IN_PROGRESS -> IN_REVIEW -> APPROVED -> DONE -> ARCHIVED
READY / IN_PROGRESS / IN_REVIEW -> BLOCKED -> IN_PROGRESS
任意未完成状态 -> CANCELLED
```

从 `IN_REVIEW` 到 `APPROVED` 必须存在审批人、意见和目标交付物版本。Conversation 无权直接推动该转换。

## 12.4 五类责任字段

| 字段 | 含义 |

|---|---|

| requested_by | 谁提出目标 |

| planned_by | 谁生成计划 |

| executed_by | 谁或哪个 Agent 执行 |

| approved_by | 谁批准 |

| verified_by | 谁或哪个规则验证 |

| accepted_by | 谁接受最终交付物 |

不得合并成一个 `operator`。同一人可以承担多个角色，但每个责任动作仍需分别记录。

# 13. 路由、可靠性、幂等与恢复

## 13.1 可靠性模型

采用“至少一次投递、业务效果幂等”：

- 网络层允许重投；
- 服务端按 `tenant_id + message_id` 去重；
- 业务提交按发送方 Agent、Method、Idempotency Key 和 canonical payload digest 去重；
- 相同 key + 相同 payload 返回先前结果；
- 相同 key + 不同 payload 返回冲突；
- NATS / JetStream 重复投递不产生重复 Message / Request / Work Item；
- Action 的幂等由 Action Contract 单独保证，不以“消息只收到一次”为前提。

## 13.2 建议幂等作用域

```text
Collaboration Send:
(tenant_id, sender_agent_id, message_type, idempotency_key)

Request Revision:
(tenant_id, request_id, request_revision)

Receiver Receipt:
(tenant_id, message_id, receiver_agent_id, receipt_type)

Delivery:
(tenant_id, request_id, delivery_id, revision)
```

## 13.3 Outbox / Inbox 顺序

发送侧：本地 Outbox 落盘 → WSS 发送 → 服务端持久接受 → 清理/归档本地记录。接收侧：WSS 收到 → SQLite Inbox 落盘 → 回 Connector Receipt → Local Gateway 投递 → Agent Receipt。

现有 Connector Protocol 缺少显式 durable receipt/result ACK 的部分不能被 heartbeat cursor 或 WebSocket send 替代。协作协议必须明确“哪一个服务端 ACK 才允许清理本地 Outbox”，否则断线后继续重放。

## 13.4 结果未知

纯消息交付可以安全重投，因为 message_id 幂等；但消息触发的外部 Action 可能进入 `OUTCOME_UNKNOWN`。此时必须按 Action ID / 外部请求 ID 查询最终状态，不能重新发送可能产生外部效果的动作。协作请求进入 `NEEDS_HUMAN`，接管包包含未决 Action 和查询入口。

## 13.5 对账

至少建立三类 Reconcile：

- Message Reconcile：PostgreSQL 状态与 Connector Receipt / Cursor 对账；
- Request Reconcile：Request 状态与 Work Item / Delivery 状态对账；
- Action Reconcile：Action Command 与 System of Record 结果对账。

任何 Reconcile 只能修正状态，不得重复执行业务动作。

# 14. 循环、预算、跳数与并发控制

## 14.1 每个请求必须显式限定

- 最大 Agent 轮次；
- 最大 Agent 跳数；
- 最大总时长和 TTL；
- 最大并行子任务数；
- Token、模型、消息和费用预算；
- 允许使用的 Skill；
- 可访问的数据范围；
- 可执行 Action 风险上限；
- `no_progress_rounds`；
- 冲突检测与目标漂移检测。

## 14.2 自动进入 NEEDS_HUMAN

- 连续两轮没有新增 Evidence、状态或可验收输出；
- 双方结论冲突且无法用正式事实裁决；
- 目标、Purpose、验收标准或期限发生变化；
- 需要扩大权限、风险等级或委托范围；
- 预算、轮次或截止时间即将耗尽；
- 涉及员工本人判断、资源优先级或正式承诺；
- Action 结果未知；
- 任一 Agent 缺少兼容 Skill / Schema / runtime capability；
- 检测到 Prompt Injection、数据外泄或身份异常。

## 14.3 预算继承规则

子请求的 Purpose、分类上限、风险上限和数据范围不得超过父请求；子请求预算总和不得超过父请求剩余预算。Agent 不能通过创建更多子请求绕过总预算、轮次或 TTL。服务端在路由前强制校验，模型输出中的“请继续多聊几轮”不具有授权效力。

# 15. Resource Reference、Access Grant 与权限重判

## 15.1 Resource Reference

```json
{
  "resource_id": "order://SO-2026-001/lines",
  "resource_type": "sales.order.lines",
  "source_system": "erp",
  "source_object_id": "SO-2026-001",
  "version": "18",
  "acl_ref": "acl_order_01",
  "classification": "confidential",
  "purpose": "customer_delivery",
  "required_freshness": "PT15M",
  "content_hash": "sha256:...",
  "expires_at": "2026-08-01T02:00:00Z"
}
```

消息优先传引用、对象 ID、Data Product 版本、Work Item、Evidence 和必要最小摘要。大文件经 Upload Grant、DLP/病毒扫描和对象存储生成引用，不走消息总线正文。

## 15.2 接收方重新鉴权

读取链路：`Agent B + on_behalf_of User B + Purpose + Workspace + Resource + Context → Policy Decision → obligations → Data Product Gateway / Source System`。服务端不能因为 Agent A 有权限就把其权限传给 B。

## 15.3 Access Grant

| 字段 | 要求 |

|---|---|

| grantor | 具有授权能力的 Data Owner / Policy Owner |

| grantee | 指定 Human + Agent，不能只写团队通配 |

| resource scope | 指定对象、字段、行范围或受控选择器 |

| purpose | 单一明确业务目的 |

| validity | not_before / expires_at / revoke_at |

| risk | 高敏感数据要求审批；默认不可再转授权 |

| audit | 创建、使用、拒绝、撤销和过期全部记录 |

## 15.4 派生结果权限

由多个来源生成的总结、报表或交付物，默认 ACL 是所有输入 ACL 的交集，并受最高分类、Purpose 和驻留限制。扩大范围必须经过 Data Owner 批准、脱敏、DLP、新分类与完整血缘。Personal Memory 永不进入企业 Agent 网络，除非员工显式提交某个对象为工作 Evidence。

# 16. Action、Approval 与外部承诺

## 16.1 风险等级

| 等级 | 示例 | A2A 默认规则 |

|---|---|---|

| R0 | 查询、比较、状态汇总 | Policy 允许后可自动 |

| R1 | 保存个人 View、评论、内部草稿 | 用户确认，可撤销 |

| R2 | 分配任务、更新明确内部状态 | Schema、权限、幂等、审计；按场景人工确认 |

| R3 | 发信、报价、交期、合同审批、客户回复 | 明确预览 + 有权人员逐次批准 |

| R4 | 付款、删除、生产变更、高影响权限 | 多人审批或仅人工执行 |

## 16.2 Prepare / Commit

```text
Agent 生成 Command Plan
  -> Action Gateway schema 校验
  -> Policy + 前置条件 + resource version
  -> prepare 返回 Preview / Risk / Approvals / Commit Token
  -> 员工或审批人确认
  -> commit 执行
  -> System of Record 结果
  -> Outcome / Evidence / Audit
```

Prepare 过期、资源版本变化、授权撤销或来源状态变化时必须重新 Prepare。

## 16.3 Business Approval 与 Mobile Control Approval 的边界

Mobile Control v1 中的 `approval.respond` 是 owner runtime 的 pending input 控制契约，不能直接充当企业业务审批事实源。建议：

- Business Approval 事实保存在 `work_item_approvals` / Approval Service；
- Workbench 将审批卡展示给有权人员；
- 若需要通过本地 Agent 提醒或收集输入，可由适配器生成 pending input；
- 最终是否批准由服务端 Business Approval 的资源版本和签名结果决定；
- 控制租约、审批 UI 和业务批准对象分别审计。

## 16.4 外部承诺门禁

价格、折扣、合同条款、交付日期、产品能力、客户数据处理、赔付、品牌立场和法律答复均属于外部承诺。Agent 可以收集事实、模拟方案和生成承诺草稿，但 `authority=commit` 必须引用有效批准和当前资源版本。

# 17. Evidence、Outcome、Trace 与 Audit

## 17.1 Evidence 最小字段

- 来源对象、版本和获取时间；
- 内容哈希与来源 ACL 快照；
- 适用 Purpose、分类和驻留；
- 生成/转换过程与使用的 Skill、模型、工具版本；
- 人工修改、确认和拒绝；
- 关联 Request、Work Item、Delivery、Action 和 Outcome；
- 保留、删除、Legal Hold 和失效规则。

## 17.2 不保存隐藏推理，保存执行证据

平台保存目标、结构化计划摘要、数据引用、工具和 Skill、Policy Decision、人工决定、输出、错误、重试、补偿和结果；不要求保存模型隐藏 Chain of Thought。只有服务端授权为可见的 Thinking 才能按 Output Parity Contract 展示，也不能作为正式业务 Evidence 的替代品。

## 17.3 Audit 事件

建议至少包含：`collaboration.message.accepted`、`collaboration.message.delivered`、`collaboration.message.received`、`collaboration.request.state_changed`、`collaboration.human_takeover`、`policy.decision.denied`、`access.grant.created`、`access.grant.revoked`、`action.outcome_unknown`、`audit.original_content.accessed`。

# 18. 服务边界与首版部署单元

## 18.1 逻辑服务

| 服务/模块 | 职责 | 不负责 |

|---|---|---|

| Collaboration API / Gateway | 校验协议、身份、委托、TTL、路由、持久化入口 | 不替员工解释承诺，不直接读源系统 |

| Agent Directory / Capability Resolver | 按请求者视角解析可协作 Agent 与 Schema | 不保存 Personal Memory，不授予权限 |

| Delivery Session Manager | 在线 Session、Cursor、可用消息通知、重连恢复 | 不保存最终消息事实 |

| Collaboration Store | Thread、Message、Request、Receipt、状态机事实 | 不保存大附件正文 |

| Work Item Service | 任务、参与者、依赖、交付、审批、交接、SLA | 不保存来源系统交易 |

| Policy Client / PEP | 请求 Policy Decision 并执行 obligations | 不自行解释业务权限 |

| Evidence Service | Evidence 元数据、引用、失效和血缘 | 不把普通聊天自动变知识 |

| Notification / Workbench Projection | 生成用户可见状态与待办 | 不成为事实源 |

| Outbox Publisher / Delivery Worker | 可靠发布、重投、DLQ、对账 | 不改变领域状态语义 |

## 18.2 首版部署建议

Gate 1 将上述逻辑模块部署在一个 Remote Server 产品单元中，但代码层使用独立 package、repository interface、Schema 和事件。这样避免过早微服务化，同时保证将来按容量拆分时不改变契约。

建议目录：

```text
remote-server/
  collaboration/
    domain/
    application/
    api/
    persistence/
    delivery/
    policy/
    schemas/
  work_items/
  evidence/
  identity/
  outbox/
  contracts/
    collaboration/v1/
```

# 19. PostgreSQL 数据模型、索引与约束

## 19.1 核心表

| 表 | 主要字段 | 关键约束 |

|---|---|---|

| communication_threads | tenant, workspace, subject, work_item_id, classification, status | tenant + thread_id 唯一 |

| thread_participants | thread, participant_type/id, owner_id, role, joined_at | 同参与者不重复 |

| collaboration_messages | message_id, type, sender, recipients, authority, purpose, payload_ref/hash, expiry | tenant + message_id；sender + idempotency 唯一 |

| collaboration_requests | request_id, revision, state, objective, output_schema, human_gate, budgets, version | tenant + request_id；乐观锁 |

| collaboration_request_events | request, from_state, to_state, actor, reason, trace | 追加式；不可覆盖 |

| collaboration_request_receipts | message, receiver, receipt_type, delivery_session, occurred_at | message + receiver + type 唯一 |

| agent_delivery_sessions | agent, connector, runtime_generation, status, cursors, lease/expiry | 每 agent 当前活动 session 受控唯一 |

| message_delivery_cursors | session, direction, next_sequence, acked_sequence | 单调递增 |

| message_outbox | event_id, aggregate, payload_ref, available_at, attempts | event_id 唯一 |

| dead_letter_messages | message/event, reason, payload_hash, first/last failure | 正文默认不进入普通运维视图 |

| agent_capability_profiles | agent, capability, schemas, protocol, policy selectors, version | 版本化、可撤销 |

| agent_delegation_policies | human, agent, purpose, action/risk, max hops/rounds, status | 有效期与版本 |

| access_grants | grantor, grantee human/agent, resource, purpose, expiry, status | 不可默认再转授权 |

| work_items / approvals / handoffs | 沿用现有设计 | 状态机与责任字段 |

| evidence_objects | source refs, hashes, ACL snapshot, lineage, retention | 不可无来源发布 |

## 19.2 关键索引

```sql
CREATE UNIQUE INDEX uq_collab_message_id
  ON collaboration_messages (tenant_id, message_id);

CREATE UNIQUE INDEX uq_collab_sender_idempotency
  ON collaboration_messages (tenant_id, sender_agent_id, message_type, idempotency_key);

CREATE INDEX ix_collab_receiver_queue
  ON collaboration_message_recipients (tenant_id, receiver_agent_id, delivery_state, available_at)
  WHERE delivery_state IN ('QUEUED', 'RETRY');

CREATE INDEX ix_collab_request_state_deadline
  ON collaboration_requests (tenant_id, state, deadline_at);

CREATE UNIQUE INDEX uq_collab_receipt
  ON collaboration_request_receipts (tenant_id, message_id, receiver_agent_id, receipt_type);

CREATE INDEX ix_collab_outbox_available
  ON message_outbox (available_at, status)
  WHERE status = 'PENDING';
```

## 19.3 数据库规则

- 所有业务表含 `tenant_id`，Server 使用 Tenant scope 校验并启用 PostgreSQL RLS 纵深防御；
- 大正文和附件进入对象存储，PG 保存元数据、摘要和对象引用；
- 状态变化使用乐观版本号；
- Request Event 与 Audit 采用追加式；
- 删除传播覆盖消息正文、对象存储、搜索投影、Agent 缓存和 DLQ 副本；
- Redis 只可作 Presence、限流、短锁和缓存，不能保存唯一消息、审批、委托和审计事实。

# 20. NATS / JetStream 内部主题与流

## 20.1 建议主题（内部实现，不进入外部协议）

```text
collab.deliver.v1.<realm>.<tenant>.<agent>
collab.receipt.v1.<realm>.<tenant>.<agent>
collab.status.v1.<realm>.<tenant>.<request>
collab.available.v1.<realm>.<tenant>.<agent>
collab.audit.v1.<realm>.<tenant>
presence.v1.<realm>.<tenant>.<agent>
```

## 20.2 Stream

| Stream | 内容 | 保留原则 |

|---|---|---|

| COLLAB_DELIVERY | 待投递消息与通知 | 不超过业务 TTL + 恢复窗口 |

| COLLAB_RECEIPTS | Connector / Agent Receipt 与状态事件 | 覆盖 PostgreSQL 消费恢复窗口 |

| COLLAB_DURABLE_EVENTS | 请求状态、人工接管、安全事件 | 有界时间和容量 |

| AUDIT_INGEST | 待持久化审计事件 | 持久化完成后可清理 |

NATS Subject 不成为 Connector 或 Agent 对外契约。Connector 只通过 Hermes WSS 与 Gateway 交互；未来替换消息基础设施不得要求升级所有终端。

# 21. Connector 与 Agent Local Gateway 改造

## 21.1 Connector 新增职责

- 在 WSS 握手中协商 `collaboration.v1.*` capability；
- 维护协作 Outbox / Inbox / Cursor；
- 将 Cloud `collaboration.message` 转换为版本化 Local Gateway RPC；
- 将 Agent Receipt、发送请求和状态转换为 Connector Protocol；
- Agent 不可用时保持 Cloud 在线，报告 `agent_unavailable`，不假装已送达；
- 不解析 SessionDB、不导入 Agent 私有模块、不理解业务 Skill。

## 21.2 Connector SQLite 建议表

| 表 | 用途 |

|---|---|

| collaboration_cloud_inbox | Cloud 已交付消息、payload digest、本地投递状态、最后结果摘要 |

| collaboration_local_outbox | 尚未得到 Server durable ACK 的 send / receipt / status |

| collaboration_cursors | 上下行 next / acked sequence |

| collaboration_dedup | message_id / idempotency key 的有界本地账本 |

| agent_runtime | 最近 Local Gateway、runtime generation 和 collaboration capability |

SQLite 继续使用 SQLAlchemy ORM、单线程执行器、operation-scoped Session、WAL 和 Alembic/版本化 DDL。敏感正文不进入常规明文列；可重建 payload 优先保存密文或对象引用。

## 21.3 Local Gateway RPC（本设计新增）

```text
collaboration.capabilities.get
collaboration.outbound.submit
collaboration.inbound.deliver
collaboration.receipt.submit
collaboration.request.status
collaboration.request.cancel
collaboration.snapshot.get
```

每个 RPC 校验 Agent ID、runtime generation、Bootstrap Manifest、method allowlist、Purpose、payload hash 和本地资源限制。Local Gateway 不替代 Agent owner transport，也不允许 Connector 直接调用任意 Agent 内部 handler。

## 21.4 与 Mobile Control 的隔离

Observer socket、Control socket 与 Collaboration lane 是不同角色：

- Observer：只观察安全投影；
- Control：单一显式控制租约，执行 Queue / Guide / Stop / pending input；
- Collaboration：Agent 业务协作消息的持久 lane，不持有 owner control lease。

不得把 Collaboration Request 包装成 `prompt.submit`，也不得用 control.request 的非持久通道承载业务消息。

# 22. REST / WSS / Local RPC 接口

## 22.1 Cloud REST API

| API | 用途 |

|---|---|

| POST /v1/collaboration-requests | 创建正式请求 |

| GET /v1/collaboration-requests/{id} | 查询权威状态与版本 |

| POST /v1/collaboration-requests/{id}:accept | 接受或请求 Owner 确认 |

| POST /v1/collaboration-requests/{id}:reject | 拒绝并返回最小原因码 |

| POST /v1/collaboration-requests/{id}:deliver | 提交结构化交付物 |

| POST /v1/collaboration-requests/{id}:verify | 验收、退回或关闭 |

| POST /v1/collaboration-requests/{id}:cancel | 在允许状态取消 |

| POST /v1/collaboration-requests/{id}:takeover | 员工接管 |

| POST /v1/conversation-messages | 发送非正式消息 |

| POST /v1/agent-capabilities:resolve | 发现可协作 Agent / Capability |

| GET /v1/agents/{id}/collaboration-profile | 获取请求者视角能力投影 |

| POST /v1/access-requests | 创建受控访问申请 |

| GET /v1/work-items/{id} | 恢复任务状态 |

## 22.2 Realtime event

```text
collaboration.message.accepted
collaboration.message.available
collaboration.message.delivered
collaboration.message.received
collaboration.request.state_changed
collaboration.delivery.created
collaboration.delivery.returned
collaboration.human_takeover
collaboration.budget.warning
collaboration.request.expired
```

每个事件包含 event_id、tenant、object ID、object version、occurred_at、trace_id、schema version。生产者使用 Transactional Outbox，消费者按 event_id 幂等。

# 23. 错误码与客户端可执行恢复

以下 4300–4349 为**建议保留范围，尚待机械碰撞扫描与协议冻结**：

| 码 | 含义 | 客户端动作 |

|---|---|---|

| 4300 | collaboration role/capability required | 隐藏发送入口，提示升级或重新激活 |

| 4301 | unsupported contract/schema version | 协商兼容版本 |

| 4302 | agent identity or delegation invalid | 重新 Bootstrap / 登录 |

| 4303 | receiver unavailable | 排队或选择其他正式责任人 |

| 4304 | purpose denied | 显示原因码和申请入口，不泄露资源 |

| 4305 | resource access required | 创建 Access Request |

| 4306 | idempotency key payload conflict | 生成新 key 或恢复原请求 |

| 4307 | request version conflict | 刷新权威状态后重试 |

| 4308 | request expired | 重新确认目标和期限 |

| 4309 | human owner confirmation required | 展示 Owner 待办 |

| 4310 | loop/round limit exceeded | 进入 NEEDS_HUMAN |

| 4311 | budget exceeded | 停止自动协作，申请预算或缩小范围 |

| 4312 | capability/schema unavailable | 选择兼容 Agent / Skill |

| 4313 | classification/model route blocked | 改用本地/专属执行或人工 |

| 4314 | message too large | 上传对象存储并发送引用 |

| 4315 | delivery session unavailable | 服务端排队等待重连 |

| 4316 | request not cancellable | 刷新状态；不能假装取消成功 |

| 4317 | action outcome unknown | 查询最终状态并人工接管 |

| 4318 | prompt injection / unsafe content | 隔离内容并创建安全事件 |

| 4319 | rate limited / quota exceeded | 按 retry_after 或联系管理员 |

| 4320–4349 | reserved | 不得未经 contract update 使用 |

# 24. Workbench 交互与移动端边界

## 24.1 协作线程

统一线程必须显示：员工 A、Agent A、员工 B、Agent B；每条 Agent 消息代表谁；Authority；是否经过本人确认；引用的数据；关联 Work Item；当前状态、截止时间、预算和人工门禁；接管、拒绝、撤回和纠错入口。Agent 消息不能与员工本人消息使用完全相同头像和样式。

## 24.2 不做社交聊天 UI

沿用 Output Parity Contract：移动端是 Inspect + Operate Agent console。协作消息按稳定事件顺序呈现，Tool / Thinking / Activity / Response / Approval 等保持语义边界。正式 Request 和 Delivery 使用可展开结构化区块，不使用左右聊天气泡掩盖责任和状态。

## 24.3 状态文案

| 内部状态 | 用户文案 |

|---|---|

| ACCEPTED_BY_GATEWAY | 已提交到 Hermes |

| QUEUED | 等待对方 Agent 上线或处理 |

| DELIVERED_TO_CONNECTOR | 已送达对方设备 |

| RECEIVED_BY_AGENT | 对方 Agent 已接收 |

| NEEDS_OWNER | 等待对方本人确认 |

| ACCEPTED | 对方已接受任务 |

| DELIVERED | 已提交交付物 |

| VERIFIED | 交付物已验收 |

| EXPIRED | 请求已过期，未自动重开 |

# 25. 安全威胁模型与控制

| 威胁 | 风险 | 控制 |

|---|---|---|

| 伪造 Agent / 员工 | 冒名请求或承诺 | Agent identity、device attestation、signed Manifest、delegation token |

| 跨 Tenant / Workspace 越权 | 数据泄露 | RLS、Policy、资源二次鉴权、请求者视角目录 |

| 发送方权限传播 | 接收方获得无权正文 | Resource Reference + receiver-side authorization + Access Grant |

| authority 升级 | propose 被解释成 commit | 受控枚举、Action / Approval 引用、Schema validator |

| 重放/重复投递 | 重复任务或动作 | message_id、idempotency key、payload digest、Action idempotency |

| 循环爆炸 | Token/成本/注意力失控 | max rounds/hops/parallel/budget、no-progress、NEEDS_HUMAN |

| Prompt Injection | 消息诱导调用工具或泄露数据 | 外部/协作内容按不可信输入、Tool Gateway、模型与 Policy 分离 |

| 恶意 Resource Reference | 引用走私、SSRF、越权 | 允许的 URI scheme、Registry resolve、source ownership、禁止客户端任意 URL |

| 过期 Manifest / 撤销延迟 | 离职员工 Agent 继续工作 | 短期 Manifest、revocation event、重连前阻断、权限收缩立即生效 |

| 日志泄露 | 消息、密钥、审批正文进入日志 | 只记录标识、哈希、错误码；内容访问独立审计 |

| DLQ 泄露 | 运维人员读取正文 | 对象引用/密文、最小可见、单独权限和保留 |

| 大文件恶意内容 | 病毒、秘密、PII、版权风险 | 隔离区、病毒/DLP/秘密扫描、短期下载 Grant |

| 管理员超级权限 | 排障变长期明文访问 | 平台管理员不默认读正文；Break-glass 工单、期限、双人复核 |

## 25.1 加密策略

传输使用 TLS/mTLS；静态消息和附件按 Tenant DEK 信封加密；高敏字段可独立加密。完全 E2EE 不能作为所有企业消息默认模式，因为会削弱 DLP、分类、路由和合规审计。Restricted 场景可采用“服务端只见路由元数据、密文和 ACL，接收端本地重新 Policy/DLP”的专用模式，需由客户安全政策明确。

# 26. 可观测性、SLO 与经营指标

## 26.1 技术指标

- 发送接受延迟、Connector 持久 ACK、Agent Receipt 延迟；
- 在线/离线投递比例、队列年龄、重投率、DLQ 数量；
- Request 各状态停留时间；
- Cursor gap、snapshot/reconcile 次数；
- 幂等命中和 payload conflict；
- Policy deny、Access Request、权限撤销传播；
- Loop / Budget / TTL / rate limit 触发；
- 人工接管、Action OUTCOME_UNKNOWN；
- 按 Tenant / Agent / Connector / Schema / version 分布。

## 26.2 建议 SLO

| 指标 | Gate 1 建议目标 |

|---|---|

| 服务端接受并持久化 Collaboration Send | 在线 P95 < 500 ms（不含客户端网络） |

| 在线 Server → Connector 持久 Receipt | P95 < 1 s |

| Gateway 故障后 Connector 重连 | P95 < 30 s |

| 权威 Request / Command 状态可查询 | 100% |

| 已提交消息永久丢失 | 0 |

| 跨 Tenant 越权 | 0 |

| 高风险写入无审计 | 0；无法同步确认则阻断 |

| 撤销传播到在线 Agent | P95 < 30 s；离线重连前阻断 |

SLO 是设计目标，需要通过真实客户网络、设备和数据量校准，不应在未测量前转化为公开 SLA。

## 26.3 经营指标

- 责任人匹配时间；
- 交接时延与一次补齐率；
- 端到端 Work Item 周期；
- 等待时间与有效处理时间；
- 有效协作率；
- 异步完成率；
- 一次通过率和结果错误率；
- 人工接管率、无进展升级率；
- 协作路径复用率；
- 每结果消息成本；
- 单 Work Item 人工、模型、系统和复核成本；
- 员工信任、纠错和继续使用意愿。

# 27. 四种部署模式与数据驻留

| 模式 | Collaboration Gateway | 正文与 Evidence | 关键约束 |

|---|---|---|---|

| Shared SaaS | Hermes 共享云 | 共享数据平面内 Tenant 隔离 | RLS、Tenant DEK、严格权限过滤 |

| Dedicated Data Plane | 共享控制面 + 客户专属数据平面 | 客户专属 VPC | 中央仅留租户/订阅/运行元数据 |

| Connected Private | 客户云账号/VPC | 客户环境 | 业务数据、日志和密钥不离开 VPC |

| Offline Private | 企业内网 | 企业离线环境 | 本地 IdP、Registry、KMS、NATS、PG、模型；仍是服务端路由，不改 P2P |

四种模式共用 Collaboration Contract、Receipt、状态机、Policy、Evidence 和审计语义。差异通过 Adapter、部署清单、密钥和发布策略表达，不维护四套领域代码。

# 28. 版本、兼容、发布与回滚

## 28.1 发布轨道

| 轨道 | 内容 | 节奏 | 回滚单位 |

|---|---|---|---|

| Runtime | Hermes Agent 执行内核 | 慢、受控 | Agent 版本 |

| Connector | 连接、重试、SQLite、本地协议适配 | 慢、独立 | Connector 版本 |

| Remote Server | 多租户、路由、事实、审计 | 持续服务端发布 | 服务版本 |

| Capability | Skill、Action Binding、Workflow、Task Contract | 快 | 单个能力版本 |

| Experience | View Schema、组件和模板 | 快 | 单个 View / 组件 |

## 28.2 协议兼容

- Connector、Local Gateway 和 Collaboration Contract 各自版本化；
- 功能按 capability 打开，不根据版本字符串猜测；
- 破坏性变更使用并行 v2 endpoint / message type；
- 数据库采用 expand → migrate → contract；
- Server 至少支持当前与前两代稳定 Connector，迁移窗口按商用基线执行；
- 新 Capability 不假定所有 Agent 已升级；不兼容时返回 `CAPABILITY_UNAVAILABLE` 和稳定旧版本；
- 运行中的 Work Item 固定 Manifest、Task Contract、Schema、Skill、Knowledge 和 Policy 版本；安全收缩可立即暂停。

## 28.3 灰度和自动停止

按 Tenant、Workspace、团队、用户、Work Item 类型、数据分类、Agent capability 和时间窗口灰度。以下情况自动停止扩大：严重越权、R3/R4 异常、OUTCOME_UNKNOWN 上升、人工接管激增、队列积压超阈值、单位成本或时延越界、Schema 不兼容、Connector 重连风暴。

# 29. 测试矩阵与上线门禁

## 29.1 Contract 与兼容

- JSON Schema、canonical JSON、payload hash 和 Golden Fixture；
- 根级未知字段、extensions、Schema minor/major；
- Agent N/N-1/N-2、Connector N/N-1/N-2、Server current/canary；
- capability 缺失、版本不兼容和安全降级；
- error code 碰撞扫描（含 4200–4219 已占用范围）。

## 29.2 状态机与可靠性

- 每条合法/非法状态转换；
- 乐观锁冲突、重复提交、相同 key 不同 payload；
- PostgreSQL commit 成功但 NATS 发布失败；
- JetStream 重投；
- Connector 收到后崩溃；
- Agent 已处理但 Receipt 丢失；
- TTL 到期、取消竞态、毒消息 DLQ；
- 重连游标缺口、snapshot/reconcile；
- Action OUTCOME_UNKNOWN 不自动重试。

## 29.3 权限与安全

- 跨 Tenant / Workspace / Project / Agent / Human 越权；
- 发送方权限传播、Resource Reference 走私；
- Access Grant 过期、撤销、不可再转授权；
- Personal Memory 泄露；
- propose → commit 权限升级；
- Prompt Injection、恶意附件、敏感日志、DLQ 明文；
- stale Manifest、离职、设备吊销、Break-glass；
- Search / Vector / View / Export / Model Gateway PEP obligations。

## 29.4 体验与物理设备

- 状态文案不混淆 accepted / delivered / received / accepted task；
- Agent 与员工身份标识；
- 长消息、表格、代码、附件引用、流式状态；
- 离线、重连、历史 prepend、滚动稳定；
- 人工接管、审批、Access Request；
- Android / H5 真实 Hermes Session E2E；物理设备作为移动端最终验收。

## 29.5 上线门禁

| 门禁 | 要求 |

|---|---|

| Contract Freeze | Schema、状态机、错误码、幂等、Receipt、TTL 完成评审并签名 |

| Security | 越权、重放、权限传播、日志泄露测试通过 |

| Reliability | 断网、崩溃、重投、恢复、对账演练通过 |

| Observability | 消息/请求/Agent/Connector/Policy 关联 Trace 完整 |

| Business | 试点 Owner、Task Contract、验收标准和高风险门禁已确认 |

| Rollback | 协议 capability 可关闭；DB expand 兼容；Connector / Server 可回滚 |

# 30. 分阶段实施计划（Gate 0–3）

## 30.1 Gate 0：经营基线与协议冻结（3–4 周）

| 工作包 | 交付物 | 退出条件 |

|---|---|---|

| 业务基线 | 选定一个至少 3 角色工作流；4–6 周历史周期/等待/返工/质量 | 问题足够大且可测 |

| Task Contract | 冻结目标、输入、输出、责任、风险、人工门禁和价值指标 | Owner 签字 |

| Contract v1 | Envelope、Request、Receipt、Delivery、状态机、错误码、Schema | Golden Fixture 通过 |

| 架构边界 | Connector / Local Gateway / Remote Server / PG / NATS 职责 | 无跨层依赖 |

| 安全模型 | 身份、委托、Policy、Resource Ref、Access Grant、Threat Model | 安全评审通过 |

| 试点设计 | 30–80 名员工、2–4 系统、对照/个人 Agent/Agent 网络分组 | 数据授权可用 |

## 30.2 Gate 1：只读优先的内部 A2A（8–12 周）

首版最小能力：

- Remote Server / Connector / Local Gateway 的 `collaboration.*` capability；
- Conversation、Task Request、Receipt、离线队列、TTL、Cursor、状态投影；
- 双身份、Purpose-bound Delegation、receiver-side Policy；
- Work Item、结构化交付、Evidence；
- Capability Resolver 的静态/治理目录；
- 人工接管；
- H5/PWA 与 Android 状态展示；
- 成本、质量、延迟和经营指标。

Gate 1 不做高风险自动执行、外部发送、自动发布 Skill、大规模历史迁移和复杂管理驾驶舱。

## 30.3 Gate 1 工程拆解

| Sprint | 重点 | 主要验收 |

|---|---|---|

| S1 | Domain model、Schema、PG migration、Outbox | 状态机和幂等单测 |

| S2 | Collaboration API / Gateway、Policy integration | 跨 Tenant / Purpose 拒绝 |

| S3 | Connector WSS、SQLite Inbox/Outbox/Cursor | 崩溃恢复和重投 |

| S4 | Local Gateway RPC、Agent receipt / accept | Runtime generation 与 capability 门禁 |

| S5 | Workbench thread、状态、接管、离线 UX | 状态不混淆、恢复一致 |

| S6 | 试点系统 Data Product、Evidence、运营指标 | 真实流程 E2E |

## 30.4 Gate 2：低风险 Action 与协作（8–12 周）

开放创建待办、更新明确字段、发起审批、提交草稿、请求材料、将多个交付汇入同一 Work Item。每个 Action 具备最小权限、幂等、状态机、超时、人工确认、结果回执、补偿/回滚、审计、Owner 和版本。协作链强制最大跳数、轮次、时长、并行和预算。

## 30.5 Gate 3：知识与 Skill 复用（12–16 周）

只将多个真实 Work Item 中重复出现、结果稳定、边界明确的方法升级为 Task Contract Template、Knowledge 或 Skill。发布前验证权限、来源、异常、成本、接管、回归和上一稳定版本。Evolution Agent 只提出候选，不能直接发布或扩大权限。

## 30.6 GO / CONDITIONAL / HOLD / STOP

| 结论 | 条件 | 动作 |

|---|---|---|

| GO | 周期/等待改善，质量与风险合格，单位经济性可扩展 | 扩大相邻工作流 |

| CONDITIONAL | 局部价值明确，但数据、成本或流程可修复 | 限时修复后复测 |

| HOLD | 结果不稳定或承接能力不足 | 暂停扩张，保留底座 |

| STOP | 无可测价值、风险越界或复核成本吃掉收益 | 关闭 capability、回滚并复盘 |

# 31. 首个试点：跨境订单非标交付可行性评估

## 31.1 场景选择

本试点是“项目交付协同”的跨境业务子场景：客户提出非标准产品、数量、包装、交期或价格要求，销售需要产品、供应链、物流和财务在有限时间内给出可验证结论，并由有权人员形成客户承诺。

它满足首个 A2A 试点条件：高频、跨 3 个以上角色、跨多个系统、等待和反复确认明显、结果与周期可测、只读能力即可先产生价值、外部承诺可保留人工门禁。

## 31.2 参与者

| 角色 | Human + Agent | 主要 Capability |

|---|---|---|

| 销售 | 销售员工 + Sales Work Agent | 需求结构化、客户上下文、创建 Work Item、汇总方案 |

| 产品/技术 | 产品员工 + Product Work Agent | 产品 Claim、版本替代、技术边界 |

| 供应链 | 供应链员工 + Supply Work Agent | 库存、产能、物料、质检和交期 |

| 物流 | 物流员工 + Logistics Work Agent | 装柜、船期、到港和尾程风险 |

| 财务 | 财务员工 + Finance Work Agent | 贡献毛利、现金、费用和报价底线 |

| 销售负责人 | Human Owner | 客户承诺、价格和交期最终确认 |

## 31.3 Task Contract v1 示例

```yaml
task_contract_id: nonstandard-delivery-review
version: 1.0.0
objective: 在客户回复时限内形成可审计的非标交付方案草案
business_object: sales.order_request
inputs:
  - customer_requirement_ref
  - product_sku_and_version_refs
  - requested_quantity
  - requested_delivery_date
  - destination_and_channel
required_deliveries:
  product: product-boundary-result.v1
  supply: supply-feasibility-result.v1
  logistics: logistics-feasibility-result.v1
  finance: contribution-margin-result.v1
human_gates:
  - before_external_delivery_commitment
  - before_price_exception
constraints:
  maximum_hops: 2
  maximum_rounds: 4
  maximum_parallel_tasks: 4
  ttl: PT24H
  action_risk_ceiling: R1
acceptance:
  - 所有结论具备 Evidence Reference
  - 明确可承诺、条件可承诺或不可承诺
  - 列出开放问题、替代方案和风险
  - 客户承诺由销售负责人确认
```

## 31.4 端到端旅程

1. Sales Agent 把客户自然语言转成 Task Contract 输入草稿，销售员工确认；
2. 创建 Work Item 和三个/四个并行 Collaboration Request；
3. Product / Supply / Logistics / Finance Agent 分别使用自己的身份读取 Data Product；
4. 无权资源生成 Access Request，不允许销售 Agent 转发正文；
5. 每个 Agent 提交结构化 Delivery 和 Evidence；
6. Sales Agent 汇总冲突、缺口和替代方案；
7. 若结论冲突、数据过期或预算耗尽，进入 NEEDS_HUMAN；
8. 销售负责人查看 Commit Preview，确认价格和交期；
9. 结果写回 Work Item，并由正式 Action 更新 CRM / ERP 或生成客户回复；
10. Outcome 与后续实际交付结果回流，形成 Evidence 和 Skill Candidate。

## 31.5 Gate 1 只读边界

Gate 1 只允许：读取授权数据、生成分析、创建 Hermes Work Item / Request、提交结构化交付物、人工确认和导出草稿。不得自动更新 ERP 订单、自动报价、自动发信或承诺交期。

## 31.6 试点指标

| 指标 | 定义 | 建议比较 |

|---|---|---|

| 责任人匹配时间 | 需求出现到正确角色/Agent 接受 | 历史中位数 vs Agent 网络组 |

| 交接时延 | 请求发出到输入完整接受 | 个人 Agent 组 vs Agent 网络组 |

| 端到端周期 | Work Item 创建到正式方案确认 | 对照组 vs 网络组 |

| 一次补齐率 | 无需额外人工追问即可处理的请求比例 | 按 request type |

| 有效协作率 | 达标关闭 Request / 已接受 Request | 周度 |

| 人工接管率 | 进入 NEEDS_HUMAN 或人工接管的比例 | 按原因码 |

| 错误率 | 经确认的实质错误或错误承诺 | 必须设上限 |

| 异步完成率 | 无需同步会议完成的跨角色请求 | 工作时段/跨时区 |

| 每结果消息成本 | 模型+消息+人工注意力 / 有效关闭 Request | 防止消息裂变 |

| Evidence 完整率 | 满足来源、版本、权限、确认字段的交付 | 必须接近 100% |

# 32. 已冻结、需新增冻结与待决事项

## 32.1 不得改动的已冻结边界

- Mac Hermes Runtime 与 SessionDB 仍是本地会话权威；
- Observer 与 Control 是分离角色，最多一个显式远程控制租约；
- UNKNOWN / OUTCOME_UNKNOWN 不自动重发可能产生业务效果的动作；
- Remote Server 不是第二个 Agent；
- Connector 独立 Python 服务，不导入 Agent internals、不读 SessionDB、不直连 NATS / PG / Redis；
- Connector / Local Gateway / Cloud API 是相邻协议，不能跨层依赖内部实现；
- `a2a.message` 当前 reserved/effect=none；
- 4200–4219 已归 Mobile Control v1；
- Agent 输出顺序、稳定 key 和移动端 Inspect + Operate 语义沿用 Output Parity Contract。

## 32.2 本设计建议新增冻结

1. Hermes Collaboration Contract v1 公共信封；
2. Conversation / Request / Delivery / Receipt / Status 的 JSON Schema；
3. `collaboration.*` WSS message types 与 capability negotiation；
4. Request、Delivery、Work Item 状态机与合法转换；
5. 幂等作用域、payload canonicalization 和 durable ACK 清理规则；
6. Resource Reference、Access Request / Grant 和接收方重新鉴权；
7. Agent Capability Profile 与请求者视角裁剪；
8. Local Gateway RPC 与 Connector SQLite 表；
9. 4300–4349 错误码范围；
10. 技术 SLO、试点指标和 Gate 1 上线门禁。

## 32.3 待决事项

| 事项 | 需要决策者 | 进入生产前要求 |

|---|---|---|

| Connector Protocol 版本号与字段放置 | 协议 Owner | Schema + compatibility fixture |

| 根未知字段 vs ignore 规则 | 协议 Owner / 安全 | 冲突正式收敛 |

| Agent Capability Profile 是独立 Registry 还是 Bootstrap 投影 | 架构 Owner | 事实归属与缓存策略 |

| Business Approval Service 与本地 pending input 的适配 | 产品 / 业务 / 控制协议 Owner | 不混淆事实源 |

| 默认 TTL、轮次、跳数、预算 | 业务 Owner / Agent Ops | 按试点数据校准 |

| Restricted 数据的 E2EE 模式 | 安全 / 客户 | DLP、审计和恢复方案 |

| 外部 A2A 标准和 Federation Gateway | 后续阶段 | 内部 E2/E3 证据成立后再启动 |

# 附录 A：完整 Request / Delivery 示例

```json
{
  "contract": "hermes.collaboration",
  "contract_version": "1.0",
  "message_id": "msg_01J0A2...",
  "message_type": "task_request",
  "idempotency_key": "sales-SO-2026-001-delivery-review-v1",
  "tenant_id": "ten_company_cn",
  "realm_id": "cn-mainland-1",
  "workspace_id": "wsp_customer_delivery",
  "thread_id": "thr_SO-2026-001",
  "request_id": "crq_SO-2026-001_supply",
  "work_item_id": "wrk_SO-2026-001_review",
  "sender": {
    "type": "agent",
    "id": "agt_sales_jason",
    "on_behalf_of": "usr_jason",
    "device_id": "dev_mac_jason",
    "runtime_generation": "rtg_20260731_01"
  },
  "recipients": [
    {"type": "agent", "id": "agt_supply_owner", "owner_id": "usr_supply_owner"}
  ],
  "authority": "request",
  "purpose": "customer_delivery",
  "classification": "confidential",
  "delegation_ref": "dlg_sales_delivery_01",
  "policy_context_version": "pcv_109",
  "sequence": 108,
  "sent_at": "2026-07-31T02:00:00Z",
  "expires_at": "2026-08-01T02:00:00Z",
  "payload_hash": "sha256:5d...",
  "payload": {
    "task_type": "delivery_feasibility_review",
    "objective": "评估 500 套 SOFA-001 在 2026-09-15 前交付的可行性",
    "input_refs": [
      {
        "resource_id": "order://SO-2026-001/requirements",
        "version": "18",
        "acl_ref": "acl_order_01",
        "required_freshness": "PT15M"
      }
    ],
    "expected_output": {"schema": "schema://delivery/feasibility-result/v1"},
    "acceptance_criteria": [
      "最早可承诺日期",
      "可交付数量",
      "物料、产能、质检和物流风险",
      "Evidence Reference"
    ],
    "human_gate": "required_before_external_commitment",
    "reply_policy": {
      "max_agent_turns": 4,
      "max_hops": 2,
      "max_parallel_tasks": 3,
      "allow_partial": true,
      "no_progress_rounds": 2
    }
  },
  "extensions": {}
}
```

# 附录 B：SQL DDL 草案（非最终 Migration）

```sql
CREATE TABLE collaboration_requests (
  tenant_id uuid NOT NULL,
  request_id uuid NOT NULL,
  request_revision integer NOT NULL DEFAULT 1,
  thread_id uuid NOT NULL,
  work_item_id uuid,
  sender_agent_id uuid NOT NULL,
  sender_human_id uuid NOT NULL,
  receiver_agent_id uuid NOT NULL,
  receiver_human_id uuid NOT NULL,
  purpose text NOT NULL,
  classification text NOT NULL,
  state text NOT NULL,
  authority text NOT NULL,
  objective text NOT NULL,
  expected_output_schema text NOT NULL,
  human_gate text,
  max_rounds integer NOT NULL,
  max_hops integer NOT NULL,
  max_parallel_tasks integer NOT NULL,
  token_budget bigint,
  deadline_at timestamptz,
  expires_at timestamptz NOT NULL,
  policy_context_version text NOT NULL,
  bootstrap_manifest_id uuid NOT NULL,
  resource_version bigint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, request_id),
  CHECK (max_rounds >= 0 AND max_hops >= 0 AND max_parallel_tasks >= 0)
);

CREATE TABLE collaboration_messages (
  tenant_id uuid NOT NULL,
  message_id uuid NOT NULL,
  request_id uuid,
  thread_id uuid NOT NULL,
  sender_agent_id uuid NOT NULL,
  sender_human_id uuid NOT NULL,
  message_type text NOT NULL,
  authority text NOT NULL,
  idempotency_key text NOT NULL,
  purpose text NOT NULL,
  classification text NOT NULL,
  payload_ref text,
  payload_hash text NOT NULL,
  sent_at timestamptz NOT NULL,
  expires_at timestamptz,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, message_id),
  UNIQUE (tenant_id, sender_agent_id, message_type, idempotency_key)
);
```

正式 Migration 必须使用项目既定 ORM / migration 机制，不能把本附录直接复制进生产。

# 附录 C：事件目录

| 事件 | 生产者 | 消费者示例 |

|---|---|---|

| collaboration.message.accepted | Collaboration Gateway | Workbench Projection、Outbox Ops |

| collaboration.message.queued | Delivery Service | Sender UI、SLA Monitor |

| collaboration.message.delivered | Receipt Processor | Request State Projector |

| collaboration.message.received | Agent Receipt | Sender UI、Metrics |

| collaboration.request.state_changed | Collaboration Service | Work Item、Audit、Notification |

| collaboration.request.needs_owner | Policy/Autonomy Gate | Owner Inbox |

| collaboration.request.needs_human | Loop/Conflict/Action Gate | Human Takeover UI |

| collaboration.delivery.created | Receiver Agent / Service | Verifier、Evidence |

| collaboration.request.expired | TTL Worker | Sender/Receiver Notification |

| collaboration.message.dead_lettered | Delivery Worker | Agent Ops / Security |

# 附录 D：上线前机械检查清单

- [ ] 扫描现有错误码，确认 4300–4349 无碰撞；
- [ ] 扫描 Connector registered message types，确认 `collaboration.*` 不与 reserved 类型冲突；
- [ ] 生成 JSON Schema / OpenAPI / AsyncAPI / Golden Fixture；
- [ ] 同一 payload 在 Python、TypeScript、Kotlin 中 canonical hash 一致；
- [ ] 所有状态转换有单测和属性测试；
- [ ] PostgreSQL Outbox / JetStream / SQLite Inbox/Outbox 崩溃恢复通过；
- [ ] 断线、重连、Agent runtime generation 变化、Manifest 撤销 E2E；
- [ ] 跨 Tenant / Workspace / Purpose / Agent 越权测试通过；
- [ ] Personal Memory、原始敏感正文、lease ID、密钥不进入日志；
- [ ] H5 / Android 真实状态不混淆；
- [ ] capability 关闭可立即退回 observe-only / no-collaboration；
- [ ] 数据删除和 Access Grant 撤销传播演练；
- [ ] 试点基线、对照组和停止条件已冻结。

# 附录 E：来源映射

| 本文件主题 | 主要来源章节 |

|---|---|

| 员工 Agent 协作模型、双身份、三类载体 | 企业工作模式设计 §15.5–15.11 |

| Request 状态机、接收规则、循环控制 | 企业工作模式设计 §15.12–15.16 |

| 服务端路由、在线/离线、Receipt、WSS 类型 | 企业工作模式设计 §15.20–15.32 |

| Bootstrap、Role Package、自主等级 | 企业工作模式设计 §8.8–8.18 |

| Action 风险、Prepare/Commit、Outcome Unknown | 企业工作模式设计 §11.1–11.7 |

| 引用优先、Access Grant、派生 ACL | 数据治理设计 §3.2–3.4、§11 |

| Connector / NATS / PG / SQLite 边界 | Connector 商用架构 §5–8、architecture.md |

| 移动控制租约、幂等、错误码占用 | Mobile Control Contract v1 |

| 移动端语义与事件顺序 | Output Parity Contract v1 |

| Gate、试点、经营指标 | 投资论证 §7–9 |

| 横向 Work Agent 与纵向能力沉淀 | 老板经营心智 §13 |

# 附录 F：最终结论

Hermes 的员工 A2A 必须同时具备两种能力：一是 Agent 与 Agent 能够真实交流、澄清、讨论和异步推进工作；二是企业能够明确区分“聊天、请求、任务、动作、承诺和证据”。

真正的落地路径不是先建设一个无限 Agent 群聊，而是先冻结稳定的 Collaboration Contract，建立身份、委托、Policy、Request、Work Item、Receipt、Resource Reference、Evidence 和人工门禁，再用一个高频跨角色试点验证 Agent 网络是否产生超出个人 Agent 的增量价值。
