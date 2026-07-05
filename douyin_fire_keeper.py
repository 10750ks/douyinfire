import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        example = ROOT / "config.example.json"
        raise SystemExit(
            f"找不到配置文件: {path}\n"
            f"请先复制 {example.name} 为 config.json，然后把 targets 改成你的好友昵称。"
        )
    config = load_json(path, {})
    if not config.get("targets"):
        raise SystemExit("config.json 里没有 targets，请至少配置一个好友昵称。")
    return config


def resolve_path(config_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return config_path.parent / path


def today_key() -> str:
    return date.today().isoformat()


def already_sent(state: Dict[str, Any], target_name: str) -> bool:
    return state.get("sent", {}).get(target_name) == today_key()


def mark_sent(state_path: Path, state: Dict[str, Any], target_name: str) -> None:
    state.setdefault("sent", {})[target_name] = today_key()
    state.setdefault("history", []).append(
        {
            "target": target_name,
            "date": today_key(),
            "time": datetime.now().isoformat(timespec="seconds"),
        }
    )
    save_json(state_path, state)


def pick_message(config: Dict[str, Any], target: Dict[str, Any]) -> str:
    messages = target.get("message_templates") or config.get("message_templates") or ["续火"]
    return random.choice([m for m in messages if str(m).strip()]).strip()


def sanitize_cookie(cookie: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(cookie)

    if "expirationDate" in cleaned and "expires" not in cleaned:
        cleaned["expires"] = cleaned["expirationDate"]

    same_site = cleaned.get("sameSite")
    if isinstance(same_site, str):
        normalized = same_site.strip().lower().replace("-", "_").replace(" ", "_")
        mapping = {
            "no_restriction": "None",
            "unspecified": None,
            "lax": "Lax",
            "strict": "Strict",
            "none": "None",
        }
        cleaned["sameSite"] = mapping.get(normalized, same_site)
        if cleaned["sameSite"] not in {"Strict", "Lax", "None"}:
            cleaned.pop("sameSite", None)
    else:
        cleaned.pop("sameSite", None)

    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    for key in list(cleaned):
        if key not in allowed:
            cleaned.pop(key, None)

    cleaned.setdefault("path", "/")
    return cleaned


def parse_cookie_data(data: Any, source: str) -> List[Dict[str, Any]]:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{source} 不是合法 JSON: {exc}") from exc

    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if not isinstance(data, list):
        raise SystemExit(f"{source} 应该是 Cookie 数组，或包含 cookies 数组的对象。")

    cookies = [sanitize_cookie(cookie) for cookie in data if cookie.get("name") and cookie.get("value")]
    if not cookies:
        raise SystemExit(f"{source} 中没有可用 Cookie。")
    return cookies


def load_cloud_cookies(config_path: Path, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = os.getenv("DOUYIN_COOKIES_JSON", "").strip()
    if raw:
        return parse_cookie_data(raw, "DOUYIN_COOKIES_JSON")

    if config.get("cookies"):
        return parse_cookie_data(config["cookies"], "config.json 的 cookies 字段")

    cookie_file = config.get("cookies_file", "cookies.json")
    cookie_path = resolve_path(config_path, str(cookie_file))
    if cookie_path.exists():
        return parse_cookie_data(load_json(cookie_path, []), str(cookie_path))

    if os.getenv("GITHUB_ACTIONS") == "true":
        raise SystemExit(
            "云端运行没有找到 Cookie。请把 Cookie JSON 放到 config.json 的 cookies 字段，"
            "或上传同目录 cookies.json。"
        )

    return []


def wait_for_manual_login(page: Page, chat_url: str, wait_seconds: int = 0) -> None:
    page.goto(chat_url, wait_until="domcontentloaded")
    logging.info("浏览器已打开。请在页面里扫码/确认登录。")
    if wait_seconds > 0:
        logging.info("登录并能看到聊天列表即可，浏览器会在 %s 秒后自动关闭。", wait_seconds)
        time.sleep(wait_seconds)
        return
    logging.info("登录并能看到聊天列表后，回到终端按 Enter 结束。")
    input()


def nearest_clickable(locator: Locator) -> Locator:
    return locator.locator(
        "xpath=ancestor-or-self::*[self::button or self::a or @role='button' or contains(@class, 'item') or contains(@class, 'list')][1]"
    )


def click_if_visible(locator: Locator, timeout_ms: int = 1500) -> bool:
    try:
        if locator.count() == 0:
            return False
        first = locator.first
        first.wait_for(state="visible", timeout=timeout_ms)
        first.click(timeout=timeout_ms)
        return True
    except Exception:
        return False


def scroll_chat_list(page: Page, pixels: int = 700) -> bool:
    return bool(
        page.evaluate(
            """(pixels) => {
                const candidates = Array.from(document.querySelectorAll('*'))
                  .map(el => {
                    const rect = el.getBoundingClientRect();
                    return {
                      el,
                      rect,
                      score: rect.height * rect.width,
                      scrollable: el.scrollHeight > el.clientHeight + 20
                    };
                  })
                  .filter(x =>
                    x.scrollable &&
                    x.rect.height > 180 &&
                    x.rect.width > 120 &&
                    x.rect.left < window.innerWidth * 0.75
                  )
                  .sort((a, b) => b.score - a.score);

                const target = candidates[0]?.el;
                if (!target) return false;
                const before = target.scrollTop;
                target.scrollTop = before + pixels;
                return target.scrollTop !== before;
            }""",
            pixels,
        )
    )


def normalize_name(value: str) -> str:
    return "".join(str(value).split())


def click_target_by_dom_text(page: Page, target_name: str) -> bool:
    """Find visible text in the current creator-center list and click its row by coordinates."""
    result = page.evaluate(
        """(targetName) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
            const target = normalize(targetName);
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none' &&
                    rect.bottom >= 0 && rect.top <= window.innerHeight;
            };

            const matches = Array.from(document.querySelectorAll('body *'))
                .filter(visible)
                .map((el) => {
                    const text = normalize(el.innerText || el.textContent);
                    return { el, text, rect: el.getBoundingClientRect() };
                })
                .filter((item) => item.text && item.text.includes(target))
                .sort((a, b) => a.text.length - b.text.length);

            if (!matches.length) {
                return { found: false };
            }

            let textEl = matches[0].el;
            let row = textEl;
            for (let cur = textEl; cur && cur !== document.body; cur = cur.parentElement) {
                const rect = cur.getBoundingClientRect();
                if (rect.width >= 280 && rect.height >= 36 && rect.height <= 180) {
                    row = cur;
                }
                if (rect.width >= window.innerWidth * 0.45 && rect.height >= 50 && rect.height <= 180) {
                    row = cur;
                    break;
                }
            }

            const rect = row.getBoundingClientRect();
            const textRect = textEl.getBoundingClientRect();
            const x = Math.max(1, Math.min(window.innerWidth - 1, textRect.left + Math.min(30, textRect.width / 2)));
            const y = Math.max(1, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
            return {
                found: true,
                x,
                y,
                text: (textEl.innerText || textEl.textContent || '').slice(0, 120),
                rowText: (row.innerText || row.textContent || '').slice(0, 200)
            };
        }""",
        target_name,
    )

    if not result.get("found"):
        return False

    logging.info("DOM 扫描命中好友: %s", result.get("text", "").strip())
    page.mouse.click(result["x"], result["y"])
    time.sleep(1)
    return True


def dump_debug_snapshot(page: Page, target_name: str) -> None:
    safe_name = "".join(ch for ch in target_name if ch.isalnum()) or "target"
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception as exc:
        text = f"读取 body 文本失败: {exc}"

    text_path = Path(f"debug-not-found-{safe_name}.txt")
    png_path = Path(f"debug-not-found-{safe_name}.png")
    text_path.write_text(text[:12000], encoding="utf-8")
    try:
        page.screenshot(path=str(png_path), full_page=True)
    except Exception as exc:
        logging.warning("保存调试截图失败: %s", exc)
    logging.info("已保存调试文件: %s / %s", text_path, png_path)


def click_target_friend(page: Page, target_name: str, config: Dict[str, Any]) -> bool:
    scroll_limit = int(config.get("scroll_limit", 80))
    pause = int(config.get("scroll_pause_ms", 800)) / 1000

    logging.info("查找好友: %s", target_name)
    for index in range(scroll_limit):
        if click_target_by_dom_text(page, target_name):
            logging.info("已点击好友: %s", target_name)
            return True

        exact_text = page.get_by_text(target_name, exact=True)
        if exact_text.count() > 0:
            candidate = exact_text.first
            clickable = nearest_clickable(candidate)
            if click_if_visible(clickable) or click_if_visible(candidate):
                logging.info("已点击好友: %s", target_name)
                return True

        loose_text = page.locator(f"text={target_name}")
        if loose_text.count() > 0:
            candidate = loose_text.first
            clickable = nearest_clickable(candidate)
            if click_if_visible(clickable) or click_if_visible(candidate):
                logging.info("已点击好友: %s", target_name)
                return True

        if not scroll_chat_list(page):
            logging.debug("第 %s 次查找未找到可滚动聊天列表。", index + 1)
        time.sleep(pause)

    logging.error("没有在聊天列表中找到好友: %s", target_name)
    dump_debug_snapshot(page, target_name)
    return False


def find_chat_input(page: Page, timeout_ms: int) -> Optional[Locator]:
    selectors = [
        "[class*='chat-input-']",
        "div[contenteditable='true']",
        "textarea",
        "input[type='text']",
    ]
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count() == 0:
                    continue
                first = locator.first
                if first.is_visible():
                    return first
            except Exception:
                continue
        time.sleep(0.5)
    return None


def send_message(page: Page, message: str, config: Dict[str, Any], dry_run: bool) -> bool:
    timeout_ms = int(config.get("default_timeout_ms", 60000))
    chat_input = find_chat_input(page, timeout_ms)
    if chat_input is None:
        logging.error("没有找到聊天输入框。")
        return False

    logging.info("准备发送消息: %s", message.replace("\n", "\\n"))
    if dry_run:
        logging.info("dry-run 模式：不会真正发送。")
        return True

    chat_input.click(timeout=timeout_ms)
    page.keyboard.insert_text(message)
    time.sleep(0.5)

    send_button = page.get_by_text("发送", exact=True)
    if click_if_visible(send_button, timeout_ms=2500):
        logging.info("已点击发送按钮。")
        return True

    page.keyboard.press("Enter")
    logging.info("未找到发送按钮，已尝试按 Enter 发送。")
    return True


def run_once(config_path: Path, force: bool, dry_run: bool) -> int:
    config = load_config(config_path)
    profile_dir = resolve_path(config_path, config.get("profile_dir", "browser-profile"))
    state_path = resolve_path(config_path, config.get("state_file", "state.json"))
    state = load_json(state_path, {"sent": {}, "history": []})
    timeout_ms = int(config.get("default_timeout_ms", 60000))
    chat_url = config.get("chat_url", "https://creator.douyin.com/creator-micro/data/following/chat")
    cloud_cookies = load_cloud_cookies(config_path, config)

    with sync_playwright() as p:
        if cloud_cookies:
            logging.info("检测到 Cookie 配置，使用无头 Cookie 模式。")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 960})
            context.add_cookies(cloud_cookies)
        else:
            browser = None
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=bool(config.get("headless", False)),
                viewport={"width": 1440, "height": 960},
            )
        context.set_default_timeout(timeout_ms)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(chat_url, wait_until="domcontentloaded")
            time.sleep(int(config.get("page_load_wait_ms", 5000)) / 1000)

            failures: List[str] = []
            for target in config["targets"]:
                target_name = str(target.get("name", "")).strip()
                if not target_name:
                    continue
                if config.get("send_once_per_day", True) and already_sent(state, target_name) and not force:
                    logging.info("今天已给 %s 发送过，跳过。", target_name)
                    continue

                if not click_target_friend(page, target_name, config):
                    failures.append(target_name)
                    continue

                message = pick_message(config, target)
                if send_message(page, message, config, dry_run):
                    if not dry_run:
                        mark_sent(state_path, state, target_name)
                    time.sleep(1.5)
                else:
                    failures.append(target_name)

            return 1 if failures else 0
        finally:
            context.close()
            if browser is not None:
                browser.close()


def login(config_path: Path, wait_seconds: int = 0) -> int:
    config = load_config(config_path)
    profile_dir = resolve_path(config_path, config.get("profile_dir", "browser-profile"))
    chat_url = config.get("chat_url", "https://creator.douyin.com/creator-micro/data/following/chat")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1440, "height": 960},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            wait_for_manual_login(page, chat_url, wait_seconds)
        finally:
            context.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音续火花本地自动发送助手")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径，默认 ./config.json")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")

    subparsers = parser.add_subparsers(dest="command", required=True)
    login_parser = subparsers.add_parser("login", help="打开浏览器，手动登录并保存本地登录态")
    login_parser.add_argument("--wait-seconds", type=int, default=0, help="等待指定秒数后自动关闭浏览器；默认等待终端按 Enter")

    run_parser = subparsers.add_parser("run", help="执行一次续火花发送任务")
    run_parser.add_argument("--force", action="store_true", help="忽略今日已发送记录，强制再次发送")
    run_parser.add_argument("--dry-run", action="store_true", help="只查找好友和输入框，不实际发送")

    args = parser.parse_args()
    setup_logging(args.verbose)

    config_path = Path(args.config).resolve()
    try:
        if args.command == "login":
            return login(config_path, args.wait_seconds)
        if args.command == "run":
            return run_once(config_path, args.force, args.dry_run)
    except PlaywrightTimeoutError as exc:
        logging.error("页面操作超时: %s", exc)
        return 2
    except KeyboardInterrupt:
        logging.info("已手动中止。")
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
