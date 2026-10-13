import pytest

from app.utils.model_labels import model_group, normalize_model_labels


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MJ V6", "Midjourney V6"),
        ("midjourney v7", "Midjourney V7"),
        ("Flux.1", "FLUX.1"),
        ("flux 1 dev", "FLUX.1 dev"),
        ("SDXL", "Stable Diffusion XL"),
        ("sd 1.5", "Stable Diffusion 1.5"),
        ("DALL-E 3", "DALL·E 3"),
        ("gpt-image-1", "GPT Image 1"),
        ("nano banana pro", "Nano Banana Pro"),
        ("Gemini 2.5", "Gemini 2.5"),
        ("即梦 3.0", "即梦 3.0"),
        ("kling 2.1", "可灵 2.1"),
        (" custom  Model ", "custom Model"),
    ],
)
def test_versions_and_unknowns_are_preserved(raw, expected):
    assert normalize_model_labels([raw]) == [expected]


def test_deduplicates_without_collapsing_versions_or_mutating_input():
    labels = ["MJ V6", "Midjourney v6", "MJ V7", "", "  ", "GPT Image 1", "DALL-E 3"]
    assert normalize_model_labels(labels) == ["Midjourney V6", "Midjourney V7", "GPT Image 1", "DALL·E 3"]
    assert labels[0] == "MJ V6"
    assert model_group("MJ V6") == "Midjourney"
    assert model_group("Gemini 2.5") == "其他"
