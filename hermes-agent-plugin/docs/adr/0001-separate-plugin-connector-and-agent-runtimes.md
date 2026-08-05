# ADR-0001：Agent Plugin、Connector 与 Agent 独立运行

- 状态：Accepted
- 日期：2026-07-30

## 背景

Hermes Agent 会持续升级。若 Connector 直接导入 Agent 私有模块、共享虚拟环境或
读取 SessionDB，任何 Agent 内部变动都可能使远程能力失效，并迫使三者同步发布。

## 决策

- Agent Plugin 通过稳定 Host SPI 适配 Agent；
- Connector 是独立 Python 进程、环境、状态目录和安装包；
- Plugin 与 Connector 通过 Local Gateway Protocol；
- Connector 与 Cloud 通过 Connector Protocol；
- Agent、Plugin、Connector、Server 独立版本、灰度和回滚。

## 后果

优点：

- Agent 更新不会天然要求 Connector 更新；
- Connector 故障不影响本地 Agent；
- 安全权限、资源和发布责任更清晰；
- 可以独立诊断和回滚。

代价：

- 需要维护两个版本化协议；
- 需要 IPC、发现、对账和兼容矩阵；
- 同源 Python 类型不能直接跨进程共享，必须形成 Schema/Fixture。

## 被拒绝方案

- Connector 作为 Agent 内部线程：故障和升级强耦合；
- Connector 直接读取 SessionDB：破坏事实和 Schema 边界；
- 共享 venv 和依赖锁：一方升级可破坏另一方；
- 把 Dashboard 暴露到公网：缺乏设备、命令和商用治理边界。
