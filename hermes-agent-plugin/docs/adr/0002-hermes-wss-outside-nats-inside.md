# ADR-0002：外部使用 Hermes WSS，NATS 只用于服务端内部

- 状态：Accepted
- 日期：2026-07-30

## 背景

Connector 需要稳定连接 Remote Server。直接让 Connector 连接 NATS/MQTT 看似减少
一层 Gateway，但会把 Broker 认证、Subject、集群拓扑和运维策略暴露到用户设备。

## 决策

- Connector 只连接 Hermes 自有 WSS Connector Gateway；
- Gateway 负责设备认证、协议协商、限流、背压和外部/内部消息转换；
- NATS Core/JetStream 只属于 Remote Server 内部；
- Agent 和 Connector 不持有 NATS 凭据；
- NATS Subject 不进入外部协议或客户端代码。

## 后果

优点：

- Hermes 拥有外部协议和兼容窗口；
- Server 可替换或调整消息基础设施而不升级设备；
- Tenant、设备、订阅和安全策略集中执行；
- 浏览器、Connector 和内部服务使用适合各自的协议。

代价：

- 需要维护 Connector Gateway；
- Gateway 需要连接索引、drain 和重连风暴治理；
- 必须建立 WSS 与内部事件的明确状态映射。

## 被拒绝方案

- Connector 直连 NATS：泄露内部拓扑和凭据；
- Connector 直连 Redis Streams：事实、缓存和交付职责混杂；
- 设备间 P2P：无法统一离线、权限、审计和版本隔离。
