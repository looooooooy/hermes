# ADR-0006：六边形边缘服务与模块化单体

- 状态：Accepted
- 日期：2026-07-30

## 背景

Agent Plugin、Hermes Connector、Remote Server 和 H5/PWA 的故障模型、发布节奏和
数据所有权不同。如果全部合并到 Agent，会破坏独立升级和故障隔离；如果首期直接
拆成大量微服务，会在业务边界尚未稳定时引入部署、网络和分布式一致性成本。

系统还必须适应 Hermes Agent 持续升级，因此 Connector 不能通过 Python 内部模块
复用形成隐式耦合。

## 决策

- Plugin、Connector 和 Remote Business API 内部采用六边形依赖方向；
- Agent Plugin 是围绕稳定 Host SPI 的薄防腐层；
- Connector 是独立、单进程、模块化、有本地状态的边缘服务，使用显式状态机、
  结构化并发和 SQLite 单写者；
- Remote Server 首期采用 Connector Gateway、模块化 Business API、Async Worker
  三个主要部署单元；
- Business API 采用模块化单体，领域模块通过 Application Port 和 Domain Event
  协作；
- PostgreSQL 保存业务事实，Transactional Outbox 与 NATS/JetStream 负责可靠异步
  传播；
- H5/PWA 采用 React + TypeScript + Vite + Workbox 的特性切片架构；
- 所有跨运行单元交互遵循 Contract-first，不共享内部模型和数据库结构；
- 只有容量、团队、发布或故障隔离证据成立后才拆分微服务或引入新基础组件。

## 后果

优点：

- Agent 更新只需维持 Host SPI，不要求 Connector 跟随内部代码变化；
- Connector 可独立恢复、升级和回滚，不影响本地 Agent 使用；
- Server 首期部署和运维单元少，适合商用早期控制成本；
- 领域模块先形成清晰边界，未来可以渐进拆分；
- 协议、幂等、Inbox/Outbox 和状态机提供可测试的可靠性边界。

代价：

- 需要维护 DTO、Domain、Record 和 Projection 之间的显式映射；
- 需要建设架构依赖检查、Schema 生成和兼容测试；
- 模块化单体需要严格代码治理，否则可能退化为共享数据库的大单体；
- Connector 的单进程并发、SQLite 写入和关闭顺序需要故障注入验证。

## 被拒绝方案

- Plugin 与 Connector 合并进 Agent：升级、网络故障和依赖冲突互相放大；
- Connector 与 Agent 共享私有 Python 类型：版本升级形成隐式锁步；
- 首期全微服务：业务边界未稳定，分布式事务和运维成本过高；
- 完整 Event Sourcing：恢复、治理和调试复杂度高于首期收益；
- Connector 直接消费 NATS/Redis：暴露云内部拓扑并扩大本地攻击面；
- 仅为了常驻性能重写 Go/Rust：在无测量证据时增加语言和交付成本。

## 执行规范

详细的代码分层、依赖规则、数据写入、测试门禁和演进条件见
[14 软件架构与工程约束](../14-software-architecture-and-engineering-constraints.md)。
