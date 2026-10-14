"""Canonical categories: finished-image purpose precedes style and subject."""

from typing import Annotated

from pydantic import AfterValidator

_ROWS = (
    ("portrait", "人像写真", "Portraits", "职业照、个人写真、情侣照、时尚人像；明确头像用途归头像"),
    ("avatar", "头像", "Avatars", "真人、动漫、宠物、情侣等社交头像"),
    ("poster", "海报与封面", "Posters & Covers", "活动海报、社媒封面、文章配图；知识讲解图归信息图解"),
    ("product", "商品与电商", "Products & E-commerce", "商品主图、展示、产品场景及广告素材；包装设计归品牌"),
    ("illustration", "插画与绘本", "Illustrations & Storybooks", "故事、绘本、艺术插画；头像或海报用途优先"),
    ("character", "角色与 IP", "Characters & IP", "角色设定、吉祥物、三视图及形象设计；普通写真归人像"),
    ("sticker", "表情包与贴纸", "Memes & Stickers", "反应图、聊天表情及贴纸套图"),
    ("wallpaper", "壁纸与背景", "Wallpapers & Backgrounds", "手机、桌面壁纸及会议背景；商品背景合成归商品"),
    ("infographic", "信息图解", "Infographics", "知识卡片、流程图、科普与教学图；活动宣传归海报"),
    ("branding", "Logo 与品牌", "Logos & Branding", "Logo、包装、品牌视觉"),
    ("architecture", "空间与建筑", "Spaces & Architecture", "室内设计、建筑与景观设计效果"),
    ("scene", "风景与静物", "Scenery & Still Life", "无前述明确用途的自然风景、美食、植物、动物及静物创作"),
)
CATEGORY_CATALOG = tuple(
    dict(key=k, label=zh, label_en=en, order=i, boundary=b) for i, (k, zh, en, b) in enumerate(_ROWS, 1)
)
CATEGORY_LABELS = {row["key"]: row["label"] for row in CATEGORY_CATALOG}
LEGACY_CATEGORY_KEYS = frozenset({"photography", "image_edit", "workflow"})


def validate_category(value: str) -> str:
    if value not in CATEGORY_LABELS:
        raise ValueError("请选择有效分类")
    return value


CategoryKey = Annotated[str, AfterValidator(validate_category)]
