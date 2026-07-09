"""
阶段3测试：验证Agent核心推理循环
包含：
1. Mock模式下的端到端流程测试（不消耗API）
2. 可选的真实API测试（需要设置环境变量）
"""
import json
import os
import tempfile
from types import SimpleNamespace

from agent import ResumeAgent


TEST_RESUME = """
姓名：李明
电话：13800000000
邮箱：liming@example.com
目标岗位：运营专员

教育经历：
2018.09-2022.06 江南大学 市场营销 本科

工作经历：
2022.07-2025.06 星河电商 运营助理
- 负责店铺日常活动配置、商品上下架、基础数据整理。
- 参与618和双11活动复盘，整理转化率、客单价、投放ROI等指标。
- 协助优化商品标题和详情页，部分核心商品点击率提升约12%。

项目经历：
会员复购提升项目
- 参与用户分层标签整理，配合社群和短信触达。
- 输出周度数据看板，跟踪复购率变化。

技能：Excel、SQL基础、数据透视表、活动运营、用户运营、飞书多维表格
"""

TEST_JD = """
岗位：电商运营专员
职责：
1. 负责平台活动运营、商品运营和用户运营，提升转化率和复购率。
2. 跟踪销售、流量、转化、客单价等数据，输出运营分析报告。
3. 协同设计、投放和客服团队推进活动落地。

要求：
1. 本科及以上学历，1年以上电商运营经验。
2. 熟练使用Excel，有基础数据分析能力。
3. 熟悉活动复盘、商品优化、用户分层者优先。
4. 沟通能力强，执行力强，对数据敏感。
"""


class _MockToolCall:
    """模拟OpenAI返回的tool_call对象"""

    def __init__(self, tool_id, tool_name, arguments):
        self.id = tool_id
        self.function = SimpleNamespace(
            name=tool_name,
            arguments=json.dumps(arguments) if isinstance(arguments, dict) else arguments,
        )


class _MockMessage:
    """模拟OpenAI返回的message对象"""

    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


def _make_mock_llm(sequence):
    """根据工具调用序列生成一个mock LLM.chat函数"""
    index = [0]

    def mock_chat(messages, tools=None, temperature=0.3):
        if index[0] >= len(sequence):
            return _MockMessage(content="分析已完成，正在生成最终报告。")
        item = sequence[index[0]]
        index[0] += 1
        return _MockMessage(
            content=item.get("content", ""),
            tool_calls=[_MockToolCall(item["id"], item["name"], item["args"])],
        )

    return mock_chat


def _build_mock_sequence():
    """构造Agent完整工作流所需的工具调用序列"""
    return [
        {
            "id": "call_1",
            "name": "extract_resume_info",
            "args": {"resume_text": TEST_RESUME},
        },
        {
            "id": "call_2",
            "name": "analyze_jd",
            "args": {"jd_text": TEST_JD},
        },
        {
            "id": "call_3",
            "name": "calculate_match",
            "args": {
                "resume_info": "__state_resume_info__",
                "jd_analysis": "__state_jd_analysis__",
            },
        },
        {
            "id": "call_4",
            "name": "generate_suggestions",
            "args": {
                "resume_info": "__state_resume_info__",
                "jd_analysis": "__state_jd_analysis__",
                "match_result": "__state_match_result__",
            },
        },
        {
            "id": "call_5",
            "name": "verify_output",
            "args": {
                "resume_info": "__state_resume_info__",
                "jd_analysis": "__state_jd_analysis__",
                "match_result": "__state_match_result__",
                "suggestions": "__state_suggestions__",
            },
        },
    ]


def _prepare_mock_agent():
    """创建一个被mock LLM驱动的Agent实例，用于离线测试"""
    agent = ResumeAgent(
        resume_input=TEST_RESUME,
        jd_text=TEST_JD,
        resume_is_file=False,
        output_dir="output_test",
    )

    sequence = _build_mock_sequence()

    def mock_chat(messages, tools=None, temperature=0.3):
        # 序列走完后返回普通消息，结束推理循环
        # （自我验证未通过会触发额外的修正步骤，本测试的固定序列不覆盖修正轮）
        if mock_chat.index >= len(sequence):
            return _MockMessage(content="分析已完成，正在生成最终报告。")
        # 用Agent当前真实状态替换占位符
        step = sequence[mock_chat.index]
        mock_chat.index += 1
        args = {}
        for key, value in step["args"].items():
            if value == "__state_resume_info__":
                args[key] = agent.state["resume_info"]
            elif value == "__state_jd_analysis__":
                args[key] = agent.state["jd_analysis"]
            elif value == "__state_match_result__":
                args[key] = agent.state["match_result"]
            elif value == "__state_suggestions__":
                args[key] = agent.state["suggestions"]
            else:
                args[key] = value
        return _MockMessage(
            content=f"步骤：调用{step['name']}",
            tool_calls=[_MockToolCall(step["id"], step["name"], args)],
        )

    mock_chat.index = 0
    agent.client.chat = mock_chat
    return agent


def test_mock_agent_workflow():
    """Mock测试：验证Agent能按顺序调用工具并完成报告"""
    print("\n" + "=" * 60)
    print("🧪 测试1：Mock模式下的Agent完整工作流")
    print("=" * 60)

    agent = _prepare_mock_agent()
    report = agent.run()

    # 断言所有关键状态已填充
    assert agent.state["resume_info"] is not None, "resume_info未生成"
    assert agent.state["jd_analysis"] is not None, "jd_analysis未生成"
    assert agent.state["match_result"] is not None, "match_result未生成"
    assert agent.state["suggestions"] is not None, "suggestions未生成"
    assert agent.state["verification"] is not None, "verification未生成"

    # 断言最终报告包含必要章节
    required_sections = [
        "【简历解析】",
        "【匹配度分析】",
        "【优化建议】",
        "【自我验证】",
        "【诚实评估】",
        "【优化版简历】",
    ]
    for section in required_sections:
        assert section in report, f"最终报告缺少章节：{section}"

    print("\n✅ Mock测试通过：Agent完整工作流正常")
    return report


def test_real_agent_workflow():
    """真实API测试：验证Agent在真实LLM驱动下能完成工作流"""
    print("\n" + "=" * 60)
    print("🌐 测试2：真实API模式下的Agent工作流")
    print("=" * 60)

    # 默认使用文本简历避免文件依赖
    agent = ResumeAgent(
        resume_input=TEST_RESUME,
        jd_text=TEST_JD,
        resume_is_file=False,
    )
    report = agent.run()

    required_sections = [
        "【简历解析】",
        "【匹配度分析】",
        "【优化建议】",
        "【自我验证】",
        "【诚实评估】",
        "【优化版简历】",
    ]
    for section in required_sections:
        assert section in report, f"最终报告缺少章节：{section}"

    print("\n✅ 真实API测试通过")
    return report


def test_agent_with_resume_file():
    """测试Agent能处理简历文件路径"""
    print("\n" + "=" * 60)
    print("📄 测试3：Agent处理简历文件路径")
    print("=" * 60)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as file:
        file.write(TEST_RESUME)
        resume_path = file.name

    try:
        # Mock模式下，让Agent第一步调用parse_resume_file，之后无工具调用
        agent = ResumeAgent(
            resume_input=resume_path,
            jd_text=TEST_JD,
            resume_is_file=True,
            output_dir="output_test",
        )

        call_count = [0]

        def mock_chat(messages, tools=None, temperature=0.3):
            call_count[0] += 1
            if call_count[0] == 1:
                return _MockMessage(
                    content="需要解析简历文件",
                    tool_calls=[_MockToolCall("call_parse", "parse_resume_file", {"file_path": resume_path})],
                )
            return _MockMessage(content="文件已解析，停止测试")

        agent.client.chat = mock_chat
        # 只验证第一步能解析文件
        agent._loop()
        assert agent.state["resume_text"] is not None, "文件解析失败"
        assert len(agent.state["resume_text"]) > 0, "简历文本为空"
        print(f"\n✅ 文件路径处理测试通过（运行{call_count[0]}步）")
    finally:
        os.unlink(resume_path)


def main():
    """测试入口：优先运行Mock测试，API可用时运行真实测试"""
    # 运行Mock测试
    test_mock_agent_workflow()

    # 运行文件路径测试
    test_agent_with_resume_file()

    # 检查是否运行真实API测试
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ZENMUX_API_KEY", "")
    if os.environ.get("AGENT_MOCK", "") == "1" or not api_key:
        print("\n⏭️ 跳过真实API测试（未设置API密钥或处于Mock模式）")
        print("如需运行真实测试，请执行：")
        print("export ZENMUX_API_KEY=你的密钥 && python test_agent.py")
        return

    # 运行真实API测试
    test_real_agent_workflow()

    print("\n🎉 阶段3测试完成，Agent核心推理循环可用！")


if __name__ == "__main__":
    main()
