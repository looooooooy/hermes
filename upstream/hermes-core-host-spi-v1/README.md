# Hermes Core Host SPI v1 上游补丁包

本目录维护通用 `gateway-extension/1` Core 补丁，不包含 Android、Cloud、Connector
或本项目 Plugin 的产品特判。它是独立上游贡献物，不是当前 Hermes 安装目录的源码。

## 当前交付边界

Stage 1（`0001`）提供：

- 冻结的 `hermes_cli.extension_host_v1` DTO、Protocol、错误与安全 `repr`；
- `PluginContext` 的 SPI 版本、capability 协商和
  `register_gateway_extension(...)`；
- capability 必须由 composition root 显式声明，默认空集且只能是 v1 已知能力子集；
- `PluginManager` 按 Plugin 托管 Registration；
- `OPEN/CLOSING/CLOSED` install/drain 门禁、并发 drain 完整性和同线程重入拒绝；
- install 后任意 `BaseException` 逆序回滚并原样重抛；
- force reload、配置 disable 后重新发现、显式 shutdown 的幂等逆序关闭；
- 单个关闭失败不跳过其他 Plugin，并保留失败句柄供下一次 drain 重试；
- session/command 标识与 payload 默认不进入 DTO `repr`，规范文本拒绝控制字符；
- 使用 Hermes 既有 pip entry-point 扫描和加载路径的 Core 行为测试。

Stage 2（`0002`）在 Stage 1 之上增加 Core 内部、尚未接入生产 composition root 的：

- `SessionAuthorityRegistry` 以 `(profile, durable_session_key)` 为二元 authority key；
  runtime generation 是 record/CAS 及全局 RuntimeDescriptor 的代际状态，不属于 map key；
- controller identity、lease deadline、revision/CAS 原子注册、续租、自动到期撤销、到期接管；
  调度器创建/启动失败和同步或异步回调早于提交均通过独立状态锁线性化，不改变 authority、
  generation 或 Observer 状态
  与关闭；竞争绑定只有唯一赢家，stale timer 不能撤销新 token/revision；
- durable session 的 sequence ledger 独立于 authority lease，重绑和 runtime generation
  轮换都不会回退或重复；公开 RuntimeDescriptor 始终反映当前可达 generation；
- 两阶段 Observer Broker：同锁 snapshot/watermark、`prepare_observer()`、有期限的
  `PreparedObserver`、写完 snapshot 后显式 `activate()`；
- 严格连续的 per-session sequence、32 帧有界队列、256 KiB 单帧上限，以及 gap、overflow
  和 backpressure fail closed；
- 严格调用 Stage 1 `EventSink(event)` callable 契约；
- 多 Observer 隔离、stale generation/revision 拒绝、Registration 逆序关闭和失败重试；
- sink delivery 关闭使用有界等待；超时 observer 保持为可观测、可重试的 `CLOSING`，不会
  阻止后续 observer cleanup；
- shutdown 的全部 Observer 共享一个绝对 deadline；最多 64 个 Observer、每 scope 8 个，
  blocked/`CLOSING` admission 上限 4 个；同一 Registration 的并发 close 把 `finish_lock`
  等待计入各自真实 monotonic 绝对期限；
- 注入 monotonic clock 仅用于 authority、prepare 与 activate 语义，并执行 finite、positive
  和防倒退校验；shutdown 等待预算始终使用独立的真实 `time.monotonic()` 绝对 deadline；
- Observer delivery、cleanup、clock 和 worker-start 失败提供有界结构化状态与计数，不含
  payload 或原始异常文本；
- 冻结的 display-safe v1 事件 allowlist，默认排除 raw tool output、隐藏 reasoning、secret
  和完整 approval payload；
- `audit.safe.v1` 仅在 callable sink 存在时提供，并校验事件名、字段、枚举、profile、当前
  runtime generation 以及递归敏感键；sink 异常不跨边界保留原始 cause/context；
- capability 只在实际组件存在时显式提供。

Stage 3（`0003`）在 Stage 2 之上增加：

- 单进程唯一 production composition root，并注入全局 `PluginManager`；
- 进程级扩展 teardown owner：TUI/Desktop `serve` 在 uvicorn event loop 退出前、Messaging
  Gateway 在其 async process lifecycle 返回/取消/异常前调用全局 `PluginManager` 的
  `shutdown_extensions()`；同一 manager 的成功 drain 恰好一次且重复 close 幂等，不创建从未
  使用的 manager，也不替换现有 signal handler；
- 权威、有界、分页且可监听的 session catalog，不扫描 `SessionDB`，不公开 Gateway routing key；
- Owner Action Router、命令摘要冲突拒绝、有界 exactly-once ledger，以及 approval/clarify
  `pending_request_id` 精确绑定与消费；
- CLI、TUI/Desktop 的 prompt/interrupt/steer/approval/clarify 真实 owner path；CLI prompt
  先进入绑定当前 session/generation 的 Core main-turn scheduler；scheduler 只暂存请求，必须由
  权威 CLI main loop 原子预分配 `turn_id` 后才返回 accepted；该同一标识随后进入真实
  `AIAgent.run_conversation`、`turn_context`、output entity 与 interrupt CAS，不会在 Agent 内再次
  生成分叉标识。并发 reservation、active turn、setup 失败与下一 turn 都 fail closed，旧 pending/
  current identity 在 terminal 路径清除。queue full、deadline、consumer
  close、rebind 或 shutdown 前尚未绑定的请求全部明确拒绝，不直接把 `_pending_input` 写入冒充
  成功，也不会因 scheduler close 静默丢失；
- TUI/Desktop owner prompt 不再把 background thread 启动或 `streaming` 当成 accepted；只有真实
  Agent 已建立同一 `(turn_id, turn_generation)` 并回调 admission，或权威队列明确接纳时才返回
  accepted；权威队列会保留该预分配 `turn_id` 到实际 Agent turn，且第二个远程 reservation
  不会被合并到同一个 ACK identity。若前序 turn 在 Agent 建立前失败、取消、失去 running
  状态或 session close，已 ACK 的队列项会在同一 history lock 下被 claim，并以原 `turn_id`
  发布唯一 terminal failure 后才暴露 idle；close 与 drain 在同一锁内竞争唯一 queue ownership，
  closing 胜出时只在锁外 terminalize、绝不 dispatch，且 registration teardown 发生在 terminal
  callback 之后。queued dispatch 或 transport 失败仍会以原 `turn_id` 发出唯一安全错误终态，并在
  `finally` 清除 admission、turn identity、running 与 inflight；默认日志不包含原始异常文本。
  close fence 会拒绝同时到达的新 prompt。Agent
  build/cancel 失败返回 rejected；短 admission deadline 内无法确认效果返回
  `effect_unknown`，不会无限等待；
- Agent 为每个真实 turn 分配单调 generation，并在 finalizer 清 interrupt flag 前先将该 turn
  标记为 ending。CLI、TUI/Desktop、Gateway、ACP 与 Cron/Worker 的 interrupt 都对当前
  `(turn_id, turn_generation)` 做 CAS，并只在底层确认同一 turn 已中断后返回这组 ack；idle、
  ending、stale/next-turn、缺少权威接口均返回 `turn_unavailable`，底层结果不确定返回
  `effect_unknown`；
- Gateway、ACP、Cron/Worker 的真实 interrupt/steer owner path；已有控制平面冲突或缺少真实
  turn acceptance 的动作明确拒绝，不把队列写入、frame 写入或网络发送冒充业务效果成功；
- CLI new/resume/branch、Gateway compression、TUI session、ACP restore/fork、Worker job 的
  durable session rebind 与 registration close；所有生产 caller 共用 revision-aware registration
  slot；每次 open 都从权威 session registry 读取当前 tombstone revision，不使用 caller 本地猜测
  或 blind retry，在 close→reopen、A→B→A、sibling slot、runtime generation 轮换及 authority bind
  后 owner/output 局部注册失败时仍使用正确 expected revision；replacement open/close 失败保持旧
  catalog 原子可见且保留可重试句柄；
- CLI、TUI/Desktop 与 Messaging Gateway 的 owner callback、control/observer snapshot 和 output
  producer 都冻结到注册时 `(profile, durable_session_key, runtime_generation)`，不会在失败的 A→B
  rebind 后读取 caller 的可变 B 状态或把 B output 投递给 A observer；成功 B rebind 的 snapshot、
  callback 与首个 output event 都使用 B identity，首序号为 1；TUI 的自动/手工 compression 共用
  session-key 收口点重注册 owner，B 注册失败时清空绑定并关闭旧 A authority，A/B 均 fail closed；
- 冻结、display-safe 且实现 `Mapping` 的 output-parity v2 snapshot/event DTO；Core 随补丁冻结
  policy、event schema 与 snapshot schema，并在 DTO 进入 broker 前验证 12 种 event 的精确
  字段/类型、未知字段、深度、数量、帧大小、控制字符、credential material、raw args/reasoning/
  tool/terminal output 与敏感 extension key；snapshot
  精确包含 profile/generation/session/runtime/status/sequence/messages/inflight/todo/subagent/
  tool/terminal/replay 字段，并在 Host 边界保证 256 KiB 总帧；
- `observer_contract=2` 与 `session.observe.output-parity.v1` 必须成对协商；prepare 与
  production producer 共用 authority 锁和连续 sequence，response 写成功前只缓存，显式
  activate 后才投递 live event；
- CLI、TUI/Desktop、Messaging Gateway、ACP 与 Cron/Worker 都从真实显示/执行回调产生
  message/status output；TUI/Desktop 另外产生 reasoning、todo、subagent、tool、terminal
  lifecycle 和 terminal output，Core 负责 revision/first_event_sequence，Extension 不补字段；
  terminal lifecycle 来自权威 `ProcessRegistry` 进程回调，严格为 revision 1 `running` 后接唯一
  revision 2 `completed|failed|interrupted`。terminal output 与 lifecycle 并发时仍幂等，旧
  registration generation 的迟到回调被拒绝；显式 kill 只有在有界轮询确认真实退出后才产生
  `interrupted`，无动作、权限拒绝或不可读状态返回 `effect_unknown` 并保持 running，由 watcher
  的后续真实结果收口；host PID identity 使用 `ours|gone_or_reused|unreadable` 三态，只有明确
  `NoSuchProcess`/dead 或成功读取后的 start-time mismatch 才能确认终态，start-time 暂时不可读和
  任意未分类 probe 异常均保留为 unknown survivor；`terminal.close` 只表示 UI tab 关闭，不冒充
  业务 terminal；
- endpoint 生命周期由 Host 托管；公开 `EndpointDescriptor` 固定提供
  `open_local_endpoint(RuntimeDescriptor) -> Registration`，Core 校验 opener 签名和返回的
  Registration；正式角色集合为 `local-gateway|observer|control`。仅测试可显式注入 registrar
  seam，不再动态猜测未公开 opener；
- Host Extension Manager 支持显式、成对配置的 signed Plugin Store manifest/trust-store。
  manifest 以 Ed25519 验证 canonical JSON，锁定 key、有效期、绝对规范路径、wheel SHA-256、
  唯一 `hermes_agent.plugins` entrypoint 与回滚下限；未知 key、篡改、过期、symlink/path escape、
  editable/direct-url、重复或额外 entrypoint 和低版本全部 fail closed；
- wheel 只原子展开到 content-addressed immutable slot，并通过 `importlib` 从 slot 受控加载；不向
  Agent venv 执行 pip，不写 `.pth`，不依赖 `PYTHONPATH`。显式 Store candidate 在同 key 时优先于
  既有 pip entrypoint，但仍受 safe mode 与 explicit disabled 约束；只有 Plugin register 与全部
  endpoint install 完成后才原子提交 active pointer。失败按逆序回滚，close 失败保留句柄与模块供
  重试，reload/unload/teardown 幂等。

Stage 3 现在只在上述真实 producer、精确 DTO 和有界 broker 都存在时广告
`session.observe.output-parity.v1`。Messaging Gateway、ACP、Cron/Worker 仍不接受 prompt、
approval 或 clarify；能力按 session catalog 的 `available_actions` 如实暴露。隔离 artifact
门禁已用最终 Core wheel 与真实 Plugin wheel 打开并连接 Local/Observer/Control 三类 macOS UDS，
同时证明第三端点失败时前两端点逆序回滚且 active 不提交。Connector client、Cloud/H5、
Android/Web 与 live cutover 仍须各自的跨边界验收，不能由该 Core/Plugin 装配测试冒充。

## 目录

```text
hermes-core-host-spi-v1/
├── README.md
├── compatibility-matrix.yaml
├── upstream.lock.json
├── patches/
│   ├── 0001-gateway-extension-host-spi-v1-stage1.patch
│   ├── 0002-session-authority-observer-stage2.patch
│   └── 0003-production-composition-owner-actions-stage3.patch
├── dist/
│   ├── hermes_agent-0.19.0-py3-none-any.whl
│   └── hermes_agent-0.19.0.tar.gz
├── tools/
│   └── apply_and_verify.py
├── tests/
│   └── test_apply_and_verify.py
└── evidence/
    ├── live-core-read-only.json
    ├── stage3-full-suite-node-comparison.json
    ├── stage1-verification.md
    ├── stage2-verification.md
    └── stage3-verification.md
```

`upstream.lock.json` 固定上游仓库、发行版本、完整 commit、patch SHA-256、wheel/sdist SHA-256、
Stage 3 patch 与产物内生产源文件的 provenance、可重复执行的 artifact build 命令和验证命令。
`compatibility-matrix.yaml` 明确已实现与仍不可用的能力，禁止把内部 Stage 2 运行面冒充
已组合的生产控制面。

## 隔离应用与验证

支持边界：本 replay executor 当前只支持 POSIX 环境中的 macOS 和 Linux。Windows
不是可执行支持目标，也不应将本目录测试结果表述为 Windows replay 证据；Windows 主机需在
受支持的 Linux CI/容器或 WSL 环境中执行同一锁定流程。

工具只允许从干净、精确匹配且等于 Git top-level 的 source 创建一个全新的 archive target。它不会在
source 中 checkout、创建分支、创建 worktree、应用 patch 或安装包。target 必须是显式
workspace root 的直接子目录且尚不存在；原始 target 路径及其父组件不得包含符号链接，
workspace/target 也不得位于 source Git 根目录内。

```bash
cd /Users/apple/hermesmobile/upstream/hermes-core-host-spi-v1
python -m tools.apply_and_verify \
  --source /Users/apple/.hermes/hermes-agent \
  --workspace-root /Users/apple/hermesmobile/.tmp \
  --target /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified \
  --test-python /path/to/dev-venv/bin/python
```

工具按以下顺序 fail closed：

1. 在任何命令、archive、partial workspace 或 target 副作用前，严格校验 `stage` 是受支持的
   `1/2/3` 整数，并与按 `0001..000N` 连续声明的 patch 集合完全一致；
2. 通过原始路径 `lstat` 校验 target 边界，并校验 source top-level、clean 状态和固定 commit；
3. 按 lock 声明顺序一次性读取全部 patch，校验并锁定各自 SHA-256 与 bytes；同时校验 bundle
   内保留的 wheel/sdist 路径边界、SHA-256，以及二者内部生产源文件与当前 Stage 3 patch 的
   provenance，artifact 任一缺失、漂移或来自旧 replay 都在创建 target 前失败；
4. 用 `git archive` 只读复制固定 commit；
5. 仅通过 stdin 将已锁定 bytes 交给 `git apply --check`、apply 和 reverse-check；
6. 若 lock 声明 locked environment，先要求显式 `--test-python` 可导入 `pytest` 与 `rich`，
   再以该环境执行 `uv sync --extra all --extra dev --locked --check --no-install-project`；
7. 三个 patch 应用后，复核 replay target 内相同生产源文件与已验证 artifact provenance 一致，
   再通过上游 `scripts/run_tests.sh` 执行锁定的 Core 测试，其中 ProcessRegistry、TUI server 和
   TUI queue/close race 文件均为强制门禁；
8. 清理 `__pycache__`、`.pyc`、`.pyo`、`.pytest-cache`、`.pytest_cache` 和
   `.ruff_cache` 并复核无残留；
9. 全部成功后才原子发布 target；从首个 partial/archive/fd 资源开始，任一步失败都统一
   归一为安全错误并清理本次资源。

重复执行必须使用新的 target 名称。已存在 target、上游更新、dirty source、patch
冲突、摘要变化或测试失败都会停止，不会尝试自动合并或修改当前 Hermes。

## Hermes 更新策略

上游 commit 变化时禁止直接套用旧补丁。应创建新的隔离 archive，重新完成 RED →
GREEN、相关 Core 回归和真实 entry-point 门禁；通过后新增兼容矩阵条目并更新 lock 和
patch digest。v1 公共方法、DTO 字段和语义不得破坏性修改；破坏性变化必须发布 v2，
并在迁移期同时保留 v1。

本次 RED → GREEN、隔离应用、相关回归及 live Core 零写入证据记录在
`evidence/`。早期验证共发生 3 次只读 live venv/pytest 探测；没有读取 live 源码或配置内容，
没有写入，也没有启动、停止或重启 Hermes 进程。验证器现已强制使用隔离 HOME，防止后续
上游 runner 再探测 operator HOME。Stage 3 证据证明 production composition、signed Plugin
Store、权威 catalog、受限 action path、Core output-parity v2 producer，以及隔离 Core wheel ×
Plugin wheel 三 UDS 装配；完整 Connector/Cloud/H5 与 Android/Web 端到端闭环仍须由后续
跨边界证据单独证明。
