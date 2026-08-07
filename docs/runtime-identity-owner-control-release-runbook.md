# Hermes Runtime Identity 与 Owner-Control 发布、回滚及验收手册

## 1. 适用范围

本手册适用于以下交付组合：

- 固定 Hermes Core `0.19.0`，上游提交 `14db1a99e21e5523ee61f10f5c3300a5087e8449`；
- `hermes-agent-plugin 0.1.0`；
- `hermes-connector 0.1.0`；
- Runtime identity、Session authority、owner action、observer、receipt 与本地控制通道。

生产部署采用两个独立 Python 环境：

1. **Agent 环境**：patched Hermes Core + Hermes Agent Plugin；
2. **Connector 环境**：Hermes Connector 及其独立依赖。

不要把 Connector 的完整依赖强制安装到 Hermes Agent 环境。Connector 是独立边缘进程，独立环境可以隔离数据库、加密和网络依赖，并缩小回滚影响面。

## 2. 发布准入条件

只有同时满足以下条件，才允许进入试运行：

- Draft PR 的 `Runtime control`、`Plugin connector control`、`Hermes Core Host SPI` 全部通过；
- Core Stage 3 补丁可从固定上游源码精确重生成；
- Core wheel 与 canonical sdist 摘要与 `upstream.lock.json` 一致；
- Core 权威 owner-action 回归通过；
- Plugin、Connector wheel 构建通过；
- patched Core、Plugin、Connector 的最小联合契约通过；
- macOS Plugin ↔ Connector UDS E2E 通过。

锁定的 Core 产物：

```text
hermes_agent-0.19.0-py3-none-any.whl
SHA-256 314593d41fd8d7673bea30310119256fee577232fd042ae4b5d005c2bdd9acea

hermes_agent-0.19.0.tar.gz
SHA-256 cd29a0696834c689108fc17b82b51ad925507d7179c852a467bfafca582ad45d
```

## 3. 获取和校验发布包

`Hermes Core Host SPI` 工作流成功后会生成：

```text
hermes-runtime-bundle-<git-sha>
```

包内至少包含：

```text
hermes_agent-0.19.0-py3-none-any.whl
hermes_agent-0.19.0.tar.gz
hermes_agent_plugin-0.1.0-py3-none-any.whl
hermes_connector-0.1.0-py3-none-any.whl
SHA256SUMS
RELEASE.txt
```

下载后先校验：

```bash
cd hermes-runtime-bundle
sha256sum -c SHA256SUMS
```

macOS 可使用：

```bash
shasum -a 256 -c SHA256SUMS
```

任何摘要不一致都必须停止安装，不允许现场重新打包后继续。

## 4. 安装前备份

记录当前状态：

```bash
python --version
python -m pip freeze > before-upgrade-agent.txt
```

备份以下内容，实际路径以当前部署为准：

- Hermes Agent 配置；
- Plugin 配置和本地端点目录；
- Connector 配置、身份材料和本地状态库；
- 当前 Hermes Core wheel 或可重装版本；
- 当前启动服务定义、LaunchAgent 或 systemd unit。

升级前停止 Connector 的新命令接收，并确认没有未决 owner action。无法确认执行效果的命令必须保留为 `effect_unknown`，不得伪造成功回执。

## 5. Agent 环境安装

创建新的候选环境，不覆盖当前环境：

```bash
python3.12 -m venv /opt/hermes/agent-next
/opt/hermes/agent-next/bin/python -m pip install --upgrade pip
/opt/hermes/agent-next/bin/python -m pip install \
  ./hermes_agent-0.19.0-py3-none-any.whl \
  ./hermes_agent_plugin-0.1.0-py3-none-any.whl
/opt/hermes/agent-next/bin/python -m pip check
```

验证 Plugin entry point：

```bash
/opt/hermes/agent-next/bin/python - <<'PY'
from importlib.metadata import entry_points, version

assert version("hermes-agent") == "0.19.0"
assert version("hermes-agent-plugin") == "0.1.0"
points = list(
    entry_points().select(
        group="hermes_agent.plugins",
        name="hermes-agent-plugin",
    )
)
assert len(points) == 1
assert points[0].load().__name__ == "hermes_agent_plugin"
print("agent-plugin preflight: ok")
PY
```

## 6. Connector 环境安装

```bash
python3.12 -m venv /opt/hermes/connector-next
/opt/hermes/connector-next/bin/python -m pip install --upgrade pip
/opt/hermes/connector-next/bin/python -m pip install \
  ./hermes_connector-0.1.0-py3-none-any.whl
/opt/hermes/connector-next/bin/python -m pip check
/opt/hermes/connector-next/bin/hermes-connector --help
```

验证最小 owner-control 模块不会提前加载数据库运行时：

```bash
/opt/hermes/connector-next/bin/python - <<'PY'
import sys
import hermes_connector
from hermes_connector.application.owner_control_lane import OwnerControlLane

assert hermes_connector.ConnectorConfig.__name__ == "ConnectorConfig"
assert OwnerControlLane.__name__ == "OwnerControlLane"
assert "hermes_connector.adapters.sqlite_storage" not in sys.modules
print("connector control-plane preflight: ok")
PY
```

## 7. 切换顺序

按以下顺序执行蓝绿切换：

1. 保持旧 Agent 与旧 Connector 可回退；
2. 启动新 Agent，确认 Plugin discovered、Host SPI ready；
3. 确认 Plugin 注册 `local-gateway`、`observer`、`control` 三类端点；
4. 启动新 Connector；
5. 确认 Connector 建立本地控制连接；
6. 再开启 Cloud WSS 和远程命令接收；
7. 完成验收后，停止旧 Connector，再停止旧 Agent。

禁止先停止旧环境再开始安装。

## 8. 运行验收

至少执行以下用例：

| 用例 | 预期结果 |
|---|---|
| `session.control.acquire` | 返回有效 lease，绑定 profile、session 和 runtime generation |
| `prompt.submit` | 真实主循环接收，返回 client/server turn ID，重复命令不重复执行 |
| `session.interrupt` | 权威 Session 中断，回执与实际效果一致 |
| `approval.respond` | 仅解决对应 pending approval，不跨 Session |
| `clarify.respond` | 仅解决对应 pending clarification |
| runtime generation 滚动 | 旧 lease、旧 Session 和旧控制通道立即失效 |
| Connector 重启 | 不重复执行已完成命令，可恢复未发送回执 |
| Cloud 短暂断线 | 本地 Agent 不失控，恢复后继续同步 |
| 相同 request ID 不同 payload | 失败关闭，返回冲突，不执行第二次 |
| 无法确认执行结果 | 返回 `effect_unknown`，不得标记 completed |

验收期间同时检查：

- 日志不包含 prompt 正文、approval 正文、令牌或凭证；
- pending command、outbox、inbox 不持续增长；
- Plugin/Connector 断开后健康状态能及时变为 unavailable；
- Agent 与 Connector 重启后 runtime generation 不复用。

## 9. 回滚

触发任一条件立即回滚：

- Host SPI 无法 ready；
- Plugin 注册失败或关闭失败；
- Connector 无法建立本地控制通道；
- owner action 出现重复执行；
- 回执与真实 Session 效果不一致；
- pending command 持续堆积；
- 敏感数据进入日志。

回滚顺序：

1. 停止 Cloud 新命令入口；
2. 停止新 Connector；
3. 将未确认命令标记为 `effect_unknown` 并保留审计证据；
4. 停止新 Agent；
5. 恢复旧 Agent 环境和旧 Plugin 配置；
6. 恢复旧 Connector 环境及状态库备份；
7. 启动旧 Agent，再启动旧 Connector；
8. 验证旧环境只接收属于当前 runtime generation 的命令。

不要通过手工修改数据库把未知命令改成成功。

## 10. 完成标准

发布闭环完成必须同时具备：

- 同一 Git commit 的三条 PR 门禁全绿；
- 可下载且带 SHA256SUMS 的发布包；
- 双环境安装成功并通过 `pip check`；
- 运行验收全部通过；
- 回滚演练成功；
- 发布记录包含 Git SHA、Core 上游 SHA、包摘要、操作者、开始时间、结束时间和异常记录。

PR 在完成现场验收和回滚演练前保持 Draft，不直接合并。
