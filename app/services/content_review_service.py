"""
AI 内容审核服务：草稿转正式内容前的自动预审。

单次结构化调用 qwen3.7-plus（阿里云百炼，OpenAI 兼容接口），对草稿的图片和文案做三件事：
  1. 判断每张图片是否是 AI 生成的效果图（而非游戏截图、实景照片等无关素材）
  2. 清洗文案——去掉话题标签、营销语气、无关的 emoji，保留真正描述图片效果的提示词内容
  3. 根据图片内容建议分类（12 个创作意图分类之一）

不使用 Agent 循环，一次请求完成所有判断，结果以结构化 JSON 返回。
enable_thinking 强制关闭，避免额外消耗推理 token。

接入背景见 docs/aigc-prompt-内容审核方案.md（待创建）。
接入时机：app/services/crawl_service.py 的 approve_draft，COS 上传之前。
"""

import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import get_settings
from app.utils.category_catalog import CATEGORY_CATALOG, CATEGORY_LABELS

logger = logging.getLogger(__name__)

# 有效的分类 key 集合，用于校验模型输出
_VALID_CATEGORIES = frozenset(CATEGORY_LABELS.keys())

# DashScope OpenAI 兼容接口地址
_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_MODEL = "qwen3.7-plus"

# 系统提示：让模型以 JSON 格式返回审核结果
_SYSTEM_PROMPT = f"""你是一个专业的 AIGC 内容审核助手。
你将收到一批图片（由爬虫从社交媒体采集）和对应的原始文案。

你需要做三件事：

1. 对每张图片判断：这张图是否是 AI 生成的效果图？
   - is_credible: true = 看起来是 AI 绘图或 AI 生成的图像效果
   - is_credible: false = 明显是游戏截图、商品实拍、真人照片、品牌宣传图等非 AI 效果图
   - reason: 用一句中文简短说明判断理由

2. 根据完整提示词、说明和图片的明确成品用途，将内容归入以下 12 个分类之一：
{json.dumps(CATEGORY_CATALOG, ensure_ascii=False, indent=2)}
   明确成品用途优先于风格或主体。不能确定用途时返回空字符串，交人工审核。
   禁止默认归入 scene；纯教程也返回空字符串。
   suggested_category 的非空值必须是上面的英文 key（如 "portrait"，不是中文标签）。

3. 清洗原始文案，提取真正有价值的提示词内容：
   - 去掉话题标签（#xxx）、平台 @ 和互动语气（"快来看"、"超好用"等）
   - 去掉与图片效果无关的 emoji 和宣传语
   - 保留描述图片风格、画面效果、使用的模型或工具的内容
   - suggested_title：8 字以内的简洁标题，描述图片的主题或风格
   - cleaned_prompt_text：清洗后的提示词正文，保留真正有价值的内容；如果原始文案几乎没有有效内容，返回空字符串

必须以纯 JSON 返回，不要有任何其他内容，格式如下：
{{
  "images": [
    {{"index": 0, "is_credible": true, "reason": "画面细节和光影具有典型的 AI 生成风格"}},
    {{"index": 1, "is_credible": false, "reason": "明显是游戏《塞尔达传说》的官方截图"}}
  ],
  "suggested_title": "赛博朋克女战士",
  "cleaned_prompt_text": "赛博朋克风格，女战士，霓虹灯背景，写实渲染，电影级光影",
  "suggested_category": "portrait"
}}"""


@dataclass
class ImageReviewResult:
    """单张图片的审核结果。"""

    index: int
    is_credible: bool
    reason: str


@dataclass
class ContentReviewResult:
    """整条草稿的 AI 审核结果。"""

    images: list[ImageReviewResult]
    suggested_title: str
    cleaned_prompt_text: str
    suggested_category: str

    @property
    def has_credible_image(self) -> bool:
        """是否至少有一张图片被判断为 AI 效果图。"""
        return any(img.is_credible for img in self.images)


class ContentReviewError(Exception):
    """内容审核调用失败（网络错误、解析错误等），上层可选择降级处理。"""


def _get_client() -> AsyncOpenAI:
    """创建 DashScope 兼容 OpenAI 的异步客户端。"""
    settings = get_settings()
    if not settings.dashscope_api_key:
        raise ContentReviewError("DASHSCOPE_API_KEY 未配置，无法调用内容审核服务")
    return AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=_DASHSCOPE_BASE_URL,
        timeout=90.0,
        max_retries=1,
    )


def _build_user_message(raw_text: str, image_urls: list[str]) -> list[dict]:
    """
    构建多模态用户消息：先列出图片（每张带序号标注），最后附上原始文案。
    index 从 0 开始，和 ImageReviewResult.index 对应。
    """
    content: list[dict] = []

    for idx, url in enumerate(image_urls):
        content.append({"type": "text", "text": f"图片 {idx}："})
        content.append({"type": "image_url", "image_url": {"url": url}})

    content.append({"type": "text", "text": f"\n原始文案：\n{raw_text}"})

    return content


def _parse_response(raw_json: str, image_count: int) -> ContentReviewResult:
    """
    解析模型返回的 JSON 字符串，构造 ContentReviewResult。
    解析失败时抛出 ContentReviewError，而不是让 KeyError/ValueError 往上冒。
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ContentReviewError(f"模型返回的不是合法 JSON：{exc}\n原始内容：{raw_json[:300]}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("images"), list):
        raise ContentReviewError("审核结果必须包含 images 数组")
    images = []
    seen = set()
    for item in data["images"]:
        if not isinstance(item, dict):
            raise ContentReviewError("图片审核结果格式错误")
        index = item.get("index")
        if type(index) is not int or not 0 <= index < image_count or index in seen:
            raise ContentReviewError("图片索引越界或重复")
        if type(item.get("is_credible")) is not bool:
            raise ContentReviewError("is_credible 必须是 JSON 布尔值")
        if not isinstance(item.get("reason", ""), str):
            raise ContentReviewError("图片理由必须是字符串")
        seen.add(index)
        images.append(ImageReviewResult(index, item["is_credible"], item.get("reason", "")))
    if seen != set(range(image_count)):
        raise ContentReviewError("模型未审核全部图片")
    for field in ("suggested_title", "cleaned_prompt_text", "suggested_category"):
        if not isinstance(data.get(field), str):
            raise ContentReviewError(f"{field} 必须是字符串")
    if data["suggested_category"] and data["suggested_category"] not in _VALID_CATEGORIES:
        raise ContentReviewError("模型返回无效分类")
    return ContentReviewResult(
        images=sorted(images, key=lambda image: image.index),
        suggested_title=data["suggested_title"].strip(),
        cleaned_prompt_text=data["cleaned_prompt_text"].strip(),
        suggested_category=data["suggested_category"],
    )


async def review_draft_content(
    raw_text: str,
    image_urls: list[str],
) -> ContentReviewResult:
    """
    对单条草稿进行 AI 预审。

    Args:
        raw_text:    草稿的原始文案（raw_content 或 extracted_prompt_text）。
        image_urls:  草稿配图的公网 URL 列表（顺序和数据库中 sort_order 一致）。
                     如果列表为空，图片相关字段会返回空列表。

    Returns:
        ContentReviewResult，包含每张图片的可信度判断、清洗后的标题/文案、建议分类。

    Raises:
        ContentReviewError: 网络错误、认证失败、JSON 解析失败等，上层决定是否降级。
    """
    if not image_urls:
        # 没有图片时跳过多模态调用，直接返回一个"待人工确认"的最小结果
        logger.warning("草稿没有配图，跳过 AI 图片审核，文案和分类仍会处理")

    client = _get_client()

    user_content = _build_user_message(raw_text, image_urls)

    try:
        response = await client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            # 关闭思考模式：避免额外消耗推理 token（qwen3.7-plus 默认开启 thinking）
            extra_body={"enable_thinking": False},
        )
    except Exception as exc:
        raise ContentReviewError(f"DashScope API 调用失败：{exc}") from exc

    raw_output = response.choices[0].message.content or ""
    logger.debug("content_review 原始输出（%d chars）：%s", len(raw_output), raw_output[:500])

    return _parse_response(raw_output, len(image_urls))
