# 微信小程序登录接入留痕（2026-10-20 更新）

## 头像昵称扩展（2026-10-20）

正式微信登录、点赞收藏及退出重登已在此前发布后由用户实测确认，生产身份与请求聚合核验一致。以下头像昵称扩展沿用已登录用户表字段，无数据库迁移；运行时发布记录以新前端项目的 `VALIDATION.md` 与 `docs/项目留痕.md` 最新段落为准。本文下方“待配置/待发布”等内容保留此前阶段背景。

用户已确认首次登录后弹出可跳过的完善资料界面，保存后从后端自动回显，且可在“我的”修改。`wx.login` 不自动提供头像昵称；客户端使用 `chooseAvatar` 与 `input type="nickname"`，提交时才处理用户选定资料。

- 新增 `POST /api/auth/me/profile`，表单 `nickname` 必填（trim 后1–30字符，拒绝控制字符），可选文件 `avatar`；仅当前 active JWT 用户且拥有配置 AppID 的微信身份可以修改。拒绝客户端指定用户、OpenID 或外部头像URL。昵称单改不覆盖并发头像。
- 头像源文件不超过2MiB及4MP，真实解码静态 JPEG/PNG/WebP，去元数据，先缩小再输出256×256 JPEG；进程内线程信号量同时只解码一张，避免高压缩图并发撑满API内存。
- 随机 `user-avatars/<user-uuid>/<random>.jpg` 写入 COS 私有对象；数据库仅存 `cos-avatar://` 永久引用。`GET /api/auth/me` 与保存结果仅为当前用户所属对象生成1小时 HTTPS 签名链接；既有网站头像 URL 保持兼容。
- 每用户每60秒最多10次；Redis故障、存储/签名故障与数据库失败均给安全错误，不记录秘密或完整上游响应。头像上传/正常事务失败清理新对象；提交结果不确定时先核对引用，无法确认则保留。旧头像不在并发更新中删除，历史对象及备份清理仍需独立维护。
- 成功及失败均 `no-store`。网关仅精确 POST profile 路由放宽为3MiB，其他入口维持128KiB及原有白名单。独立 exact location 显式包含guard、rewrite、proxy、JWT与缓存头；不能假设嵌套 location 会继承父块。

TDD与复核：后端全量437通过/11跳过，新增资料57通过，资料服务分支覆盖87%；网关16通过，真实临时nginx实例14项探针通过。新前端98项中95通过/3默认跳过，行覆盖97.79%（不是全部指标达到80%），官方编译10页4组件。独立需求与代码安全复核未留下高/中阻塞。依赖不变，审计仍为既有2包3条告警、无可用修复版本，不能称零漏洞。

真实COS合成探针已确认私有对象匿名403、签名200及字节一致，随后仅删除测试对象。此证据不代替真实用户的头像选择/保存/重登验收。真机上传还需正式号 uploadFile 合法域名 `https://aigc-prompt.com`，隐私指引同步头像昵称的自愿提供用途；未关闭域名或证书校验。

## 原始登录实现与历史发布背景

接入代码与隔离验证已完成。前端已改为用户正式备案页面的 AppID `wx9b772924398374a7`；2026-10-20 已经用户明确授权，通过隐藏终端输入及 SSH stdin 将同一正式号凭据写入服务器运行环境。配置检查仅返回 AppID 匹配、密钥存在等状态，不表示凭据已通过真实微信交换。发布前数据库为 `c74d219e8a60`，公开网关仍只读，微信登录与互动尚待本轮发布和真实账号验收。

## 技术栈与身份边界

- FastAPI / Pydantic 2 校验 `wx.login` 临时 code。
- HTTPX 异步 HTTPS transport 请求微信官方 `code2session`，连接/读写分别最多 5 秒，整次交换最多 8 秒；不自动重试、不跟随重定向。
- SQLAlchemy 2 异步会话；Alembic 增量迁移；正式数据库仍使用现有 PostgreSQL。
- Redis Lua 原子限流：每个连接来源地址每 60 秒最多 60 次。Redis 不可用时返回 503。
- 沿用现有 JWT 和 `users`，使点赞、收藏、个人信息接口继续使用同一套鉴权。

微信身份存入新增 `wechat_identities`，唯一约束为 `(app_id, openid)`。首次登录在同一事务创建用户和身份，重复登录使用原用户。并发首次登录发生唯一冲突时回滚新用户，再查找已成功的身份，避免重复账号和孤立用户。

不查找或绑定网站邮箱、Google ID、历史 `users.wechat_openid` 或 unionid。新微信用户的昵称仅为“微信用户”，不请求头像、昵称、手机号授权。AppID 变更意味着另一个身份作用域，不自动合并账号。

## 请求契约

```http
POST /api/auth/wechat
Content-Type: application/json

{"code":"wx.login 返回的临时 code"}
```

请求只允许 `code`，去除首尾空格后长度为 1–512，字符为英文字母、数字、下划线或短横线。客户端传入 openid、AppID 等额外身份字段会被拒绝。

成功响应沿用现有格式，带 `Cache-Control: no-store`：

```json
{"access_token":"服务端签发的 JWT","token_type":"bearer"}
```

后续使用 `Authorization: Bearer <access_token>` 调用 `/api/auth/me`、点赞和收藏接口。

失败格式沿用 FastAPI：`{"detail":"安全的中文错误消息"}`。

| 状态码 | 情况 |
| --- | --- |
| 400 | 微信 code 无效或已使用（40029 / 40163） |
| 403 | 微信拦截高风险账号（40226），或本地用户被停用 |
| 422 | 输入格式错误或包含多余字段 |
| 429 | 本地限流，或微信调用限流（45011） |
| 503 | 未配置、Redis 故障、微信超时/网络错误、微信其他错误、上游响应异常 |

停用用户不能通过微信登录获得新令牌；现有令牌也不能再通过公共用户鉴权依赖。匿名接口会将该身份视为未登录。

## 注册后的服务端配置

在后端环境中设置，禁止放入小程序代码、源码提交或聊天消息：

```dotenv
WECHAT_APP_ID=
WECHAT_APP_SECRET=
```

AppID 格式为 `wx` 加 16 位十六进制字符；AppSecret 使用 Pydantic `SecretStr` 避免配置对象打印出原值。配置为空时接口直接返回 503，不访问微信、不生成模拟用户或令牌。

上线时应先备份现有数据库，再按既有发布流程执行新增迁移 `d91a72f4b308`（前置 `c74d219e8a60`），重启后端使环境配置生效。新增表不改写现有用户、提示词和互动数据。此次没有执行这些线上步骤。

公网网关需允许 `POST /mini-api/api/auth/wechat` 并映射到该接口，客户端的 request 合法域名与实际 HTTPS 网关一致。网关应传递真实来源地址，Uvicorn 只信任已知反向代理；应用不自行信任任意请求的 `X-Forwarded-For`。否则所有访问可能按同一代理地址共同限流。

## 凭证保护

- 只使用固定的 `https://api.weixin.qq.com/sns/jscode2session`，客户端不能指定上游 URL。
- AppSecret 和 code 只用于服务端交换；session_key 和 unionid 不落库、不返回客户端。
- 使用底层 HTTPX transport，避免默认 HTTPX INFO 请求日志打印带 AppSecret / code 的完整 GET URL；不记录上游正文或异常原文。
- Redis 限流键为 JWT 密钥参与的来源地址 HMAC，不存明文 IP 或 code。
- 不增加任何网站账号绑定、发布、审核或举报接口。

## 验证与后续

`tests/test_wechat_auth.py` 使用独立内存 SQLite 真正执行事务和唯一约束，通过 ASGI 完成登录到携带 JWT 访问 `/api/auth/me` 的链路；微信传输由测试替身隔离，不调用真实微信服务。

验证包括配置缺失、输入限制、固定 HTTPS 地址、有限超时、敏感日志检查、微信错误转换、Redis 故障与限流、重复登录、跨 AppID 身份隔离、不绑定网站账号、停用用户、唯一冲突回滚及无孤立用户。迁移另做 PostgreSQL 离线 SQL 检查，没有连接生产数据库。

本次结果：34 个微信专项测试通过；后端全部默认测试为 292 通过、11 个因未配置独立数据库等条件跳过。新增微信服务/身份模型共 108 行全部覆盖，含分支覆盖率 99%；该数字不代表整个后端项目的覆盖率。Ruff 和 `git diff --check` 通过。

```bash
# 在后端项目目录执行；均不连接生产服务
.venv/bin/python -m pytest tests/test_wechat_auth.py -q
.venv/bin/python -m pytest -q
.venv/bin/python tests/wechat_coverage.py
.venv/bin/python -m alembic upgrade c74d219e8a60:d91a72f4b308 --sql
```

覆盖率脚本需要开发环境安装 `coverage`。当前 Python 3.13 / SQLAlchemy 2.1 编译扩展在追踪模式下触发 `InternalTraversal` 类型错误，因此脚本仅在该测量进程选择 SQLAlchemy 随包提供的等价 Python 实现；不改动第三方安装文件、业务代码或正常测试流程。

正式 AppID 和服务端凭据现已配置；真实微信账号登录和正式 PostgreSQL 并发首次登录仍须在发布后验收，测试替身不能替代。配置工具为 `python3 scripts/configure_wechat.py --app-id 正式AppID --configure`，通过终端隐藏输入及 SSH stdin 写入固定运行环境，不入参数/日志；已有非空密钥拒绝覆盖。工具仅配置，不部署或重启。正式上线步骤见新前端项目 `docs/上线操作清单.md`。

官方接口说明：[微信 code2session](https://developers.weixin.qq.com/miniprogram/dev/server/API/user-login/api_code2session.html)，[小程序登录流程](https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/login.html)。

## 2026-10-20 发布准备补充

显式 reviewed dispatch 已增加 `d91a72f4b308 <- c74d219e8a60`，push 自动迁移范围不变。网关本地升级稿仅增加微信登录、当前用户、私人收藏列表及 UUID 点赞/收藏 POST/DELETE；支持从已知浏览版配置安全升级。本次仅准备未上线。生产 Uvicorn 尚未配置可信代理；上线时必须按实测的 gateway 与外层桥接来源设置精确信任，并验证伪造 XFF 被忽略，不能使用 `*`。

本轮最终检查：后端 374 通过、11 跳过；Ruff、diff 检查通过。配置工具 57 项隔离测试与独立安全复核通过；默认 check 实测仅返回安全布尔，服务端微信两项仍未配置，未执行 configure。迁移守卫 57 项通过，网关升级 10 项通过。正式微信账号和生产 PostgreSQL 并发验收待正式账号、凭证及部署后进行。

## 正式发布执行前复查

上述“未配置”的结果为早期历史检查。最新配置已成功写入，备份位于受保护的 `releases/wechat-config-*` 目录；任何密钥值均不记录在本文或仓库。最新后端回归仍为374通过、11跳过，Ruff及差异检查通过。

发布采用同一已审阅main提交，push标记`[skip ci]`避免没有新迁移权限的重复push部署，再显式dispatch `reviewed_migration=d91a72f4b308`完整执行CI和部署；不扩大自动迁移白名单。生产精确可信代理在API重建前配置并备份，开放登录前核验真实来源与伪造XFF边界。

后端更新脚本会替换deploy目录，可能更换宿主机nginx文件inode。后端发布后先检查宿主与gateway容器的nginx文件hash及inode；必要时只重启既有gateway容器以刷新单文件bind mount，并重新确认其IP，随后再使用同一提交的精确网关升级工具。不得绕过挂载hash保护。

本轮`pip-audit`发现3条既有告警，涉及ecdsa 0.19.2（含重复条目，CVE-2024-23342）与python-jose 3.5.0（CVE-2026-85394），无修复版本。当前JWT调用明确限制配置算法，生产只读核验为HS256及已配置的对称密钥；没有ECDSA签名或密钥生成调用。此项为现有依赖的调用路径审阅，不得写成“审计无告警”。
