import builtins
import os
import tempfile

from tools import execute_tool, get_tool_definitions
from utils import parse_json_safely, to_pretty_json


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


def print_section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def assert_success(name, result):
    if not isinstance(result, dict):
        raise AssertionError(f"{name} 未返回dict")
    if not result.get("success"):
        raise AssertionError(f"{name} 执行失败：{result}")


def main():
    print_section("阶段2测试：工具定义数量")
    definitions = get_tool_definitions()
    print(f"工具数量：{len(definitions)}")
    print("工具名称：" + ", ".join(item["function"]["name"] for item in definitions))
    if len(definitions) != 8:
        raise AssertionError("工具定义数量必须为8")

    print_section("测试utils.parse_json_safely")
    parsed = parse_json_safely('```json\n{"a":1, "b":[2,3,],}\n```')
    print(to_pretty_json(parsed))
    if parsed.get("a") != 1:
        raise AssertionError("JSON容错解析失败")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as file:
        file.write(TEST_RESUME)
        resume_path = file.name

    try:
        print_section("工具1：parse_resume_file")
        parsed_file = execute_tool("parse_resume_file", {"file_path": resume_path})
        assert_success("parse_resume_file", parsed_file)
        print(f"文件类型：{parsed_file['file_type']}，字符数：{parsed_file['char_count']}")
        print(parsed_file["text"][:120])

        print_section("工具2：extract_resume_info")
        resume_result = execute_tool("extract_resume_info", {"resume_text": parsed_file["text"]})
        assert_success("extract_resume_info", resume_result)
        resume_info = resume_result["resume_info"]
        print(to_pretty_json({
            "basic_info": resume_info.get("basic_info"),
            "skills": resume_info.get("skills"),
            "potential_issues": resume_info.get("potential_issues"),
        }))

        print_section("工具3：analyze_jd")
        jd_result = execute_tool("analyze_jd", {"jd_text": TEST_JD})
        assert_success("analyze_jd", jd_result)
        jd_analysis = jd_result["jd_analysis"]
        print(to_pretty_json({
            "job_title": jd_analysis.get("job_title"),
            "hard_requirements": jd_analysis.get("hard_requirements"),
            "keywords": jd_analysis.get("keywords"),
        }))

        print_section("工具4：calculate_match")
        match_result_wrapper = execute_tool("calculate_match", {
            "resume_info": resume_info,
            "jd_analysis": jd_analysis,
        })
        assert_success("calculate_match", match_result_wrapper)
        match_result = match_result_wrapper["match_result"]
        print(to_pretty_json({
            "score": match_result.get("score"),
            "score_reason": match_result.get("score_reason"),
            "missing_requirements": match_result.get("missing_requirements"),
        }))

        print_section("工具5：generate_suggestions")
        suggestions_wrapper = execute_tool("generate_suggestions", {
            "resume_info": resume_info,
            "jd_analysis": jd_analysis,
            "match_result": match_result,
        })
        assert_success("generate_suggestions", suggestions_wrapper)
        suggestions = suggestions_wrapper["suggestions"]
        print(to_pretty_json({
            "overall_strategy": suggestions.get("overall_strategy"),
            "honesty_boundaries": suggestions.get("honesty_boundaries"),
            "optimized_resume_preview": str(suggestions.get("optimized_resume", ""))[:300],
        }))

        print_section("工具6：verify_output")
        verification_wrapper = execute_tool("verify_output", {
            "resume_info": resume_info,
            "jd_analysis": jd_analysis,
            "match_result": match_result,
            "suggestions": suggestions,
        })
        assert_success("verify_output", verification_wrapper)
        verification = verification_wrapper["verification"]
        print(to_pretty_json({
            "passed": verification.get("passed"),
            "safe_to_deliver": verification.get("safe_to_deliver"),
            "required_fixes": verification.get("required_fixes"),
        }))

        print_section("工具7：recommend_jobs")
        reco_wrapper = execute_tool("recommend_jobs", {"resume_info": resume_info})
        assert_success("recommend_jobs", reco_wrapper)
        recommendations = reco_wrapper["recommendations"]
        candidates = recommendations.get("candidates") or []
        if not candidates:
            raise AssertionError("recommend_jobs 未返回候选岗位")
        print(to_pretty_json([{
            "company": c.get("company"),
            "role_title": c.get("role_title"),
            "estimated_score": c.get("estimated_score"),
        } for c in candidates]))

        print_section("工具8：ask_user")
        original_input = builtins.input
        builtins.input = lambda prompt="": "我可以补充：该项目中我主要负责数据整理和周报输出。"
        try:
            ask_result = execute_tool("ask_user", {
                "question": "请补充你在会员复购提升项目中的具体职责。",
                "context": "测试自动输入",
            })
        finally:
            builtins.input = original_input
        assert_success("ask_user", ask_result)
        print(to_pretty_json(ask_result))

        print_section("阶段2测试完成")
        print("✅ 8个工具全部可用")

    finally:
        os.unlink(resume_path)


if __name__ == "__main__":
    main()
