"""CLI preview is offline, writes private audit files and cannot imply approval."""

import json
import subprocess
import sys
import uuid


def test_preview_is_offline_and_private(tmp_path):
    record_id = str(uuid.uuid4())
    snapshot = tmp_path / "snapshot.json"
    review = tmp_path / "review.json"
    output = tmp_path / "preview.json"
    snapshot.write_text(
        json.dumps(
            {
                "prompts": [
                    {
                        "id": record_id,
                        "title": "test",
                        "prompt_text": "test",
                        "category": "workflow",
                        "is_published": True,
                    }
                ],
                "prompt_images": [],
                "crawl_drafts": [],
                "crawl_draft_images": [],
            }
        )
    )
    review.write_text(
        json.dumps(
            [
                {
                    "entity": "prompt",
                    "id": record_id,
                    "old_category": "workflow",
                    "new_category": "poster",
                    "confirmed": False,
                }
            ]
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.category_migration",
            "preview",
            "--snapshot",
            str(snapshot),
            "--review",
            str(review),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    saved = json.loads(output.read_text())
    assert saved["summary"]["applicable"] == 0
    assert saved["summary"]["unresolved_public"] == 1
    assert output.stat().st_mode & 0o077 == 0
    repeat = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.category_migration",
            "preview",
            "--snapshot",
            str(snapshot),
            "--review",
            str(review),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert repeat.returncode != 0


def test_apply_requires_explicit_target(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.category_migration",
            "apply",
            "--snapshot",
            "x",
            "--review",
            "y",
            "--batch-id",
            "test",
            "--output",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "--expected-target" in result.stderr
