"""主任务 — 边滚动好友列表边匹配目标，找到立即发消息"""
import traceback
import json
import time
from pathlib import Path
from core.signer import Signer
from core.msg_builder import build_message
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils.notifier import notify_failure

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
groupMatchMode = config.get("groupMatchMode", "name")


def _safe_name(value):
    return "".join(ch for ch in str(value) if ch.isalnum()) or "target"


def dump_debug_snapshot(page, label):
    Path("logs").mkdir(exist_ok=True)
    safe = _safe_name(label)
    text_path = Path("logs") / f"debug-{safe}.txt"
    png_path = Path("logs") / f"debug-{safe}.png"
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception as e:
        text = f"读取页面文本失败: {e}"
    text_path.write_text(text[:12000], encoding="utf-8")
    try:
        page.screenshot(path=str(png_path), full_page=True)
    except Exception as e:
        logger.warning(f"保存截图失败: {e}")
    logger.info(f"已保存调试文件: {text_path} / {png_path}")


_SCROLL_CONTAINER_JS = """
() => {
    const el = document.querySelector('[class*="semi-list"]') ||
               document.querySelector('#sub-app ul');
    if (el && el.scrollTop + el.clientHeight < el.scrollHeight - 10) {
        el.scrollTop += 600; return true;
    }
}
"""


def _type_and_send(page, label, logger):
    """在已打开的聊天窗口中输入消息并发送"""
    chat_input = page.locator('xpath=//div[contains(@class, "chat-input-")]')
    chat_input.wait_for(timeout=config.get("browserTimeout", 120000))
    message = build_message()
    for line in message.split("\\n"):
        chat_input.type(line)
        if line != message.split("\\n")[-1]:
            chat_input.press("Shift+Enter")
    logger.debug(f"发送: {message[:50]}...")
    chat_input.press("Enter")
    time.sleep(2)
    dump_debug_snapshot(page, f"after-send-{label}")
    logger.info(f"已给 {label} 发送续火花消息")
    return True


def _match_target(mapping, target, mode):
    """纯函数：在映射中查找目标。返回 {"nickname": ..., "user_id": ...} 或 None。"""
    target_text = str(target).strip()
    target_id = _target_short_id(target_text)
    if mode == "short_id":
        return mapping.get(target_text) or (mapping.get(target_id) if target_id else None)
    for _sid, v in mapping.items():
        if v["nickname"] == target_text:
            return v
    return None


def _target_short_id(target):
    text = str(target).strip()
    for sep in ["：", ":"]:
        if sep in text:
            return text.split(sep, 1)[1].strip()
    return text


def _record_user_mapping(item, mapping):
    if not isinstance(item, dict):
        return
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    ids = [
        user.get("ShortId")
        or user.get("short_id"),
        user.get("shortId"),
        user.get("unique_id"),
        user.get("uniqueId"),
        user.get("display_id"),
        user.get("displayId"),
        user.get("douyin_id"),
        user.get("douyinId"),
        item.get("ShortId") or item.get("short_id"),
        item.get("shortId"),
        item.get("unique_id"),
        item.get("uniqueId"),
        item.get("display_id"),
        item.get("displayId"),
        item.get("douyin_id"),
        item.get("douyinId"),
    ]
    nick = (
        user.get("nickname")
        or user.get("nick_name")
        or user.get("remark_name")
        or user.get("display_name")
        or item.get("nickname")
        or item.get("nick_name")
        or item.get("remark_name")
        or item.get("display_name")
    )
    uid = item.get("user_id") or item.get("uid") or user.get("uid") or user.get("user_id") or ""
    if nick:
        for sid in ids:
            sid_text = str(sid or "").strip()
            if sid_text:
                mapping[sid_text] = {"nickname": str(nick), "user_id": str(uid)}


def _walk_json_for_users(obj, mapping):
    if isinstance(obj, dict):
        _record_user_mapping(obj, mapping)
        for value in obj.values():
            _walk_json_for_users(value, mapping)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json_for_users(item, mapping)


def handle_response_for_map(resp, mapping):
    """拦截 IM/用户相关响应，尽量更新 short_id 映射。"""
    if resp.status != 200:
        return
    url = resp.url
    if not any(key in url for key in ["user_detail", "creator/im", "/im/", "following", "chat"]):
        return
    try:
        data = resp.json()
        _walk_json_for_users(data, mapping)
    except Exception:
        pass


def scroll_and_find(page, mapping, logger):
    """滚动好友列表，拦截 user_detail 收集映射"""
    def on_resp(resp):
        handle_response_for_map(resp, mapping)

    page.on("response", on_resp)

    try:
        page.reload(wait_until="domcontentloaded", timeout=config.get("browserTimeout", 120000))
        page.wait_for_timeout(3000)
    except Exception as e:
        logger.debug(f"刷新私信页以重新收集好友信息失败，继续当前页面: {e}")

    try:
        if page.get_by_text("朋友私信", exact=True).count() > 0:
            page.get_by_text("朋友私信", exact=True).click(timeout=5000)
            logger.debug("已点击朋友私信 tab")
        else:
            page.locator('xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]').click(timeout=5000)
    except Exception:
        pass
    time.sleep(config.get("friendListTimeout", 2000) / 1000)

    for _ in range(80):
        page.evaluate(_SCROLL_CONTAINER_JS)
        time.sleep(0.3)

    page.remove_listener("response", on_resp)
    logger.debug(f"收集到 {len(mapping)} 个 short_id 映射")


def try_click_and_send(page, target, mapping, logger):
    """尝试点击一个目标好友并发送消息。返回 True 如果成功。"""
    info = _match_target(mapping, target, matchMode)

    if not info:
        if matchMode == "short_id":
            logger.warning(f"映射中未找到目标抖音号: {target}")
            nickname = _target_short_id(target) or str(target)
            logger.warning(f"尝试用页面可见文本兜底查找: {nickname}")
        else:
            nickname = str(target)
            logger.warning(f"映射中未找到目标: {target}，尝试用可见昵称兜底: {nickname}")
    else:
        nickname = info["nickname"]

    # 边滚边找可见元素
    page.evaluate("""
    () => {
        const el = document.querySelector('[class*="semi-list"]') ||
                   document.querySelector('#sub-app ul');
        if (el) el.scrollTop = 0;
    }
    """)
    time.sleep(0.3)

    clicked = False
    for _ in range(80):
        # 检查目标是否可见
        visible = page.evaluate(f"""
        () => {{
            const spans = document.querySelectorAll('[class*="item-header-name-"]');
            const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
            const target = normalize({json.dumps(nickname, ensure_ascii=False)});
            for (const s of spans) {{
                const text = normalize(s.textContent);
                if ((text === target || text.includes(target) || target.includes(text)) &&
                    s.offsetParent !== null) {{
                    const r = s.getBoundingClientRect();
                    return r.top >= 0 && r.bottom <= window.innerHeight;
                }}
            }}
            return false;
        }}
        """)
        if visible:
            # 找到了，用坐标点击（绕过 Playwright 可见性检查）
            page.evaluate(f"""
            () => {{
                const spans = document.querySelectorAll('[class*="item-header-name-"]');
                const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
                const target = normalize({json.dumps(nickname, ensure_ascii=False)});
                for (const s of spans) {{
                    const text = normalize(s.textContent);
                    if (text === target || text.includes(target) || target.includes(text)) {{
                        const r = s.getBoundingClientRect();
                        s.click();
                        return;
                    }}
                }}
            }}
            """)
            clicked = True
            break

        page.evaluate("""
        () => {
            const el = document.querySelector('[class*="semi-list"]') ||
                       document.querySelector('#sub-app ul');
            if (el && el.scrollTop + el.clientHeight < el.scrollHeight - 10)
                el.scrollTop += 300;
        }
        """)
        time.sleep(0.2)

    if not clicked:
        logger.warning(f"未找到可见的 {nickname}")
        dump_debug_snapshot(page, f"not-visible-{nickname}")
        return False

    time.sleep(2)
    try:
        return _type_and_send(page, nickname, logger)
    except Exception as e:
        logger.error(f"发送消息失败: {e}")
        dump_debug_snapshot(page, f"send-failed-{nickname}")
        return False


def do_user_task(signer, username, targets):
    """处理单个账号"""
    page = signer.page
    logger.info(f"开始处理账号 {username}")
    failed = []

    # 收集映射
    mapping = {}
    scroll_and_find(page, mapping, logger)

    if not mapping:
        logger.warning("未收集到好友信息，将尝试按配置中的可见昵称直接查找")
        dump_debug_snapshot(page, f"no-mapping-{username}")

    # 逐个目标发消息
    for target in targets:
        sent = False
        for attempt in range(config.get("taskRetryTimes", 3)):
            try:
                if try_click_and_send(page, target, mapping, logger):
                    sent = True
                    break
            except Exception as e:
                logger.warning(f"给 {target} 发消息失败(尝试{attempt+1}): {e}")
                time.sleep(2)
        if not sent:
            failed.append(target)

    logger.info(f"账号 {username} 任务完成")
    return failed


def do_group_task(signer, username, groups):
    """群聊续火花"""
    page = signer.page
    logger.info(f"开始群聊任务: {username}")
    failed = []

    # 重新导航到聊天页（私聊任务后可能跳到了 home）
    page.goto("https://creator.douyin.com/creator-micro/data/following/chat", timeout=30000)
    time.sleep(5)

    # 点击 "群消息" tab
    try:
        tabs = page.evaluate("""
        () => {
            var divs = document.querySelectorAll('#sub-app > div > div > div:first-child > div');
            for (var i = 0; i < divs.length; i++) {
                var d = divs[i]; var r = d.getBoundingClientRect();
                if (d.textContent.indexOf('群消息') >= 0) return {x: r.x + r.width/2, y: r.y + r.height/2};
            }
            return null;
        }
        """)
        if tabs:
            page.mouse.click(tabs['x'], tabs['y'])
            logger.debug("已点击群消息 tab")
            time.sleep(5)
    except Exception as e:
        logger.error(f"点击群消息 tab 失败: {e}")
        dump_debug_snapshot(page, f"group-tab-failed-{username}")
        return list(groups)

    # 逐个群发消息
    for group_name in groups:
        group_str = str(group_name)
        sent = False
        for attempt in range(config.get("taskRetryTimes", 3)):
            try:
                sent = _send_to_group(page, group_str, logger)
                if sent:
                    break
            except Exception as e:
                logger.warning(f"群 {group_str} 发消息失败(尝试{attempt+1}): {e}")
                time.sleep(2)
        if not sent:
            failed.append(group_str)

    logger.info(f"群聊任务完成: {username}")
    return failed


def _send_to_group(page, target, logger):
    """在群列表中找到目标群，点击并发送消息"""
    # 滚回顶部
    page.evaluate("""
    () => {
        const el = document.querySelector('[class*="semi-list"]') ||
                   document.querySelector('#sub-app ul');
        if (el) el.scrollTop = 0;
    }
    """)
    time.sleep(0.3)

    clicked = False
    for _ in range(80):
        # 找目标群（名称包含匹配）
        found = page.evaluate("""
        (target) => {
            const spans = document.querySelectorAll('[class*="item-header-name-"]');
            for (const s of spans) {
                if (s.textContent.indexOf(target) >= 0 &&
                    s.offsetParent !== null) {
                    const r = s.getBoundingClientRect();
                    if (r.top >= 0 && r.bottom <= window.innerHeight) {
                        s.click();
                        return s.textContent;
                    }
                }
            }
            return null;
        }
        """, target)

        if found:
            clicked = True
            break

        page.evaluate("""
        () => {
            const el = document.querySelector('[class*="semi-list"]') ||
                       document.querySelector('#sub-app ul');
            if (el && el.scrollTop + el.clientHeight < el.scrollHeight - 10)
                el.scrollTop += 300;
        }
        """)
        time.sleep(0.2)

    if not clicked:
        logger.warning(f"未找到群聊: {target}")
        dump_debug_snapshot(page, f"group-not-found-{target}")
        return False

    time.sleep(2)
    try:
        return _type_and_send(page, target, logger)
    except Exception as e:
        logger.error(f"群聊发送消息失败: {e}")
        dump_debug_snapshot(page, f"group-send-failed-{target}")
        return False


def runTasks():
    logger.info("开始执行任务")
    failures = []
    for user in userData:
        username = user.get("username", "未知用户")
        cookies = user["cookies"]
        targets = user.get("targets", [])
        groups = user.get("groups", [])

        if not targets and not groups:
            logger.warning(f"账号 {username} 没有目标好友或群聊")
            continue

        signer = None
        try:
            signer = Signer(cookies)
            if targets:
                failures.extend([f"{username}:{target}" for target in do_user_task(signer, username, targets)])
            if groups:
                failures.extend([f"{username}:群:{group}" for group in do_group_task(signer, username, groups)])
        except Exception as e:
            logger.error(f"账号 {username} 处理失败: {e}")
            traceback.print_exc()
            failures.append(username)
        finally:
            if signer:
                signer.close()
    logger.info("所有任务执行完毕")
    if failures:
        title = "抖音续火花任务异常"
        lines = [
            "以下目标处理失败，火花可能没有续上：",
            ", ".join(failures),
            "",
            "如果日志中出现登录、扫码、验证码、未收集到好友信息等内容，Cookie 可能已失效，请重新导出 CONFIG。",
            "请查看 GitHub Actions 日志和 run-logs 调试截图。",
        ]
        notify_failure(config, title, lines, logger)
        raise SystemExit(f"以下目标处理失败: {failures}")
