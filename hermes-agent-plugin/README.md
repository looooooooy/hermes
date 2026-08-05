# Hermes Agent Plugin 与 Hermes Connector

本目录当前包含 Hermes Agent Plugin 的实现；唯一 Python 包为
`hermes_agent_plugin`。历史错误命名的导入包已从源码和发行物移除，只在外部升级
工具中保留旧发行版识别与回滚信息。本目录同时保存 Hermes Connector 的规范性设计
文档。

必须区分两个发布单元：

- **Hermes Agent Plugin / Local Gateway Adapter**：运行在 Hermes Agent 侧，只通过
  稳定 Host API 暴露本地观察、控制和能力发现。
- **Hermes Connector**：独立 Python 服务，负责设备身份、Hermes Cloud WSS、本地
  SQLite 可靠队列、Agent 发现、协议转换、诊断和独立升级。

面向用户采用一个签名安装器：一次安装同时部署 Connector 运行时和签名 Agent
Plugin Bundle。两个组件在内部保持独立版本、进程和故障边界，用户不需要分别安装。

当前代码已经实现 Observer/Control 的私有 Unix Socket 中继、控制租约和进程内
幂等账本，但 Hermes 0.19 的真实 `PluginContext` 不提供所需 Host SPI，Plugin
会在注册阶段明确 fail closed，不会启动或伪造控制闭环。阻断证据和 Hermes Core
最小接口提案见
[`docs/15-hermes-019-host-spi-gap-and-v1-core-contract.md`](docs/15-hermes-019-host-spi-gap-and-v1-core-contract.md)。
完整现状与目标差距见
[`docs/07-delivery-roadmap-and-acceptance.md`](docs/07-delivery-roadmap-and-acceptance.md)。

生产注册还要求 Host 公开 `hermes_cli.extension_host_v1`，并且只使用该公共模块提供的
`ObserverRequest`、`ControlScope`、`OwnerActionRequest` 和 `SafeAuditEvent` 原始类型。
字段相同的 Plugin 私有副本不能跨越 Host 边界；公共 DTO 模块缺失、SPI 版本不为 1
或构造器不完整时，注册会在创建目录、socket 或线程之前失败。Plugin 内的同形 DTO
仅供隔离单元测试使用，不是生产兼容层。

未来 Host SPI 适配器同时实现能力门控的 Observer output-parity v2 producer。只有
Host runtime descriptor 明确提供 `session.observe.output-parity.v1`，且 wheel 内
policy、event schema、snapshot schema 三份生成资源彼此一致时，Observer endpoint
才在 descriptor 和 Local Gateway handshake 中声明 v2。Host 未发布该 capability 时
保留 Observer v1；Host 已发布但版本不为 1 或生成资源漂移时必须在 endpoint 注册前
fail closed，不得静默回退 v1。macOS endpoint 依据同一 runtime descriptor 绑定 wire
版本：v1 `gateway.ready` 保持六字段身份，v2 只能发送
`{observer_contract: 2, connection_role: "observer"}`；capability 或 runtime generation
变化时先关闭旧 endpoint，再按新绑定重建。
v2 采用 exact `observer_contract=2` 请求、prepare/activate 两阶段生命周期、原子
snapshot、连续 replay/live sequence 和显式 unsubscribe。Plugin 会在 Host 边界完成
四类生命周期集合的 revision、复合身份、树闭包、终态删除、脱敏和有界校验，任何
不安全事实均不得启动准备态订阅或推进下游游标。该 producer 的可执行 Host double
与隔离 wheel 门禁不改变 Hermes 0.19 仍缺 Host SPI 的阻断结论。

Observer wire 资源预算固定为单 transport 最多 64 个、单 Plugin controller 最多
1024 个订阅。预算同时统计 prepare/activate 中的 reservation 与 active registration；超限必须
在调用 Host prepare、transport write 或 activate 前 fail closed。unsubscribe、异常、transport
断开和 runtime rollover 都会先在锁内唯一领取或释放容量，再在锁外关闭 Host registration。

macOS 生产运行使用一个冻结的六路径契约，同时保持 Local Gateway capability
discovery、Control 和 Observer 三种 descriptor registry 物理隔离。字段名为
`local_gateway_registry_directory/local_gateway_socket_directory`、
`control_registry_directory/control_socket_directory`、
`observer_registry_directory/observer_socket_directory`；对应环境变量声明在
`plugin.yaml`。生产 composition root 只解析一次并向三种 relay 注入同一个不可变
snapshot；路径必须 canonical、绝对、非空、无 NUL、无 `..`，六目录既要逻辑互异，
也要在创建后按设备号和 inode 物理互异。最终 descriptor 临时文件名和 Unix Socket
文件名（含最长 PID、hash、UUID、后缀及结尾 NUL）的字节预算在任何文件系统副作用
前验证，每个目录和最终文件名还要逐 component 服从所属文件系统的 `NAME_MAX`。
所有 endpoint `instance_id`、本地 Observer `subscription_id` 以及 relay 生成的 wire
request ID 均使用 RFC 4122 小写 canonical hyphenated UUID。
Connector 必须使用同名字段和完全相同的六个值，不能再把三种 role 压缩成一对目录。

三个 endpoint writer 只接受同一个 generation-bound `MacOSRuntimeAuthorityV2`。Host
进程先捕获一次 PID、进程启动时间、可执行文件 canonical path/device/inode、bundle ID
和 process instance UUID，再绑定当前 `runtime_generation`；Local、Control、Observer
不得各自生成身份。三种 registry 文件均发布 exact discovery descriptor v2：
`version/pid/profile/runtime_generation/socket_path/instance_id/process_start_time_ns/
process_executable/process_executable_device/process_executable_inode/host_bundle_id`，不允许
增删字段。每个 writer 在创建目录、socket 或线程前重新核验进程证据，失配即 fail
closed；通过后用 mode 0600 临时文件、`fsync`、原子替换和目录 `fsync` 发布。Socket
device/inode 由 Connector 在读取时从实际 socket 推导并复核，不写入 descriptor。

## 设计文档

文档入口见 [`docs/README.md`](docs/README.md)。其中包括：

- 产品边界和不可破坏约束；
- 目标架构、进程和模块设计；
- Cloud、Connector、Local Gateway 三层协议；
- Hermes Agent 独立升级兼容机制；
- 安全、数据治理、部署、运维和商用门禁；
- 员工专属 Agent 与 Agent-to-Agent 协作扩展；
- Agent Plugin 与 Connector 的独立实现级详细设计；
- Local/Cloud 数据协议、功能责任矩阵和全链路逻辑图；
- 软件架构、代码依赖、工程门禁和规模化演进约束；
- 架构决策记录。

## 当前插件开发

本地真实 Host 契约测试固定使用 `hermes-agent==0.19.0`。该版本是已知不兼容探针，
不是 Plugin 的运行时兼容声明；Plugin 运行兼容性以 `gateway-extension/1` 的版本
和 capability 为准。行为变更必须先增加失败测试，再实施最小修改并通过完整测试。

生产连接只有模块级 `register(context)` 一个入口，并且只能由当前运行 Hermes Agent
的 PluginManager 调用。历史 `bootstrap.runtime` 独立运行时及其资源工厂已经成为
fail-closed tombstone：直接调用会在创建目录、线程或 socket 之前抛出稳定诊断。
生命周期单元测试使用 `tests/test_support/` 下的显式 test-only harness；该目录不在
wheel 包含范围内，也不构成生产兼容 API。

```bash
uv run --isolated --extra hermes-019-contract-test \
  --with-editable . --with 'pytest>=8,<10' python -m pytest -q
uv run --group dev --locked ruff check src tests packaging
```

真实 Host 契约测试（`tests/integration/host/test_real_hermes_019_contract.py`）
还要求项目根存在含 `hermes-agent==0.19.0` 的 `.venv`（`.venv/lib/pythonX.Y/site-packages`
下可导入 `hermes_cli`）；该环境缺失时这些用例在收集期 skip 并注明原因，其余测试照常运行。

Ruff 版本固定在 `pyproject.toml` 的 dev dependency，并由 `uv.lock` 锁定；不得使用
机器上漂移的全局 Ruff 作为发布结论。

可选的本机真实源码门禁只创建隔离的临时 `HERMES_HOME`，不读取用户配置、不启动
endpoint，也不修改或重启 Hermes：

```bash
HERMES_LIVE_SOURCE_ROOT=/absolute/path/to/hermes-agent \
  uv run --locked python -m pytest -q \
  tests/integration/host/test_live_hermes_source_contract.py
```

也可以直接运行只读结构化诊断。输出只包含 Host distribution/module/version、实际
源码根、预期绑定方式、SPI 版本、缺失的公共成员与 capability、源码匹配结果和 Core
patch map，不包含配置、凭据、异常正文或 Host 私有对象。退出码 `0` 表示兼容，`2`
表示 Host SPI 不兼容，`3` 表示安装/进程绑定不成立或探针无法加载 Host，`4` 表示
诊断参数无效：

```bash
export HERMES_LIVE_SOURCE_ROOT=/absolute/path/to/hermes-agent
PYTHONPATH="$PWD/src:$HERMES_LIVE_SOURCE_ROOT" \
  "$HERMES_LIVE_SOURCE_ROOT/venv/bin/python" \
  -m hermes_agent_plugin.diagnostics \
  --expected-source-root "$HERMES_LIVE_SOURCE_ROOT"
```

上述命令只验证由当前解释器导入的 Hermes 安装和一个隔离构造的公开
`PluginContext`，不会把诊断子进程描述为正在运行的 PluginManager。需要把证据绑定
到运维已经只读选定的真实 Hermes 进程时，额外显式传入 PID 与该进程的可执行文件：

```bash
PYTHONPATH="$PWD/src:$HERMES_LIVE_SOURCE_ROOT" \
  "$HERMES_LIVE_SOURCE_ROOT/venv/bin/python" \
  -m hermes_agent_plugin.diagnostics \
  --expected-source-root "$HERMES_LIVE_SOURCE_ROOT" \
  --expected-host-pid "$HERMES_LIVE_PROCESS_ID" \
  --expected-host-executable "$HERMES_LIVE_PROCESS_EXECUTABLE"
```

成功绑定要求以下证据同时成立：导入的 `hermes_cli` module、`hermes-agent`
distribution、可编辑安装元数据、当前解释器 venv 和 `--expected-source-root` 指向同一
源码根；目标 PID 的 executable 与命令行均指向该源码根的 `venv/bin/python`；启动入口
为 Desktop 使用的 `python -m hermes_cli.main` 规范 headless 参数；直接父进程为同一
源码根发布目录中的 Hermes.app。诊断输出只暴露 allowlist 中的 PID、executable、父
PID、父 executable 和规范化启动类型，不输出原始命令行。

成功绑定仍只是 `compatibility_only_no_registration`，不会注册 Plugin 或启动
endpoint。上述任一安装或进程证据不一致时稳定返回
`reason=host_process_binding_mismatch` 和退出码 `3`，不会退回未绑定的兼容性结论。
未知参数、缺失参数值、PID 非整数/非正数/超过 `2147483647`，以及 PID/executable/
source root 三项绑定参数不完整时，稳定返回 `reason=diagnostic_arguments_invalid` 和
退出码 `4`；stderr 与 JSON 均不会回显无效输入，也不会把参数错误误报为 Host SPI
不兼容。

未来 Core 提供 `gateway-extension/1` 后，发布 entry point 的安装、Observer
prepare/activate、Control snapshot 和逆序关闭由
`tests/integration/host/test_future_host_spi_entrypoint.py` 验证。在该门禁和真实 Core
wheel 集成测试通过前，不得把当前 Fake Host 结果描述为生产接通。

发布门禁还会从刚构建的非 editable wheel 创建隔离虚拟环境，以等价的未来
PluginManager 执行 discover/install、成功 reload、失败 reload 回滚、unload 和重复
shutdown，并验证所有注册资源严格逆序且只清理一次。
