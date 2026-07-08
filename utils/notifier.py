"""Failure-only notification helpers."""
import smtplib
from email.message import EmailMessage
from typing import Dict, Iterable, List

import requests


def _enabled(section: Dict) -> bool:
    return bool(section and section.get("enabled", False))


def _as_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def build_failure_message(title: str, lines: Iterable[str]) -> str:
    body = "\n".join(str(line) for line in lines if str(line).strip())
    if not body:
        body = "任务失败，但没有更多错误细节。请查看 GitHub Actions 日志和 run-logs。"
    return f"{title}\n\n{body}"


def send_wxpusher(section: Dict, title: str, content: str) -> str:
    app_token = section.get("appToken") or section.get("app_token")
    uids = _as_list(section.get("uids"))
    topic_ids = section.get("topicIds") or section.get("topic_ids") or []
    if not app_token:
        return "WxPusher 未配置 appToken，跳过"
    if not uids and not topic_ids:
        return "WxPusher 未配置 uids/topicIds，跳过"

    payload = {
        "appToken": app_token,
        "content": content,
        "summary": title[:99],
        "contentType": int(section.get("contentType", 1)),
    }
    if uids:
        payload["uids"] = uids
    if topic_ids:
        payload["topicIds"] = topic_ids

    resp = requests.post(
        "https://wxpusher.zjiecode.com/api/send/message",
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") not in (1000, "1000"):
        raise RuntimeError(f"WxPusher 返回异常: {data}")
    return "WxPusher 推送成功"


def send_wecom(section: Dict, title: str, content: str) -> str:
    webhook = section.get("webhook")
    if not webhook:
        return "企业微信未配置 webhook，跳过"

    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"**{title}**\n\n{content}"},
    }
    resp = requests.post(webhook, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") not in (0, "0", None):
        raise RuntimeError(f"企业微信返回异常: {data}")
    return "企业微信推送成功"


def send_qq_email(section: Dict, title: str, content: str) -> str:
    username = section.get("username")
    password = section.get("password") or section.get("authorizationCode") or section.get("auth_code")
    to_addrs = _as_list(section.get("to"))
    if not username or not password or not to_addrs:
        return "QQ 邮箱未配置 username/password/to，跳过"

    host = section.get("smtpHost", "smtp.qq.com")
    port = int(section.get("smtpPort", 465))
    sender_name = section.get("fromName", "DouYinSpark-ALL")

    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = f"{sender_name} <{username}>"
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(content)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)
    return "QQ 邮箱推送成功"


def notify_failure(config: Dict, title: str, lines: Iterable[str], logger) -> None:
    notify = config.get("notify", {}) or {}
    if not _enabled(notify):
        logger.info("失败通知未启用，跳过推送")
        return

    channels = notify.get("channels", {}) or {}
    content = build_failure_message(title, lines)
    handlers = [
        ("wxpusher", send_wxpusher),
        ("wecom", send_wecom),
        ("qq_email", send_qq_email),
    ]

    sent_any = False
    for name, handler in handlers:
        section = channels.get(name, {}) or {}
        if not _enabled(section):
            continue
        try:
            result = handler(section, title, content)
            logger.info(result)
            sent_any = True
        except Exception as exc:
            logger.error(f"{name} 推送失败: {exc}")

    if not sent_any:
        logger.warning("失败通知已启用，但没有任何通道成功执行。")
