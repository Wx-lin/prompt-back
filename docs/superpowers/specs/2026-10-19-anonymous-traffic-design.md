# 匿名独立访客统计（UV）

用户已确认：未登录也计入，同一访客重复访问去重，更新现有后台大盘。采用第一方随机浏览器Cookie识别，同浏览器当天及7/30天区间均精确去重；不同设备/浏览器和清Cookie可能重复，不能称真实人数。只统计成功上报，从启用起生效，不补历史流量。

流程：主站不可见客户端tracker在页面进入、路由变化和恢复可见时调用同源POST /api/analytics/visit。Next BFF忽略客户端标识与URL，自行读取或生成UUID Cookie（HttpOnly、SameSite=Lax，生产Secure，365天），SHA256后转发到内部API。仅同源Origin/Fetch-Site允许写入，限制请求体，禁缓存，不记录完整URL/referrer/IP/邮箱。上报失败不阻塞网站，也不伪造零流量。

后端POST /api/analytics/visit严格接收{visitor_hash:64hex}，按服务器北京时间日界插入日去重表，日期+hash唯一，ON CONFLICT DO NOTHING。GET /api/admin/traffic需已有管理员鉴权，no-store；单次一致快照查询返回今日/7天/30天UV及最近30个自然日每日UV，7/30天用COUNT DISTINCT hash，不用SUM(日UV)。状态表保留首次启用时间。每日匿名记录保留90天，由现有scheduler定期删除过期记录。新增加法迁移，不改现有表及部署自动迁移白名单，部署前需明确授权并先应用迁移。

契约GET /api/admin/traffic：{generated_at:ISO,tracking_since:ISO|null,timezone:'Asia/Shanghai',today_uv:int,seven_day_uv:int,thirty_day_uv:int,daily:[{date:'YYYY-MM-DD',uv:int}]}。daily固定近30日零填充，启用前数据不代表实际0，UI明确说明历史未统计。Cookie仅标识浏览器，未登录不触及登录状态；浏览器端过滤常见crawler但不宣称彻底排除机器人。

看板复用现有ECharts，新增独立访客卡片和近7/30天柱状图切换；图表不与用户注册量混淆，流量失败仅显示统计区错误，可重试。
