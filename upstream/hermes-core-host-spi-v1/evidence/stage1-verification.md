# Stage 1 验证证据

- 上游：`hermes-agent==0.19.0`
- 固定 commit：`14db1a99e21e5523ee61f10f5c3300a5087e8449`
- 补丁 SHA-256：`0d5b946e809e0792305a1f0b8f234ece6054b7eb14d208c871b53a03c9bcf8aa`
- 最终隔离 target：
  `/Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage1-r4`

## RED 证据

1. 两个 Core 测试文件首次通过 `scripts/run_tests.sh` 执行，均因
   `ModuleNotFoundError: hermes_cli.extension_host_v1` collection error 失败。
2. 公共契约最小实现后，Extension 生命周期测试 8/8 因
   `PluginManager.__init__()` 不接受 Host/capability 注入而失败。
3. 回滚关闭失败测试观察到同一句柄被立即二次关闭，随后修正为失败句柄保留到下一次
   drain。
4. 补丁工具在 ignored parent Git worktree 中观察到 `git apply` 将 patch 全部标记为
   `Skipped` 但返回 0；随后加入 disposable inner Git repository，并新增回归测试。
5. 补丁工具观察到解析 venv Python symlink 会丢失 venv site-packages；随后保留运维显式
   提供的 launcher 路径，并新增回归测试。
6. Extension install 异常文本会进入 Plugin error/log；新增秘密文本回归测试后，改为
   稳定安全错误 `gateway extension installation failed`。

### 独立质量审查追加 RED

1. `PluginManager(host=...)` 在未显式声明 capability 时错误地自动开放全部能力；非法类型
   和未知能力也被静默降级或接受。
2. 并发 install 可在 drain 快照之后写入 Registration，导致 shutdown 返回后仍有未关闭
   句柄；closing 期间仍可开始新 install；同线程 install/close 重入没有 fail closed。
3. Plugin 在成功注册 Extension 后抛出 `BaseException` 时，句柄没有回滚，但异常仍向上
   传播。
4. source 指向 Git worktree 子目录时未在入口拒绝；工具随后生成空 archive 并以通用错误
   失败。
5. dangling target symlink、symlink workspace 以及含 `symlink/..` 的父路径会在 resolve 后
   绕过原始路径检查。
6. patch 在摘要校验后、`git apply` 前被替换时，工具应用的是替换内容；evidence 又二次
   读取 patch，形成 TOCTOU。
7. partial 创建后 `mkstemp` 失败会遗留目录并泄漏原始 `OSError`，没有统一资源清理。
8. DTO `repr` 暴露 durable session key 与 command id，规范文本允许换行、NUL 和 DEL
   控制字符。
9. Registration 的 `close` 属性在 contract validation 中抛出 `BaseException` 时，install
   gate 计数没有在 `finally` 释放，后续 drain 被误判为同线程重入。

## GREEN 证据

### 补丁工具

```text
10 passed in 1.63s
ruff: All checks passed!
```

测试使用 `PYTHONDONTWRITEBYTECODE=1` 与 `pytest -p no:cacheprovider`；永久包中的历史
`.pytest_cache`、`__pycache__` 和 `.pyc` 生成物已清理。

### 正式隔离应用

```json
{"ok": true, "stage": 1, "target": "/Users/apple/hermesmobile/.tmp/hermes-core-host-spi-v1-verified-stage1-r4", "upstream_commit": "14db1a99e21e5523ee61f10f5c3300a5087e8449"}
```

隔离 target 的 `APPLIED_BUNDLE.json` 记录的 commit 和 patch SHA-256 与 lock 一致；target
不包含 `.git`。四个 patch 交付文件与 TDD 开发副本逐文件 SHA-256 相同。

### 锁定 Core 门禁

```text
3 files, 57 tests passed, 0 failed
```

覆盖公共 DTO/Protocol、安全 `repr`、真实 pip entry-point discovery、capability/version
fail closed、显式 capability 子集与类型门禁、并发 install/drain、统一生命周期状态、
同线程重入拒绝、`BaseException` 回滚、逆序幂等 shutdown、失败句柄重试、force reload
和 disable 后重新发现。

### 相关 PluginManager 回归

```text
9 clean-environment files, 55 tests passed, 0 failed
3 dependency-heavy files, 52 tests passed, 0 failed
```

第二组使用具备 Hermes Plugin 开发依赖的隔离 venv；第一组使用不安装本项目 entry-point
的隔离 venv，避免污染 Hermes 的真实 entry-point 计数断言。首次把 3 个依赖密集文件
放入轻量 venv 时仅出现缺少 `httpx`、`requests`、`rich` 的环境失败；在完整依赖 venv
逐文件重跑后全部通过。锁定门禁始终使用未安装本项目 entry-point 的隔离 venv。

## 未证明范围

本证据不覆盖 Session Authority、Observer Broker、Owner Action Router、CLI/TUI/
Gateway/ACP/Worker composition root 或真实 Agent 控制效果。这些能力在兼容矩阵中仍为
`not_implemented_stage_1` / `unavailable`。
