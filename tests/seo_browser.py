"""Local-only UI acceptance against disposable PostgreSQL API fixture on 8000."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

out = Path(__file__).parents[2] / "artifacts/seo-2026-10-16"
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("sessionStorage.setItem('admin_key','test-seo-browser')")
    page.goto("http://127.0.0.1:5187/seo")
    expect(page.get_by_role("heading", name="SEO 搜索表现")).to_be_visible()
    expect(page.get_by_text("待接入：", exact=False)).to_be_visible()
    page.get_by_label("开始日期", exact=True).fill("2026-10-12")
    page.get_by_label("结束日期", exact=True).fill("2026-10-14")
    expect(page.get_by_text("待接入：", exact=False)).to_be_visible()
    page.get_by_label("CSV 文件").set_input_files(
        {
            "name": "dates.csv",
            "mimeType": "text/csv",
            "buffer": b"Date,Clicks,Impressions,Position\n2026-10-12,0,0,\n2026-10-14,4,100,5\n",
        }
    )
    expect(page.get_by_text("待导入：", exact=False)).to_be_visible()
    page.get_by_role("button", name="确认导入", exact=True).click()
    expect(page.get_by_text("数据来源：人工导入", exact=False)).to_be_visible()
    expect(page.locator(".insights-metric").nth(0).locator("strong")).to_have_text("100")
    expect(page.locator(".insights-metric").nth(1).locator("strong")).to_have_text("4")
    expect(page.locator(".insights-metric").nth(2).locator("strong")).to_have_text("4.00%")
    expect(page.locator(".seo-chart svg")).to_have_count(1)
    expect(page.get_by_text("每日数据未完整覆盖所选范围").first).to_be_visible()
    page.get_by_label("报表维度").select_option("query")
    page.get_by_label("CSV 文件").set_input_files(
        {
            "name": "queries.csv",
            "mimeType": "text/csv",
            "buffer": "热门查询,点击次数,展示次数,平均排名\nAI 提示词,2,50,6\n".encode(),
        }
    )
    page.get_by_role("button", name="确认导入", exact=True).click()
    expect(page.locator(".seo-table").first).to_contain_text("AI 提示词")
    expect(page.locator(".insights-metric").nth(1).locator("strong")).to_have_text("4")
    page.screenshot(path=str(out / "admin-seo-desktop.png"), full_page=True)
    page.get_by_label("搜索引擎", exact=True).select_option("baidu")
    expect(page.get_by_text("待接入：", exact=False)).to_be_visible()
    expect(page.locator(".insights-metric").nth(0).locator("strong")).to_have_text("—")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(out / "admin-seo-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "horizontal overflow"
    page.route("**/api/admin/seo?**", lambda route: route.fulfill(status=503, json={"detail": "测试错误"}))
    page.get_by_role("button", name="刷新数据").click()
    expect(page.get_by_role("alert")).to_contain_text("测试错误")
    expect(page.locator(".insights-metrics")).to_have_count(0)
    assert not errors, errors
    browser.close()
print(
    "PASS: CSV import, DB report, missing-day coverage, chart, independent query totals, "
    "engine switch, mobile, failure state"
)
