# Stage 3 verification evidence

## 固定边界

- 上游：`hermes-agent 0.19.0`，commit
  `14db1a99e21e5523ee61f10f5c3300a5087e8449`。
- 当前 Stage 3 patch SHA-256：
  `7a10d91a2e18185f64d741e3df4608d1e175206c6f50017be963eae4ef0d0687`。
- 当前三阶段洁净 replay：
  `/Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-slice-a-replay-r1`。
- wheel/sdist 已从当前三阶段 replay 重建、锁定并验证 provenance。本次按明确范围未运行
  22k 完整 Core；下文完整 Core 结果是 teardown 补丁前的历史对比证据，不冒充本轮结果。
- live `/Users/apple/.hermes/hermes-agent` 始终只读；没有安装 wheel、修改配置、创建分支、
  启停或重启 Hermes，也没有部署。

## 本轮完成的生产语义

- Host Extension Manager 新增 signed Plugin Store v1：显式 manifest 与只读 trust-store 必须成对
  配置；Ed25519 验证不含 `signature` 字段的 canonical JSON，并校验 key/bundle 有效期、绝对规范
  路径、wheel SHA-256、唯一 entrypoint、immutable slot 与持久 rollback floor。未知 key、篡改、
  过期、symlink/path escape、editable/direct-url 和重复或额外 entrypoint 均 fail closed。
- Store 模块经 `importlib` 从 content-addressed slot 加载并验证 origin；未修改 `sys.path`，未使用
  `PYTHONPATH`/`.pth`，未向 Agent venv pip install。same-key Store candidate 优先于 pip candidate，
  active pointer 仅在 register 和全部 endpoint install 成功后原子提交。register/close 失败逆序回滚，
  close 失败保留 module 与 registration 供下一次 shutdown 重试。
- Core endpoint role 已对齐真实 Plugin 的 Local→Observer→Control 安装顺序。隔离源码 target 与真实
  Plugin wheel 打开三份 registry 和三个真实 AF_UNIX socket；Control 打开故障会按
  Observer→Local 逆序关闭且不写 active。最终 Core wheel 安装到临时 venv 后，从 site-packages
  证明真实 module origin，再以真实 Plugin wheel 重跑成功/故障两条，`2 passed`。

- TUI/Desktop `hermes serve` 在 uvicorn `_serve` coroutine 的 `finally` 内完成全局
  `PluginManager.shutdown_extensions()`，因此 normal return、task cancellation 与异常都在
  event loop 关闭前收口。Messaging Gateway 的公开 `start_gateway()` 同样以 async process
  wrapper 托管既有运行体，保留原 signal handler 不变。
- 全局 teardown owner 不会在退出时创建从未使用的 manager；同一 manager 成功 drain 后重复
  close 不会再次调用 `shutdown_extensions()`，失败则保留下一次 retry。Cron 是 Gateway
  进程内线程，仍先按既有 cooperative drain 退出，再由 Gateway process owner 收口扩展。
- CLI 和 ACP 的独立 process root 已完成审计，但本次最小补丁尚未为它们增加 process owner；
  它们是发布前仍需补齐的生产 surface，不能用本轮 H5/Gateway 证据冒充覆盖。

- CLI scheduler ACK 的预分配 UUID 现在直接传入真实 `AIAgent.run_conversation`，并由同一个
  原子 admission primitive 建立 `current_turn_identity()`、`turn_context`、output entity 与
  interrupt CAS 身份。任意 pending 或 active reservation（包括重复使用同一 UUID）都拒绝；
  setup/callback/terminal 路径清除 pending 和 current identity，下一 turn 不会复用 stale ID。
- TUI/Desktop owner prompt 只有在真实 Agent 已建立同一 `(turn_id, turn_generation)` 并执行
  admission callback 后才返回 accepted；background thread 启动和 `streaming` 不再算业务接纳。
  权威 busy queue 保留该预分配 ID 到实际 Agent turn，远程 owner prompt 强制走 distinct-turn
  queue 而不会套用本地 redirect/steer 策略，第二个远程 reservation 明确拒绝而不合并。
  Agent build/cancel 失败返回 rejected；0.5 秒短期限内无法确认则返回 `effect_unknown`，不会死锁。
  已 ACK 的队列项若在前序 Agent build 失败、取消、running 丢失或 session close 前仍未 admission，
  会在 history lock 下 claim，并以原 `turn_id` 发布唯一 terminal failure 后才暴露 idle；close
  fence 拒绝同时到达的新 prompt。close 与 background drain 在同一 history lock 下竞争
  唯一 queue ownership，terminal callback 只在 history lock 和全局 session resume lock 之外发布。
  compute/local dispatch 或 terminal emitter 抛异常时，仍使用原 `turn_id` 收敛 admission metadata，
  不泄露原始异常或留下永久 busy。
- terminal lifecycle 由 `ProcessRegistry` 权威进程回调产生，严格按 revision 1 `running` 到唯一
  revision 2 `completed|failed|interrupted`。output/lifecycle callback 竞争时仍只发布一次 running；
  terminal 状态吸收、旧 registration generation 的迟到回调被拒绝，UI `terminal.close` 不再被
  当作业务 terminal。显式 kill 现在只有在有界轮询确认进程真实退出后才发布 `interrupted`；
  PID/start-time 身份明确分为 `ours|gone_or_reused|unreadable`；只有 `gone_or_reused` 是退出证据。
  no-op、AccessDenied、暂时 start-time 不可读或其他未分类 process probe 异常均视为
  unknown survivor，返回 `effect_unknown` 并保持 running，后续 watcher 终态保持权威且
  不产生重复 lifecycle revision。
- 所有 caller 的 owner registration slot 每次 open 都从权威 session registry 读取当前
  tombstone revision。它不再依赖 caller 本地 revision 猜测，也不做 blind retry；sibling
  close、generation rollover，以及 authority bind 后 owner/output 局部注册失败都能以正确
  expected revision 恢复。
- CLI、TUI/Desktop、Messaging Gateway 的 callback、control/observer snapshot 和 output
  producer 冻结到注册时 identity。失败的 A→B rebind 不会读取可变 B 状态、对 B 执行动作，
  或把 B output 投递给 A observer；成功 B rebind 的 snapshot、action、output identity 均为 B，
  首个 event sequence 为 1。
- TUI 自动压缩与手工压缩通过同一个 session-key 收口点重注册 owner。成功时关闭 A 并注册 B；
  B 注册失败时清空 binding 并关闭旧 A authority，使 A/B 都 fail closed。
- 真实 Agent turn 使用 `(turn_id, turn_generation)` CAS。main-loop 结束路径只对真实 Agent
  调用一次权威 end primitive；轻量 test double 不会被第二个私有 seam 重复结束，真实 end
  异常不被吞掉。
- ACP/Cron 的真实 close→reopen 以及 owner/output post-bind partial failure 均通过相同权威
  revision source 恢复到 revision 2。

## 严格 TDD 记录

- Plugin Store 首轮 13 个签名、信任、路径、wheel、回滚与原子 active 测试先稳定为 `13 failed`，
  最小模块实现后 `13 passed`。Manager precedence/env seam 与真实 `local-gateway` role 第二轮先
  `4 failed`，实现后 `5 passed`；无 `register()` 与首次 close 失败重试两条深层清理先 `2 failed`，
  修复后 `2 passed`。真实三 UDS 与 Control 故障逆序回滚均通过；最终聚焦组合 `66 passed`。

- production teardown 首先通过真实 `web_server.start_server()` 与
  `gateway.run.start_gateway()` 入口增加 normal/cancel/exception 及 repeated-close 门禁；修复前
  稳定为 `8 failed`，全部是全局 manager 调用数 `0 != 1`。最小实现后为
  `8 passed, 21 deselected in 2.31s`；Stage 3 文件全量为 `29 passed in 0.85s`。测试同时断言
  shutdown 调用时 running loop 未关闭、生产入口返回后 loop 已关闭，未在测试中直接调用
  shutdown 冒充 lifecycle 证明。

- CLI ACK/Agent identity：预分配参数与 CLI adapter 两组测试先分别失败；最小实现后真实 relay、
  `current_turn_identity()`、admission callback、output identity 和 interrupt CAS 使用同一 UUID。
  后续同 UUID 并发重用测试先 `1 failed`（未拒绝），收紧 pending reservation 后相关门禁
  `5 passed`。
- TUI admission：Agent build failure、短 deadline、真实 admission callback 三组测试先
  `3 failed`；实现有界握手后 `3 passed`。busy queue 的 ID 丢失与第二 reservation 合并测试再先
  `2 failed`；携带 admission metadata 并 fail closed 后通过。随后 busy redirect 身份分叉测试
  先 `1 failed`，强制远程 prompt 进入 distinct-turn queue 后 admission 组合门禁 `6 passed`，
  TUI 全文件最终 `508 passed`。r20 针对 accepted queue 在 build failure、cancel、running 丢失、
  close 与新 prompt 竞态的首批测试先 `6 failed` 后 `6 passed`；进一步 close fence 测试先
  `2 failed`，最小实现后与相关 race 门禁共 `4 passed`。
- terminal lifecycle：初始 completed/failed/interrupted、revision、idempotence 与 generation
  测试共 `7 failed`；接入权威回调后 lifecycle focused `6 passed`。另一个 output/lifecycle
  并发测试先稳定复现重复 revision 1（`1 failed`），加 session-scoped 映射锁后 `1 passed`。
  r20 的 no-op、AccessDenied 与真实进程退出测试先 `3 failed`，最小确认语义后 `4 passed`；
  不可读 liveness 与 watcher 抢先终态测试再先 `2 failed`，修复后相关门禁 `4 passed`。
- r21 针对 `psutil.Process(pid)` 构造阶段抛出 `AccessDenied` 的回归测试先观察到
  `effect_unknown` 期望与 `error` 实际不一致（`1 failed`）；最小实现将该异常解释为“不可读而非
  已退出”，测试转绿。随后补齐 `NoSuchProcess|ZombieProcess` 正向退出证据及
  `PermissionError|OSError` 受控 fallback 的边界刻画；ProcessRegistry 全文件最终 `57 passed`。
- r22 先用 7 个回归测试稳定复现 7 个失败：暂时无法读取 detached PID start-time
  被伪造为 exited，detached kill 误报 `already_exited`，未分类 process probe 异常被当作
  死亡证据，closing drain 仍分发，dispatch/emitter 异常遗留 admission identity，close
  在 session resume lock 内发布 terminal。最小实现后针对门禁 `7 passed`，Process/TUI
  三文件 `580 passed`，与 extension 联合锁定门禁 `788 passed`。
- 真实 Git full 预检前的隔离运行暴露 unbound `AIAgent.run_conversation` relay doubles 缺少新的
  reserve primitive，既有 relay 文件 `4 failed`；增加只对真实 Agent 生效的 reserve/admit helper
  后该文件 `15 passed`，真实 Agent 仍强制权威 primitive。
- revision rollover 与 post-bind partial failure：新增测试先 `2 failed`，最小实现后 `2 passed`。
- turn-end double seam：既有 relay 4 个失败加新增真实 Agent 5 个失败，共 `9 failed`；最小
  helper 与 caller 改动后相关双文件门禁 `52 passed`，最终 full 中 relay 文件 `15 passed`。
- CLI/TUI/Gateway immutable failed rebind：先 `3 failed`；修复后失败 A→B、成功 B、snapshot、
  action 和 output identity/sequence 组合 `3 passed`。
- TUI compression common choke：auto/manual 成功与失败四种参数先 `4 failed`；fail-closed
  重注册后 `4 passed`。
- 初次把真实 turn primitive 接入完整 Agent seam 时暴露 6 个 Agent 文件共 23 个失败；补齐
  `turn_context` 兼容 seam 后该门禁 `56 passed`。
- replay 清理器遗漏 `test_durations.json` 的测试先 `1 failed`；纳入清理后通过。r22 的
  lock 内 TUI close/queue/Process 强制门禁测试先因命令未覆盖 TUI 而失败；补入命令后
  通过。wheel/sdist lock binding 和篡改在 target 创建前拒绝的两个测试先 `2 failed`，
  实现 digest 预校验后通过；bundle tool 全套 `31 passed`。
- artifact provenance 门禁曾以前一版 `bac11b2d...` Stage 3 patch 对更旧 retained artifacts
  执行，稳定因三个 production source member 与 replay source 摘要不一致而失败；重建并锁定
  当前 wheel/sdist 后转绿。门禁同时验证 Stage 3 patch SHA、wheel/sdist 内三个源文件摘要，
  并在三个 patch 应用后再次验证 replay target 源文件，防止旧产物仅靠更新外层 digest 混入。

## Replay、focused 与静态门禁

- 当前 bundle tool tests：`34 passed`；三阶段隔离 replay 返回
  `ok=true, stage=3`，并执行 lock 中的十文件门禁。最终 artifact replay target 上门禁为
  `10 files, 817 tests passed, 0 failed in 27.3s (2 workers)`，该次没有 flake。更早的
  teardown 预产物门禁中，`tests/test_tui_gateway_server.py` 的既有并发 stdout 测试曾首次出现一次
  `9 != 8`、自动 retry 后通过；最终 artifact run 未复现，仍保留为历史风险。
- 当前修改 Python 文件 Ruff：`All checks passed!`。

- `tools.apply_and_verify` 在最终 target 上完成三补丁顺序 apply-check、apply、reverse-check、
  locked environment check 和锁定测试，返回 `ok=true, stage=3`。
- 当前 `APPLIED_BUNDLE.json` 中三个 patch digest 与 lock 完全一致：
  `0d5b946e...`、`f1f06c53...`、`7a10d91a...`；两个 retained artifact digest 为
  `885bfff7...`、`f7e4a7e3...`，并记录 `7a10d91a...` 与五个 replay source member 的
  provenance。
- all+dev/no-project 环境的 `uv sync --extra all --extra dev --locked --check
  --no-install-project` 报告 `Audited 103 packages`、`Would make no changes`；`pytest`、`rich`
  均可导入。历史 broad-full 或已安装 project 的 venv 都被 locked check 安全拒绝；r20 在 `.tmp`
  新建精确 all+dev/no-project 环境后通过，拒绝尝试未计为 replay 通过。
- Stage 3 新增 5 个测试文件：`5 files, 173 passed, 0 failed`。r22 锁定 extension/
  Process/TUI 联合门禁为 `788 passed, 0 failed`。
- 完整 extension surface：`51 passed`；ProcessRegistry 全文件：`57 passed`；
  `run_agent` 全文件在双方都缺少可选 `anthropic` 依赖的单一既有节点之外为
  `221 passed, 1 deselected`。
- ProcessRegistry 与 TUI 三文件 `580 passed`；完整 extension 五文件 `173 passed`。
- 两组 Plugin Manager 附加回归分别为 `55 passed` 与 `52 passed`；bundle
  apply/reverse/isolation/artifact binding/provenance/console 测试：`34 passed`。
- 全部改动 Python 文件 Ruff 通过。
- 最终 replay 中 `__pycache__`、`.pyc`、`.pyo`、Pytest/Ruff cache 和
  `test_durations.json` 均为 0。

## 同环境完整 Core runner（teardown 补丁前的历史证据）

最终比较使用同一当前 interpreter/package set（134 packages，`pip list --format=json` SHA-256
`379724d739e06f1e2be5debf067d8c9578b4eb309b1fd69f9c7e65bfa0ad455a`）下的干净、固定 commit、
真实 Git baseline 与 r22 candidate。baseline 复用 r21 已完成的真实 Git 结果；复用前重新核对
134-package fingerprint 完全一致。candidate 使用独立 HOME、HERMES_HOME、XDG 运行
`2469 files, j4`，运行期间没有并行构建或候选工作树写入。candidate 为保持同一
文件拓扑而排除 5 个 Stage 3 新增测试文件，这些文件已由独立 `173 passed` 门禁
覆盖；既有文件中新增加的 28 个测试计入 full run。

- current-env baseline：`22693 passed, 80 final failed, 953.1s`。
- r22 candidate：`22724 passed, 80 final failed, 1729.9s`。
- baseline 的 `test_base_environment` 并发节点首轮失败后 retry 通过；candidate 没有
  retry-pass flake。两侧最终失败节点集合均为 80 个且完全相等，集合
  SHA-256 都是 `718eb4aae321c14060da2fdc60ae8d07c6179db2bc40464d746a9b8155d84d34`。
  baseline-only=0、candidate-only=0、patch-only failure=0。
- 两侧日志 SHA-256 分别为 `9ac10b1fb191...` 与 `86b970f98d28...`；完整路径、精确节点和历史
  无效 run 排除原因保存在 `evidence/stage3-full-suite-node-comparison.json`。
无效 run 均永久排除：历史一次 run 在 42.8% 时受并行 wheel build 污染；r19 的两次审计中止
分别暴露 same-ID concurrency 与 TUI queue identity 缺陷；final3 在 53.8% 时因 replay 没有
真实 `.git` 元数据而使 update-path 结果失真。最终 baseline 与 r22 candidate 都来自真实 Git
工作树，启动前 build/egg-info/cache/bytecode 为 0。

## Wheel 与 sdist（当前 signed Plugin Store 补丁产物）

- final wheel：`hermes_agent-0.19.0-py3-none-any.whl`，SHA-256
  `885bfff72d203ad0cea88b7906d2b5845bc089b37c79696a1fa02a9aea89854c`。
- final sdist：`hermes_agent-0.19.0.tar.gz`，SHA-256
  `f7e4a7e3292af70a04bc749fd49dc00fc189b53dbfb2e5e0e10de100c48602bc`。
- 两个 artifact 内的 `gateway/run.py`、`hermes_cli/plugins.py`、
  `hermes_cli/web_server.py`、`hermes_cli/plugin_store_v1.py`、
  `hermes_cli/extension_runtime.py` 均与当前三阶段 replay target 一致。lock 将五个摘要绑定到
  `7a10d91a...` Stage 3 patch，replay 前后均 fail closed 验证。
- wheel console metadata 固定权威 H5 入口为 `hermes = hermes_cli.main:main`；
  `hermes-agent = run_agent:main` 仍存在，但不作为本地 H5 serve/cutover 入口。
- wheel 与 sdist 均包含 policy/event/snapshot 三份冻结资源；大小分别为 4616、20143、11840
  bytes，SHA-256 分别为 `f20350c4...`、`e4a5c4b...`、`0eab82d2...`。
- 使用 `python3 -I -S -B`、仅插入各自解包目录，在隔离环境中分别从 wheel 和 sdist 导入，均得到
  SPI version 1、`session.observe.output-parity.v1` capability、policy version 2；导入树未生成
  cache，且没有安装到 live。
- lock 记录 `HERMES_NIX_BUILD=1`、冻结 source epoch 与 `uv build --wheel --sdist`
  完整命令。本轮保留产物由 lock digest、内部 provenance、隔离 replay 与临时 venv
  site-packages origin 共同验证；本轮未执行第二次独立构建，因此不新增字节级可重复性声明。

## Live probe 偏差与不声明事项

- 历史上一共发生 3 次 early live runner probe；仅检查 live venv/activate/Python/pytest-guard
  路径，并只用该 Python 尝试导入 pytest。没有读取 live 源码或配置内容，没有写入、安装、
  更新、启停或重启 Hermes。验证器现已强制隔离 HOME/HERMES_HOME/XDG；细节见
  `evidence/live-core-read-only.json`。
- 本证据不声明 live Hermes 已应用补丁。真实 Plugin-owned 三 UDS opener 已在隔离 Core
  source/final wheel 中验证，但不声明 Connector client、Cloud/H5、Android/Web 或部署端到端
  已闭环；这些需要后续跨边界实机证据和单独授权。
