# 05 安全与数据治理设计

- 状态：规范
- 基线版本：1.0
- 更新日期：2026-07-30

## 1. 安全目标

Connector 是企业数据与员工本地执行环境之间的高信任边缘组件，安全目标为：

- 未授权设备不能观察或控制 Agent；
- 已授权设备只能在明确 Tenant、用户、Agent、Session 和 Purpose 内工作；
- 任一层被绕过时，下一层仍会重新鉴权；
- 网络重放和重复投递不能重复产生业务效果；
- Secret、sudo 和终端敏感输入不进入 Cloud 可读明文；
- 日志、Trace、诊断和统计不形成新的数据泄露通道；
- 删除、撤权、离职和设备吊销能够传播；
- 企业治理不能演变为隐蔽的员工监控。

## 2. 威胁模型

至少覆盖：

| 威胁 | 控制 |
|---|---|
| 伪造 Connector | 设备私钥 Challenge、短期令牌、吊销 |
| 窃取 WSS Token | 短时效、设备绑定、重放检测 |
| 本机其他用户连接 IPC | UDS/Named Pipe 用户 ACL、路径校验 |
| 旧 Runtime 命令重放 | runtime generation、TTL、lease、digest |
| 跨 Tenant/Session IDOR | 每层作用域校验、PostgreSQL RLS |
| 消息篡改 | TLS、会话完整性、payload digest |
| 重复副作用 | Server/SQLite/Agent 三层幂等和状态查询 |
| 日志泄密 | 字段分级、默认脱敏、秘密扫描 |
| 恶意文件/Prompt Injection | 隔离、扫描、不可信内容标签、Tool Gateway |
| 更新供应链 | 离线签名、制品摘要、SBOM、灰度回滚 |
| 被吊销设备继续离线执行 | 吊销推送、短令牌、命令 TTL、敏感队列清除 |
| Agent 之间越权转发 | 引用优先、接收方重新鉴权、Access Grant |

## 3. 身份模型

系统同时识别：

- Tenant；
- Human Identity；
- Agent Identity；
- `on_behalf_of` 员工；
- Connector Device；
- Browser/Mobile Client Instance；
- Agent Runtime Generation；
- Service Identity；
- Purpose-bound Delegation。

Agent 不能冒充员工。审计记录必须同时保留：

```text
actor = agent identity
on_behalf_of = human identity
device = connector device
purpose = current business purpose
authorization = delegation/policy decision reference
```

## 4. 设备配对与生命周期

### 4.1 配对

1. Connector 本地生成 Ed25519 设备密钥；
2. 私钥写入 OS 安全存储；
3. Connector 展示 5 分钟有效的一次性配对码和可核对指纹；
4. 用户在已登录 H5 中确认设备名称、平台、Agent 和授权范围；
5. Server 将公钥绑定 Tenant、Realm、用户和设备状态；
6. Connector 对随机 Challenge 签名；
7. Server 签发短期连接令牌。

配对码不是长期凭据，不能单独完成设备接管。

### 4.2 生命周期

```text
UNPAIRED -> PENDING -> ACTIVE -> SUSPENDED -> REVOKED / RETIRED
```

吊销后：

- 关闭现有 WSS；
- 拒绝新 Challenge；
- 清除未执行敏感命令；
- 普通离线命令按政策取消或过期；
- 保留不含正文的必要安全审计；
- H5 展示操作者、时间和影响。

## 5. 控制命令授权链

每条命令依次经过：

1. H5 用户会话和 CSRF/Origin；
2. Tenant membership、订阅和配额；
3. 用户对目标 Agent/Session 的授权；
4. client instance 与 Remote control lease；
5. Server method Allowlist、Purpose 和风险策略；
6. Connector 设备会话、Schema、TTL、digest 和 Inbox 幂等；
7. Local Gateway role、runtime generation、session binding；
8. Agent control lease、pending request ID/revision；
9. owner action adapter 的最终业务前置条件。

任一步失败均不执行。Connector 不是授权事实源，不能自行扩大 Server 或 Agent
授予的范围。

## 6. 密钥分层

| 密钥 | 保存位置 | 用途 |
|---|---|---|
| Release Signing Key | 离线/HSM | Connector 安装包和版本清单 |
| Server Signing Key | Cloud KMS | 短期令牌与信封签名 |
| Device Identity Key | OS 安全存储 | Connector 身份证明 |
| Tenant Data Encryption Key | KMS 信封加密 | Cloud 投影、消息和对象 |
| Sensitive Session Key | H5/Connector 内存 | sudo/secret/terminal 临时密文 |

要求：

- Release Signing Key 与 Server Signing Key 分权；
- Key ID 支持轮换和旧签名验证窗口；
- 应用通过工作负载身份/RAM Role 获取 KMS 权限；
- 禁止长期 AccessKey 写入配置；
- 密钥读取、轮换和吊销进入独立审计。

## 7. 敏感输入

`sudo.respond`、`secret.respond`、`terminal.read.respond` 在以下条件全部完成前保持
不可用：

- H5 与 Connector 建立经过审计的临时端到端密文方案；
- Connector 临时公钥带设备签名；
- Tenant、Agent、Session、Generation、Command、Expiry 和 digest 进入 AEAD AAD；
- Server 只保存路由元数据和密文，不记录正文；
- Connector 只在内存解密并一次性提交；
- 超时、断线、吊销、执行后立即销毁；
- 崩溃转储和诊断包排除明文；
- 针对重放、错绑、重复提交和内存残留有测试。

## 8. 数据分类

| 级别 | 示例 | Connector/Cloud 默认处理 |
|---|---|---|
| Public | 已批准公开内容 | 可按公开策略传输 |
| Internal | 一般工作资料 | Tenant/Workspace 限制 |
| Confidential | 客户、财务、研发、合同 | 严格 ACL、审计和加密 |
| Restricted | 密钥、核心算法、高敏个人信息 | 默认本地/专属环境，外发需显式策略 |

数据标签必须随引用、事件、投影和派生结果传播。派生结果默认权限是所有输入 ACL
的交集；扩大范围必须重新审批、脱敏和分类。

## 9. 本地数据

### 9.1 Connector SQLite

允许保存：

- 消息 ID、digest、状态、结果摘要；
- 上下行 cursor；
- Agent endpoint、generation 和 capability；
- 待 ACK 的安全事件；
- Schema 和更新状态。

默认禁止保存：

- 模型供应商密钥；
- Device Identity 私钥；
- Lease ID 的日志副本；
- sudo/secret/terminal 明文；
- 完整 SessionDB 副本；
- 员工 Personal Memory；
- 默认完整工具输出。

SQLite 文件权限限制到服务账户。需要保存的业务正文使用 Tenant/Device 绑定加密和
最短保留；损坏恢复不能通过删除数据库后盲目重放解决。

### 9.2 Agent 本地文件

Connector 不主动扫描员工目录。Agent 或用户明确引用文件时：

- 记录稳定文件 ID、版本、哈希和来源；
- 先做分类、DLP、病毒和 Prompt Injection 扫描；
- 默认传引用或批准的派生物；
- 原件上传需明确目的和保留策略；
- 本地删除/更正事件向投影传播。

## 10. Cloud 读取投影

Cloud Projection 是为了多端读取，不是 SessionDB 备份：

- 默认近期保留，基线建议 30 天；
- Tenant 可关闭、缩短或按套餐延长；
- Tenant DEK 信封加密；
- 高频 delta 合并后保存；
- 保存来源 generation、sequence 和内容哈希；
- 用户可导出、清除和关闭；
- 删除覆盖 PostgreSQL、对象存储、搜索索引、缓存和密钥材料；
- 法律保留与用户删除冲突时记录明确依据和范围。

## 11. 企业工作信息治理

工作过程中产生的信息按以下层次处理：

```text
Transient Runtime Data
-> Work Event
-> Evidence Candidate
-> Approved Evidence
-> Knowledge Candidate
-> Knowledge Asset / Skill Candidate
```

聊天、工具输出或屏幕活动不会自动成为公司资产。Work Event 至少保留：

- 来源对象和作者；
- 发生/采集时间；
- Tenant、Workspace、Project、Purpose；
- 来源 ACL 快照；
- 内容哈希与引用；
- 分类、保留和员工告知版本；
- 后续脱敏、转换和审批记录。

默认不采集：

- 与工作无关的私人活动；
- 键盘输入量、鼠标轨迹和在线时长排名；
- 未授权的私人账号数据；
- 模型隐藏推理；
- 无业务目的的全量屏幕录制；
- 用于自动人事结论的个人行为推断。

## 12. 内外部系统数据

### 12.1 接入优先级

```text
Official API
-> Webhook / Domain Event
-> CDC
-> Managed Sync
-> Controlled File Import
-> Read-only DB as last resort
```

每个数据连接器声明服务身份、最小 Scope、Schema、主键、版本、ACL 映射、增量
游标、删除/更正/撤权、质量、新鲜度、限流、回放和停用程序。

Agent 不直连生产数据库自由生成 SQL，而是通过有 Owner、合同、权限、质量 SLO 和
废弃策略的 Data Product。

### 12.2 外部内容

互联网、第三方报告和客户上传先进入隔离域，记录来源、许可、哈希、获取时间、
PII、商业秘密、可信度、驻留、模型使用范围和 Prompt Injection 风险。

外部内容中的“忽略规则”“调用工具”“发送数据”等文本一律视为不可信数据，不能
进入系统指令或自动触发工具。

## 13. Agent-to-Agent 数据

- 优先交换 Resource Reference、对象 ID、Data Product Version、Evidence Reference；
- 接收 Agent 使用自己的身份和 Purpose 重新鉴权；
- 发送 Agent 不得复制正文绕过 ACL；
- 跨部门共享使用有范围、字段、用途、接收方、有效期和撤销能力的 Access Grant；
- Personal Memory 永不自动传播；
- 正式消息和 Work Item 由 Server 保存事实，Connector 只缓存交付；
- 大文件通过对象存储 Grant 和受控引用，不走消息帧。

## 14. 删除、更正和撤权

传播顺序：

```text
Source event
-> Registry/Policy state
-> Cloud projection
-> Search/vector index
-> Object derivatives
-> Connector cache
-> Agent working cache
-> Knowledge/Skill review
-> Audit completion record
```

删除任务必须可查询状态、重试、隔离失败和人工处理。来源更正不能只覆盖正文，必须
使旧 Evidence、Knowledge 和 Skill 引用进入失效或复审状态。

## 15. 日志、Trace 与诊断

允许默认记录：

- 时间、组件、版本、状态；
- message/command/trace ID；
- Tenant/Agent 的不可逆或受控标识；
- payload 大小和 digest；
- 错误码、重试次数、队列深度和延迟。

默认禁止：

- Access Token、Refresh Token、Device Private Key；
- Lease ID、配对码；
- 完整 Prompt、审批载荷、文件正文和工具输出；
- sudo、secret、terminal input；
- 用户私人标识和支付信息。

诊断包由用户明确授权产生，先本地展示内容清单并脱敏，可设置自动过期。

## 16. 员工权益

企业部署必须提供：

- 采集目的、范围、保留和使用者的清晰告知；
- 员工查看自己的采集记录和访问记录；
- 纠错、标记私人、撤回候选和申诉入口；
- 经理只看有业务依据的项目/团队聚合，不默认看原文；
- 平台管理员默认只看运行状态，不自动拥有业务正文；
- 禁止用提示词数量、在线时长、键盘量作绩效排名；
- 禁止仅凭算法作人事决定。

## 17. 安全测试门禁

- 跨 Tenant/Agent/Session IDOR；
- 设备伪造、Token 重放和吊销；
- UDS/Named Pipe 跨用户访问；
- 旧 generation 和过期命令重放；
- message ID/digest 冲突；
- 无 lease、错 lease、错 pending revision；
- WebSocket 洪泛、超大帧和压缩炸弹；
- 日志、Trace、崩溃转储、诊断包秘密扫描；
- 文件病毒、DLP 和间接 Prompt Injection；
- SBOM、依赖漏洞、制品签名和安装器供应链；
- 删除、更正、撤权和离职传播；
- Agent-to-Agent 权限不扩散。

上线要求跨租户越权为 0、重复投递产生重复业务效果为 0，且不存在未关闭的 P0/P1
安全问题。
