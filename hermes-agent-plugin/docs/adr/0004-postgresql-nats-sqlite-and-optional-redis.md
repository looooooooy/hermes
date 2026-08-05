# ADR-0004：PostgreSQL、NATS、SQLite 分工，Redis 可选

- 状态：Accepted
- 日期：2026-07-30

## 背景

系统需要命令事实、在线路由、可恢复交付、本地离线和缓存。使用单一中间件承担所有
职责会使故障恢复、幂等和数据治理复杂化。

## 决策

- PostgreSQL 保存 Server 端命令、设备、协作、审计等业务事实；
- NATS Core 传输可快照恢复的在线流；
- JetStream 承载需要 ACK、重放和有界保留的交付；
- Connector SQLite 保存本地 Inbox、Outbox 和 cursor；
- Redis 只在有实测需求时用于可丢缓存、限流、Presence、短锁或路由加速；
- Connector 不直连 PostgreSQL、NATS 或 Redis。

## 后果

优点：

- 每类状态有明确权威来源；
- 事务 Outbox 可恢复数据库与消息之间的边界；
- Connector 离线和崩溃可本地对账；
- Redis 故障不会丢失正式命令或授权事实。

代价：

- 需要实现 Outbox、Consumer 幂等和对账；
- 运维 PostgreSQL、NATS 和本地 SQLite 三类状态；
- 需要明确哪些事件可丢、哪些必须持久。

## 被拒绝方案

- Redis Streams 统一命令、审计和缓存：恢复与保留职责混乱；
- JetStream 作为最终业务数据库：难以承担事务查询和长期治理；
- Connector 仅内存队列：断电或升级后无法安全恢复；
- 首期同时部署 Kafka、Redis、NATS：维护成本高且无负载证据。
