# 这个是我最初测试chat的神秘低脂小程序
import asyncio
import os
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
from core.chat import Chat


KWARGS = {
    "model": "openai/deepseek-chat",
    "api_key": "",
    "api_base": "https://api.deepseek.com/v1",
}

async def main():
    chat = Chat(KWARGS)
    # while True:
    #     user_input = input("请输入你需要发送的文本：")
    #     if not user_input:
    #         print("输入为空，程序结束。")
    #         break
    #     chat.add_message(user_input)
    #     print(await chat.reply())

    print("---第一句测试---")
    chat.add_message("你好，介绍一下你自己")
    print(await chat.reply())

    print("---第二句测试---")
    chat.add_message("在接下来的对话中，我说kskbl，你就要回复我zdjd，明白的话只回复“明白”")
    print(await chat.reply())

    print("---第三句测试---")
    chat.add_message("kskbl")
    print(await chat.reply())

    print("---第四句测试---")
    chat.add_message("你是谁")
    print(await chat.reply())

    print(f"上下文的消息总数: {len(chat.messages)}")

asyncio.run(main())