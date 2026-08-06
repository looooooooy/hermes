# Hermes 本地安装验证记录

## 验证范围

本记录用于补充 CI 之外的发布包本地安装检查。验证对象为 Draft PR #1 对应候选版本，测试机为 Linux x86_64、Python 3.13.5。正式支持与发布验收仍以 Python 3.12 和 macOS Connector 运行环境为准。

## 初次安装发现

初次下载并校验 `hermes-runtime-bundle-37e12c6d18cd280c62fd3163f34f6ebac2d17d07` 后完成以下检查：

- ZIP digest 与 GitHub Artifact digest 一致；
- `sha256sum -c SHA256SUMS` 全部通过；
- patched Core、Plugin、Connector wheel 可解包并安装；
- `hermes --version`、`hermes --help`、`hermes-connector --help` 可运行；
- Plugin entry point 可发现并加载；
- Plugin 可在隔离 Hermes Home 中启用；
- patched Host SPI 的 acquire、prompt、幂等重放、command status、runtime generation rollover、旧绑定拒绝及逆序关闭 smoke test 通过。

本地执行 `hermes doctor` 时发现 Core wheel 只包含 `hermes_state.py`，但该模块依赖的以下文件没有进入 wheel：

- `hermes_state_common.py`
- `hermes_state_portability.py`
- `hermes_state_schema.py`
- `hermes_state_search.py`

固定上游源码包含这些文件，缺陷来自固定版本 `pyproject.toml` 的 `py-modules` 清单遗漏，而不是运行时配置或本地依赖缺失。

## 修复与永久门禁

Stage 3 stabilization patch 现已同时修复 `pyproject.toml`，将四个状态模块纳入 Core wheel。发布门禁新增：

- `pyproject.toml` 独立 build provenance；
- stabilization patch 重生成与 drift 检查；
- wheel 解包必须包含完整 `hermes_state*` 模块族；
- 安装后的 `importlib.metadata.distribution("hermes-agent").files` 合同检查；
- Core wheel/sdist 摘要重新锁定；
- Cloud integration source lock 与发布手册摘要同步。

修复后的锁定产物为：

```text
hermes_agent-0.19.0-py3-none-any.whl
SHA-256 314593d41fd8d7673bea30310119256fee577232fd042ae4b5d005c2bdd9acea

hermes_agent-0.19.0.tar.gz
SHA-256 cd29a0696834c689108fc17b82b51ad925507d7179c852a467bfafca582ad45d
```

## 本地环境限制

当前测试容器不能访问公共 PyPI，内部镜像也缺少 Hermes 固定依赖中的部分版本，例如 `openai==2.24.0` 和 Connector 的部分依赖。因此：

- 完整在线依赖解析不能在该容器中完成；
- wheel、entry point、CLI、Host SPI 与控制链使用隔离安装加系统现有依赖完成验证；
- 该限制不得被记录为 Hermes 包依赖冲突；
- 完整依赖安装、macOS UDS 和真实模型调用仍需在 staging 环境执行。

## 完成条件

最终候选发布前必须再次：

1. 下载新 head 对应的 runtime bundle；
2. 校验 Artifact digest 与 `SHA256SUMS`；
3. 确认 wheel 包含五个 `hermes_state*` 文件；
4. 安装 patched Core 与 Plugin，并确认 entry point 可加载；
5. 在隔离 Hermes Home 中运行 CLI、Plugin 启用、`doctor` 与 Host SPI smoke；
6. 确认不再出现 `ModuleNotFoundError: hermes_state_common`；
7. 保持 PR 为 Draft，直至 Issue #2 的真实 staging 全链路及回滚演练完成。
