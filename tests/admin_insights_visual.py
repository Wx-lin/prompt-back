"""Dense and empty-state chart rendering checks; all displayed preview values are test data."""

import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).parents[2] / "artifacts/admin-insights-2026-10-16"


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.add_init_script("sessionStorage.setItem('admin_key','test-insights-browser')")
        data = page.request.get(
            "http://127.0.0.1:5180/api/admin/dashboard", headers={"X-Admin-Key": "test-insights-browser"}
        ).json()
        counts = [690, 285, 1760, 510, 438, 220, 138, 190, 85, 73, 62, 146, 18]
        data["categories"] = [
            {**row, "total": count, "published": int(count * 0.91)}
            for row, count in zip(data["categories"], counts, strict=True)
        ]
        total = sum(row["total"] for row in data["categories"])
        published = sum(row["published"] for row in data["categories"])
        data["content"] = dict(
            total=total,
            published=published,
            pending=132,
            rejected=49,
            other_unpublished=total - published - 132 - 49,
            images=6350,
        )
        data["crawl"] = dict(total=1280, pending=286)
        page.route("**/api/admin/dashboard", lambda route: route.fulfill(json=data))
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:5180/dashboard")
        expect(page.get_by_role("img", name="作品状态环形图")).to_be_visible()
        expect(page.locator(".category-chart svg")).to_have_count(1)
        # Finish finite chart intro animations before exporting the preview.
        page.wait_for_timeout(650)
        page.screenshot(path=str(OUT / "dashboard-echarts.png"), full_page=True)
        page.get_by_role("button", name="刷新数据").click()
        expect(page.locator(".category-chart svg")).to_have_count(1)
        page.get_by_role("link", name="用户管理", exact=True).click()
        expect(page.locator("tbody tr")).to_have_count(20)
        page.screenshot(path=str(OUT / "users-echarts-style.png"), full_page=False)
        page.get_by_role("link", name="数据大盘", exact=True).click()
        expect(page.locator(".category-chart svg")).to_have_count(1)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator(".category-chart svg")).to_have_count(1)
        page.screenshot(path=str(OUT / "dashboard-echarts-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
            "Page overflows mobile viewport"
        )
        zero = {
            **data,
            "content": dict(total=0, published=0, pending=0, rejected=0, other_unpublished=0, images=0),
            "categories": [{**row, "total": 0, "published": 0} for row in data["categories"]],
        }
        page.unroute("**/api/admin/dashboard")
        page.route("**/api/admin/dashboard", lambda route: route.fulfill(json=zero))
        page.get_by_role("button", name="刷新数据").click()
        expect(page.get_by_text("暂无作品", exact=True)).to_be_visible()
        expect(page.locator(".donut-center strong")).to_have_text("0%")
        expect(page.locator(".category-chart svg")).to_have_count(1)
        assert not errors, errors
        browser.close()
    (OUT / "echarts-checks.json").write_text(
        json.dumps(
            {
                "passed": True,
                "preview_uses_test_data": True,
                "checks": [
                    "bar and donut SVG render",
                    "refresh and route remount",
                    "mobile resize without page overflow",
                    "empty inventory stays 0 percent",
                    "no browser runtime exceptions",
                ],
            },
            indent=2,
        )
    )
    print("ECharts visual, resize, lifecycle and zero-data checks passed")


if __name__ == "__main__":
    run()
