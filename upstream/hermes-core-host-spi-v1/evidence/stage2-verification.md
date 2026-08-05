# Stage 2 验证证据

- 上游：`hermes-agent==0.19.0`
- 固定 commit：`14db1a99e21e5523ee61f10f5c3300a5087e8449`
- Stage 1 SHA-256：`0d5b946e809e0792305a1f0b8f234ece6054b7eb14d208c871b53a03c9bcf8aa`
- Stage 2 SHA-256：`f1f06c530c9605470a31aaf68fc0ca49729b83a415cfc6bee2bf939e6bcb71b2`
- 正式隔离 target：`/Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7`

## 质量审查 RED

加入异常/倒退时钟、统一 finite timeout、资源总边界、自动 lease 撤销、共享 shutdown
deadline、Thread.start 回滚、audit 安全、结构化失败、Unicode surrogate 和精确 UTF-8 边界
后，Stage 2 首次执行为 `24 passed, 20 failed`。随后自审另取得 worker clock 异常未退役和
audit 异常仍保留 `__context__` 两个 RED。
最后自审还收紧了 activation 线性化：worker 必须在共享锁内启动，消除 close 可能先于
Thread.start 的窗口。

最终质量复审新增 authority 调度失败原子性和真实 shutdown deadline 用例。修复前首次
bind 的 Timer 创建/启动失败会遗留 record，续租失败会撤销旧 binding，generation rollover
失败会提前提交 generation；冻结注入时钟时 4 个阻塞 worker 的 close 耗时约为 `0.34s`，
超过单一 `0.08s` deadline 的约束。新增用例取得 `8 failed, 43 passed` RED，其中一个失败
来自 RED 用例清理前中断造成的线程干扰；补齐测试 finally 清理后对应行为 RED 均可独立复现。

r6 复审继续以真正独立线程和 Barrier 复现 callback 在 registry lock 外提前进入的竞态：首次
bind、续租、generation rollover 均错误成功，取得 3 个 RED。同一 Registration 的 4 个
并发 close 最大调用耗时约 `0.35s`，证明 deadline 在取得 `finish_lock` 后才创建，取得第 4 个
RED。独立状态锁线性化和 lock-wait deadline 修复后，定向执行为 `4 passed`。

补丁工具新增验证命令主动生成 `__pycache__`、`.pyc`、`.pytest_cache` 和 `.ruff_cache`，
首次执行确认这些内容被带入成功 target，取得独立 RED。

Stage 2 历史 RED 命令：

```bash
cd /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-stage2-rework-dev
HERMES_PYTHON=/Users/apple/hermesmobile/hermes-cloud/.venv/bin/python \
PYTHONDONTWRITEBYTECODE=1 \
scripts/run_tests.sh -j 1 tests/hermes_cli/test_extension_runtime_stage2.py -q
```

缓存清理 RED/GREEN 命令：

```bash
cd /Users/apple/hermesmobile/upstream/hermes-core-host-spi-v1
PYTHONDONTWRITEBYTECODE=1 \
/Users/apple/hermesmobile/hermes-cloud/.venv/bin/python \
  -m pytest -p no:cacheprovider \
  tests/test_apply_and_verify.py::test_success_target_removes_all_python_and_test_cache_artifacts -q
```

## GREEN

### 正式隔离应用

```bash
cd /Users/apple/hermesmobile/upstream/hermes-core-host-spi-v1
PYTHONDONTWRITEBYTECODE=1 \
/Users/apple/hermesmobile/hermes-cloud/.venv/bin/python \
  -m tools.apply_and_verify \
  --source /Users/apple/.hermes/hermes-agent \
  --workspace-root /Users/apple/hermesmobile/.tmp \
  --target /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7 \
  --test-python /Users/apple/hermesmobile/hermes-cloud/.venv/bin/python
```

```json
{"ok": true, "stage": 2, "target": "/Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7", "upstream_commit": "14db1a99e21e5523ee61f10f5c3300a5087e8449"}
```

`APPLIED_BUNDLE.json` 按顺序记录 `0001`、`0002` 和锁定摘要；target 不含 `.git`。

### 最终复审定向门禁

```bash
cd /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7
PYTHONDONTWRITEBYTECODE=1 \
/Users/apple/hermesmobile/hermes-cloud/.venv/bin/python \
  -m pytest -p no:cacheprovider \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_first_bind_timer_failure_is_atomic_and_retryable \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_authority_timer_firing_before_commit_fails_without_state_change \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_failed_authority_renewal_preserves_binding_observer_and_timer \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_failed_generation_rollover_preserves_all_sessions_and_observers \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_close_all_uses_one_real_deadline_when_injected_clock_is_frozen \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_invalid_clock_fails_closed_during_prepare_and_activate_only \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_async_early_fire_first_bind_is_atomic_and_retryable \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_async_early_fire_renewal_preserves_old_authority_and_observer \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_async_early_fire_rollover_preserves_all_old_generation_state \
  tests/hermes_cli/test_extension_runtime_stage2.py::test_concurrent_close_on_one_registration_includes_finish_lock_wait -q
```

```text
11 passed
```

### Stage 1 + Stage 2 锁定门禁

```bash
cd /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7
HERMES_PYTHON=/Users/apple/hermesmobile/hermes-cloud/.venv/bin/python \
PYTHONDONTWRITEBYTECODE=1 \
scripts/run_tests.sh -j 2 \
  tests/hermes_cli/test_extension_host_v1_contract.py \
  tests/hermes_cli/test_extension_runtime_stage2.py \
  tests/hermes_cli/test_gateway_extension_registration.py \
  tests/hermes_cli/test_plugins.py -q
```

```text
112 passed, 0 failed
Stage 1 contract: 6; Stage 2 runtime: 55; Gateway registration: 16; Plugin lifecycle: 35
```

Stage 2 覆盖新增质量边界：

- bind/resolve/prepare/activate/worker 的非数值、bool、NaN、Inf、异常和倒退注入时钟；
- 首次 bind、续租和多 session generation rollover 的 scheduler/Timer 创建或启动失败完全原子；
- scheduler 回调早于 authority commit 时安全失败且不消耗 record、revision 或 generation；
- 独立线程与 Barrier 证明异步 early-fire 在 registry lock 前完成 armed/fired 线性化；
- 64 global、8 per-scope、4 blocked/closing admission 上限；
- token/revision-safe 自动 lease 撤销、stale timer、到期接管；
- 冻结注入时钟下 N 个阻塞 sink 仍共享真实 monotonic shutdown deadline，worker/cleanup
  Thread.start 失败回滚；
- 同一 Registration 的 N 个并发 close 将 `finish_lock` 等待计入单项 absolute deadline；
- audit 事件/字段/枚举白名单、递归敏感键、profile/generation 和 sink 异常无 cause/context；
- delivery/cleanup/clock/start 的安全 failure status/counter；
- 孤立 surrogate 归一为安全 frame error，UTF-8 编码帧精确 256 KiB 通过、超一字节拒绝；
- 使用 Broker condition 证明进入 `CLOSING` 后再释放 sink，不依赖固定 sleep 竞态。

正式 target 的 Stage 2 文件连续执行 10 次全部通过：

```bash
cd /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7
for iteration in {1..10}; do
  PYTHONDONTWRITEBYTECODE=1 \
  /Users/apple/hermesmobile/hermes-cloud/.venv/bin/python \
    -m pytest -p no:cacheprovider tests/hermes_cli/test_extension_runtime_stage2.py -q \
    >/dev/null || exit 1
done
```

### lock 中明确列出的相关回归

```bash
cd /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7
PYTHONDONTWRITEBYTECODE=1 \
/Users/apple/hermesmobile/hermes-cloud/.venv/bin/python \
  -m pytest -p no:cacheprovider \
  tests/hermes_cli/test_plugin_auxiliary_tasks.py \
  tests/hermes_cli/test_plugin_cli_registration.py \
  tests/hermes_cli/test_plugin_scanner_recursion.py \
  tests/hermes_cli/test_plugins_transcription_registration.py \
  tests/hermes_cli/test_plugins_tts_registration.py \
  tests/hermes_cli/test_safe_mode.py \
  tests/hermes_cli/test_startup_plugin_gating.py \
  tests/test_plugin_skills.py tests/test_transform_tool_result_hook.py -q
```

```text
55 passed
```

```bash
cd /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7
PYTHONDONTWRITEBYTECODE=1 \
/Users/apple/hermesmobile/hermes-agent-plugin/.venv/bin/python \
  -m pytest -p no:cacheprovider \
  tests/agent/test_context_engine_host_contract.py \
  tests/hermes_cli/test_plugins_cmd.py \
  tests/hermes_cli/test_plugins_cmd_enable_disable_nested.py -q
```

```text
52 passed
```

### 补丁工具、Ruff 与缓存扫描

```bash
cd /Users/apple/hermesmobile/upstream/hermes-core-host-spi-v1
PYTHONDONTWRITEBYTECODE=1 \
/Users/apple/hermesmobile/hermes-cloud/.venv/bin/python \
  -m pytest -p no:cacheprovider tests/test_apply_and_verify.py -q
/Users/apple/hermesmobile/hermes-cloud/.venv/bin/ruff check --no-cache tools tests

cd /Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage2-r7
/Users/apple/hermesmobile/hermes-cloud/.venv/bin/ruff check --no-cache \
  hermes_cli/extension_runtime.py tests/hermes_cli/test_extension_runtime_stage2.py
find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -print
find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -print
```

```text
patch tool: 12 passed
bundle Ruff: All checks passed!
Stage 2 Ruff: All checks passed!
final cache scans: empty
```

正式 target 与 TDD 副本中的 Runtime 和 Stage 2 测试逐文件 SHA-256 相同。

## 生命周期边界与未证明范围

Python 无法安全强杀任意正在执行的同步 callable。超时 delivery 不会被假装关闭：它保持
登记内 `CLOSING`，停止接收事件，提供 blocked/pending 状态与安全失败记录；全局/每 scope/
blocked admission 上限阻止资源无限累积，且所有 Observer 共享 shutdown deadline。sink
返回后再次 close 才完成 Registration cleanup。

本 Stage 不提供 Owner Action Router，不宣告 `session.observe.output-parity.v1`，也未接入
TUI、Desktop、CLI、Gateway、ACP 或 Worker composition root，因此不证明生产闭环。
