# 腾讯云提示词站部署与运维

空间保留与清理规则见[腾讯云服务器空间治理](server-governance.md)，规则是否已生效以该文档的实施状态为准。以下保留历次部署与运维记录。

部署时间：2026-10-09。服务器：62.234.69.93（Ubuntu 24.04、4核4GB）。部署目录：`/opt/aigc-prompt`，独立 Compose 项目 `aigc-prompt`。

## 访问

- 主站：https://62.234.69.93:8443/
- 管理端：https://62.234.69.93:8443/admin/
- 健康检查：https://62.234.69.93:8443/health
- HTTP 8082 跳转到 HTTPS 8443。
- 管理端沿用原 ADMIN_SECRET_KEY，配置位于服务器受保护的 runtime.env；本文不记录密钥。

前端是 Next.js standalone Node 服务；默认 `/api/*` 走 Next BFF，`/api/admin/*` 走 FastAPI，保留 HttpOnly/Secure 会话机制。PostgreSQL、Redis、API、Node 不映射公网端口。腾讯云防火墙新增 TCP 8082、8443，已有规则未修改。

## 数据迁移

本机 pg_dump → 新 PostgreSQL 16 数据卷，未覆盖已有项目的数据库。迁移校验：54 prompts、251 prompt_images、61 crawl_drafts、316 crawl_draft_images、6 crawl_tasks、2 users、3 user_likes、1 user_collects；49条正式内容已发布。

迁移前备份保存在服务器 `/opt/aigc-prompt/database.dump`，权限600。部署临时副本在验收后清理，服务器备份保留。迁移后的旧 running 采集任务已标记失败，因为原进程没有迁移。草稿引用的159个本地图片文件已迁移，缺失0。

配置独立保存在服务器 `.env`（Compose数据库密码）和 `runtime.env`（后端、COS、百炼等），均权限600；不提交Git、不放入源码镜像。

## 每日任务

`aigc-crawl.timer`：北京时间每日10:00、22:00，首次2026-10-10 10:00。由 systemd 启动一次性 worker；每轮小红书关键词“AI绘画提示词”，数量20；同一时间只执行一个采集流程。15分钟采集超时、45分钟整体超时。

流程：采集 → 导入本轮新草稿 → 图片转COS → AI图片/提示词/分类审核 → 仅保留可信图片 → 原子写入正式内容并发布。历史pending不会批量自动发布。上传/模型/格式校验失败、无可信图片或无有效提示词，保持pending并记录审核备注。保留作者和来源链接。模型输出必须包含正确布尔值、唯一且完整的图片索引与有效分类。

**最新状态：已通过实际采集与发布验证。** Xvfb有界面浏览器、`CHROME_HEADLESS=false`、新的专用profile正常访问，用户已服务器扫码登录。实际落盘12条记录；因日志行超过64KiB中断后，已修复4MiB读取上限及日志截断，并从本次记录恢复导入3条、审核发布3条。正式内容57条（52已发布）、草稿64条。旧失败任务保留便于追溯，另有恢复任务成功记录。历史pending未批量发布。未迁移公司电脑登录状态。

Linux浏览器/子进程日志8个回归测试通过，其中真实20万字符日志读取测试验证不会触发原错误。后续如平台要求重新登录，需要用户扫码。

```bash
cd /opt/aigc-prompt
sudo docker compose ps
sudo docker compose logs --tail=100 api frontend gateway
sudo systemctl list-timers aigc-crawl.timer
sudo journalctl -u aigc-crawl.service --since today
sudo systemctl start aigc-crawl.service  # 手动启动同一流程，运行前确认登录可用
sudo systemctl stop aigc-crawl.timer   # 暂停未来触发，不影响站点
```

## HTTPS续期

Let’s Encrypt IP证书：`certificates/live/62.234.69.93/`，初次到期2026-10-16。`aigc-certificate.timer` 每日03:00、15:00检查续期并reload网关。

模拟续期已成功验证（第一次外部校验连接超时，重试通过）。

ACME HTTP验证文件通过现有博客80端口的静态目录 `/opt/blog/html/frontend` 提供；未修改博客nginx配置。保留该目录和80端口，否则续期失败。Certbot配置及证书在本项目 certificates 下。

```bash
sudo systemctl list-timers aigc-certificate.timer
sudo journalctl -u aigc-certificate.service --since today
sudo systemctl start aigc-certificate.service
```

IP证书使用Let’s Encrypt的短期证书方案，续期配置依据[官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

## 验证范围

新增审核/任务流程覆盖率91%，27项单元测试及隔离PostgreSQL schema集成测试全部通过。服务器真实AI审核调用也已验证；该检查仅审核既有图片，不写入重复内容。服务器迁移表计数一致；公网首页图片/布局、API健康检查、管理端鉴权401/200、用户BFF会话和收藏接口通过。旧7个运行容器仍运行。实际采集被平台IP限制阻塞。

前端详情的“猜你喜欢”仍沿用既有demo推荐，尚未对接真实相似内容接口；后续应单独修正。

原有 Google OAuth 授权来源需在Google控制台添加最终正式HTTPS站点；IP入口不作为完整Google登录验证。邮件发信仍受原Resend账户/域名配置限制。

## 回滚与更新

变更源码后在目录中 `sudo docker compose build api frontend`，再 `sudo docker compose up -d`。只操作该Compose项目。不要运行全局 `docker system prune --volumes`，否则可能删掉原项目数据/回滚镜像。

停止本项目：先 `sudo systemctl stop aigc-crawl.timer`，再 `sudo docker compose down`。不要加`-v`，保留数据库卷。更新前执行pg_dump并保存带日期的备份，数据库已有写入后不能无条件重新导入初始备份。

### 2026-10-10 管理后台采集计划更新

管理后台新增 `/admin/crawl-schedules`：按北京时间设置每天多个时间点、每平台1–50条、关键词、平台、启停、AI通过后发布；每日记录包含采集、等待扫码、审核、完成、失败及数量、日志。默认小红书每天10:00/22:00，每轮20条。

`aigc-crawl.timer` 每分钟调用 `scripts.crawl_dispatch`，实际执行时间来自数据库 `crawl_schedules`。排队任务可恢复；进程中断的任务记录失败原因。平台二维码仅通过管理员鉴权接口读取，二维码过期可刷新，登录状态保存在服务器私有 profile。

发布迁移：`9c4f8a2b6d10`。发布前数据库备份 `/opt/aigc-prompt/database-before-admin-20261010.dump`，旧镜像标签 `aigc-prompt-api:rollback-admin-20261010`，旧后台 `/opt/aigc-prompt/admin-dist-before-admin-20261010`。数据库迁移为新增表/列；回滚应用前需停本项目 timer 并确认没有采集运行，不自动降级数据库。

实测后台原计划保存、今日失败记录、手动小红书单条采集自动显示扫码弹窗。用户须自行手机扫码；平台可能撤销登录会话。

## 一键更新后端

在本机项目目录运行（要求已有 `.venv`、Python 3、SSH、scp）：

```bash
python3 scripts/deploy_backend.py
```

也可从任意目录运行绝对路径。默认目标为 `ubuntu@62.234.69.93`，SSH 按本机配置认证；如需密码会在终端提示，不保存密码。脚本先运行 lint/测试，再打包必要后端源码，远端通过锁防止同一脚本并发发布；暂停未来定时触发、等待正在执行的 worker 结束（最多5分钟）、备份数据库/源码/旧镜像、构建并定向重建 API，最后检查 HTTPS 健康并恢复原定时器状态。不会更新主站或管理端，不触发采集。其他手工部署也应避免同时运行。

本地演练：`python3 scripts/deploy_backend.py --dry-run`，不会连接服务器。已有连接可用 `--socket /tmp/aigc-deploy-session/control`，否则默认建立独立连接并在完成后关闭。

数据库有新迁移时默认停止；核对过迁移与旧版本兼容后，使用 `python3 scripts/deploy_backend.py --migrate`，在备份验证后用新镜像执行 `alembic upgrade head`，确认到达目标 head 再切换 API。`--dry-run --migrate` 仍仅本地检查；迁移失败不切换、不自动降级数据库。本次 `e7a13c9b40d2` 为新增可空列及新表，已验证向后兼容。构建日志和备份位于服务器 `/opt/aigc-prompt/releases/backend-日期-时间/`。切换后健康检查失败会恢复旧镜像。源码增量更新复用旧镜像依赖；依赖或Dockerfile变化时需要至少5GiB空闲空间进行完整构建。服务器磁盘不足1GiB会拒绝部署；失败时查看该发布的 build.log，不要删数据卷。保留旧备份和镜像供回滚，不自动全局清理Docker。

## 2026-10-10 正式域名与 Google 登录

正式入口：`https://aigc-prompt.com/`，管理端 `/admin/`。DNSPod `@ A 62.234.69.93` 已生效；Google Cloud 项目 aigc-prompt-511011 的现有 Web 客户端已添加 `https://aigc-prompt.com` 为已授权 JavaScript 来源，保留 localhost 开发来源。客户端 ID沿用后端，服务器Compose前端build.args已持久配置 NEXT_PUBLIC_GOOGLE_CLIENT_ID 与 NEXT_PUBLIC_SITE_URL；前端镜像已重建。后端CORS加入正式域名。

路由：现有 blog-nginx 的域名专属80/443虚拟主机负责 HTTP→HTTPS 与TLS入口，通过验证证书的HTTPS反代到本项目8443域名虚拟主机。原博客/shuce配置保留。修改文件 bind mount 时避免以新inode替换后只reload；本次网关和blog-nginx已定向重建并验证。

证书通过DNS TXT验证签发，位置 `/opt/aigc-prompt/certificates/live/aigc-prompt.com/`，到期日 **2027-01-08**。**域名证书目前不能自动续期**，因为HTTP验证在CA侧失败，DNSPod尚未配置自动DNS验证凭据。原IP自动续期服务已限定 `--cert-name 62.234.69.93`，防止manual证书使原自动续期整体失败。

域名证书续期：在服务器重新运行 certbot manual DNS验证，根据提示在DNSPod更新 `_acme-challenge` TXT记录，确认公共DNS生效后按Enter。成功后将fullchain.pem/privkey.pem复制到 `/opt/blog/blog-deploy/ssl/aigc-prompt.com.crt`、`.key`（私钥600），校验配置并reload两个网关。若需要自动续期，应另行配置最小权限DNSPod API凭据与DNS认证hook，不在源码中保存凭据。

本次验证：TLS可信、HTTP308、首页200、管理端200、内容API200、Google登录组件加载且显示当前Google账号。未代替用户完成最终登录。域名上线回滚备份：`/opt/aigc-prompt/releases/domain-20261010/`；Google参数上线备份：`releases/google-frontend-20261010/`。

## 2026-10-10 画廊分页和详情卡住修复

根因证据：Google登录请求在FastAPI async路由内同步调用google-auth requests，服务器到Google证书地址TCP连接持续等待，uvicorn主线程处于poll且/health本机请求也超时。修复：验证放入独立线程、接口12秒期限、Google HTTP transport10秒期限；网络错误返回503，不影响其他请求。线上并发验证：Google请求等待12秒并返回503期间，health200约0.08秒，分页200约0.18秒；真实详情200约0.48秒。Google网络连通性本身尚未解决，不能声称已完成真实Google账号登录。

前端分页15秒期限、错误重试不改变页码、不误显示列表结束；切分类取消旧请求、忽略过期响应并避免并发加载同页。共享服务端请求12秒期限覆盖列表与详情。前端42项测试、lint/typecheck与生产构建通过；后端新增同步验证不阻塞/传输期限/503测试通过。线上浏览器验证分页卡片19→39→59，用户截图对应“童趣手绘风男孩头像”详情弹窗正常打开。

后端发布备份：releases/backend-20261010-170920；前端备份：releases/loading-fix-20261010。服务器磁盘剩余不足1GiB，下一次部署前需有范围地清理构建缓存或扩容，不删除数据库卷或有效回滚镜像。

## 2026-10-13 首页推荐队列（待部署）

后台入口 `/admin/homepage`：搜索已公开作品，加入推荐，拖拽或上/下移动排列，移除后点击“保存推荐”。最多 50 个；顺序预览用于检查题材与顺序，不能保证所有设备恰好两屏。没有精选时按热度兜底。保存冲突保留本地修改，重新加载后再整理；未保存队列可在当前标签页恢复。

主站首页默认 `recommended`：符合筛选条件的精选按 `homepage_rank` 在前，其余按点赞、创建时间、ID 稳定排序，整体查询后分页。`popular`、`latest`、`hot` 仍独立；关键词搜索默认 latest，主动选推荐可应用同一筛选排序。下架作品不公开展示。管理员 GET/PUT `/api/admin/homepage/featured` 使用 revision 防止覆盖其他编辑，PUT 原子替换；候选接口 `/api/admin/homepage/candidates` 支持搜索与分页。

新迁移 `f8b24d0c51e3`（父版本 `e7a13c9b40d2`）只新增可空字段及索引。新后端启动前必须完成迁移；先发布后端及后台，再发布首页默认推荐前端。现有 Actions 自动迁移仅允许旧的 e7 白名单，本次不会自动迁移，不能先推送新前端造成接口失败。部署时先备份、审阅目标 head，用新镜像执行迁移，再切换后端并校验后台，之后推送前端。迁移兼容旧应用，镜像回滚无需删除字段，不自动 downgrade。

本地验收：真实隔离 PostgreSQL 全套 156 个测试通过，含升级/降级兼容与并发保存；后台 lint/build 通过。浏览器真实数据库回归验证后台拖拽保存、首页排序分页、过滤、热门独立及保存冲突，登录/COS/Redis 用测试替身，不代表线上账号和 COS 完整验收。

本次定向迁移可通过 workflow_dispatch 参数 `reviewed_migration=f8b24d0c51e3` 明确执行，只接受 e7→f8 或已在 f8，其他父版本/head 拒绝；普通 push 自动迁移门禁不变。生产 JWT 算法在发布前只读核对 HS256，不读取或打印密钥。依赖审计的既有 python-jose/ecdsa 无修复版本告警已按固定 HS256 调用路径审阅，不涉及 ECDSA/非对称解码，不能将该审计称为全部无告警。

## 2026-10-19 Chat2DB 线上库连接故障及本机自动恢复

实际根因：Chat2DB 的线上数据源指向 `127.0.0.1:15432/aigc_prompt`，该端口原先依赖临时 SSH 转发。隧道消失后，客户端依然显示“关闭连接”，但列库、执行 SQL 均失败。原始日志为 `Connection to 127.0.0.1:15432 refused`；UI 仅显示 `Unknown Error / connection.error`，不能据此判断数据库宕机，也不能把客户端选中状态当作连接成功。

本机恢复服务：`~/Library/LaunchAgents/com.aigc-prompt.database-tunnel.plist`，脚本目录 `~/Library/Application Support/AIGC Database Tunnel/`。用户登录后运行、每30秒检查 SSH master、本机15432监听及远端 Postgres 容器当前地址；掉线后重建连接，容器地址变化时重新建立转发。SSH心跳30秒、连续3次失败断开，保留严格主机校验。仅监听本机127.0.0.1，不暴露公网数据库；不修改生产数据库、账号或安全组。认证复用已授权的 macOS钥匙串条目，不将密码写入脚本或日志。钥匙串锁定、网络离线或凭据失效时仍需恢复相应条件，不能承诺绝不断线。

排查顺序：
1. `pg_isready -h 127.0.0.1 -p 15432`，先确认隧道；失败时不要随意改数据源密码。
2. `launchctl print gui/$(id -u)/com.aigc-prompt.database-tunnel` 查看最近退出码和运行状态；该任务为定时短任务，空闲时not running正常。
3. 检查上述脚本目录的`service.log`，不输出钥匙串密码、数据库密码或完整环境。
4. 隧道健康后，在Chat2DB“编辑数据源”底部测试连接，再验证展开public表列表和只读查询。不要只根据TCP端口开放声称客户端已恢复。

手动触发恢复：`launchctl kickstart gui/$(id -u)/com.aigc-prompt.database-tunnel`。关闭自动恢复：`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.aigc-prompt.database-tunnel.plist`，再关闭该服务state.json所指的专用SSH master；不要关闭其他工作会话。

已验证：修复前15432无监听；恢复后pg_isready成功，Chat2DB显示“测试连接成功”，public下18张表可展开、users已有4行可读取。执行专用隧道断线模拟后检查自动恢复，不影响生产API或其他SSH会话。
