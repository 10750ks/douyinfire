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


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=120000)
        print("请在打开的浏览器里登录抖音网页版私信。")
        print("确认能看到私信列表后，回到这里按 Enter 保存登录态。")
        input()
        context.storage_state(path=str(OUTPUT))
        print(f"已保存: {OUTPUT.resolve()}")
        browser.close()


if __name__ == "__main__":
    main()
