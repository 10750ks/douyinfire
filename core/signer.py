"""签名器 — 管理浏览器实例，提供 API 调用和页面操作"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

import os as _os

CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
WWW_CHAT_URL = "https://www.douyin.com/chat?isPopup=1"

def _find_chrome():
    """定位 Playwright 安装的 Chromium"""
    import glob
    base = _os.path.expanduser("~/AppData/Local/ms-playwright")
    if _os.path.exists(base):
        # 找最新版本的 chromium
        dirs = sorted(glob.glob(base + "/chromium-*"), reverse=True)
        for d in dirs:
            exe = d + "/chrome-win64/chrome.exe"
            if _os.path.exists(exe):
                return exe
    return None

CHROME_EXE = _find_chrome()


def _looks_logged_out(page):
    url = page.url or ""
    if "login" in url or "passport" in url:
        return True
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    login_words = ["登录", "扫码", "验证码", "请先登录"]
    page_words = ["私信管理", "朋友私信", "群消息", "互动管理"]
    return any(word in text for word in login_words) and not any(word in text for word in page_words)


def _looks_www_logged_out(page):
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    login_words = ["登录", "扫码", "验证码", "请输入手机号"]
    chat_words = ["发送消息", "分享[视频]", "火花"]
    return any(word in text for word in login_words) and not any(word in text for word in chat_words)


def _www_chat_status(page):
    try:
        return page.evaluate(
            """() => ({
                url: location.href,
                hasConvStore: !!window.conversationStore,
                convItems: document.querySelectorAll('div[class*="conversationConversationItemwrapper"]').length,
                hasUserStore: !!window.userInfoStore,
                userCount: window.userInfoStore?.usersInfoMap?.data_?.size || 0,
                text: (document.body && document.body.innerText || '').slice(0, 300)
            })"""
        )
    except Exception as e:
        return {"error": str(e), "url": page.url or "", "text": ""}


def _is_www_chat_ready(status):
    return bool(
        status.get("hasConvStore")
        and (status.get("convItems", 0) > 0 or status.get("userCount", 0) > 0)
    )


def _wait_www_chat_ready(page, max_seconds=90):
    last = {}
    login_markers = ["扫码登录", "请输入手机号", "验证码登录", "密码登录", "登录后免费畅享"]
    for second in range(max_seconds):
        last = _www_chat_status(page)
        if _is_www_chat_ready(last):
            print(
                "网页版登录态检查通过: "
                f"convItems={last.get('convItems')}, userCount={last.get('userCount')}, wait={second}s"
            )
            return True, last
        text = last.get("text", "")
        if any(word in text for word in login_markers) and second >= 10:
            # 仍然多等一会儿，避免 GitHub 云端首屏文字先出来、数据稍后才挂载导致误判。
            pass
        page.wait_for_timeout(1000)
    return False, last


def _dump_web_state_failure(page, status):
    Path("logs").mkdir(exist_ok=True)
    text_path = Path("logs") / "signin-web-state-failed.txt"
    png_path = Path("logs") / "signin-web-state-failed.png"
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception as e:
        body_text = f"读取页面文本失败: {e}"
    text_path.write_text(
        "网页版登录态检查失败\n"
        f"url={status.get('url')}\n"
        f"hasConvStore={status.get('hasConvStore')}\n"
        f"convItems={status.get('convItems')}\n"
        f"hasUserStore={status.get('hasUserStore')}\n"
        f"userCount={status.get('userCount')}\n"
        f"error={status.get('error')}\n\n"
        f"{body_text[:12000]}",
        encoding="utf-8",
    )
    try:
        page.screenshot(path=str(png_path), full_page=True)
    except Exception as e:
        text_path.write_text(text_path.read_text(encoding="utf-8") + f"\n\n截图失败: {e}", encoding="utf-8")
    print(f"已保存网页版登录态调试文件: {text_path} / {png_path}")


class Signer:
    def __init__(self, cookies, storage_state=None):
        for c in cookies:
            if "sameSite" in c:
                del c["sameSite"]
        self._pw = sync_playwright().start()
        # 优先用 Playwright 默认启动，失败则用自定义路径
        try:
            self._browser = self._pw.chromium.launch(headless=True)
        except Exception:
            if CHROME_EXE:
                self._browser = self._pw.chromium.launch(
                    headless=True, executable_path=CHROME_EXE
                )
            else:
                raise
        context_args = {}
        if storage_state:
            context_args["storage_state"] = storage_state
        ctx = self._browser.new_context(**context_args)
        ctx.set_default_navigation_timeout(120000)
        ctx.set_default_timeout(120000)
        page = ctx.new_page()
        if cookies:
            ctx.add_cookies(cookies)
        start_url = WWW_CHAT_URL if storage_state else CREATOR_CHAT_URL
        page.goto(start_url, timeout=60000)
        if storage_state:
            ok, status = _wait_www_chat_ready(page, max_seconds=90)
            if not ok:
                _dump_web_state_failure(page, status)
                raise RuntimeError(
                    "网页版登录态可能已失效或云端加载超时。请先下载 run-logs 查看 "
                    "logs/signin-web-state-failed.png 和 logs/signin-web-state-failed.txt；"
                    "如果截图是登录/验证页面，再重新运行 export_web_state.py 导出 web_storage_state.json。"
                )
        else:
            page.wait_for_timeout(5000)
        if not storage_state and _looks_logged_out(page):
            raise RuntimeError("Cookie 可能已失效，请重新导出 CONFIG/Cookie 后更新 GitHub Secret。")
        self.page = page

    def api_fetch(self, method, path, params=None, body=None):
        """通过浏览器内 fetch() 调用 API。仅用于不需要 a_bogus 的简单端点"""
        import urllib.parse
        url = "https://creator.douyin.com" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        arg = {"method": method, "url": url, "body": body}
        js = """
        async (arg) => {
            const o = {method: arg.method, headers: {'Accept':'application/json'}, credentials: 'include'};
            if (arg.body) { o.body = JSON.stringify(arg.body); o.headers['Content-Type'] = 'application/json'; }
            const r = await fetch(arg.url, o);
            const t = await r.text();
            try { return JSON.parse(t); }
            catch (e) { return {_status: r.status, _text: t.substring(0,300)}; }
        }
        """
        return self.page.evaluate(js, arg)

    def close(self):
        self._browser.close()
        self._pw.stop()
