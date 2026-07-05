"""消息模板解析，构建发送内容"""
from utils.config import get_config
from utils.hitokoto import request_hitokoto


def build_message() -> str:
    message = get_config().get("messageTemplate", "续火花")
    if "[API]" in message:
        api_content = request_hitokoto()
        message = message.replace("[API]", api_content)

    return message.strip()
