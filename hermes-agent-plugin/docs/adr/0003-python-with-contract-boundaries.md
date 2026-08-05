# ADR-0003：Connector 使用 Python，但保持契约隔离

- 状态：Accepted
- 日期：2026-07-30

## 背景

Hermes Agent 已使用 Python。Connector 需要 WebSocket、SQLite、加密、跨平台系统
服务、协议模型和快速迭代，同时必须避免 Agent 升级造成强耦合。

## 决策

- Connector 使用 Python 3.11+；
- 可以复用经过发布的协议 Schema、Fixture 和独立公共库；
- 不导入 Agent 私有模块，不共享 Agent venv；
- 通过 Local Gateway Protocol 和 Connector Protocol 通信；
- Connector、Agent 和 Server 使用独立包与依赖锁；
- 性能热点经实测后可用 Rust/原生扩展，不先重写整个 Connector。

## 后果

优点：

- 团队和 AI/数据生态一致；
- 现有 WebSocket、Pydantic、SQLite、测试和可观测生态成熟；
- 迭代速度快，协议模型容易验证。

代价：

- 安装包需要管理 Python 运行时；
- 长连接和并发模型必须严格测试；
- 需要避免 GIL 阻塞、同步 I/O 混用和依赖供应链风险。

## 被拒绝方案

- 仅因常驻服务而改用 Go：增加双语言成本且不能自动解决协议耦合；
- 与 Agent 共享包内部类型：短期方便，长期破坏独立升级；
- Electron/Node 内置 Connector：与桌面 UI 生命周期和跨平台发布耦合。
