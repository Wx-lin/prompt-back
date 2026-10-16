"""Run against local admin Vite5180 + the disposable insights API fixture8000."""

import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ARTIFACTS = Path(__file__).parents[2] / "artifacts/admin-insights-2026-10-16"


def run():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    checks = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.add_init_script("sessionStorage.setItem('admin_key','test-insights-browser')")
        page.goto("http://127.0.0.1:5180/")
        expect(page).to_have_url("http://127.0.0.1:5180/dashboard")
        expect(page.get_by_role("heading", name=re.compile("^数据大盘"))).to_be_visible()
        expect(page.locator(".insights-metrics .insights-metric").first.locator("strong")).to_have_text("26")
        page.locator(".category-details summary").click()
        expect(page.locator(".category-details")).to_have_attribute("open", "")
        expect(page.locator(".category-chart svg")).to_have_count(1)
        expect(page.locator(".status-chart svg")).to_have_count(1)
        expect(page.get_by_role("row").filter(has_text="人像写真")).to_contain_text("2")
        expect(page.get_by_role("row").filter(has_text="其他/历史分类")).to_contain_text("1")
        expect(page.locator("tbody tr")).to_have_count(13)
        page.locator(".category-details summary").click()
        page.screenshot(path=str(ARTIFACTS / "dashboard-desktop.png"), full_page=True)
        checks.append("dashboard real API totals, 12 categories plus historical bucket")
        page.get_by_role("link", name="用户管理", exact=True).click()
        expect(page.get_by_role("heading", name="用户管理", exact=True)).to_be_visible()
        expect(page.locator(".insights-list-summary")).to_contain_text("筛选结果 26 位 · 全站用户 26 位")
        expect(page.locator("tbody tr")).to_have_count(20)
        page.get_by_role("button", name="下一页", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(6)
        expect(page.locator("tbody")).to_contain_text("未填写")
        page.get_by_role("textbox", name="昵称 / 邮箱").fill("operator03")
        page.get_by_role("button", name="搜索", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(1)
        expect(page.locator("tbody")).to_contain_text("operator03@example.com")
        page.get_by_role("combobox", name="账号状态").select_option("true")
        expect(page.get_by_text("没有符合条件的用户", exact=True)).to_be_visible()
        page.get_by_role("button", name="重置筛选", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(20)
        page.get_by_role("textbox", name="昵称 / 邮箱").fill("%_")
        page.get_by_role("button", name="搜索", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(1)
        expect(page.locator("tbody")).to_contain_text("literal%_@example.com")
        page.get_by_role("button", name="重置筛选", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(20)
        page.screenshot(path=str(ARTIFACTS / "users-desktop.png"), full_page=True)
        checks.append("user pagination/search/filter/reset/literal wildcards/null fields")
        page.route(
            "**/api/admin/users?**",
            lambda route: route.fulfill(status=503, json={"detail": "模拟服务不可用"}),
        )
        page.get_by_role("button", name="刷新列表", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("模拟服务不可用")
        expect(page.get_by_text("没有符合条件的用户", exact=True)).to_have_count(0)
        page.unroute("**/api/admin/users?**")
        page.get_by_role("button", name="重试", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(20)
        checks.append("503 shows error, never fake zero; retry recovers")
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("link", name=re.compile("^数据大盘")).click()
        expect(page.get_by_role("heading", name=re.compile("^数据大盘"))).to_be_visible()
        expect(page.locator("tbody tr")).to_have_count(13)
        page.screenshot(path=str(ARTIFACTS / "dashboard-mobile.png"), full_page=True)
        checks.append("mobile dashboard renders")
        page.route(
            "**/api/admin/dashboard", lambda route: route.fulfill(status=401, json={"detail": "需要重新登录"})
        )
        page.get_by_role("button", name="刷新数据", exact=True).click()
        expect(page.get_by_role("heading", name="AIGC Prompt", exact=True)).to_be_visible()
        checks.append("401 returns to login")
        browser.close()
    (ARTIFACTS / "browser-checks.json").write_text(
        json.dumps({"passed": True, "checks": checks}, ensure_ascii=False, indent=2)
    )
    print(json.dumps({"passed": True, "checks": checks}, ensure_ascii=False))


if __name__ == "__main__":
    run()
