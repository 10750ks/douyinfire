"""主任务 — 边滚动好友列表边匹配目标，找到立即发消息"""
import traceback
import json
import time
import urllib.parse
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

WWW_CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
WWW_CONV_ITEM_SELECTOR = 'div[class*="conversationConversationItemwrapper"]'
CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"


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


def _looks_www_chat_logged_out(page):
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    login_words = ["登录", "扫码", "验证码", "请先登录"]
    chat_words = ["发送消息", "搜索", "分享[视频]", "火花"]
    return any(word in text for word in login_words) and not any(word in text for word in chat_words)


def _type_and_send_www_chat(page, label, logger):
    """在 www.douyin.com/chat 的聊天窗口中输入并发送消息。"""
    message = build_message()
    input_locator = page.locator(
        'textarea[placeholder*="发送"], textarea[placeholder*="消息"], '
        'div[contenteditable="true"], [contenteditable="true"], '
        'input[placeholder*="发送"], input[placeholder*="消息"]'
    ).last
    input_locator.wait_for(timeout=config.get("browserTimeout", 120000))
    input_locator.click()
    for idx, line in enumerate(message.split("\n")):
        input_locator.type(line)
        if idx != len(message.split("\n")) - 1:
            input_locator.press("Shift+Enter")
    logger.debug(f"网页版发送: {message[:50]}...")
    input_locator.press("Enter")
    time.sleep(1)

    # 有些版本按 Enter 只换行，需要点击右下角发送按钮。
    page.evaluate("""
    () => {
        const candidates = Array.from(document.querySelectorAll('button, div, span, svg'))
            .filter(el => el.offsetParent !== null);
        const byText = candidates.find(el => /发送/.test(el.textContent || ''));
        if (byText) { byText.click(); return true; }
        const bottom = candidates
            .map(el => ({ el, r: el.getBoundingClientRect() }))
            .filter(x => x.r.width > 16 && x.r.height > 16 && x.r.bottom > window.innerHeight - 120)
            .sort((a, b) => b.r.left - a.r.left);
        if (bottom[0]) { bottom[0].el.click(); return true; }
        return false;
    }
    """)
    time.sleep(2)
    dump_debug_snapshot(page, f"www-after-send-{label}")
    logger.info(f"已通过网页版私信给 {label} 发送续火花消息")
    return True


def _find_www_chat_search_box(page):
    return page.locator(
        'input[placeholder*="搜索"], textarea[placeholder*="搜索"], '
        'input[type="search"], input'
    ).first


def _wait_www_chat_ready(page, logger, max_seconds=75):
    for _ in range(max_seconds):
        status = page.evaluate(
            """() => ({
                hasConvStore: !!window.conversationStore,
                convItems: document.querySelectorAll('div[class*="conversationConversationItemwrapper"]').length,
                hasUserStore: !!window.userInfoStore,
                userCount: window.userInfoStore?.usersInfoMap?.data_?.size || 0,
                text: (document.body && document.body.innerText || '').slice(0, 120)
            })"""
        )
        if status.get("hasConvStore") and (status.get("convItems", 0) > 0 or status.get("userCount", 0) > 0):
            logger.info(
                f"网页版私信加载完成: convItems={status.get('convItems')}, userCount={status.get('userCount')}"
            )
            return True
        if "扫码登录" in status.get("text", "") or "请输入手机号" in status.get("text", ""):
            return False
        page.wait_for_timeout(1000)
    logger.warning("网页版私信加载超时，继续尝试当前页面")
    return False


def _wait_www_user_cache(page, logger, max_seconds=75):
    for _ in range(max_seconds):
        count = page.evaluate("() => window.userInfoStore?.usersInfoMap?.data_?.size || 0")
        if count:
            logger.info(f"网页版用户缓存加载完成: userCount={count}")
            return True
        page.wait_for_timeout(1000)
    logger.warning("网页版用户缓存加载超时，可能无法按抖音号解析会话")
    return False


def _wait_www_user_by_short_id(page, target_id, logger, max_seconds=90):
    for _ in range(max_seconds):
        user = _resolve_www_user_by_short_id(page, target_id)
        if user:
            logger.info(f"网页版目标用户缓存命中: short_id={target_id}, display={user.get('display_name')}")
            return user
        page.wait_for_timeout(1000)
    logger.warning(f"网页版目标用户缓存未命中: {target_id}")
    return None


def _resolve_www_user_by_short_id(page, target_id):
    return page.evaluate(
        """
        (target) => {
            const wanted = String(target || '').trim();
            const m = window.userInfoStore?.usersInfoMap?.data_;
            if (!m) return null;
            for (const [key, boxed] of m.entries()) {
                const u = boxed?.value_ !== undefined ? boxed.value_ : boxed;
                if (!u) continue;
                const ids = [u.short_id, u.unique_id, u.uid, key].map(x => String(x || '').trim()).filter(Boolean);
                if (ids.includes(wanted)) {
                    return {
                        uid: String(u.uid || key || ''),
                        short_id: String(u.short_id || ''),
                        unique_id: String(u.unique_id || ''),
                        nickname: String(u.nickname || ''),
                        remark_name: String(u.remark_name || ''),
                        display_name: String(u.remark_name || u.nickname || u.unique_id || u.short_id || wanted)
                    };
                }
            }
            return null;
        }
        """,
        str(target_id),
    )


def _click_www_chat_by_visible_name(page, name, logger):
    def try_match():
        return page.evaluate(
            """
            (targetName) => {
                const normalize = s => String(s || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
                const target = normalize(targetName);
                const items = Array.from(document.querySelectorAll('div[class*="conversationConversationItemwrapper"]'));
                const names = [];
                for (let i = 0; i < items.length; i++) {
                    const item = items[i];
                    const titleEl = item.querySelector('div[class*="conversationConversationItemtitle"]') || item;
                    const fullText = normalize(titleEl.textContent);
                    names.push(fullText.slice(0, 30));
                    if (fullText === target || fullText.includes(target) || target.includes(fullText)) {
                        return { index: i, text: fullText, names };
                    }
                }
                return { index: -1, text: '', names };
            }
            """,
            name,
        )

    result = try_match()
    if result.get("index", -1) >= 0:
        page.locator(WWW_CONV_ITEM_SELECTOR).nth(result["index"]).click(timeout=10000)
        logger.info(f"网页版私信按用户缓存命中会话: {name}")
        return True

    page.evaluate(
        """() => {
            const list = document.querySelector('div[class*="conversationConversationListwrapper"]');
            const scrollable = list?.querySelector('[style*="overflow"]') || list;
            if (scrollable) scrollable.scrollTop = 0;
        }"""
    )
    page.wait_for_timeout(500)
    for _ in range(30):
        result = try_match()
        if result.get("index", -1) >= 0:
            page.locator(WWW_CONV_ITEM_SELECTOR).nth(result["index"]).click(timeout=10000)
            logger.info(f"网页版私信滚动后命中会话: {name}")
            return True
        page.evaluate(
            """() => {
                const list = document.querySelector('div[class*="conversationConversationListwrapper"]');
                const scrollable = list?.querySelector('[style*="overflow"]') || list;
                if (scrollable) scrollable.scrollTop += 480;
            }"""
        )
        page.wait_for_timeout(300)
    logger.warning(f"网页版私信未找到可见会话名 {name}: {result.get('names', [])}")
    return False


def _resolve_www_group_by_id(page, target_id):
    """Best-effort lookup for a group conversation in the www chat runtime cache."""
    return page.evaluate(
        """
        (target) => {
            const wanted = String(target || '').trim();
            if (!wanted) return null;
            const seen = new Set();
            const candidates = [];
            let visited = 0;
            const nameKey = /(name|title|display|remark|nickname)/i;
            const idKey = /(id|cid|conversation|conversation_id|conversationId|short)/i;
            const groupKey = /(group|群)/i;
            const unwrap = value => value && value.value_ !== undefined ? value.value_ : value;
            const text = value => value === undefined || value === null ? '' : String(value).trim();

            function addCandidate(obj, path) {
                const ids = [];
                const names = [];
                let groupLike = groupKey.test(path);
                for (const [k, raw] of Object.entries(obj)) {
                    const v = unwrap(raw);
                    if (typeof v !== 'string' && typeof v !== 'number') continue;
                    const s = text(v);
                    if (!s) continue;
                    if (idKey.test(k)) ids.push(s);
                    if (nameKey.test(k)) names.push(s);
                    if (groupKey.test(k) || groupKey.test(s)) groupLike = true;
                }
                const exact = ids.some(id => id === wanted);
                const contains = ids.some(id => id.includes(wanted) || wanted.includes(id));
                if (exact || (groupLike && contains)) {
                    candidates.push({
                        id: ids.find(id => id === wanted) || ids[0] || wanted,
                        display_name: names.find(Boolean) || '',
                        group_like: groupLike,
                        path
                    });
                }
            }

            function walk(value, path, depth) {
                if (visited++ > 5000 || depth > 7) return;
                value = unwrap(value);
                if (!value || typeof value !== 'object') return;
                if (seen.has(value)) return;
                seen.add(value);

                if (value instanceof Map) {
                    for (const [k, v] of value.entries()) {
                        if (text(k) === wanted) {
                            const unboxed = unwrap(v);
                            if (unboxed && typeof unboxed === 'object') addCandidate(unboxed, `${path}.${k}`);
                        }
                        walk(v, `${path}.${k}`, depth + 1);
                    }
                    return;
                }

                if (Array.isArray(value)) {
                    for (let i = 0; i < Math.min(value.length, 300); i++) {
                        walk(value[i], `${path}[${i}]`, depth + 1);
                    }
                    return;
                }

                addCandidate(value, path);
                for (const [k, v] of Object.entries(value)) {
                    if (typeof v === 'object' && v !== null) walk(v, `${path}.${k}`, depth + 1);
                }
            }

            walk(window.conversationStore, 'conversationStore', 0);
            walk(window.__pace_f?.store, '__pace_f.store', 0);

            candidates.sort((a, b) => {
                if (!!b.display_name !== !!a.display_name) return b.display_name ? 1 : -1;
                if (!!b.group_like !== !!a.group_like) return b.group_like ? 1 : -1;
                return 0;
            });
            return candidates[0] || null;
        }
        """,
        str(target_id),
    )


def _wait_www_group_by_id(page, target_id, logger, max_seconds=75):
    for _ in range(max_seconds):
        group = _resolve_www_group_by_id(page, target_id)
        if group:
            logger.info(
                f"网页版群聊缓存命中: id={target_id}, display={group.get('display_name')}, path={group.get('path')}"
            )
            return group
        page.wait_for_timeout(1000)
    logger.warning(f"网页版群聊缓存未命中: {target_id}")
    return None


def _send_via_www_group_chat(page, target, logger):
    """通过 www.douyin.com/chat 按群会话 ID 发送。支持 群名:群ID 写法。"""
    target_id = _target_short_id(target)
    visible_name = _target_visible_name(target)
    if not target_id:
        return False
    logger.info(f"尝试使用网页版私信按群会话 ID 发送: {target_id}")
    try:
        page.goto(WWW_CHAT_URL, wait_until="domcontentloaded", timeout=config.get("browserTimeout", 120000))
        page.wait_for_timeout(3000)
        if _looks_www_chat_logged_out(page):
            raise RuntimeError("网页版私信页面疑似未登录，请重新导出 web_storage_state。")
        if not _wait_www_chat_ready(page, logger):
            raise RuntimeError("网页版私信页面未加载完成或疑似未登录，请检查 web_storage_state。")

        group = _wait_www_group_by_id(page, target_id, logger)
        names = []
        if group and group.get("display_name"):
            names.append(group.get("display_name"))
        if visible_name and visible_name != target_id:
            names.append(visible_name)

        for name in dict.fromkeys(names):
            if _click_www_chat_by_visible_name(page, name, logger):
                page.wait_for_timeout(2000)
                return _type_and_send_www_chat(page, f"群:{target_id}", logger)

        logger.warning(f"网页版群聊 ID 已解析但未找到可点击会话: {target_id}, names={names}")
        dump_debug_snapshot(page, f"www-group-not-visible-{target_id}")
        return False
    except Exception as e:
        logger.error(f"网页版群聊发送失败: {e}")
        dump_debug_snapshot(page, f"www-group-send-failed-{target_id}")
        return False


def _send_via_www_group_name_chat(page, target, logger):
    """通过 www.douyin.com/chat 按群名称发送。"""
    group_name = _target_visible_name(target)
    if not group_name:
        return False
    logger.info(f"尝试使用网页版私信按群名称发送: {group_name}")
    try:
        page.goto(WWW_CHAT_URL, wait_until="domcontentloaded", timeout=config.get("browserTimeout", 120000))
        page.wait_for_timeout(3000)
        if _looks_www_chat_logged_out(page):
            raise RuntimeError("网页版私信页面疑似未登录，请重新导出 web_storage_state。")
        if not _wait_www_chat_ready(page, logger):
            raise RuntimeError("网页版私信页面未加载完成或疑似未登录，请检查 web_storage_state。")

        if _click_www_chat_by_visible_name(page, group_name, logger):
            page.wait_for_timeout(2000)
            return _type_and_send_www_chat(page, f"群:{group_name}", logger)

        dump_debug_snapshot(page, f"www-group-name-not-found-{group_name}")
        return False
    except Exception as e:
        logger.error(f"网页版群名称发送失败: {e}")
        dump_debug_snapshot(page, f"www-group-name-send-failed-{group_name}")
        return False


def _click_www_chat_search_result(page, target_id, logger):
    """搜索后点击 www 私信列表结果。优先点击包含目标文本的结果；否则仅在唯一可见结果时点击。"""
    target = str(target_id).strip()
    result = page.evaluate(
        """
        (target) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
            const targetText = normalize(target);
            const items = Array.from(document.querySelectorAll('div[class*="conversationConversationItemwrapper"], div[class*="conversationConversationItem"]'))
                .filter(el => el.offsetParent !== null);
            const matched = items.find(el => normalize(el.textContent).includes(targetText));
            if (matched) {
                matched.click();
                return { clicked: true, reason: 'text-match', text: matched.textContent.slice(0, 120) };
            }
            if (items.length === 1) {
                items[0].click();
                return { clicked: true, reason: 'single-result', text: items[0].textContent.slice(0, 120) };
            }
            return { clicked: false, count: items.length, sample: items.slice(0, 5).map(el => el.textContent.slice(0, 80)) };
        }
        """,
        target,
    )
    if result and result.get("clicked"):
        logger.info(f"网页版私信搜索命中 {target}: {result.get('reason')}")
        return True
    logger.warning(f"网页版私信搜索未能精确命中 {target}: {result}")
    return False


def _send_via_www_chat(page, target, logger):
    """通过 www.douyin.com/chat 按抖音号搜索并发送，作为 short_id 模式兜底。"""
    target_id = _target_short_id(target)
    if not target_id:
        return False
    logger.info(f"尝试使用网页版私信按抖音号搜索发送: {target_id}")
    try:
        page.goto(WWW_CHAT_URL, wait_until="domcontentloaded", timeout=config.get("browserTimeout", 120000))
        page.wait_for_timeout(3000)
        if _looks_www_chat_logged_out(page):
            raise RuntimeError("网页版私信页面疑似未登录，请重新导出 Cookie/CONFIG。")
        if not _wait_www_chat_ready(page, logger):
            raise RuntimeError("网页版私信页面未加载完成或疑似未登录，请检查 web_storage_state。")

        user = _wait_www_user_by_short_id(page, target_id, logger)
        if user:
            logger.info(
                f"已从网页版用户缓存解析抖音号 {target_id}: uid={user.get('uid')}, display={user.get('display_name')}"
            )
            if _click_www_chat_by_visible_name(page, user.get("display_name"), logger):
                page.wait_for_timeout(2000)
                return _type_and_send_www_chat(page, target_id, logger)
            dump_debug_snapshot(page, f"www-cache-not-visible-{target_id}")
            return False
        logger.warning(f"网页版用户缓存未解析到抖音号 {target_id}，回退搜索框")

        search_box = _find_www_chat_search_box(page)
        search_box.wait_for(timeout=20000)
        search_box.click()
        search_box.fill("")
        search_box.type(target_id)
        search_box.press("Enter")
        page.wait_for_timeout(3000)

        if not _click_www_chat_search_result(page, target_id, logger):
            dump_debug_snapshot(page, f"www-search-not-found-{target_id}")
            return False
        page.wait_for_timeout(2000)
        return _type_and_send_www_chat(page, target_id, logger)
    except Exception as e:
        logger.error(f"网页版私信发送失败: {e}")
        dump_debug_snapshot(page, f"www-send-failed-{target_id}")
        return False


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


def _target_visible_name(target):
    text = str(target).strip()
    for sep in ["：", ":"]:
        if sep in text:
            return text.split(sep, 1)[0].strip()
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
    if not any(key in url for key in ["user_detail", "creator/im", "/im/", "following", "chat", "search", "aweme/v1/web"]):
        return
    try:
        data = resp.json()
        _walk_json_for_users(data, mapping)
    except Exception:
        pass


def _guess_nickname_from_search_text(text, short_id):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    skip_words = {"抖音", "搜索", "用户", "视频", "综合", "直播", "抖音号", short_id}
    for index, line in enumerate(lines):
        if short_id not in line:
            continue
        for offset in (-1, -2, 1, 2):
            pos = index + offset
            if pos < 0 or pos >= len(lines):
                continue
            candidate = lines[pos].strip()
            if not candidate or candidate in skip_words:
                continue
            if len(candidate) <= 40 and short_id not in candidate:
                return candidate
    return ""


def resolve_nickname_by_short_id(page, target, logger):
    """通过抖音搜索页动态解析 short_id 当前昵称。失败时返回空字符串。"""
    short_id = _target_short_id(target)
    if not short_id:
        return ""

    mapping = {}
    search_page = None

    def on_resp(resp):
        handle_response_for_map(resp, mapping)

    try:
        logger.info(f"尝试通过抖音搜索解析抖音号: {short_id}")
        search_page = page.context.new_page()
        search_page.on("response", on_resp)
        url = f"https://www.douyin.com/search/{urllib.parse.quote(short_id)}?type=user"
        search_page.goto(url, wait_until="domcontentloaded", timeout=config.get("browserTimeout", 120000))
        search_page.wait_for_timeout(8000)
        try:
            search_page.mouse.wheel(0, 600)
            search_page.wait_for_timeout(2000)
        except Exception:
            pass

        info = _match_target(mapping, short_id, "short_id")
        if info and info.get("nickname"):
            logger.info(f"搜索页解析到抖音号 {short_id} 当前昵称: {info['nickname']}")
            return info["nickname"]

        try:
            text = search_page.locator("body").inner_text(timeout=5000)
        except Exception:
            text = ""
        guessed = _guess_nickname_from_search_text(text, short_id)
        if guessed:
            logger.info(f"搜索页文本推断抖音号 {short_id} 当前昵称: {guessed}")
            return guessed

        dump_debug_snapshot(search_page, f"search-shortid-{short_id}")
        logger.warning(f"未能通过搜索页解析抖音号: {short_id}")
    except Exception as e:
        logger.warning(f"搜索解析抖音号 {short_id} 失败: {e}")
    finally:
        try:
            if search_page:
                search_page.remove_listener("response", on_resp)
                search_page.close()
        except Exception:
            pass

    return ""


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
    if matchMode == "short_id" and config.get("preferWebChat", True):
        if _send_via_www_chat(page, target, logger):
            return True
        if not config.get("allowCreatorFallback", False):
            logger.warning("网页版私信发送失败，未启用创作者中心兜底，本目标按失败处理。")
            return False
        logger.warning("网页版私信发送失败，回退到创作者中心私信。")
        try:
            page.goto(CREATOR_CHAT_URL, wait_until="domcontentloaded", timeout=config.get("browserTimeout", 120000))
            page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning(f"回到创作者中心失败，继续尝试当前页面: {e}")

    info = _match_target(mapping, target, matchMode)

    if not info:
        if matchMode == "short_id":
            logger.warning(f"映射中未找到目标抖音号: {target}")
            resolved = resolve_nickname_by_short_id(page, target, logger)
            nickname = resolved or _target_visible_name(target) or _target_short_id(target) or str(target)
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
        if matchMode == "short_id" and _send_via_www_chat(page, target, logger):
            return True
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
    creator_groups = []

    if config.get("preferWebChat", True):
        for group_name in groups:
            group_str = str(group_name).strip()
            if not group_str:
                continue
            sent = False
            for attempt in range(config.get("taskRetryTimes", 3)):
                try:
                    if groupMatchMode in ("id", "short_id", "conversation_id"):
                        sent = _send_via_www_group_chat(page, group_str, logger)
                    else:
                        sent = _send_via_www_group_name_chat(page, group_str, logger)
                    if sent:
                        break
                except Exception as e:
                    logger.warning(f"网页版给群 {group_str} 发消息失败(尝试{attempt+1}): {e}")
                    time.sleep(2)
            if not sent:
                fallback_name = _target_visible_name(group_str)
                if fallback_name and fallback_name != _target_short_id(group_str):
                    creator_groups.append(fallback_name)
                elif groupMatchMode not in ("id", "short_id", "conversation_id"):
                    creator_groups.append(group_str)
                else:
                    failed.append(group_str)

        if not creator_groups:
            logger.info(f"群聊任务完成: {username}")
            return failed
    else:
        creator_groups = list(groups)

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
        failed.extend(list(creator_groups))
        return failed

    # 逐个群发消息
    for group_name in creator_groups:
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
            signer = Signer(cookies, user.get("web_storage_state"))
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
