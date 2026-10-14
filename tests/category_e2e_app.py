"""Local browser fixture with actual SQL filtering in a disposable PostgreSQL schema."""

from contextlib import asynccontextmanager

from app.models import CategoryLegacyMembership, CrawlDraft, Prompt, PromptImage
from tests import submission_e2e_app as base


@asynccontextmanager
async def lifespan(app):
    async with base.lifespan(app):
        async with base._sessions() as db:
            wallpaper = Prompt(
                title="cat wallpaper",
                prompt_text="wallpaper",
                category="wallpaper",
                models=["FLUX"],
                is_published=True,
                images=[PromptImage(image_url="http://127.0.0.1:3027/demo/portrait.jpg")],
            )
            portrait = Prompt(
                title="legacy cat portrait",
                prompt_text="portrait",
                category="portrait",
                models=["FLUX"],
                is_published=True,
            )
            db.add_all([wallpaper, portrait])
            for project in ("desktop", "mobile"):
                db.add(
                    Prompt(
                        title=f"未迁移旧分类 {project}",
                        prompt_text="test",
                        category="workflow",
                        models=["FLUX"],
                        is_published=False,
                    )
                )
                db.add(
                    Prompt(
                        title=f"待审核投稿 {project}",
                        prompt_text="test",
                        category="portrait",
                        models=["FLUX"],
                        is_published=False,
                        review_status="pending",
                        submitter_id=base._user.id,
                    )
                )
                db.add(
                    CrawlDraft(
                        source_platform="xiaohongshu",
                        source_note_id=f"category-e2e-{project}",
                        source_url="https://example.com/test",
                        raw_title=f"未迁移旧草稿 {project}",
                        raw_content="test",
                        extracted_category="image_edit",
                        status="pending",
                    )
                )
            await db.flush()
            db.add(CategoryLegacyMembership(legacy_key="photography", prompt_id=portrait.id))
            await db.commit()
        yield


app = base.app
app.router.lifespan_context = lifespan
