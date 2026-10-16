"""Canonical model labels retain versions; unknown models are never discarded."""

import re

MODEL_FAMILIES = (
    "Midjourney",
    "Stable Diffusion",
    "FLUX",
    "DALL·E",
    "即梦",
    "可灵",
    "GPT Image",
    "Nano Banana",
    "豆包",
    "通义万相",
)
# Prefixes must end at a boundary so e.g. 'SD' never matches an unrelated word.
_ALIASES = (
    (r"midjourney|mj", "Midjourney"),
    (r"stable[\s_-]*diffusion|sd", "Stable Diffusion"),
    (r"flux", "FLUX"),
    (r"dall[\s·._-]*e", "DALL·E"),
    (r"即梦(?:ai)?|jimeng", "即梦"),
    (r"可灵(?:ai)?|kling", "可灵"),
    (r"gpt[\s_-]*image", "GPT Image"),
    (r"nano[\s_-]*banana", "Nano Banana"),
    (r"豆包|doubao", "豆包"),
    (r"通义万相|wanx", "通义万相"),
)


def normalize_model_label(label: str) -> str:
    value = " ".join(label.strip().split())
    value = re.sub(r"^sdxl(?=$|[\s._-]|\d)", "Stable Diffusion XL", value, flags=re.I)
    for aliases, family in _ALIASES:
        match = re.match(rf"^(?:{aliases})(?=$|[\s._-]|[vV]?\d)", value, re.I)
        if not match:
            continue
        suffix = value[match.end() :].strip(" ._-")
        suffix = re.sub(r"^v\s*(?=\d)", "V", suffix, flags=re.I)
        if family == "Midjourney" and re.match(r"^\d", suffix):
            suffix = "V" + suffix
        if family == "FLUX" and re.match(r"^\d", suffix):
            return family + "." + suffix.lower()
        if family == "Nano Banana":
            suffix = suffix.title()
        return family + (" " + suffix if suffix else "")
    return value


def normalize_model_labels(labels: list[str] | None) -> list[str]:
    """Normalize an array and deduplicate in source order without modifying it."""
    return list(dict.fromkeys(value for label in labels or [] if (value := normalize_model_label(label))))


def model_group(label: str) -> str:
    normalized = normalize_model_label(label)
    return next(
        (
            family
            for family in MODEL_FAMILIES
            if normalized == family
            or normalized.startswith(family + " ")
            or normalized.startswith(family + ".")
        ),
        "其他",
    )


def is_catalog_model(label: str) -> bool:
    """Only recognized model names/version forms enter public filters; retain raw data elsewhere."""
    value = normalize_model_label(label)
    families = (*MODEL_FAMILIES, "Gemini", "Grok", "Seedream", "GPT-4o")
    version = (
        r"(?:[ .-](?:V?\d+(?:\.\d+)*(?:Pro|Lite)?|XL|Pro|Lite|dev|schnell|Image)"
        r"(?:[ .-](?:\d+(?:\.\d+)*|dev|pro|lite|schnell|turbo|fast|omni|preview|ultra|max))*)?"
    )
    return any(re.fullmatch(re.escape(family) + version, value, re.I) for family in families)
