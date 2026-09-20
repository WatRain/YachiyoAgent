import os
from pathlib import Path
from smolagents import ToolCallingAgent, LiteLLMModel, WebSearchTool
from dotenv import load_dotenv
load_dotenv()

model = LiteLLMModel(
    # LiteLLM 要求模型名带 provider 前缀；DeepSeek 兼容 OpenAI 协议，故用 openai/
    model_id = f"openai/{os.getenv('LLM_MODEL_ID')}",
    api_key = os.getenv("LLM_API_KEY"),
    api_base = os.getenv("LLM_BASE_URL"),
    # smolagents 默认发 tool_choice="required"，但思考模式模型不支持，只能用它支持的 "auto"
    tool_choice = "auto",
)

agent = ToolCallingAgent(
    tools=[WebSearchTool()],
    model = model,
    # 基于脚本自身位置定位，避免受当前工作目录影响
    instructions = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
)
fuck = agent.run("你好！请介绍一下自己")
print(fuck)