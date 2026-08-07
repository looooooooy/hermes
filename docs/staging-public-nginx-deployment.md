# Hermes staging 公网 Nginx 部署

本部署只管理 `/etc/nginx/conf.d/hermes-public.conf`，不会替换全局
`nginx.conf`，不会把 SSH 密码、TLS 私钥、Cloud token 或模型密钥写入仓库。

## 前置条件

1. 在云安全组中仅向可信来源开放 SSH 端口；对公网开放 `80/443`。
2. 轮换任何曾通过聊天或其他非密钥系统传递的 root 密码。
3. 建议创建独立部署用户并配置 `sudo -n`；至少应改用 SSH key，关闭 root 密码登录。
4. 服务器已经安装与证书 SAN 匹配的 TLS 证书和私钥。
5. Hermes Cloud 后端默认仅监听 loopback：
   - Business API `127.0.0.1:8101`；
   - Connector Gateway `127.0.0.1:8102`；
   - File Gateway `127.0.0.1:8104`。
6. `8101/live` 与 `8101/ready` 都必须返回成功后，才应公开控制链路。

HTTP 上只做 HTTPS 重定向。认证、控制和 WebSocket 不应通过明文 HTTP 暴露。

## GitHub Environment

创建 Environment：

```text
hermes-runtime-staging
```

配置两个 Secret：

```text
HERMES_STAGING_SSH_PRIVATE_KEY
HERMES_STAGING_SSH_KNOWN_HOSTS
```

`HERMES_STAGING_SSH_KNOWN_HOSTS` 必须是从可信控制台或可信管理机取得并人工核对过的主机公钥记录。工作流不会使用 `StrictHostKeyChecking=no`，也不会在部署时临时接受未知指纹。

不要把 root 密码作为 workflow input；workflow input 会进入运行元数据，不能作为秘密载体。

## 执行

在 Actions 中手动运行：

```text
Deploy Hermes staging Nginx
```

输入：

- SSH host；
- SSH user；
- 与证书一致的 `server_name`；
- 服务器上的证书链和私钥绝对路径；
- `require_backend_ready=true`。

工作流执行以下步骤：

1. 校验输入；
2. 使用固定 SSH key 与固定 `known_hosts`；
3. 渲染独立 Nginx server block；
4. 上传到 `/tmp`；
5. 检查 `8101/8102/8104`；
6. 备份现有 Hermes Nginx 文件；
7. 执行 `nginx -t`；
8. reload；
9. 本机 HTTPS `/hermes/live` smoke test；
10. 任一步失败则恢复旧文件。

## 公网路径

```text
/hermes/api/
/hermes/auth/
/hermes/api/ws
/hermes/internal/connector/ws
/hermes/files/
/hermes/live
/hermes/ready
```

WebSocket access log 被关闭，避免查询票据进入访问日志。

## 回滚

每次替换前都会生成：

```text
/etc/nginx/conf.d/hermes-public.conf.backup.<UTC timestamp>
```

回滚时将确认过的备份复制回目标文件，然后执行：

```bash
nginx -t && systemctl reload nginx
```

真实 staging full-chain、重启、`runtime_generation` 滚动、`effect_unknown` 和回滚演练完成前，PR #1 继续保持 Draft。
