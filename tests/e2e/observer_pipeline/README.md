# Observer 三模块真实链路验收边界

本目录验证 `Plugin → Connector → Cloud → Business WebSocket` Observer 链路。Plugin
本地端点、Connector AF_UNIX/WebSocket 客户端、状态机、SQLite outbox，以及 Cloud
ASGI、ORM、订阅路由、投影和 SQLite 均使用生产实现与真实 I/O。

唯一协议替身是未来 `gateway-extension/1` 的 Host。Hermes 0.19 尚未提供该 SPI，
因此测试通过不代表已经接入真实 Hermes Core，也不得作为真实 Hermes 集成声明。
替身严格执行 `prepare → 写出 snapshot 响应 → activate → close` 两阶段契约。

Observer v2 场景中，Host 替身精确声明 `gateway-extension/1` 与
`session.observe.output-parity.v1`，其余 Plugin relay、Connector UDS/WSS/SQLite、
Cloud WSS/ORM/SQLite 和 Business ASGI 均为生产实现。场景验证 `.v2` open、snapshot、
event、ACK、gap NACK 与替换 snapshot；初始 snapshot 同时包含 todo、subagent、tool、
terminal，并覆盖 replay 与 live 生命周期事件。替换 snapshot ACK 未完成时，后续事件
不得发布；ACK 后 Cloud 才接受该事件，Business v2 订阅按 snapshot 基线加 replay 恢复。

v2 安全注入分别将 Basic Authorization 和 JWT 放入两个 Host 私有 extension。生产 Plugin
在最早投影边界 fail closed，测试确认 Connector 不创建 outbox、Cloud ORM 不新增记录、
Business WebSocket 不广播，且 Connector/Cloud SQLite 文件均不含凭证明文。Web 与
Android 的 generated-resource 测试同时校验 v2 realtime、输出同构策略和 session-event
schema 与根合同完全一致。这是协议资源消费证据，不代表真实浏览器、真实 Android 设备
或真实 Hermes 0.19 集成。

当前绿色证据覆盖 snapshot、连续 live event、Cloud ACK、同目标多客户端聚合、不同
目标独立订阅、最后一个观察者关闭、真实 gap NACK、下一 Connector transport sequence
的替换 snapshot、中文 UTF-8 canonical digest、临时 keyring/SQLite 和资源回收。

重连场景使用 Gateway 唯一持久 transport cursor 验证固定设备、Connector instance、
runtime generation、上一连接和双向 cursor 的精确恢复；真实传输断开后不会重复写入
Observer projection。完全相同的持久双向 cursor 才返回 `resumed`；同一 Connector
instance 和 runtime generation 发生连接或 cursor 偏差时返回 `reset_required`，并从
Cloud 已提交的 authoritative pair 重放 pending outbox；instance 或 generation 变化才
从新 epoch `(0, 0)` 开始。`reset_required` rewind 会重放 settled sequence 0；新
epoch 不重放旧 settled frame。resume hello 被判定为新 epoch 时，旧 epoch 的
hello/welcome 不计入新 cursor，Connector 与 Cloud 的首个 active frame 都从 0 开始。

故障注入还覆盖两个真实 post-send/pre-commit 崩溃窗口：Cloud 已发送 Observer ACK、
但未提交 cursor，以及 Cloud 已发送 durable observe-open intent、但未提交 cursor。
前者重连后重新处理相同 Observer fact，Cloud inbox、session/event projection 仍各自产生
一次业务效果；后者重放相同 intent，Connector 对 active target 只重新确保 cached
snapshot 已送达，复用 pending/sent-unacked 的原 `message_id` 和 Connector sequence，
不重复执行 Host prepare。最终 outbox 由精确 ACK 结算为 `acked`。

执行：

```text
hermes-cloud/.venv/bin/python -m pytest -q tests/e2e/observer_pipeline
```
