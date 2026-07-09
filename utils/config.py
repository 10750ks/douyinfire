import json
import logging
import os
import sys
import hashlib
from enum import Enum
from pathlib import Path

from utils.logger import setup_logger

logger = setup_logger(level=logging.DEBUG)

config_cache = None
userData_cache = None


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"
    LOCAL = "LOCAL"
    PACKED = "PACKED"

    def __str__(self):
        return self.value


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    if os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    return Environment.LOCAL


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def sanitize_cookies(cookies):
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure"}
    cleaned = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        item = dict(cookie)
        if "expirationDate" in item and "expires" not in item:
            item["expires"] = item["expirationDate"]
        item.pop("sameSite", None)
        for key in list(item):
            if key not in allowed:
                item.pop(key, None)
        item.setdefault("path", "/")
        if item.get("name") and item.get("value"):
            dedupe_key = (item.get("domain", ""), item.get("path", "/"), item.get("name", ""))
            cleaned[dedupe_key] = item
    return list(cleaned.values())


def parse_cookie_value(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            logger.warning("cookies 字符串不是合法 JSON")
            return []
    return value if isinstance(value, list) else []


def resolve_cookies(account, full):
    cookies = []
    cookies.extend(parse_cookie_value(account.get("cookies") or full.get("cookies") or []))
    cookies.extend(parse_cookie_value(account.get("web_cookies") or full.get("web_cookies") or []))
    cookies.extend(parse_cookie_value(account.get("webCookies") or full.get("webCookies") or []))

    cookie_file = account.get("cookies_file") or full.get("cookies_file")
    if not cookies and cookie_file:
        cookie_path = Path(cookie_file)
        if not cookie_path.is_absolute():
            cookie_path = project_root() / cookie_path
        cookies = read_json(cookie_path, [])

    return sanitize_cookies(cookies)


def normalize_full_config(full):
    """Support original BTH CONFIG and a simpler direct config.json."""
    if full.get("accounts"):
        return full

    cookies_file = full.get("cookies_file", "cookies.json")
    full["accounts"] = [
        {
            "username": full.get("username", "default"),
            "unique_id": full.get("unique_id", "default"),
            "cookies_file": cookies_file,
            "cookies": full.get("cookies", []),
            "targets": full.get("targets", []),
            "groups": full.get("groups", []),
        }
    ]
    return full


def _load_config():
    global config_cache, userData_cache
    if config_cache is not None:
        return

    raw = os.getenv("CONFIG", "")
    full = {}
    source = "CONFIG 环境变量"
    if raw:
        try:
            full = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"CONFIG 环境变量不是合法的 JSON: {e}")
            if get_environment() == Environment.GITHUBACTION:
                raise RuntimeError("CONFIG Secret 不是合法 JSON，请重新复制配置生成器输出的完整 CONFIG。") from e
    else:
        if get_environment() == Environment.GITHUBACTION:
            raise RuntimeError("GitHub Actions 未读取到 CONFIG Secret，请检查仓库 Settings -> Secrets and variables -> Actions。")
        config_file = project_root() / "config.json"
        source = str(config_file)
        full = read_json(config_file, {})
        if full:
            logger.info(f"从 {config_file} 加载配置")

    full = normalize_full_config(full)
    raw_for_hash = raw or json.dumps(full, ensure_ascii=False, sort_keys=True)
    config_hash = hashlib.sha256(raw_for_hash.encode("utf-8")).hexdigest()[:12]
    notify = full.get("notify", {}) or {}
    channels = notify.get("channels", {}) or {}
    channel_status = ",".join(
        f"{name}={1 if bool((channels.get(name, {}) or {}).get('enabled', False)) else 0}"
        for name in ("wxpusher", "wecom", "qq_email")
    )
    logger.info(
        "配置摘要: source=%s, fingerprint=%s, accounts=%s, targets=%s, groups=%s, notify=%s, channels=%s",
        source,
        config_hash,
        len(full.get("accounts", [])),
        sum(len(account.get("targets", []) or []) for account in full.get("accounts", [])),
        sum(len(account.get("groups", []) or []) for account in full.get("accounts", [])),
        bool(notify.get("enabled", False)),
        channel_status,
    )

    config_cache = {
        "messageTemplate": full.get("messageTemplate", "续火"),
        "hitokotoTypes": full.get("hitokotoTypes", ["文学", "影视", "诗词", "哲学"]),
        "matchMode": full.get("matchMode", "nickname"),
        "groupMatchMode": full.get("groupMatchMode", "name"),
        "browserTimeout": int(full.get("browserTimeout", 120000)),
        "friendListTimeout": int(full.get("friendListTimeout", 2000)),
        "taskRetryTimes": int(full.get("taskRetryTimes", 3)),
        "logLevel": full.get("logLevel", "Info"),
        "preferWebChat": full.get("preferWebChat", True),
        "allowCreatorFallback": full.get("allowCreatorFallback", False),
        "notify": full.get("notify", {}),
    }

    userData_cache = []
    for account in full.get("accounts", []):
        username = account.get("username", "未知用户")
        cookies = resolve_cookies(account, full)
        if not cookies:
            logger.warning(f"账户 {username} 的 cookies 为空，已跳过")
            continue
        userData_cache.append(
            {
                "unique_id": account.get("unique_id", username),
                "username": username,
                "cookies": cookies,
                "targets": account.get("targets", []),
                "groups": account.get("groups", []),
            }
        )


def get_config():
    _load_config()
    return config_cache


def get_userData():
    _load_config()
    return userData_cache
