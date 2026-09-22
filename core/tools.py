import json
from typing import Any

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某城市的天气信息，用户问天气时，调用此函数获取天气信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，例如：北京、上海、广州等"
                    }
                },
                "required": ["city"]
            },
        },
    },
]

async def get_weather(city: str) -> str:
    
    fake = {"东京": "小雨，12℃", "上海": "多云，18℃"}
    return fake.get(city, f"{city}：暂时查不到")


TOOL_IMPL: dict[str, Any] = {
    "get_weather": get_weather,
}