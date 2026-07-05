import json
import logging
import os
import sys
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
    cleaned = []
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
            cleaned.append(item)
    return cleaned


def resolve_cookies(account, full):
    cookies = account.get("cookies") or full.get("cookies") or []
    if isinstance(cookies, str):
        try:
            cookies = json.loads(cookies)
        except json.JSONDecodeError:
            logger.warning("cookies 字符串不是合法 JSON")
            cookies = []

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
    if raw:
        try:
            full = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"CONFIG 环境变量不是合法的 JSON: {e}")
    else:
        config_file = project_root() / "config.json"
        full = read_json(config_file, {})
        if full:
            logger.info(f"从 {config_file} 加载配置")

    full = normalize_full_config(full)

    config_cache = {
        "messageTemplate": full.get("messageTemplate", "续火"),
        "hitokotoTypes": full.get("hitokotoTypes", ["文学", "影视", "诗词", "哲学"]),
        "matchMode": full.get("matchMode", "nickname"),
        "groupMatchMode": full.get("groupMatchMode", "name"),
        "browserTimeout": int(full.get("browserTimeout", 120000)),
        "friendListTimeout": int(full.get("friendListTimeout", 2000)),
        "taskRetryTimes": int(full.get("taskRetryTimes", 3)),
        "logLevel": full.get("logLevel", "Info"),
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
