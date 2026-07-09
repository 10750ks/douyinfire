"""测试 web_storage_state.json 是否能登录 www.douyin.com/chat。

只检查登录态，不发送消息。
"""
from pathlib import Path

from playwright.sync_api import sync_playwright


CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
STATE = Path("web_storage_state.json")


def main():
    if not STATE.exists():
        raise SystemExit("未找到 web_storage_state.json，请先运行 python export_web_state.py")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE), viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=120000)
        info = {}
        for _ in range(75):
            info = page.evaluate(
                """() => ({
                    url: location.href,
                    title: document.title,
                    hasConvStore: !!window.conversationStore,
                    convItems: document.querySelectorAll('div[class*="conversationConversationItemwrapper"]').length,
                    hasUserStore: !!window.userInfoStore,
                    userCount: window.userInfoStore?.usersInfoMap?.data_?.size || 0,
                    text: (document.body && document.body.innerText || '').slice(0, 300)
                })"""
            )
            if info.get("hasConvStore") and (info.get("convItems", 0) > 0 or info.get("userCount", 0) > 0):
                break
            page.wait_for_timeout(1000)
        page.screenshot(path="web_state_test.png", full_page=True)
        browser.close()

    print(info)
    if "扫码登录" in info.get("text", "") or "请输入手机号" in info.get("text", ""):
        raise SystemExit("网页登录态测试失败：仍然是登录页")
    if info.get("hasConvStore") and (info.get("convItems", 0) > 0 or info.get("userCount", 0) > 0):
        print("网页登录态测试成功")
        return
    raise SystemExit("网页登录态状态不确定，请查看 web_state_test.png")


if __name__ == "__main__":
    main()
