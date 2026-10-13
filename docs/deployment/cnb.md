# CNB 免费额度接入

## 当前边界

前端私有 CNB 仓库：[wx-lin-aigc-prompt/frontend](https://cnb.cool/wx-lin-aigc-prompt/frontend)。
账户/仓库建立不代表流水线已成功运行，实际构建记录、镜像摘要和权限需另行验收。
后端（含管理后台）私有仓库已创建：https://cnb.cool/wx-lin-aigc-prompt/backend 。本地 `.cnb.yml` 已准备，上传和云端执行待完成。
没有在此流程开通 CLS、托管 PostgreSQL、Redis、Grafana 或付费服务。

## 免费额度与账户设置

只使用组织默认免费额度，不绑定预算、不购买资源包、不提高用量上限。
官方标准额度为构建 160 核时/月，Git 与制品存储各 100 GiB；账户页面的实际权益为准。
构建消耗按核数和运行时间累计；后端与后台各 2 核，前端 4 核。
镜像占用制品空间，保留线上和回滚版本，定期移除无用候选。

仓库必须保持私有；保护 main，限制可信管理员修改流水线及发布镜像。
不要导入 `.env`、生产数据库地址、Redis 密码、COS/JWT/SSH 密钥。

## 流水线

- 前端 push/PR：npm ci、lint、typecheck、88 项现有单元/组件测试。
  main 的“构建主站镜像”按钮由管理员手动执行检查、Docker 构建和推送；
  需要公开 Google Web OAuth client ID。固定站点 https://aigc-prompt.com。
  版本标签含完整 SHA 和构建号，后续部署必须固定输出的 sha256 摘要。
- 后端 push/PR：Python 3.13 + uv 0.11.17，uv sync --locked --group dev，
  Ruff 和默认 pytest。`AIGC_TEST_DATABASE_URL` 留空，真实 PostgreSQL 集成测试
  默认跳过；不连接生产数据库，不运行迁移，不启动生产 API 或爬虫。
- 管理后台：独立 Node 24 pipeline，npm ci、lint、build，ADMIN_BASE_PATH=/admin/。
  两条独立 pipeline 各自安装依赖，不依赖另一条的容器或目录状态。
- 采用 main 显式事件与 `$` 分支兜底，避免分支匹配导致重复检查；已有 PR 的分支更新仍可能同时触发 push 和 PR。

暂不构建后端生产镜像：现有 Docker 构建依赖仓库外 MediaCrawler，尚未固定与注入
可复现的源代码版本。生产 Compose、数据库和 Redis 保留原状。
人工生产发布、备份、健康检查、网关更新、回滚和日志/基础监控接入另行完成。

## 验收

在 CNB 中确认 push/PR 检查成功、镜像按钮权限和缺失 OAuth ID 报错，记录镜像摘要。
本地检查不能证明 CNB 网络、镜像拉取、权限或云端额度设置成功。

## 官方依据

- [CNB 免费额度与计费](https://docs.cnb.cool/zh/pricing.html)
- [事件与分支匹配](https://docs.cnb.cool/zh/build/trigger-rule.html)
- [runner CPU 配置](https://docs.cnb.cool/zh/build/grammar.html)
- [手动按钮](https://docs.cnb.cool/zh/build/web-trigger.html)
- [官方 uv Docker 镜像](https://docs.astral.sh/uv/guides/integration/docker/)

已创建后端私有仓库：https://cnb.cool/wx-lin-aigc-prompt/backend 。代码上传和云端执行仍待验证。前后端共用顶级组织的免费额度，不各自独立享有 160 核时。

## GitHub 自动发布（当前实施）

后端公开 GitHub 仓库使用 `.github/workflows/deploy.yml`：main push 自动检查，
Python 测试与管理后台 /admin/ 构建成功后，将同一次提交的源包和后台静态包上传到
既有服务器，调用 `deploy/update-backend.sh`。CNB 配置保留为备用检查入口，
当前不需要建立后端 CNB 仓库。前端由其独立仓库流水线发布。

在 GitHub production environment 配置 DEPLOY_HOST、DEPLOY_USER、DEPLOY_SSH_KEY、
DEPLOY_KNOWN_HOSTS 四个 Secrets。SSH 公钥需由用户授权安装在服务器；known_hosts
必须来自独立核验的服务器指纹，流水线不通过 ssh-keyscan 自动信任远端。
密钥不会进入公开代码、制品或日志。不配置付费 runner，公开 GitHub 仓库使用
标准托管 runner；私有仓库须重新检查账户免费额度。

服务器部署保留磁盘保护、数据库备份验证、定时爬虫等待、源码与镜像备份、
健康检查及回滚。所有前后端发布共用 `.release-deploy.lock`，后端另保留既有锁。
管理后台校验 SHA256 并拒绝危险 tar 成员，先追加静态资源、最后原子替换 index；
失败恢复旧 index，旧哈希资源不删除。当前仅自动许可数据库
`5c8e9f0a12b3 → e7a13c9b40d2` 的已审核向后兼容迁移；未来迁移仍保护性停止。
人工脚本的 `--migrate` 行为保持不变，不自动数据库降级或恢复。

此配置尚未上传或真实执行；Secrets、远端免密 sudo、共享锁与线上健康检查需验收。

后台静态文件显式设置目录0755、文件0644，避免发布备份umask077导致Nginx403；
回滚index同样设置0644。后台公共HTTPS /admin/ 返回内容必须与本次index的SHA256
一致，API健康正常但后台错误/旧页面也会判定发布失败并回滚。

前后端共享生产发布锁最多等待600秒，避免两个仓库同时推送时其中一条立即失败；
既有后端发布锁同样有600秒上限。超时退出，不无限等待或取消正在发布的任务。

## 真实CI安装失败修复

GitHub运行38360241552在后端容器拉取与管理后台依赖安装阶段失败，尚未部署。
改用官方setup-python（完整提交SHA固定）提供Python3.13，再从公开PyPI安装已核验
存在的uv0.11.17；CNB备用配置同样改为官方Python镜像。管理后台锁文件的resolved
地址从公司内网域名迁到registry.npmjs.org，保留全部依赖版本和integrity；uv.lock
原本只使用pypi.org/files.pythonhosted.org，不需要更改。
