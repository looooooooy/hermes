# Hermes Core Host SPI v1 Patch Map

- 状态：Stage 3 隔离补丁已验证；真实 Plugin UDS opener 与安装仍待独立门禁/授权
- 审计目标：Hermes Core `0.19.0`
- 审计源码：`/Users/apple/.hermes/hermes-agent`
- 审计日期：2026-07-31

## 1. 目标和边界

Core 需要新增通用、版本化、可注销的 `gateway-extension/1` 窄腰，使独立 Plugin
可以观察会话并向唯一权威会话提交明确的 Owner Action。Core 不得出现 Hermes
Mobile、Android、Cloud 或当前 Plugin 的产品特判。

本改造必须保持：

- 一个权威 Agent runtime；
- 多客户端可观察；
- 一个会话最多一个 controller；
- Plugin 不获得 `AIAgent`、Runner、CLI 队列、ACP connection 或 SessionDB；
- 网络发送、队列写入和 hook 调用不等于业务效果成功；
- `effect_unknown` 不自动重放；
- 不记录 token、审批正文、工具参数、完整工具输出或秘密。

## 2. 新公共模块

新增 `hermes_cli/extension_host_v1.py`，仅包含冻结 DTO、枚举和 Protocol：

- `Registration`
- `GatewayExtensionV1`
- `GatewayExtensionHostV1`
- `RuntimeDescriptor`
- `EndpointDescriptor`
- `ObserverRequest` / `ObserverEvent` / `PreparedObserver`
- `OutputParitySnapshotV2` / `OutputParityEventV2`
- `ControlScope` / `ControlSnapshot`
- `OwnerActionRequest` / `OwnerActionResult` / `OwnerActionStatus`
- `SafeAuditEvent`

Host Facade v1 只公开：

```text
runtime_descriptor()
register_local_endpoint(...)
prepare_observer(...)
control_snapshot(...)
invoke_owner_action(...)
session_catalog(...)
add_session_catalog_listener(...)
add_runtime_listener(...)
audit(...)
```

`RuntimeDescriptor` 必须至少提供当前 `profile`、`runtime_generation` 与 Hermes Host
bundle ID，使 Plugin composition root 能在一次 runtime 启动中捕获一个共享的 macOS
Host authority。Plugin 由该 authority 统一派生 Local、Control、Observer 的 exact
filesystem discovery descriptor v2；Core 不得为三个 role 分别生成 PID、instance ID
或 generation。

discovery descriptor v2 的唯一字段集合为
`version/pid/profile/runtime_generation/socket_path/instance_id/process_start_time_ns/
process_executable/process_executable_device/process_executable_inode/host_bundle_id`。
Plugin 在任何目录、socket、线程副作用前，用 PID 对启动时间和可执行文件 canonical
path/device/inode 做 fail-closed 复核；三类 writer 必须使用同一 authority。Socket
device/inode 由 Connector 从实际 socket 推导并在连接前后复核，不进入 JSON。
`runtime.descriptor.v1` 是 Host SPI capability 名，不得据此把 filesystem descriptor
版本降为 v1。

新增 `hermes_cli/extension_runtime.py`，内部持有：

- `ExtensionRuntimeV1`
- `ExtensionHostFacadeV1`
- `SessionAuthorityRegistry`
- `OwnerActionRouter`
- `OwnerActionLedger`
- `ObserverBroker`
- `PendingInteractionRegistry`
- `ManagedRegistration`
- `InstallationScope`

Stage 3 另新增 `hermes_cli/extension_composition.py`、
`hermes_cli/extension_owner_runtime.py` 与 `hermes_cli/extension_output_parity.py`，分别持有
进程唯一 production composition、owner/catalog/pending runtime，以及 display-safe v2
权威 projection。全局 `PluginManager` 只接收这一份 Host facade。

每个 Owner Action 必须按
`profile + durable_session_key + runtime_generation + command_id` 精确绑定。
相同 command 和相同 digest 返回原结果；相同 command 不同 digest 拒绝。

## 3. Plugin Manager 改造

修改 `hermes_cli/plugins.py`：

| 位置 | 改造 |
|---|---|
| `PluginContext` | 增加只读 SPI version、capabilities 和 `register_gateway_extension(..., spi_version=1)` |
| `LoadedPlugin` | 保存 extension registrations 和隔离的 install error |
| `PluginManager.__init__` | 持有进程级 Extension runtime 与逆序 registration store |
| `discover_and_load(force=True)` | 清空 registry 前先幂等关闭旧 extension |
| `_load_plugin` | 使用安装事务；Plugin register 后续失败也要逆序回滚本 Plugin 资源 |
| 新公开方法 | `shutdown_extensions(plugin_key=None)`；必要时增加 `unload_plugin(plugin_key)` |

任一 Plugin 安装或关闭失败不得阻止 Hermes 主程序与其他 Plugin。

## 4. 权威运行面映射

| 运行面 | durable session key | 接入位置 | 关键限制 |
|---|---|---|---|
| CLI | `agent.session_id` | `hermes_cli/cli_agent_setup_mixin.py`、`cli.py`、`cli_commands_mixin.py` | new/resume/branch 原子 rebind；prompt 不得写私有 pending queue |
| TUI/Desktop | `session["session_key"]` | `tui_gateway/server.py`、`methods_session.py`、`methods_prompt.py` | 易失 `sid` 不能作为远程目标；Host 和 JSON-RPC handler 共用 protocol-neutral authority |
| Messaging Gateway | `SessionEntry.session_id` | `gateway/session.py`、`gateway/run.py` | 内部来源 routing key 不得外露；turn generation 不能冒充 runtime generation |
| ACP | `SessionState.session_id` | `acp_adapter/session.py`、`server.py`、`entry.py` | ACP 客户端已有控制权；P0 approval/clarify 冲突必须拒绝 |
| Cron/Worker | job 对应 Agent session id | `cron/scheduler.py` 和 CLI quiet process | 每个 job 只关闭自己的 session registration，不关闭进程级 endpoint |

Compute-host 的 frame 写成功不等于子进程已接受 turn。必须增加 `turn.accepted`
确认；在收到确认前返回 `effect_unknown`。未实现的 steer/approval/clarify 路径必须
明确 rejected。

## 5. Pending Interaction 与 Observer

Approval 和 Clarify 必须有不可伪造的 `pending_request_id`，并绑定 profile、
durable session、runtime generation 和当前 controller。

Observer 从 `hermes_cli.lifecycle` 向 Core broker 分流稳定白名单 DTO：

- 每个 `(runtime_generation, durable_session_key)` 独立单调 sequence；
- `open_observer` 只建立未激活订阅并返回权威 snapshot 与不透明
  `subscription_id`；在承载层确认 snapshot RPC response 写成功前，任何 pending/live
  event 都不得越过该 response；
- snapshot 写成功后，Host Adapter 必须用同一 transport 和
  `subscription_id` 调用 `activate_observer`；写失败、activation 失败或调用方消失时
  必须关闭 prepared subscription 和上游连接；
- prepared 阶段必须有一个总 deadline 和固定 pending event 上限，不能逐帧重置
  timeout 或使用无界缓存；
- snapshot/replay 必须从 `snapshot_event_sequence` 连续合并到
  `event_sequence`；live stale event 忽略，gap、跨 runtime、未知 v1 type 或非法合并
  range 必须关闭订阅并由新 snapshot 恢复，不能推进游标或猜测内容；
- subscription close 后零回调；
- 慢 sink、异常 sink 和越界 payload 互相隔离；
- 默认不投递 token、完整 approval、tool args、tool output 或未脱敏错误。

Stage 3 已把真实 CLI、TUI/Desktop、Messaging Gateway、ACP、Cron/Worker 回调分流到
冻结 v2 DTO，并只在 producer、projection 和 broker 同时存在时广告
`session.observe.output-parity.v1`。Core 在共享 authority 锁内分配 sequence，并在
prepared 阶段缓存事件；承载层仍必须在 snapshot/subscribe response 完整写成功后才调用
`PreparedObserver.activate()`。当前剩余边界是 Plugin 的真实 UDS opener、Connector client
和端到端验收，Core 不代写该 transport。

## 6. 严格 TDD 顺序

所有 Core 测试只能通过该仓约定的 `scripts/run_tests.sh` 执行。

1. `tests/hermes_cli/test_extension_host_v1_contract.py`
2. `tests/hermes_cli/test_gateway_extension_registration.py`
3. `tests/hermes_cli/test_extension_session_authority.py`
4. `tests/hermes_cli/test_extension_pending_interactions.py`
5. 各运行面真实 owner action 测试：
   - `tests/cli/test_extension_owner_actions.py`
   - `tests/tui_gateway/test_extension_owner_actions.py`
   - `tests/gateway/test_extension_owner_actions.py`
   - `tests/acp/test_extension_owner_actions.py`
   - `tests/cron/test_extension_owner_actions.py`
6. `tests/hermes_cli/test_extension_observer_broker.py`
   - snapshot response 写成功前零 event；
   - prepared activation/rollback、总 deadline、pending 上限；
   - snapshot/replay/live 连续游标、stale/gap/runtime rollover；
   - close 后上游连接和回调均为零；
7. CLI、Gateway、ACP、TUI、one-shot 的 shutdown 回归
8. `tests/integration/test_gateway_extension_v1_wheel.py`

当前补丁包将上述 Core contract/authority/observer/owner 行为集中在
`test_extension_host_v1_contract.py`、`test_extension_runtime_stage2.py`、
`test_extension_runtime_stage3.py` 与 `test_extension_surface_output_parity.py`；Plugin wheel、
真实 UDS 与 Connector client 仍属于下一层跨仓集成门禁。

每项必须先观察红灯，再做最小实现。集成测试必须从真实 wheel entry point 加载，
不能只 import 源码 Fake Host。

## 7. 隔离开发路径

当前 Stage 3 已按以下隔离路径完成；后续 opener/安装仍不得直接改动当前运行态：

1. 从当前 Core 源码创建一次性本地 clone；
2. 在临时目录创建独立 venv、HOME、Hermes 配置、SQLite、UDS 和日志目录；
3. 只在临时 clone 开发并运行 Core 测试；
4. 构建 Core wheel 与 Plugin wheel，并只安装到临时 venv；
5. Plugin enable 只写临时 `$HOME/.hermes/config.yaml`；
6. 不连接当前 Gateway、用户真实 UDS 或远程 Cloud；
7. 完成后删除临时目录即可回滚。

只有隔离环境中真实
`Android/Client -> Cloud -> Connector -> Plugin -> Host SPI -> authoritative session`
效果验收通过，才进入当前 Hermes 安装、配置和重启的第二次授权。
