# GitHub 自动部署 Implementation Plan

> **For agentic workers:** Use subagent-driven-development to implement the independent frontend and backend tasks, then review and verify the combined release.

**Goal:** 推送 main 自动完成测试、构建、腾讯云发布及健康检查。

**Architecture:** 两个现有 GitHub 仓库分别部署主站与后端/管理后台。标准云端 Runner 执行检查；前端在 Runner 构建 Docker 镜像并传输，后端复用已验证的增量构建脚本。远端共用发布锁，保留旧镜像/源码/后台入口及数据库备份，失败恢复应用，不自动降级数据库。

**Tech Stack:** GitHub Actions、Node 24、Python 3.13、OpenSSH、Docker Compose。

- [ ] 前端：.github/workflows/deploy.yml 调用 npm ci、lint、typecheck、test，构建带 Google 公开 Client ID 的镜像；deploy/update-frontend.sh 校验 SHA256、并发镜像状态、磁盘空间，导入并切换、nginx reload、HTTPS 检查与回滚。
- [ ] 后端：.github/workflows/deploy.yml 调用 uv sync --locked、Ruff、pytest，构建 admin dist（ADMIN_BASE_PATH=/admin/）；git archive 同一提交，调用 deploy/update-backend.sh，共享锁、后台资产先于 index、备份和回滚；仅已审查的直接迁移 5c8e9f0a12b3→e7a13c9b40d2 允许自动执行。
- [ ] 测试：先验证缺少脚本/行为时失败，再用独立临时目录及 Docker/SSH mock 覆盖校验失败、并发更新、健康失败回滚、迁移门禁；执行 bash -n、diff check、lint/typecheck/单测和构建。
- [ ] Git：核对 prompt-fe 远端导入历史与本地历史，保留两边历史合并；禁止 force push。提交精确文件清单，推送前复查源码不含生产配置或凭据。
- [ ] 权限：使用两个仓库的 Actions Secrets 保存部署 SSH 凭据；主机公钥由腾讯云控制台实读核对，不关闭 StrictHostKeyChecking。新增服务器访问授权和秘密传输时按浏览器规则确认。
- [ ] 验收：确认实际 GitHub push workflow 运行成功、远端镜像版本和公网主站/health/admin 静态资源成功；未跑到部署不能报告自动部署已接通。
