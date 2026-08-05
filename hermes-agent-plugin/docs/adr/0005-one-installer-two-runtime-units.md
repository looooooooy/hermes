# ADR-0005：一个安装入口，两个独立运行单元

- 状态：Accepted
- 日期：2026-07-30

## 背景

Agent Plugin 与 Connector 都部署在用户本地。如果要求用户分别安装、配置、升级和
排障，会显著增加推广成本；如果为了安装方便把两者合并进 Agent，又会破坏独立升级
和故障隔离。

## 决策

- 用户只下载和运行一个 Hermes Connector 签名安装器；
- 安装器同时携带 Connector Runtime 和签名 Agent Plugin Bundle；
- Connector 作为独立 OS 服务运行；
- Plugin 由 Agent Host Extension Manager 安装、验证和加载；
- Plugin 不写入 Agent 应用包、源码目录或 venv；
- Connector Updater 负责下载兼容发布集合，Agent Host 负责 Plugin 最终激活；
- Agent、Plugin、Connector 仍保持独立版本、健康检查和回滚；
- Agent 未安装时允许 Connector 安装完成并进入等待状态。

## 后果

优点：

- 用户只有一次安装、一次配对和统一 Repair；
- 企业可以通过 MDM/软件分发批量部署；
- Agent 更新不覆盖 Plugin Store 或 Connector；
- Plugin/Connector 失败不阻止 Agent 本地使用；
- 兼容组合可以自动检查和回滚。

代价：

- Agent 必须实现稳定 Extension Manager；
- 安装器需要协调两个槽位和两个健康检查；
- 必须避免 Agent Updater 与 Connector Updater 的双写；
- 需要处理 Agent 缺失、运行中任务和需要重启的激活窗口。

## 被拒绝方案

- 两个独立安装器：用户成本和版本错配高；
- 把 Connector 嵌入 Agent：网络、状态和更新故障强耦合；
- 直接把 Plugin wheel 安装进 Agent venv：依赖冲突且容易被更新覆盖；
- 安装器直接复制到 Agent 包目录：破坏签名、权限和升级所有权。
