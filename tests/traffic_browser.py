from playwright.sync_api import expect, sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    for _visitor in range(2):
        c = b.new_context()
        pg = c.new_page()
        pg.goto("http://localhost:3180/en")
        pg.wait_for_timeout(1500)
        for _i in range(2):
            r = pg.evaluate(
                """async()=>{let r=await fetch('/api/analytics/visit',{method:'POST'});return r.status}"""
            )
            assert r == 204, r
        cookies = c.cookies()
        assert any(x["name"] == "aigc_visitor" and x["httpOnly"] for x in cookies), cookies
        c.close()
    c = b.new_context()
    pg = c.new_page()
    pg.add_init_script("sessionStorage.setItem('admin_key','test-insights-browser')")
    pg.goto("http://127.0.0.1:5180/dashboard")
    panel = pg.get_by_role("region", name="网站访问统计")
    expect(panel.locator(".traffic-total strong")).to_have_text(["2", "2", "2"])
    expect(panel.locator(".traffic-chart svg")).to_have_count(1)
    panel.get_by_role("button", name="近30天", exact=True).click()
    expect(panel.get_by_role("button", name="近30天", exact=True)).to_have_attribute("aria-pressed", "true")
    pg.screenshot(
        path="/Users/wangxinlin/Desktop/aigc-prompt/artifacts/traffic-dashboard-2026-10-19.png",
        full_page=True,
    )
    print("PASS: anonymous cookie dedup, admin totals2/2/2, chart and range toggle")
    b.close()
