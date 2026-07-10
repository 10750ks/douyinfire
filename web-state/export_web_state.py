"""导出 www.douyin.com/chat 完整网页登录态。

用法：
1. 本地运行：python export_web_state.py
2. 浏览器打开后扫码/验证码登录 www.douyin.com/chat。
3. 登录成功并能看到私信列表后，回到终端按 Enter。
4. 脚本会生成 web_storage_state.json。
"""
from pathlib import Path

from playwright.sync_api import sync_playwright


CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
OUTPUT = Path("web_storage_state.json")


def chat_status(page):
    return page.evaluate(
        """() => ({
            hasConvStore: !!window.conversationStore,
            convItems: document.querySelectorAll('div[class*="conversationConversationItemwrapper"]').length,
            hasUserStore: !!window.userInfoStore,
            userCount: window.userInfoStore?.usersInfoMap?.data_?.size || 0,
            text: (document.body && document.body.innerText || '').slice(0, 300)
        })"""
    )


def wait_chat_ready(page, max_seconds=90):
    last = {}
    for _ in range(max_seconds):
        last = chat_status(page)
        if last.get("hasConvStore") and (last.get("convItems", 0) > 0 or last.get("userCount", 0) > 0):
            return True, last
        page.wait_for_timeout(1000)
    return False, last


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=120000)
        print("请在打开的浏览器里登录抖音网页版私信。")
        print("确认能看到私信列表后，回到这里按 Enter 保存登录态。")
        input()
        ok, status = wait_chat_ready(page)
        if not ok:
            page.screenshot(path="web_state_export_failed.png", full_page=True)
            print("未检测到私信列表，暂不保存 web_storage_state.json。")
            print("请确认浏览器里不是登录/验证页面，并能看到左侧私信列表。")
            print("已保存截图: web_state_export_failed.png")
            print(status)
            browser.close()
            raise SystemExit(1)
        context.storage_state(path=str(OUTPUT))
        print(f"已保存: {OUTPUT.resolve()}")
        print(f"检测结果: convItems={status.get('convItems')}, userCount={status.get('userCount')}")
        browser.close()


if __name__ == "__main__":
    main()
