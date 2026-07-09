from llm_client import LLMClient

print("=" * 50)
print("测试1：基础对话能力")
print("=" * 50)
client = LLMClient()
reply = client.simple_ask("请只回复四个字：连接成功")
print(f"模型回复：{reply}")

print()
print("=" * 50)
print("测试2：Function Calling（工具调用）能力")
print("=" * 50)
test_tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"}
            },
            "required": ["city"]
        }
    }
}]
message = client.chat(
    messages=[{"role": "user", "content": "北京今天天气怎么样？"}],
    tools=test_tools
)
if message.tool_calls:
    tc = message.tool_calls[0]
    print(f"✅ 模型成功发起工具调用！")
    print(f"   工具名：{tc.function.name}")
    print(f"   参数：{tc.function.arguments}")
else:
    print(f"❌ 模型没有调用工具，而是直接回复了：{message.content}")

print()
print("🎉 阶段1测试完成")
