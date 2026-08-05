# Hermes Agent Plugin 与 Connector 设计文档

跨 Plugin、Connector、Cloud 和 Android 的唯一执行状态源位于仓库根
[`docs/plans/feature-commercial-connector-cloud-1.md`](../../docs/plans/feature-commercial-connector-cloud-1.md)。
本目录保留 Plugin 与 Connector 的组件边界、设计和验收证据，不维护第二份跨仓
执行计划。

- 文档状态：目标架构基线
- 基线版本：1.0
- 更新日期：2026-07-30
- 适用范围：Hermes Agent Plugin、Hermes Connector、Hermes Remote Server、
  H5/PWA、Android 参考客户端

## 1. 阅读入口

| 文档 | 回答的问题 | 状态 |
|---|---|---|
| [01 产品边界与责任](01-product-boundary-and-responsibilities.md) | Connector 是什么、不是什么，当前插件与目标 Connector 如何分工 | 规范 |
| [02 目标架构](02-target-architecture.md) | 端、云、Agent 如何部署和协作，模块如何拆分 | 规范 |
| [03 协议与可靠性](03-protocols-and-reliability.md) | 三层协议、消息信封、命令状态、幂等、断线恢复如何工作 | 规范 |
| [04 Agent 集成与升级](04-agent-integration-and-upgrade.md) | 如何避免 Hermes Agent 更新破坏 Connector | 规范 |
| [05 安全与数据治理](05-security-and-data-governance.md) | 身份、权限、密钥、内容、文件和企业数据如何治理 | 规范 |
| [06 部署与运维](06-deployment-observability-and-operations.md) | 如何在阿里云商用部署、监控、升级、回滚和灾备 | 规范 |
| [07 交付路线与验收](07-delivery-roadmap-and-acceptance.md) | 当前差距、实施顺序、测试矩阵和上线门禁 | 执行基线 |
| [08 企业协作扩展](08-enterprise-extension-and-agent-collaboration.md) | 员工专属 Agent、Agent-to-Agent 和双层进化如何使用 Connector | 扩展规范 |
| [09 Agent Plugin 详细设计](09-agent-plugin-detailed-design.md) | Plugin 内部模块、Host SPI、Observer、Control、租约和生命周期 | 实现级规范 |
| [10 Connector 详细设计](10-hermes-connector-detailed-design.md) | Connector 模块、SQLite、设备、WSS、对账、更新和诊断 | 实现级规范 |
| [11 Local/Cloud 数据协议](11-local-and-cloud-data-protocols.md) | Local JSON-RPC、Connector WSS、信封、状态、错误和版本 | 协议规范 |
| [12 功能责任边界矩阵](12-feature-and-responsibility-boundary-matrix.md) | 全量功能归属、事实、存储、安装、失败和依赖边界 | 规范 |
| [13 全链路逻辑](13-end-to-end-logical-flows.md) | 安装、启动、配对、观察、控制、更新、恢复和删除时序 | 实现级规范 |
| [14 软件架构与工程约束](14-software-architecture-and-engineering-constraints.md) | 代码分层、依赖方向、进程模型、技术基线、测试门禁和演进条件 | 实现级规范 |
| [15 Hermes 0.19 Host SPI 阻断与 v1 Core 契约](15-hermes-019-host-spi-gap-and-v1-core-contract.md) | 真实 0.19 公共 SPI 的硬阻断、fail-closed 规则和 Hermes Core 最小改造接口 | 阻断事实与 Core 提案 |

架构决策记录：

| ADR | 决策 |
|---|---|
| [ADR-0001](adr/0001-separate-plugin-connector-and-agent-runtimes.md) | Agent Plugin、Connector、Agent 使用独立运行与发布单元 |
| [ADR-0002](adr/0002-hermes-wss-outside-nats-inside.md) | 对外使用 Hermes WSS，NATS 只属于 Remote Server 内部 |
| [ADR-0003](adr/0003-python-with-contract-boundaries.md) | Connector 使用 Python，但不与 Agent 共享内部模块 |
| [ADR-0004](adr/0004-postgresql-nats-sqlite-and-optional-redis.md) | PostgreSQL、NATS、SQLite 分工，Redis 不进入核心事实链路 |
| [ADR-0005](adr/0005-one-installer-two-runtime-units.md) | 用户一次安装，内部保持 Plugin 与 Connector 独立 |
| [ADR-0006](adr/0006-hexagonal-edge-and-modular-monolith.md) | Plugin/Connector 使用六边形边界，Server 首期采用模块化单体 |

## 2. 文档权威顺序

发生表述冲突时按以下顺序处理：

1. 已发布的版本化 JSON Schema、错误码和 Golden Fixture；
2. 本目录中的协议、安全、兼容和工程约束；
3. 已接受 ADR；
4. 本目录中的目标架构和实施路线；
5. 父项目的企业工作台、数据治理和经营规划文档；
6. README、示例和未标记为规范的说明。

当前 Mobile Control v1 的已实现方法和错误码，以
`src/hermes_agent_plugin/contracts/generated/mobile-control-v1.json`、根
`/contracts` 权威源、代码常量和兼容测试共同约束。旧包不再保存 Contract
副本；契约同步只写入 canonical generated 路径。文档不能仅凭“协议已保留方法名”
宣称功能已经可用。

## 3. 统一术语

| 术语 | 定义 |
|---|---|
| Hermes Agent | 唯一权威的本地执行运行时；拥有本地会话和执行事实 |
| Agent Plugin | 通过稳定 Host API 安装到 Agent 的薄适配层 |
| Local Gateway | Agent 对 Connector 暴露的本地版本化协议边界 |
| Hermes Connector | 独立常驻服务；连接 Local Gateway 与 Remote Server |
| Remote Server | 公开云入口、设备控制面、命令事实、投影和企业协作服务 |
| H5/PWA | 主要跨平台用户端；不直接连接本地 Dashboard |
| Observer | 只读会话观察角色 |
| Controller | 持有显式控制租约的唯一远程控制实例 |
| Runtime Generation | Agent 每次有效运行实例的代次；重启后变化 |
| Durable Session Key | 跨运行代次引用同一会话谱系的稳定键 |
| Runtime Session ID | 当前运行实例中的会话 ID，不能替代 Durable Session Key |
| Command Fact | Server 中命令生命周期的权威记录 |
| Session Fact | Agent 中会话与执行结果的权威记录 |
| Projection | 为跨端读取生成的有保留期、可删除、非权威副本 |

## 4. 不可破坏约束

1. 只有一个权威 Hermes Agent 执行运行时。
2. 可以有多个 Observer，但同一实时会话最多一个显式远程 Controller。
3. Connector 不接管或替换 Agent owner transport。
4. Connector 不读取 Agent SessionDB，不导入 Agent 私有模块。
5. Connector 只通过出站 TLS 443 连接 Remote Server，不暴露本机公网入口。
6. Agent、Plugin、Connector、Server、H5 使用独立版本和发布列车。
7. PostgreSQL 保存云端业务事实，Agent 保存会话事实，消息系统不充当最终事实源。
8. 系统采用“至少一次投递、业务效果幂等”，`UNKNOWN` 不自动重试。
9. Connector 不直连 NATS、Redis、PostgreSQL、OSS 或 KMS。
10. 秘密、sudo 密码和终端敏感输入不以 Cloud 可读明文持久化。
11. 企业数据按 Tenant、Workspace、Purpose、分类和来源 ACL 重新鉴权。
12. 员工 Personal Memory 不进入公司级 Agent 网络或进化循环。
13. 用户只需要一个安装入口；Agent Host 与 Connector Updater 通过明确所有权协作，
    不允许两个更新器同时直接改写 Plugin 文件。
14. 跨运行单元只共享版本化协议、Schema、Fixture 和公共 SDK，不共享内部模块、
    ORM Model、数据库表或依赖环境。
15. Plugin、Connector 和 Server 必须遵守向内依赖的六边形边界；Remote Server
    首期采用 Gateway、模块化 Business API 和 Async Worker，拆分微服务必须有
    容量、发布或故障隔离证据。

## 5. 当前实现标签

文档使用以下标签区分事实与规划：

- **[CURRENT]**：当前目录代码中已经存在且有测试约束。
- **[TARGET-V1]**：商用 Connector v1 必须实现。
- **[FUTURE]**：企业协作或规模化扩展，不进入 Connector v1 首发阻断路径。
- **[PROHIBITED]**：即使实现方便也不允许采用的做法。

任何实施 PR 都应同步更新相应文档的标签、兼容矩阵和验收证据。
