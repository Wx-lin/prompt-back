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
