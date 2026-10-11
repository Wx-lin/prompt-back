"""
版权声明关键词过滤：拦截原文里带"禁止转载/禁止商用/未经许可"等声明的草稿。

背景：爬虫采集的素材来自社交媒体原创作者，很多作者会在正文里明确写"禁止转载"
"仅供学习交流，禁止商用"之类的声明。本产品属于二次整理展示+引导复制使用的场景，
继续发布这类明确声明过的内容存在侵权风险，必须在转为正式内容之前拦截掉。

设计取舍：
- 用关键词硬匹配而不是完全依赖 AI 判断——关键词命中后直接拦截，不受模型单次判断
  准确率的影响，这是版权风险问题，必须用确定性规则兜底，AI 判断只能作为补充。
- 关键词列表覆盖常见措辞变体（转载/商用/二次销售的否定表达），但不可能覆盖所有
  变体说法，关键词命中是充分条件（命中就一定拦截），不是必要条件（没命中不代表
  一定没有版权限制，AI 预审的 content_review_service 可以从语义层面补充识别）。
- 只检查原始文案（raw_content / prompt_text），不检查图片里嵌入的文字（如水印里
  写的版权声明），这是已知局限，图片文字识别需要 OCR，超出当前范围。
"""

import re

# 覆盖"禁止转载""禁止商用""未经许可""仅供学习/交流"等常见措辞变体。
# 关键词设计为"核心词"而不是完整短句，兼容标点、空格等排版差异
# （比如"禁止转载❗️商用"中间插了表情符号，完整短句匹配会漏掉）。
_COPYRIGHT_RESTRICTION_KEYWORDS: tuple[str, ...] = (
    "禁止转载",
    "禁止商用",
    "禁止二次",
    "未经许可",
    "未经授权",
    "未经本人同意",
    "不得转载",
    "不得商用",
    "不得二次",
    "谢绝转载",
    "抄袭必究",
    "侵权必究",
    "盗用必究",
    "搬运必究",
    "原创勿搬",
    "原创禁止",
    "请勿商用",
    "请勿转载",
    "请勿二次",
    "不得用于商业",
    "不可商用",
    "仅供学习",
    "仅供交流",
    "仅供参考学习",
    "非商业用途",
    "all rights reserved",
    "no reproduction",
    "do not repost",
)


def contains_copyright_restriction(text: str) -> str | None:
    """
    检查文本是否包含版权限制声明关键词。

    Returns:
        命中的关键词（第一个匹配到的），没有命中返回 None。
        返回具体关键词而不是单纯的 bool，方便日志记录"因为命中了哪个词被拦截"，
        运营方复核时能一眼看出拦截理由，不用重新读一遍原文找。
    """
    if not text:
        return None
    normalized = text.lower()
    for keyword in _COPYRIGHT_RESTRICTION_KEYWORDS:
        if keyword.lower() in normalized:
            return keyword
    return None


def strip_emoji_and_punctuation(text: str) -> str:
    """
    去掉文本里的 emoji 和常见分隔符号，用于提高关键词匹配的鲁棒性。

    原因：实际抓取到的文案经常出现"禁止转载❗️❗️商用"这种关键词被表情符号/感叹号
    打断的情况，直接 contains 匹配会漏掉。调用方可以先用这个函数清洗一遍文本，
    再传入 contains_copyright_restriction，提高命中率。

    不在 contains_copyright_restriction 内部自动调用这个清洗，是因为清洗本身有
    性能开销（正则扫描全文），由调用方按需决定是否需要更鲁棒的匹配。
    """
    # 去掉 emoji（基本的 emoji Unicode 区段）和常见的强调符号
    no_emoji = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF❗️❕‼️⚠️]+",
        "",
        text,
    )
    return re.sub(r"[\s,，。.!！]+", "", no_emoji)
