# AIGC提示词站 · 图片存储方案（腾讯云 COS）

> 最后更新：2026-10-09
> 与爬虫方案的关系：本方案不属于采集流程本身，只负责"草稿审核通过转正式内容"这一步的图片归宿处理。采集阶段的图片仍然落在本机（MediaCrawler 的 `data/` 目录），技术原理和风险评估见 `aigc-prompt-爬虫方案.md`。

---

## 一、要解决的问题

草稿阶段的图片由 MediaCrawler 下载到本机磁盘，审核通过转为正式 `Prompt` 内容后，如果继续直接引用本地路径：

- 正式发布内容的图片完全依赖"这台 Mac 一直开机、后端服务一直运行"，电脑关机或服务重启，已发布内容的封面图就会全部失效
- 没有任何云端备份，本地文件意外丢失（误删、磁盘故障）会导致发布内容图片永久失效

所以审核通过这一步，需要把本地图片上传到可公网稳定访问的对象存储服务。

## 二、服务选型

选用**腾讯云 COS**（对象存储），Bucket：`aigc-prompt-images-1310986640`，地域：`ap-guangzhou`。

当时也考虑过 Cloudflare R2、七牛云，最终选 COS 的原因是用户已有腾讯云账号、国内访问速度有保障，权衡下直接用腾讯云账号最省事。

## 三、技术实现

### 3.1 触发时机

只在**审核通过**（`approve_draft`）这一步触发上传，不在采集阶段就上传。原因：

- 采集到的草稿大部分会被运营方人工筛掉，没必要把所有抓到的图片都占用 COS 存储和流量
- 只有真正要发布成正式内容的图片才值得长期保存

### 3.2 核心代码

`app/services/cos_service.py`：用腾讯云官方 SDK `cos-python-sdk-v5`（同步 API），上传操作用 `asyncio.to_thread` 包一层避免阻塞事件循环。

```python
async def upload_prompt_image(local_path: Path) -> str:
    """上传一张正式内容的配图到 COS，返回公网访问 URL。"""
    object_key = f"prompts/{uuid.uuid4().hex}{local_path.suffix.lower()}"
    return await asyncio.to_thread(_upload_sync, local_path, object_key)
```

`object_key` 规则：`prompts/{随机uuid}{原扩展名}`，不用本地文件名（可能重复/带特殊字符），也不保留原始目录结构。

### 3.3 和审核流程的集成

`app/services/crawl_service.py` 的 `approve_draft`：

- 遍历草稿的每张配图，调用 `cos_service.upload_prompt_image` 上传
- `image_url`（原图）和 `thumbnail_url`（列表缩略图）都写入 COS 返回的 URL（目前没有真正的缩略图生成服务，`thumbnail_url` 直接复用原图地址）
- 单张图片上传失败不会让整条内容审核失败：跳过这张图、记日志（`logger.warning`），不阻塞其他图片和审核操作本身——取舍是"宁可内容先带着少一张图发出去，也不要一张坏图卡住整条审核流程"
- 如果草稿图片的 `local_path` 本身已经是完整 URL（极早期个别手写脚本直接存了 CDN 地址），跳过上传，原样复用

### 3.4 权限配置

Bucket 创建时默认是 `private`（私有读写），必须手动改成 `public-read`，否则图片传上去了但浏览器直接访问会 403。

```python
client.put_bucket_acl(Bucket=bucket, ACL='public-read')
```

权限变更有几秒延迟生效，验证时需要注意。

### 3.5 前端配套

Next.js 的 `<Image>` 组件默认只信任同域图片，COS 域名必须显式加入白名单，否则图片会被静默拦截（不报错，页面空白）：

`admin-panel-frontend/next.config.ts`：
```typescript
images: {
  remotePatterns: [
    { protocol: "https", hostname: "aigc-prompt-images-1310986640.cos.ap-guangzhou.myqcloud.com" },
    { protocol: "http", hostname: "localhost", port: "8000" },
  ],
},
```

## 四、配置项

`.env` / `.env.example`：

```
COS_SECRET_ID=
COS_SECRET_KEY=
COS_BUCKET=aigc-prompt-images-1310986640
COS_REGION=ap-guangzhou
COS_CDN_DOMAIN=
```

`COS_CDN_DOMAIN` 留空则直接用 COS 默认访问域名；接了 CDN 加速后再填自定义域名。

## 五、真实验证记录（2026-10-09）

- 直接调用 SDK 上传测试图片，确认密钥和 Bucket 权限配置正确，上传后 curl 返回 200 + `image/jpeg`
- 调用真实 `approve` 接口，用一条真实微博草稿走完整流程，查数据库确认 `image_url`/`thumbnail_url` 都是真实 COS 地址，浏览器截图确认图片正常渲染
- 批量审核 30 条草稿（215 张图），全部图片成功上传，数据库里没有空的 `thumbnail_url` 或非 `http` 开头的 `image_url`

## 六、已知局限 / 待办

- [ ] 历史已发布内容的本地图片未批量迁移：这次改动只对"以后新审核通过"的内容生效，之前已经存量入库、`image_url` 还是本地路径的旧内容暂未处理（用户明确表态暂不需要）
- [ ] COS 密钥目前直接写在 `.env` 明文存储，长期建议结合密钥管理方案定期轮换（对话中出现过明文密钥，需要找时间去控制台禁用重建）
- [ ] 没有真正的缩略图生成服务，`thumbnail_url` 直接复用原图，列表页加载的其实是原图大小，流量和加载速度有优化空间
