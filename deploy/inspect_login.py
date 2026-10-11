import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://127.0.0.1:9222')
        context = browser.contexts[0]
        page = await context.new_page()
        await page.goto('https://www.xiaohongshu.com', wait_until='domcontentloaded', timeout=60000)
        await page.wait_for_timeout(3000)
        qr = page.locator('img.qrcode-img')
        if not await qr.count():
            button = page.get_by_role('button', name='登录', exact=True)
            if await button.count():
                await button.first.click()
                await page.wait_for_timeout(2000)
        await page.screenshot(path='/opt/MediaCrawler/.chrome_debug_profile/login-page.png', full_page=False)
        if await qr.count():
            await qr.first.screenshot(path='/opt/MediaCrawler/.chrome_debug_profile/login_qrcode.png')
            print('QR_READY')
        print('Page title:', await page.title())
        await browser.close()
asyncio.run(main())
