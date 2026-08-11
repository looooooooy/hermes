# Hermes Desktop 版本化发布、升级与阿里云 OSS 制品管理架构

- 状态：P0 设计冻结候选
- 日期：2026-08-07
- 适用范围：Hermes Desktop / Runtime Manager / Managed Runtime / Installer / Release Control Plane
- 前置：`DESKTOP-020B` 完成可安装制品与裸机闭环
- 后续执行项：`DESKTOP-021 Release & Update Plane`

## 1. 决策摘要

Hermes 的客户安装闭环不能止于“能安装一次”。商用软件必须继续闭环：

```text
Build
→ Sign
→ Publish
→ Discover update
→ Download
→ Verify
→ Stage
→ Activate
→ Health gate
→ Rollback
→ Observe
→ Promote / Pause / Block
```

本设计冻结以下原则：

1. **OSS 不是版本真相源。** OSS 只承担签名制品、清单、证据的持久化与分发 Origin。
2. **版本真相是签名 Release Manifest。** Runtime Manager 只信任 Hermes 发布密钥签名、内容摘要、兼容矩阵和单调 generation。
3. **发布物不可覆盖。** `release_id`、内容寻址 artifact key 一经发布不得原地修改；修改必须生成新 release。
4. **“latest” 不是版本。** latest/stable/beta/canary 只是可回滚的签名 channel pointer。
5. **Runtime Manager 继续保持唯一生命周期权威。** OSS/CDN/Cloud 只能提供候选版本，不能直接激活本机 Runtime。
6. **下载与激活彻底分离。** 所有更新先进入 inactive staging/release slot，校验完成后才切换。
7. **默认保留上一已知健康版本。** 任何升级失败均回滚 previous known-good release。
8. **Cloud 管策略，OSS 管文件。** 企业锁版、灰度、强制安全升级、阻断列表由 Release Control Plane 决策；大文件由 OSS/CDN 传输。
9. **客户端永不持有 OSS AccessKey/Secret。** 下载使用 Cloud 下发的短时访问地址或 CDN URL 鉴权。
10. **平台签名 + Hermes 跨平台签名同时存在。** Apple/Windows/Linux 平台签名解决 OS 信任；Hermes Ed25519 Release Manifest 解决产品供应链、跨平台一致性和 anti-rollback。

## 2. 当前仓库已有基础

当前 Runtime Manager 已具备：

- `LifecycleState::Updating`
- `LifecycleState::RollingBack`
- `active_release`
- `previous_release`
- `ManagedReleaseManifestV1`

macOS Activation Controller 已具备：

- immutable release；
- pending activation recovery；
- old/candidate authority 验证；
- health gates；
- activation receipt；
- activation failure rollback；
- rollback failure blocked evidence。

现有 Agent/Plugin/Connector 升级设计已经冻结四条独立发布列车，并要求 inactive slot、兼容矩阵、回滚窗口和失败隔离。

因此 `DESKTOP-021` 不重新发明升级机制，而是在现有 Runtime/Activation 之上补齐：

```text
Release identity
+ Release channel
+ OSS artifact repository
+ Download transport
+ Rollout policy
+ Anti-rollback
+ Release telemetry
+ Emergency block / rollback
```

## 3. 版本模型：不能只用一个 Hermes Version

### 3.1 Customer Product Version

用户看到标准产品版本，例如：

```text
Hermes Desktop 1.4.2
```

建议遵循 SemVer：

```text
MAJOR.MINOR.PATCH
```

### 3.2 Release ID

`product_version` 不能唯一定位一个构建。每个发布构建必须有不可复用的 `release_id`：

```text
1.4.2+20260807.3.g9839a049
```

推荐规则：

```text
<product-version>+<yyyymmdd>.<build-seq>.g<git-sha8>
```

`release_id` 一旦发布永不复用。资格构建必须把平台、`github.run_id` 与
`github.run_attempt` 写入 release identity；同一提交因临时签名或重跑导致内容变更时，
也必须生成新的不可变 release，而不能覆盖本地已有目录。

### 3.3 Release Generation

增加单调递增的 `release_generation: u64`：

```text
1042
1043
1044
```

用途：

- anti-rollback；
- emergency minimum safe generation；
- channel pointer 更新顺序；
- 防止 CDN/OSS 缓存把客户端带回历史 release；
- 审计发布顺序。

SemVer 用于人理解，generation 用于机器安全顺序。

### 3.4 Component Versions

一个 Product Release 是多个组件的兼容集合：

```json
{
  "desktop": "1.4.2",
  "runtime_manager": "1.4.2",
  "private_python": "3.13.14",
  "uv": "0.12.2",
  "core": "0.19.0-hermes.7",
  "plugin": "0.1.3",
  "connector": "0.4.1",
  "runtime_contract": "1",
  "host_spi": "1",
  "local_protocol": "1",
  "cloud_protocol": "1"
}
```

客户只看到 Product Version；Doctor/Diagnostics 必须展示完整 component matrix。

## 4. 发布状态机

发布控制面使用独立于客户端生命周期的 Release State：

```text
DRAFT
  ↓
BUILT
  ↓
QUALIFIED
  ↓
SIGNED
  ↓
UPLOADED
  ↓
VERIFIED
  ↓
CANARY
  ↓
BETA
  ↓
STABLE
  ↓
DEPRECATED
  ↓
ARCHIVED
```

任何已发布 release 还可能进入：

```text
BLOCKED
REVOKED
```

规则：

- `UPLOADED` 以前不可被客户端发现；
- `VERIFIED` 必须包含“从用户下载域名重新下载并校验”的证据；
- `CANARY/BETA/STABLE` 只修改 Channel Manifest，不改 Release Artifact；
- `BLOCKED` 不删除制品，只取消引用并加入签名阻断列表；
- `REVOKED` 用于密钥/供应链级严重事件，必须产生人工审批证据。

## 5. Channel 模型

第一版建议四个通道：

| Channel | 用途 | 典型对象 |
|---|---|---|
| `canary` | 内部/极小流量 | 开发、运维、指定设备 |
| `beta` | 提前体验 | 测试客户、试点团队 |
| `stable` | 默认正式 | 普通客户 |
| `enterprise` | 企业锁版 | 组织指定版本/维护窗口 |

不建议增加过多通道。安全修复不是独立 channel，而是 release policy：

```text
security_critical=true
minimum_safe_generation=1043
mandatory_after=2026-08-10T00:00:00Z
```

## 6. Release Manifest V1

在当前 `ManagedReleaseManifestV1` 之上增加产品级 `ProductReleaseManifestV1`。

示意：

```json
{
  "schema_version": 1,
  "product": "hermes-desktop",
  "product_version": "1.4.2",
  "release_id": "1.4.2+20260807.3.g9839a049",
  "release_generation": 1042,
  "published_at": "2026-08-07T14:00:00Z",
  "source": {
    "repository": "looooooooy/hermes",
    "git_commit": "9839a049...",
    "workflow_run_id": "..."
  },
  "components": {
    "desktop": "1.4.2",
    "runtime_manager": "1.4.2",
    "core": "0.19.0-hermes.7",
    "plugin": "0.1.3",
    "connector": "0.4.1",
    "private_python": "3.13.14",
    "uv": "0.12.2"
  },
  "contracts": {
    "runtime": 1,
    "host_spi": 1,
    "local_protocol": 1,
    "cloud_protocol": 1
  },
  "targets": {
    "macos-aarch64": {
      "installer": {
        "object_key": "artifacts/v1/sha256/ab/.../Hermes-1.4.2-arm64.dmg",
        "sha256": "...",
        "size_bytes": 123,
        "platform_signature": "apple-developer-id"
      },
      "bootstrap_payload": {
        "object_key": "artifacts/v1/sha256/cd/.../bootstrap.tar.zst",
        "sha256": "...",
        "size_bytes": 123
      },
      "managed_release_payload": {
        "object_key": "artifacts/v1/sha256/ef/.../managed-release.tar.zst",
        "sha256": "...",
        "size_bytes": 123
      }
    }
  },
  "compatibility": {
    "desktop_min": "1.3.0",
    "runtime_manager_min": "1.3.0",
    "minimum_os": {
      "macos": "...",
      "windows": "...",
      "ubuntu": "24.04"
    }
  },
  "security": {
    "critical": false,
    "minimum_safe_generation": 0
  },
  "release_key_id": "release-2026-q3",
  "signature_algorithm": "ed25519",
  "signature": "..."
}
```

签名 canonicalization 必须冻结：

```text
remove signature field
→ canonical JSON
→ Ed25519 sign
```

## 7. Channel Manifest V1

Channel 只指向 release，不复制 release 内容：

```json
{
  "schema_version": 1,
  "channel": "stable",
  "channel_generation": 203,
  "target_release_id": "1.4.2+20260807.3.g9839a049",
  "target_release_generation": 1042,
  "previous_release_id": "1.4.1+20260801.1.g...",
  "minimum_allowed_release_generation": 1030,
  "rollout": {
    "enabled": true,
    "percentage": 25,
    "cohort_salt": "stable-203",
    "started_at": "2026-08-07T14:00:00Z",
    "paused": false
  },
  "expires_at": "2026-08-08T14:00:00Z",
  "key_id": "release-2026-q3",
  "signature_algorithm": "ed25519",
  "signature": "..."
}
```

灰度命中建议采用确定性 cohort：

```text
hash(device_id || cohort_salt) % 10000 < rollout_basis_points
```

同一 generation 下设备不会在“命中/不命中”之间随机跳动。

## 8. Emergency Block Manifest

独立维护：

```text
control/v1/security/blocklist.json
```

内容至少包括：

- blocked `release_id`；
- blocked component digest；
- minimum safe generation；
- severity；
- reason code；
- remediation release；
- issued_at / expires_at；
- offline root/release key signature。

客户端规则：

1. 当前版本若被 block，禁止重新激活；
2. 已 staging 的被 block release 必须删除 staging；
3. previous release 若也被 block，不得自动 rollback 到它；
4. 没有 safe previous 时进入 `BLOCKED`，只开放 Repair/Update/Export diagnostics。

## 9. 阿里云 OSS Bucket 拓扑

不要把所有对象塞进一个 Bucket。

建议至少四个逻辑 Bucket：

### 9.1 `release-artifacts-prod`

用途：正式安装包、Bootstrap、Managed Release、SBOM。

策略：

- Private ACL；
- Versioning enabled；
- 对象 key immutable；
- SSE-KMS 或 SSE-OSS；
- Release Publisher 可写；
- 客户端无 OSS IAM 凭证；
- CDN/下载域名只读；
- 不在该 Bucket 使用“latest.zip”覆盖式发布。

### 9.2 `release-control-prod`

用途：

- channel manifest；
- blocklist；
- key metadata；
- release index。

特点：小文件、高一致性要求、需要频繁更新 channel pointer。

策略：

- Private；
- Versioning enabled；
- 当前对象可更新，但历史版本必须保留；
- 客户端仍必须验证 Hermes Ed25519 签名；
- CDN TTL 极短或通过 Cloud Update API 获取；
- 任何 pointer 回退都受 `channel_generation`/`release_generation` 检查。

### 9.3 `release-evidence-prod`

用途：

- CI qualification receipt；
- signing receipt；
- notarization receipt；
- upload verification；
- release approval；
- rollout/promotion receipt；
- rollback/block evidence；
- SBOM/provenance snapshots。

建议与 distribution bucket 分离。如果确有审计要求，再对这一 Bucket 启用 Bucket WORM；**不要第一天直接在正式 artifacts bucket 锁不可逆 WORM**。

### 9.4 `release-staging`

用途：CI 临时上传和待发布制品。

策略：

- CI Builder 只写 staging；
- Release Publisher 从 staging 验证后发布 prod；
- lifecycle 自动清理过期制品；
- 清理 incomplete multipart uploads；
- 禁止客户端访问。

## 10. OSS Object Key 规范

### 10.1 内容寻址制品

```text
artifacts/v1/sha256/<first2>/<sha256>/<filename>
```

例如：

```text
artifacts/v1/sha256/7f/7f.../Hermes-1.4.2-macos-arm64.dmg
```

同一 SHA 不重复存储；文件名仅用于可读性，信任身份是 SHA-256。

### 10.2 Release Index

```text
releases/v1/<release-id>/release-manifest.json
releases/v1/<release-id>/release-manifest.sig
releases/v1/<release-id>/sbom.spdx.json
releases/v1/<release-id>/provenance.json
```

### 10.3 Channel

```text
channels/v1/canary/macos-aarch64.json
channels/v1/canary/windows-x86_64.json
channels/v1/beta/macos-aarch64.json
channels/v1/stable/macos-aarch64.json
channels/v1/stable/windows-x86_64.json
channels/v1/stable/linux-x86_64.json
```

### 10.4 Security Control

```text
security/v1/blocklist.json
security/v1/release-keys.json
```

## 11. OSS Versioning 的角色

OSS Versioning 是存储侧误覆盖/误删除保护，不替代 Hermes Release Version。

两者职责：

```text
Hermes release_id / generation
= 产品发布语义 + anti-rollback + compatibility

OSS VersionId
= 存储对象历史恢复
```

客户端不得根据 OSS VersionId 判断软件版本。

## 12. OSS Lifecycle

生命周期按 Bucket/prefix/tag 分开规划：

### staging

- 短期保留；
- 自动删除失败/过期构建；
- 自动删除未完成 multipart；
- 不做长期归档。

### production artifacts

至少保留：

```text
active stable
previous stable
所有仍在 supported window 的版本
所有 enterprise pinned 版本
所有安全调查中的版本
```

旧版本可按业务规则转低频/归档，但不能因为“当前不是 latest”立即删除。

### control previous versions

开启 Versioning 后使用 lifecycle 管理 noncurrent object versions，避免历史 channel pointer 无限增长。

### evidence

根据审计策略配置长期 retention；若正式启用 WORM，必须先在独立测试 Bucket 演练，因为 locked WORM 属于不可逆控制面。

## 13. OSS 数据完整性与 Hermes 信任根

上传侧：

```text
local SHA-256
+ Content-MD5 / OSS CRC64 transport verification
+ OSS upload success
+ remote HEAD/GET
+ remote object length
+ remote download SHA-256
```

客户端最终激活前仍必须：

```text
SHA-256 == Release Manifest
AND
Hermes Ed25519 signature valid
AND
platform package signature valid where applicable
```

OSS ETag 不作为 Hermes artifact trust digest。

## 14. 下载域名与 CDN

### 14.1 第一阶段：直接 OSS

推荐：

```text
updates.<company-domain>
    CNAME
      → private OSS bucket custom domain
```

Cloud Update API 生成短时 V4 presigned URL；Desktop/Runtime Manager 不持有 AK/SK。

### 14.2 规模化：CDN + private OSS origin

正式客户规模增大后：

```text
Hermes Client
   ↓ CDN signed URL
updates.<company-domain>
   ↓ Alibaba Cloud CDN
Private OSS Bucket
```

使用：

- CDN private OSS origin access；
- CDN URL signing；
- HTTPS；
- private OSS Bucket；
- limited read-only CDN origin role。

**不要在同一路径同时使用 OSS URL presign 和 CDN origin Authorization。**

如果 Bucket 位于中国内地，升级域名/备案必须在产品发布前解决。

## 15. Cloud Release Control Plane

OSS 只存文件，策略由 Cloud 服务负责。

新增逻辑服务：

```text
Release Service
├── Release Registry
├── Channel Policy
├── Rollout Engine
├── Enterprise Version Pin
├── Security Blocklist
├── Download Authorization
├── Promotion / Pause
└── Release Telemetry
```

### 15.1 Update Check API

示意：

```http
POST /api/v1/desktop/update/check
```

输入：

```json
{
  "device_id": "...",
  "organization_id": "...",
  "channel": "stable",
  "platform": "macos",
  "architecture": "aarch64",
  "os_version": "...",
  "product_version": "1.4.1",
  "release_id": "...",
  "release_generation": 1038,
  "runtime_manager": "1.4.1",
  "components": {},
  "capability_digest": "sha256:..."
}
```

返回：

```json
{
  "eligible": true,
  "mandatory": false,
  "channel_generation": 203,
  "target_release_id": "1.4.2+20260807.3.g9839a049",
  "release_manifest": {},
  "download_grants": {
    "managed_release_payload": {
      "url": "https://updates.example/...",
      "expires_at": "..."
    }
  }
}
```

Cloud 可以拒绝：

- 企业锁版；
- rollout 未命中；
- OS 不支持；
- incompatible current state；
- 当前版本被阻断且没有安全升级目标；
- 设备需要 Repair 而不是 Update。

## 16. Runtime Manager 更新流程

新增 Update Coordinator，但生命周期仍归 Runtime Manager。

```text
READY
 ↓
CHECKING
 ↓
AVAILABLE
 ↓
DOWNLOADING
 ↓
VERIFYING
 ↓
STAGED
 ↓
WAITING_SAFE_WINDOW
 ↓
UPDATING
 ↓
HEALTH_CHECK
 ├─ success → READY
 └─ fail    → ROLLING_BACK → READY / FAILED
```

推荐把 download/update progress 做成 LifecycleState 的子状态，不要膨胀顶层生命周期枚举。

### 16.1 下载

必须：

- 下载到 `.partial`；
- 不写 active release；
- 可断点续传；
- 完成前不 rename 到 staged artifact；
- 校验 size + SHA-256；
- 校验 Release Manifest Ed25519；
- 校验 platform signature；
- 失败删除 partial 或保留可安全 resume 的 metadata。

### 16.2 Stage

目标：

```text
releases/<exact-release-id>/
```

服务启动永远引用 exact release ID；禁止 LaunchAgent/systemd/task 指向 `latest/current` symlink。

`current` / `previous` 只允许作为 Runtime Manager state metadata，不允许作为服务实际执行路径。

### 16.3 Safe Window

默认规则：

- 有正在执行的 Agent task：不切 Runtime；
- 有 pending approval/clarification：可下载但延迟 activation；
- Connector command lane 非空：进入 drain/reconcile；
- security critical：允许更强策略，但必须有用户/企业 policy 和审计。

### 16.4 Activate

复用现有 Activation Contract：

```text
old authority exact match
→ stop/drain
→ candidate exact release path
→ start
→ runtime identity
→ local IPC
→ Connector readiness
→ Cloud handshake
→ Agent Ready
→ live capability
```

### 16.5 Rollback

回滚目标必须是：

```text
previous known-good release
```

不是“版本号减一”。

如果 previous 已进入 blocklist：

```text
DO NOT ROLLBACK
→ BLOCKED / REPAIR REQUIRED
```

## 17. Desktop 与 Managed Runtime 分开升级

不要要求 Desktop、Core、Plugin、Connector 永远同步重启。

一个 Product Release 可以包含：

```text
Desktop update: required / optional / unchanged
Runtime Manager: required / unchanged
Managed Runtime: required / unchanged
```

典型场景：

### 仅 Connector/Plugin 修复

Desktop 不需要退出。

### Core 修复

Desktop 可继续显示状态；Runtime 在 safe window 切换。

### Desktop UI 修复

Runtime Manager/Agent 不应停止。Desktop 自身退出/替换/重启后重新连接 Runtime Manager。

### Runtime Manager 自更新

必须有最小 bootstrap/updater authority，避免 Runtime Manager 在覆盖自身过程中成为单点失败。

`DESKTOP-021` 需要单独设计 Runtime Manager self-update handoff，不允许进程直接覆盖当前 executable 后假定成功。

## 18. 平台发行策略

### macOS

- customer: DMG/PKG + Developer ID + notarization；
- Runtime payload user-scoped immutable release；
- Desktop update 需要进程退出后替换 app bundle；
- Runtime Manager 保持 Agent 生命周期独立。

### Windows

- per-user installer 优先；
- Authenticode；
- inactive installer/update staging；
- 更新不得依赖 PowerShell 安装 Python/VC build tools。

### Linux

需要区分：

1. Enterprise/system package：DEB；
2. user-scoped auto-update product path。

如果 DEB 需要系统包管理权限，就不能把它作为“无管理员权限自动升级”的唯一方案。Linux V1 需要在 B3/021 阶段冻结：

```text
DEB = enterprise/admin distribution
user-scoped package = desktop self-managed update path
```

否则 Linux auto-update 会与当前“user systemd + user Secret Service + no developer tooling”目标冲突。

## 19. Local Release Cache 与清理

本地至少保留：

```text
ACTIVE
PREVIOUS_KNOWN_GOOD
STAGED_TARGET (if updating)
```

建议正常保留最近 2~3 个 verified release，企业锁版另算。

禁止清理：

- active；
- previous；
- rollback in progress；
- evidence referenced release；
- enterprise pinned；
- support bundle 正在采集的 release。

磁盘空间不足时：

1. 清 partial；
2. 清过期 staging；
3. 清非 active/previous 的旧 verified release；
4. 仍不足则 fail closed，提示释放空间；
5. 禁止为升级删除 active/previous。

## 20. Release Signing Key 管理

至少三层：

```text
Offline Root Key
  ↓ signs
Release Key Metadata
  ↓ authorizes
Online/controlled Release Signing Key
  ↓ signs
Release Manifest / Channel Manifest / Blocklist
```

平台签名密钥与 Hermes Release Key 分离。

禁止：

- 私钥进入 GitHub repo；
- 私钥进入 OSS；
- 私钥进入 Desktop；
- CI 普通 build job 获得 prod publish key；
- 同一 RAM identity 同时拥有 build + prod delete + release signing 全权限。

## 21. Publish Pipeline

```text
GitHub source commit
 ↓
Build artifacts
 ↓
Tests / blank-machine qualification
 ↓
SBOM / provenance
 ↓
Platform sign/notarize
 ↓
Hermes Release Manifest sign
 ↓
Upload staging OSS
 ↓
Remote object verification
 ↓
Publish immutable prod object keys
 ↓
Download through customer update domain
 ↓
Re-verify SHA/signature
 ↓
Mark VERIFIED
 ↓
Update canary channel pointer
 ↓
Observe
 ↓
Promote beta/stable
```

关键原则：**先发布 immutable release，再更新 pointer。**

绝不：

```text
upload latest.zip
→ overwrite latest.json
→ hope clients get the right pair
```

## 22. RAM / 权限边界

建议角色：

### CI Builder

- 只能写 staging prefix；
- 不能写 production channel；
- 不能 delete production；
- 无 release signing prod key。

### Release Publisher

- 读取 staging；
- 写 immutable production artifact/release prefix；
- 写 control channel；
- 不能读取客户 secret；
- release promotion 需要 environment approval。

### CDN Origin Role

- 仅 production artifact read；
- 不允许 list/write/delete control/evidence。

### Cloud Release Service

- 读取 control/release registry；
- 生成 download authorization；
- 不持有长期客户端 secret。

### Human Operator

- Promotion/Pause/Block 经 RBAC；
- Block/revoke 需要二次审批和审计 receipt。

## 23. 发布灰度与自动暂停

每个 release 至少监控：

```text
update_check_eligible_rate
update_download_success_rate
artifact_hash_failure_rate
signature_failure_rate
stage_success_rate
activation_success_rate
health_gate_failure_rate
rollback_rate
runtime_crash_rate
connector_cloud_reconnect_failure_rate
agent_ready_latency
live_session_success_rate
```

自动暂停条件应基于“相对上一稳定版本异常”，不是写死一个永远不变的数字。

发生以下任一项立即 pause：

- signature/hash failure > 0 的不可解释事件；
- rollback rate 激增；
- Agent Ready 明显下降；
- Cloud protocol incompatibility；
- crash loop；
- unknown/effect_unknown 异常上升；
- security block event。

## 24. 版本回退与回滚攻击区别

### 合法产品回滚

例如 stable 1.4.2 有严重 bug，需要回 1.4.1。

不能简单把 channel pointer 指回旧 generation，因为客户端 anti-rollback 会拒绝。

应发布一个新的“rollback authorization release/channel generation”：

```text
channel_generation: 204
rollback_authorized_to_release_id: 1.4.1+...
reason: regression
expires_at: ...
```

由当前 Release Key 签名。

因此：

```text
旧软件版本
≠ 旧控制面 generation
```

可以“新 generation 授权运行旧 payload”，从而兼顾安全 anti-rollback 和业务 rollback。

## 25. Delta Update

V1 **不做 delta**。

第一闭环只做完整 payload：

- 逻辑简单；
- 验证清晰；
- rollback 清晰；
- 不引入 base-version patch matrix。

V2 若优化流量：

```text
exact base SHA
→ signed delta
→ apply to inactive staging
→ full target SHA verify
→ activate
```

任何 delta 失败自动 fallback full payload。

## 26. Offline / Enterprise

OSS 在线升级和企业离线安装必须共用同一 Release Manifest。

在线：

```text
Release Manifest
+ OSS/CDN artifacts
```

离线：

```text
Release Manifest
+ exact referenced artifacts
+ signatures
+ SBOM/provenance
→ offline bundle
```

两条路径只允许 transport 不同，release identity 不同视为失败。

## 27. DESKTOP-021 执行拆分

### 021A Release Identity & Manifest

- [ ] ProductReleaseManifestV1
- [ ] ChannelManifestV1
- [ ] BlockManifestV1
- [ ] monotonic generation
- [ ] canonical Ed25519 signature
- [ ] anti-rollback contract tests

### 021B OSS Repository

- [ ] staging/artifacts/control/evidence bucket topology
- [ ] RAM least privilege
- [ ] Versioning
- [ ] lifecycle
- [ ] SSE policy
- [ ] custom update domain
- [ ] staging → prod publish policy

### 021C Release Publisher

- [ ] content-addressed object upload
- [ ] MD5/CRC transport verification
- [ ] remote SHA re-download verification
- [ ] immutable key collision fail closed
- [ ] publish receipt
- [ ] channel promotion transaction

### 021D Runtime Manager Update Resolver

- [ ] Update Check
- [ ] channel eligibility
- [ ] enterprise pin
- [ ] anti-rollback
- [ ] blocklist
- [ ] safe window

### 021E Downloader

- [ ] short-lived grant
- [ ] resume
- [ ] `.partial`
- [ ] size/SHA verify
- [ ] Ed25519 verify
- [ ] platform signature verify
- [ ] no host tool dependency

### 021F Cross-platform Activation

- [ ] macOS N → N+1 → rollback → N+1
- [ ] Windows N → N+1 → rollback → N+1
- [ ] Linux N → N+1 → rollback → N+1
- [ ] interrupted update recovery
- [ ] power loss/crash injection

### 021G CDN

- [ ] private OSS origin
- [ ] HTTPS custom domain
- [ ] URL signing
- [ ] cache headers
- [ ] cache purge only when necessary
- [ ] customer-domain download qualification

### 021H Rollout / Pause / Block

- [ ] canary cohort
- [ ] beta/stable promotion
- [ ] pause
- [ ] security block
- [ ] signed rollback authorization

### 021I Telemetry / Audit

- [ ] update metrics
- [ ] activation receipt
- [ ] rollback receipt
- [ ] promotion receipt
- [ ] evidence retention

### 021J End-to-End Release Drill

必须真实完成：

```text
1.0.0 installed
→ discover 1.0.1 canary
→ download from OSS/CDN
→ verify
→ stage
→ activate
→ real Agent Ready
→ real Cloud live Session
→ inject regression
→ automatic rollback 1.0.0
→ publish corrected 1.0.2
→ activate 1.0.2
→ reboot/login recovery
→ receipts complete
```

只有这条链真实通过，才叫“软件版本化升级管理闭环”。

## 28. 与 DESKTOP-020B 的关系

```text
DESKTOP-020B
= 第一次安装闭环

DESKTOP-021
= 第二次、第三次、长期升级闭环
```

020B3 产出的 signed installer 成为 021 的第一个 Release Artifact。

020B4 裸机测试通过后，021J 再使用同一 VM 基线跑：

```text
N install
→ N+1 update
→ rollback
→ N+2 update
```

不要用“能第一次安装”替代“能长期升级”。

## 29. 阿里云 OSS 能力使用边界

本设计依赖的 OSS 能力：

- Bucket Versioning：保护误覆盖/误删除历史版本；
- Lifecycle：清理 staging、noncurrent versions、旧制品归档；
- Content-MD5 / CRC64：传输完整性校验；
- V4 Presigned URL：不暴露 AccessKey 的短时下载；
- Custom Domain/CNAME：统一 `updates` 产品域名；
- Private OSS Bucket origin for CDN：私有源站分发；
- CDN URL signing：终端下载鉴权；
- SSE-KMS/SSE-OSS：静态数据加密；
- Bucket WORM：仅建议用于独立 audit/evidence bucket，且需先演练不可逆锁定策略。

OSS 能力只增强存储、传输和审计，**不能替代 Hermes 自己的 Release Manifest signature、SHA-256 和 anti-rollback。**

## 30. 官方能力参考

- OSS Versioning: https://www.alibabacloud.com/help/en/oss/user-guide/overview-78/
- Manage versioned objects: https://www.alibabacloud.com/help/en/oss/user-guide/manage-objects-in-a-versioning-enabled-bucket
- Lifecycle: https://www.alibabacloud.com/help/en/oss/user-guide/overview-54/
- Data verification: https://www.alibabacloud.com/help/en/oss/user-guide/data-verification/
- V4 presigned URL: https://www.alibabacloud.com/help/en/oss/developer-reference/add-signatures-to-urls
- Custom domain: https://www.alibabacloud.com/help/en/oss/user-guide/access-buckets-via-custom-domain-names
- OSS encryption: https://www.alibabacloud.com/help/en/oss/user-guide/data-encryption/
- Bucket WORM: https://www.alibabacloud.com/help/en/oss/user-guide/oss-retention-policies
- CDN private OSS origin: https://www.alibabacloud.com/help/en/cdn/user-guide/grant-alibaba-cloud-cdn-access-permissions-on-private-oss-buckets
- CDN URL signing: https://www.alibabacloud.com/help/en/cdn/user-guide/configure-url-signing
