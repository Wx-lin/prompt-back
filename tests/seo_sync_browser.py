"""Read-only local UI acceptance: intercept all SEO APIs; no production writes."""

from playwright.sync_api import expect, sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for width in [1440, 390]:
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page.add_init_script("sessionStorage.setItem('admin_key','local-test')")
        state = {
            "configured": False,
            "status": "not_configured",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "data_through": None,
            "truncated": False,
        }
        calls = []

        def handle(route, state=state, calls=calls):
            url = route.request.url
            if url.endswith("/sync/google"):
                calls.append(route.request.method)
                state.update(status="running", last_attempt_at="2026-10-27T01:00:00Z")
                route.fulfill(status=202, json={"accepted": True})
            elif url.endswith("/sync/google/status"):
                state.update(
                    status="success",
                    last_success_at="2026-10-27T01:00:01Z",
                    data_through="2026-10-24",
                    truncated=True,
                )
                route.fulfill(json=state)
            else:
                engine = "baidu" if "engine=baidu" in url else "google"
                route.fulfill(
                    json={
                        "engine": engine,
                        "start_date": "2026-09-28",
                        "end_date": "2026-10-26",
                        "connection_status": "connected"
                        if state["status"] == "success" and engine == "google"
                        else "not_connected",
                        "updated_at": None,
                        "sync": state,
                        "totals": None,
                        "daily": [],
                        "queries": [],
                        "pages": [],
                        "coverage": {
                            key: {"status": "missing", "start_date": None, "end_date": None, "row_count": 0}
                            for key in ["daily", "query", "page"]
                        },
                    }
                )

        page.route("**/api/admin/seo**", handle)
        page.goto("http://127.0.0.1:5198/seo")
        expect(page.get_by_role("heading", name="数据同步 · 未配置自动同步")).to_be_visible()
        expect(page.get_by_role("button", name="立即同步 Google")).to_be_disabled()
        state.update(configured=True, status="failed", last_error="permission_denied")
        page.get_by_role("button", name="刷新数据").click()
        expect(page.get_by_role("heading", name="数据同步 · 最近同步失败")).to_be_visible()
        expect(page.get_by_role("alert")).to_contain_text("Google 账号无权访问该站点")
        page.get_by_role("button", name="立即同步 Google").click()
        expect(page.get_by_text("Google 同步完成，已刷新搜索报表。")).to_be_visible(timeout=10000)
        expect(page.get_by_role("heading", name="数据同步 · 最近同步成功")).to_be_visible()
        expect(page.get_by_text("2026-10-24（Google 报表日期）")).to_be_visible()
        expect(page.get_by_text("平台返回的关键词或页面行数达到上限", exact=False)).to_be_visible()
        assert calls == ["POST"], calls
        expect(page.locator(".insights-metric").first.locator("strong")).to_have_text("—")
        expect(page.get_by_text("每日数据未完整覆盖所选范围").first).to_be_visible()
        state.update(mode="external", trigger_available=False)
        page.get_by_role("button", name="刷新数据").click()
        expect(page.get_by_text("由 GitHub Actions 每日同步", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="立即同步 Google")).to_have_count(0)
        workflow = page.get_by_role("link", name="打开 GitHub Actions 手动运行同步")
        expect(workflow).to_have_attribute(
            "href", "https://github.com/Wx-lin/prompt-back/actions/workflows/seo-sync.yml"
        )
        expect(workflow).to_have_attribute("target", "_blank")
        assert calls == ["POST"], calls
        page.get_by_label("搜索引擎", exact=True).select_option("baidu")
        expect(page.get_by_role("heading", name="数据同步 · 仅人工导入")).to_be_visible()
        expect(page.get_by_role("button", name="立即同步 Google")).to_have_count(0)
        page.get_by_label("CSV 文件").set_input_files(
            {
                "name": "dates.csv",
                "mimeType": "text/csv",
                "buffer": b"Date,Clicks,Impressions\n2026-10-24,0,0",
            }
        )
        expect(page.get_by_text("待导入：", exact=False)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal overflow"
        page.close()
    browser.close()
print(
    "PASS: desktop/mobile Google unconfigured/failure/retry/success, POST once, poll status, "
    "truthful metrics/coverage, external workflow without POST, Baidu manual, CSV preview"
)
