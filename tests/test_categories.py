"""
分类标签映射的单元测试：保证分类枚举值和展示名的对应关系符合 PRD 定义。
"""

from app.routers.categories import CATEGORY_LABELS


def test_category_labels_cover_all_prd_categories():
    """2026-10-09 分类调整定义了八个创作意图分类，这里校验 key 和展示名都对得上，防止手写字符串打错字。"""
    expected = {
        "portrait": "人像写真",
        "avatar": "头像 & IP",
        "poster": "海报 & 封面",
        "illustration": "插画 & 概念艺术",
        "photography": "模拟摄影",
        "character": "角色 & 人物设计",
        "image_edit": "图片编辑",
        "workflow": "工作流 & 技巧",
    }
    assert CATEGORY_LABELS == expected
