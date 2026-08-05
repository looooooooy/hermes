# 15 Hermes 0.19 Host SPI 阻断与 v1 Core 契约

- 状态：阻断事实与 Core 变更提案
- 契约版本：`gateway-extension/1`
- 验证版本：`hermes-agent==0.19.0`
- 更新日期：2026-08-02

## 1. 结论

[CURRENT] Hermes 0.19 的真实 `hermes_cli.plugins.PluginContext` 不存在
`register_gateway_extension`，也没有版本化、可注销的 Extension 生命周期或面向指定
权威会话的 Owner Action Port。当前公开 SPI 不能安全承载 Hermes Agent Plugin 的
Local Gateway、Observer 和 Control 生产闭环。

Plugin 因此必须 fail closed：

- 不启动 Local、Control 或 Observer endpoint；
- 不把 hook、`inject_message` 或工具分发包装成权威控制成功；
- 不导入 `tui_gateway`、Gateway Runner、CLI 私有队列或 Agent 私有对象；
- 不使用反射、monkey patch 或进程全局注册表补洞；
- 向 Hermes 插件发现器返回可操作的兼容性错误，同时不阻止 Hermes 主运行。

`hermes-agent>=0.19,<0.21` 已从 Plugin 的运行依赖中删除。`plugin.yaml` 改为声明
`requires_host_spi: "gateway-extension/1"`，并明确
`known_incompatible_hermes: ">=0.19,<0.21"`。Hermes 0.19 只作为隔离的真实 Host
契约测试依赖，不再被描述为可运行版本。

2026-08-01 对当前运行目录 `/Users/apple/.hermes/hermes-agent` 的只读探针确认：
distribution 与 `hermes_cli` module 均为 `0.19.0`，module 源码位于该目录；公开
`PluginContext` 仍缺少 `gateway_extension_spi_version`、
`gateway_extension_capabilities` 和 `register_gateway_extension`。因此诊断稳定输出
实际 source root、distribution/module 绑定证据、`gateway-extension/1` 要求以及
`docs/plans/hermes-core-host-spi-v1-patch-map.md` 下一步，不读取用户配置或秘密。
默认诊断明确标记为隔离 Host context 安装探针，不冒充运行中的 PluginManager；当
运维显式提供真实 Hermes PID、预期可执行文件和源码根时，诊断只在 module、
distribution、可编辑安装元数据、解释器 venv、源码根、后端启动入口和 Desktop 直接
父进程全部一致时标记为 `verified_live_process_installation`。仅 PID executable 字符串
匹配不足以建立该结论。该绑定只证明进程安装身份，不代表 Plugin 已注册或 Host SPI
已兼容。

历史 `bootstrap.runtime` 不能作为旁路。它仅是无资源副作用的 fail-closed tombstone；
生产绑定必须来自当前运行 Agent 的 PluginManager，测试 harness 则只存在于 `tests/`
且不会进入 wheel。

## 2. 真实 Hermes 0.19 公共能力评估

| 公开能力 | 可用于什么 | 为什么不足以承载本 Plugin |
|---|---|---|
| `register_hook` | 观察部分会话、LLM、工具、审批和子 Agent 事件 | 返回 `None`，没有注销句柄；不是进程级 Extension 生命周期；部分 payload 不是冻结的跨版本 DTO |
| `inject_message` | 在交互式 CLI 中注入用户消息 | Gateway 模式返回 `False`；不能绑定 `profile + session_key + runtime_generation`；运行中固定为中断语义；不能处理审批和澄清 |
| `dispatch_tool` | 调用工具注册表 | 工具调用不是会话控制面；不能证明命令触达目标权威会话，也没有 Owner lease 或 effect receipt |
| `register_auxiliary_task` / `llm` | 发起 Host 管理的模型旁路任务 | 不拥有会话、pending input、审批或中断权威 |
| `subagent_start` / `subagent_stop` hooks 与部分 0.19 源码中的 `subagent_lifecycle` | 观察或管理子 Agent 生命周期 | 发布的 `hermes-agent==0.19.0` 契约环境没有 `subagent_lifecycle`；当前本机同版本源码已暴露该公共属性，但它只面向子 Agent，不是可注销的 Gateway Extension 生命周期，也不提供绑定指定权威会话的 Owner Action Port |
| `register_command` | 注册斜杠命令 | 需要用户在既有会话中主动调用，不能成为远程、定向、带租约的 Owner Action Port |

其中 Observer hook 可以作为未来 Host SPI 的事件来源之一，但必须由 Hermes Core
先转换为稳定 DTO，并通过可关闭的 `Subscription` 暴露。它不能直接作为当前
Local Gateway 的生产绑定。

### 2.1 标准 API Server GET 的边界

Hermes 0.19 标准 API Server 提供 session 列表、session 详情和 message 列表 GET，
但其公开响应没有 Observer/Control 所需的权威 `runtime_generation`、
`runtime_session_id`、live `running/status` 与 per-runtime `event_sequence`。message
响应还可能包含 tool calls、工具输出和 reasoning。Plugin 不得把 session id 同时
冒充 durable session key 与 runtime session id，不得按 message id/数量补造 sequence，
也不得按 `ended_at` 或 `end_reason` 推断实时状态。

因此 API Server GET 不能解除本文件的 Host SPI 阻断，不能启动或发布 Observer/
Control endpoint/capability，也不能形成 owner action。Connector 不得直连 API
Server、发现临时端口、读取 Desktop 私有 token 或持有 `API_SERVER_KEY`。

允许后续把这些 GET 定义为独立的只读 catalog/history 同步协议。依赖方向固定为：

```text
Connector -> Agent Local Gateway -> Plugin catalog application
          -> Plugin standard-API HTTP adapter -> Hermes API Server GET
```

该协议只能输出带 source/provenance/cursor 的非权威历史记录，必须丢弃未获允许的
tool/reasoning/secret 字段，并且不得合成任何 Observer/Control 字段或改变
`AGENT_UNAVAILABLE`。当前 API Server bearer key 同时覆盖读写接口，不是 read-only
scope；在独立 contract、固定 GET allowlist、loopback endpoint、响应上限、凭据注入
和 TDD 门禁完成前，catalog capability 也必须保持 unavailable。允许该独立协议的
架构位置，不等于当前已经实现、部署或获得生产启用授权。

### 2.2 H5 首次真实会话发现边界

当前 Observer/Control 合约只处理调用方已经知道 `durable_session_key` 的会话。
Connector Protocol 的 `session.observe.open` 也只携带 Cloud 已授权的明确目标；它不是
Agent 会话枚举协议。因此“已知真实会话的快照与增量可转发”不等于“H5 能从空状态
发现当前 Hermes 的全部真实会话”。

要完成首次闭环，Hermes Core Stage 3 必须增加权威、有界、可取消的会话 catalog
边界，至少提供当前 profile 下的 durable session key、可展示的安全元数据、catalog
revision/cursor，以及新增、更新、删除通知。该 catalog 必须与 runtime generation
绑定，并明确区分历史存在与当前可控制状态。Plugin 再把 catalog 转换成独立的 Local
Gateway 协议，Connector 通过 ORM 可靠队列同步到 Cloud，H5 只读取 Cloud 投影。

在该边界落地前，禁止从 API Server GET、本地文件、测试 seed 或 Cloud 占位记录推断
权威会话；禁止把任意 session id 同时冒充 durable session key 与 runtime session id。
这项缺口是当前 H5 从空库自动出现真实 Hermes 会话的明确阻断项。

## 3. 最小 Host SPI v1

以下是 Hermes Core 必须提供的公共契约。类型应位于稳定公共模块，例如
`hermes_cli.extension_host_v1`，不能要求 Plugin 导入 `cli.py`、`gateway.run`、
`tui_gateway` 或 `agent` 私有实现。

```python
from typing import Literal, Protocol


class Registration(Protocol):
    def close(self) -> None: ...


class PreparedObserver(Protocol):
    snapshot: object
    activation_deadline_monotonic: float

    def activate(self) -> Registration: ...
    def close(self) -> None: ...


class GatewayExtensionContextV1(Protocol):
    gateway_extension_spi_version: Literal[1]
    gateway_extension_capabilities: frozenset[str]

    def register_gateway_extension(
        self,
        extension: "GatewayExtensionV1",
        *,
        spi_version: Literal[1],
    ) -> Registration: ...


class GatewayExtensionV1(Protocol):
    def install(self, host: "GatewayExtensionHostV1") -> Registration: ...


class GatewayExtensionHostV1(Protocol):
    host_api_version: Literal[1]

    def runtime_descriptor(self) -> "RuntimeDescriptor": ...
    def register_local_endpoint(
        self,
        endpoint: "EndpointDescriptor",
    ) -> Registration: ...
    def prepare_observer(
        self,
        request: "ObserverRequest",
        sink: "EventSink",
    ) -> PreparedObserver: ...
    def control_snapshot(
        self,
        scope: "ControlScope",
    ) -> "ControlSnapshot": ...
    def invoke_owner_action(
        self,
        request: "OwnerActionRequest",
    ) -> "OwnerActionResult": ...
    def add_runtime_listener(
        self,
        listener: "RuntimeListener",
    ) -> Registration: ...
    def audit(self, event: "SafeAuditEvent") -> None: ...
```

Observer output-parity v2 不另建私有 Host API。Host 必须在 runtime descriptor 中显式
发布 `session.observe.output-parity.v1`，Plugin 才能把 `ObserverRequest` 的
`observer_contract=2` 与同名 `required_capabilities` 传入既有
`prepare_observer`。Plugin 同时必须成功加载发行物中的 output-parity policy、session
event v2 schema 和 session snapshot v2 schema，descriptor/handshake 才能宣称 v2。
Host 未发布该 capability 时提供 v1；Host 已发布但 capability version 不为 1，或任一
生成资源无法通过一致性校验时必须在 endpoint 注册前 fail closed，禁止静默回退 v1。
运行中 capability 或 runtime generation 变化必须先关闭旧 subscription 与 endpoint，
再按新 contract 重建并重新协商。Hermes 0.19 因缺少整个 Host SPI，仍在注册前 fail
closed。

Observer 必须采用 `prepare -> activate -> close` 的两阶段边界：

1. `prepare_observer` 只创建有界、尚未投递 live event 的准备态资源，并返回不可变
   snapshot 与同一进程单调时钟上的总激活期限；
2. endpoint caller 必须先把 snapshot RPC result 完整写入下游，写成功后才能调用
   `activate()`；不得在 response 与 reader thread 之间依赖调度顺序；
3. snapshot 写失败、连接断开、caller 取消或未在总期限内激活时，必须调用
   `PreparedObserver.close()`；Host 同时必须在期限到达时自动回滚，不能依赖 caller；
4. `activate()` 与自动过期必须在同一生命周期锁上竞争，恰好一个路径取得所有权；
   过期、重复激活或已关闭对象必须 fail closed；
5. `activate()` 成功后返回活动 `Registration`，此后由该 Registration 负责关闭
   subscription；激活前由 `PreparedObserver` 负责回滚；
6. 准备态上游读取必须同时限制 RPC result 前的 pending frames 和 WebSocket
   post-response queue，不能用“尚未启动 reader”换取无界内存；
7. 关闭失败时必须保留可重试的资源引用；批量关闭要继续处理其余 subscription，
   最后再抛出首个错误。

Plugin 当前 macOS relay 的执行上限为：subscribe ready+result 总预算 3 秒、
prepare 后激活总预算 3 秒、RPC result 前最多 32 个 pending event、WebSocket
post-response queue 最多 32 帧，单帧仍受 256 KiB 上限约束。

[BLOCKED] Hermes Core 尚未授权该两阶段接口，也没有 production caller 能证明
“snapshot result 已写出”后再执行 `activate()`。因此当前
`ObserverEndpointDescriptor.prepare_observer` 只是冻结的 SPI 边界，不能据此宣称
Observer 已形成生产闭环，也不得退回立即启动的 `open_observer`。

Plugin 当前已经按该 Facade 实现 `install(host) -> Registration`，不再调用
`register_connection_role` 或任何未冻结的 Host 方法。安装顺序为读取 runtime
descriptor、注册 runtime listener、注册 Observer endpoint、注册 Control
endpoint、写入安全审计；任何一步失败都会逆序关闭已经取得的 Registration。
返回的聚合 Registration 幂等关闭，并在单个子资源关闭失败时继续关闭其余资源。

Host SPI 的 `runtime.descriptor.v1` capability 与 macOS 文件系统 discovery descriptor
v2 是两个独立版本面。未来 Core composition root 在一次 Host runtime 启动中必须只
捕获一个 Plugin `MacOSHostAuthorityV2`，用当前 `runtime_generation` 绑定为同一个
`MacOSRuntimeAuthorityV2`，再同时注入 Local、Control、Observer writer。三个 writer
不得各自生成 `instance_id` 或独立读取 PID。权威进程证据包括启动时间纳秒、可执行
文件 canonical path、device、inode 和 `host_bundle_id`；任一证据在发布前不可读取、
发生变化或与 authority 不一致，必须在目录、socket、线程等副作用前失败。

三个 registry 的 exact discovery descriptor v2 字段集合统一为
`version/pid/profile/runtime_generation/socket_path/instance_id/process_start_time_ns/
process_executable/process_executable_device/process_executable_inode/host_bundle_id`。
Connector 从实际 socket 推导 device/inode 并在连接前后复核，因此这两项 socket
证据不进入 JSON。发布必须使用 mode 0600 临时文件、文件 `fsync`、原子替换和目录
`fsync`；不得回退到 descriptor v1。

生产边界的权威 DTO 必须来自 Core 公共模块
`hermes_cli.extension_host_v1`。Plugin 在生产注册时加载并校验该模块的 SPI 版本和
冻结 dataclass 构造器的定义模块、类名、字段、参数顺序、必填/默认形状和可构造性，
所有 Host 调用都实例化这些原始类型；Plugin 内同形 DTO 只允许隔离测试显式注入，
不能作为 duck-typed 生产替代。Core 的 `OwnerActionRequest` 应保持以下不可混淆的身份：

```python
@dataclass(frozen=True)
class OwnerActionRequest:
    profile: str
    durable_session_key: str
    runtime_generation: str
    command_id: str
    method: str
    payload: Mapping[str, object]
```

Control v1 的 `client_request_id` 仍是客户端幂等键，不是 Host `command_id`。
Connector 把 Cloud `command_id` 放在本地 Control JSON-RPC `id`，Plugin 将该
JSON-RPC `id` 精确映射为 Host `command_id`；`client_request_id` 仅用于 Plugin
账本与 Control v1 返回兼容映射，不进入 `OwnerActionRequest`。方法 payload 会移除
lease、profile、session、runtime generation 和 Control 幂等字段，避免把控制面
身份误当成 Agent 业务 payload。

Control endpoint 的可用方法由 Plugin 自有的 acquire/renew/release/status 与当前
runtime descriptor 的 Owner Action capability 交集组成。Runtime listener 撤销
capability 后，Plugin 会在副作用边界前拒绝对应方法，不能依赖 Host 返回
`rejected` 作为唯一能力门禁。

Context 必须至少发布以下 capability，Plugin 才允许注册：

- `extension.lifecycle.v1`
- `runtime.descriptor.v1`
- `session.observe.v1`
- `session.owner-actions.v1`
- `audit.safe.v1`

缺少任一 capability 即不可用，不允许根据 Hermes 版本字符串猜测能力。

安全审计同样必须使用 Core 的 `SafeAuditEvent` 原始类型和 allowlist：安装成功、失败、
关闭分别表示 `runtime.lifecycle` 的 `started/ready`、`failed/unavailable`、
`closed/closed`。关闭时必须读取绑定对象的当前 runtime generation，不能复用安装时
捕获的旧 generation。

## 4. 可注销生命周期

`register_gateway_extension` 返回的 `Registration` 必须同时由
`PluginManager` 托管。Core 必须保证：

1. Extension 安装失败时，已创建资源按逆序关闭；
2. Plugin disable、remove、强制重新发现和进程退出时，`close()` 恰好调用一次；
3. `close()` 完成后不再回调 Observer、Runtime Listener 或 Owner Action；
4. 关闭顺序为停止接收控制、撤销订阅、关闭 endpoint、释放运行期租约；
5. CLI、Gateway、ACP 和后台 Worker 使用同一套关闭语义；
6. 单个 Plugin 的安装或关闭失败不能阻止 Hermes Core 运行或关闭其他 Plugin；
7. 同一 transport 的一个 Observer subscription 关闭失败时，Core 必须继续关闭
   其余 subscription，并保留失败句柄供 shutdown/drain 重试，不能先从注册表删除。

Plugin 不应依赖 `atexit` 作为正常清理机制。`atexit` 无法表达 force reload、
disable、Gateway drain 和按 Plugin 隔离失败。

## 5. 权威 Owner Action 契约

`invoke_owner_action` 必须由 Hermes Core 绑定真实权威会话，至少支持：

- `prompt.submit`
- `session.interrupt`
- `session.steer`
- `approval.respond`
- `clarify.respond`

每个请求必须包含并校验：

- `profile`
- `durable_session_key`
- `runtime_generation`
- `command_id`
- `method`
- 经过方法级 Schema 校验的 payload

Core 返回的 `OwnerActionResult` 必须区分 `accepted`、`rejected`、
`effect_unknown`。Plugin 只能把 Core 的结果映射到 Control v1，不能把线程池提交、
UDS 写成功、hook 执行或消息入队自行解释成业务成功。目标会话不存在、代次不匹配
或无法确认效果时必须 fail closed。

## 6. Hermes Core 必须修改的接口

### 6.1 `hermes_cli/plugins.py`

- 为 `PluginContext` 增加只读 `gateway_extension_spi_version`；
- 增加只读 `gateway_extension_capabilities`；
- 增加 `register_gateway_extension(..., spi_version=1)`；
- 为 `PluginManager` 增加 Extension Registration Store；
- 在 `discover_and_load(force=True)` 前关闭旧 Extension；
- 增加幂等的公开 `shutdown_extensions()`，按逆序关闭注册句柄。

### 6.2 新公共 Host 模块

新增稳定公共模块，输出本文件第 3 节的 DTO、Protocol、错误和枚举。Core 内部可继续
使用当前 CLI、Gateway、ACP 和 Session 实现，但 Plugin 只能看到冻结 DTO 和公共
Facade。

### 6.3 Core 运行入口

CLI、Gateway、ACP 和 Worker 的正常退出、异常退出及 drain 路径必须调用同一个
`PluginManager.shutdown_extensions()`。会话切换只能关闭会话订阅，不能误关
进程级 Local Gateway。

### 6.4 Session 权威适配

Core 必须在 Host Facade 内完成 `profile + durable_session_key +
runtime_generation` 到当前权威会话的解析，并在同一权威执行上下文内提交 Owner
Action。Plugin 不得获得 Agent 实例、CLI 队列、Gateway Runner 或 SessionDB。

## 7. Core 验收测试

Hermes Core 合并 Host SPI v1 前至少需要：

1. 真实 `PluginManager.discover_and_load()` 加载 v1 Extension；
2. 缺版本、缺 capability 和版本不匹配均 fail closed，Hermes 主运行继续；
3. install 中途失败按逆序清理；
4. force reload、disable 和 shutdown 对每个句柄只关闭一次；
5. Observer prepare 后、snapshot response 写出前不投递 live event；
6. Observer prepare 未激活时按总期限自动关闭，activate/expiry 竞态无泄漏；
7. 多 Observer 关闭时隔离单个失败并保留失败句柄供重试；
8. Observer 订阅关闭后不再投递；
9. Owner Action 只触达指定会话和指定 runtime generation；
10. prompt、interrupt、steer、approval、clarify 分别验证真实权威效果；
11. 无法确认效果返回 `effect_unknown`，不得自动重放；
12. Plugin 不需要任何私有 import、反射或 monkey patch；
13. 使用发布 wheel 的 entry point 运行跨包集成测试，而不是只用 Fake Host。

## 8. 当前 Plugin 验收证据

`tests/integration/host/test_real_hermes_019_contract.py` 使用真实
`hermes_cli.plugins.PluginContext`、`PluginManager` 和发布 entry point，验证：

- Hermes 0.19 触发明确的 Host SPI v1 兼容性错误；
- 插件发现器记录错误且不把 Plugin 标记为已启用；
- hook 返回值没有注销句柄；
- Gateway 场景下 `inject_message` 不可用；
- Plugin 没有启动伪造的 Control/Observer 闭环。

`tests/unit/adapters/host/test_extension.py` 另外使用最小 v1 Facade 验证：

- `install(host)` 只使用本文件冻结的 Host 方法并返回 Registration；
- 安装失败逆序回滚，关闭幂等且不会因一个子资源失败跳过其他资源；
- Observer prepare/activate/close、Control Snapshot 与 Owner Action 使用精确
  runtime scope；
- Observer snapshot-first、激活总期限、队列上限、过期竞态及可重试清理均由
  Plugin relay 单元测试覆盖，但尚无 Hermes Core production caller；
- Owner Action 在进入 Host 前校验 `profile + durable_session_key +
  runtime_generation + command_id + method + payload`；
- runtime capability 缺失或被 listener 撤销时，在副作用前 fail closed；
- Host `effect_unknown` 映射为 Control v1 `unknown`，且外部
  `client_request_id` 不会覆盖或冒充 Host `command_id`。

`tests/integration/host/test_live_hermes_source_contract.py` 可通过显式
`HERMES_LIVE_SOURCE_ROOT` 使用当前本机 Hermes venv 和源码执行只读门禁：它构造
不读取用户配置的真实 `PluginContext`，验证 0.19 缺失三项 Extension context 成员、
注册明确 fail closed，且隔离 `HERMES_HOME` 和六个 endpoint 目录均未创建。

同一门禁还调用 `python -m hermes_agent_plugin.diagnostics`，以稳定 JSON 和退出码输出
无副作用兼容性结论。诊断只允许输出 Host 版本、期望/观测 SPI 版本、缺失的公共成员
与必需 capability、源码匹配布尔值；不得输出配置、凭据、异常正文或任意 Host 属性值。
退出码 `0` 表示兼容，`2` 表示不兼容，`3` 表示安装/进程绑定不成立或探针无法加载
Host，`4` 表示诊断参数无效。该诊断不会调用 `register_gateway_extension`，因此即使
未来 Host 已兼容，也不会启动 endpoint。显式 PID 模式额外只输出 allowlist 中的进程
ID、可执行文件、父进程 ID、父进程可执行文件、规范化启动类型和匹配布尔值，不输出
原始命令行。PID 不存在，以及 executable、distribution、module、source root、解释器
venv、`python -m hermes_cli.main` headless 启动参数或 Hermes.app 直接父进程任一证据
不一致时，返回稳定 `host_process_binding_mismatch` 与退出码 `3`，不输出未验证的 Host
兼容性结论。未知参数、缺失值、非法 PID 或不完整的 PID/executable/source root 组合
返回稳定 `diagnostic_arguments_invalid` 与退出码 `4`，且 stdout/stderr 不回显无效
输入；参数错误不与 Host SPI 缺失合并。

`tests/integration/host/test_future_host_spi_entrypoint.py` 从发布 entry point 加载
Plugin，并以未来 v1 context 驱动 `register -> install -> Observer prepare/activate ->
Control snapshot -> close`。该门禁证明 Plugin 已具备未来 Host 的安装和运行边界，但
不替代 Hermes Core wheel、真实权威会话和真实 UDS 的最终集成测试。

`tests/integration/host/test_canonical_host_spi_types.py` 不依赖既存 Stage 2 verified
target。测试先校验仓内 `upstream.lock.json` 固定的上游 repository、version、commit、
Stage 1 patch 路径与 patch SHA-256，再从该锁定 patch 提取真实
`hermes_cli.extension_host_v1` 公共 contract；隔离子进程用例将 contract 物化到临时目录。
测试分别驱动公开 `register(context)` 与
`HermesAgentPluginExtension().install(host)`，并要求 Observer、Control、Owner Action
和安全审计四类跨边界对象均为 Core 原始类型。畸形 DTO 构造器以及 bool/float Host API
版本另有 RED→GREEN 门禁，且在 platform composition、Host 注册和 endpoint 调用前失败。
该测试只证明公共类型互操作，不把锁定 patch 描述为 production composition。

当 Hermes Core 提供本契约后，应新增真实 v1 Host 绿灯测试。在该测试通过前，
`session.control` capability 不得向 Connector 或 Cloud 发布。
