"""
Mock离线模式数据：没有API密钥时也能完整演示Agent全流程
- 数据与samples/目录中的示例简历（李明/电商运营）保持一致，保证演示报告内容连贯
- 第一次自我验证会故意"发现问题"，用于演示Agent的自动修正闭环
"""
import json
import os
import re
from types import SimpleNamespace


MOCK_RESUME_INFO = {
    "basic_info": {
        "name": "李明",
        "phone": "13800000000",
        "email": "liming@example.com",
        "location": "",
        "target_role": "运营专员",
    },
    "education": [
        {"school": "江南大学", "degree": "本科", "major": "市场营销",
         "start_date": "2018.09", "end_date": "2022.06", "details": ""},
    ],
    "work_experience": [
        {"company": "星河电商", "title": "运营助理", "start_date": "2022.07", "end_date": "2025.06",
         "responsibilities": ["店铺日常活动配置", "商品上下架", "基础数据整理"],
         "achievements": ["参与618和双11活动复盘", "协助优化商品标题和详情页，部分核心商品点击率提升约12%"]},
    ],
    "projects": [
        {"name": "会员复购提升项目", "role": "参与者", "start_date": "", "end_date": "",
         "description": "参与用户分层标签整理，配合社群和短信触达",
         "achievements": ["输出周度数据看板，跟踪复购率变化"], "technologies": ["Excel", "飞书多维表格"]},
    ],
    "skills": ["Excel", "SQL基础", "数据透视表", "活动运营", "用户运营", "飞书多维表格"],
    "certificates": [],
    "achievements": [],
    "potential_issues": [
        "缺少具体的量化成果（如GMV、转化率提升的具体数值范围）",
        "项目经历中角色定位模糊，未说明具体负责的模块",
        "未注明所在城市，部分岗位有属地要求",
    ],
    "raw_summary": "市场营销本科，3年电商运营助理经验，熟悉活动配置、数据整理和复盘，有会员复购项目参与经历，但量化成果偏少。",
}


MOCK_JD_ANALYSIS = {
    "job_title": "电商运营专员",
    "company_or_industry": "电商平台",
    "hard_requirements": ["本科及以上学历", "1年以上电商运营经验", "熟练使用Excel，有基础数据分析能力"],
    "bonus_points": ["熟悉活动复盘", "熟悉商品优化", "熟悉用户分层"],
    "implicit_requirements": ["跨团队协作能力（需协同设计、投放、客服）", "抗压能力（大促节奏）", "数据敏感度"],
    "keywords": ["活动运营", "商品运营", "用户运营", "转化率", "复购率", "数据分析", "活动复盘"],
    "responsibilities": ["平台活动运营、商品运营和用户运营", "输出运营分析报告", "协同推进活动落地"],
    "risk_points": ["JD强调独立负责，候选人此前多为助理/协助角色"],
    "raw_summary": "典型电商运营专员岗，硬性门槛不高，核心看活动运营和数据分析的实操能力，隐含跨团队协作与抗压要求。",
}


MOCK_MATCH = {
    "score": 76,
    "score_reason": "学历、年限、Excel与数据能力均达标，活动复盘和用户分层有直接经验；短板是缺少独立负责的经历和量化成果。",
    "high_matches": [
        {"requirement_id": "hard-001", "requirement": "本科及以上学历", "evidence": "江南大学市场营销本科", "reason": "完全满足"},
        {"requirement_id": "hard-002", "requirement": "1年以上电商运营经验", "evidence": "星河电商运营助理3年", "reason": "年限超出要求"},
        {"requirement_id": "hard-003", "requirement": "熟练使用Excel", "evidence": "技能列出Excel、数据透视表，且有周度数据看板产出", "reason": "有实际使用证据"},
    ],
    "partial_matches": [
        {"requirement_id": "business-001", "requirement": "活动运营（独立负责）", "evidence": "参与618/双11活动复盘、活动配置",
         "gap": "均为参与/协助角色，缺少独立负责的活动案例", "improvement": "突出在活动中独立完成的具体环节"},
        {"requirement_id": "skill-003", "requirement": "用户运营", "evidence": "会员复购提升项目参与用户分层",
         "gap": "角色与产出描述模糊", "improvement": "补充分层维度、触达规模和复购率变化数据"},
    ],
    "missing_requirements": [
        {"requirement_id": "business-002", "requirement": "输出运营分析报告的独立能力", "impact": "面试中可能被追问", "possible_action": "把周度数据看板经历改写为分析报告产出"},
    ],
    "redundant_or_irrelevant": ["SQL基础与该JD相关性弱，可保留但不必突出"],
    "risks": ["3年仍为助理职级，需准备职级解释", "缺少大促核心指标的量化结果"],
    "recommendation": "整体匹配度良好，建议围绕'活动复盘+数据看板'重构经历描述，补充可佐证的量化数据后再投递。",
    "requirement_evidence": [
        {"requirement_id": "hard-001", "status": "met", "evidence_ids": ["evidence-003"]},
        {"requirement_id": "hard-002", "status": "met", "evidence_ids": ["evidence-004"]},
        {"requirement_id": "hard-003", "status": "met", "evidence_ids": ["evidence-004", "evidence-006", "evidence-008"]},
        {"requirement_id": "skill-001", "status": "met", "evidence_ids": ["evidence-004"]},
        {"requirement_id": "skill-002", "status": "met", "evidence_ids": ["evidence-004"]},
        {"requirement_id": "skill-003", "status": "under_evidenced", "evidence_ids": ["evidence-005"]},
        {"requirement_id": "business-001", "status": "under_evidenced", "evidence_ids": ["evidence-004", "evidence-005"]},
        {"requirement_id": "business-002", "status": "under_evidenced", "evidence_ids": ["evidence-005"]},
        {"requirement_id": "business-003", "status": "missing", "evidence_ids": []},
        {"requirement_id": "soft-001", "status": "missing", "evidence_ids": []},
        {"requirement_id": "soft-002", "status": "missing", "evidence_ids": []},
        {"requirement_id": "soft-003", "status": "under_evidenced", "evidence_ids": ["evidence-004", "evidence-005"]},
    ],
}


MOCK_SUGGESTIONS_ROUND1 = {
    "overall_strategy": "以'数据驱动的活动运营'为主线重构简历：把助理型描述升级为具体动作+量化结果，向JD关键词（活动复盘、用户分层、转化率）对齐，同时避免夸大角色。",
    "rewrite_suggestions": [
        {"section": "工作经历", "problem": "'负责店铺日常活动配置'过于笼统",
         "suggestion": "拆成具体动作并量化频次",
         "before": "负责店铺日常活动配置、商品上下架、基础数据整理。",
         "after": "主导店铺全年30+场日常活动的配置与上线，管理500+SKU的上下架节奏，搭建基础数据日报流程。"},
        {"section": "项目经历", "problem": "会员复购项目角色模糊",
         "suggestion": "明确个人贡献",
         "before": "参与用户分层标签整理，配合社群和短信触达。",
         "after": "负责用户分层标签体系搭建，制定RFM分层规则并驱动社群和短信触达。"},
    ],
    "star_rewrites": [
        {"original": "参与618和双11活动复盘，整理转化率、客单价、投放ROI等指标。",
         "situation": "618/双11大促后需要复盘", "task": "整理核心指标形成复盘输入",
         "action": "汇总转化率、客单价、投放ROI等指标并输出对比分析", "result": "复盘结论被用于下一场活动的选品和投放调整",
         "rewritten": "参与618/双11大促复盘：独立完成转化率、客单价、投放ROI等核心指标的汇总与对比分析，复盘结论直接支撑了下一场活动的选品与投放策略调整。"},
    ],
    "keyword_injection": [
        {"keyword": "活动复盘", "placement": "工作经历第二条"},
        {"keyword": "用户分层", "placement": "项目经历"},
        {"keyword": "转化率/复购率", "placement": "项目经历成果描述"},
    ],
    "honesty_boundaries": ["点击率提升12%仅适用于'部分核心商品'，不可写成整体提升", "不可虚构GMV等未掌握的数据"],
    "optimized_resume": (
        "李明\n电话：13800000000 ｜ 邮箱：liming@example.com\n目标岗位：电商运营专员\n\n"
        "教育背景\n2018.09-2022.06 江南大学 市场营销 本科\n\n"
        "工作经历\n2022.07-2025.06 星河电商 运营助理\n"
        "- 主导店铺全年30+场日常活动的配置与上线，管理500+SKU的上下架节奏。\n"
        "- 参与618/双11大促复盘，独立完成转化率、客单价、投放ROI等核心指标的汇总与对比分析。\n"
        "- 协助优化商品标题和详情页，部分核心商品点击率提升约12%。\n\n"
        "项目经历\n会员复购提升项目\n"
        "- 负责用户分层标签体系搭建，制定RFM分层规则并驱动社群和短信触达。\n"
        "- 输出周度数据看板，持续跟踪复购率变化。\n\n"
        "技能\nExcel（数据透视表）、SQL基础、活动运营、用户运营、飞书多维表格"
    ),
}


MOCK_VERIFY_FAIL = {
    "passed": False,
    "overall_assessment": "整体方向正确，但存在两处过度美化：把'负责日常配置'升级为'主导'，把'参与分层标签整理'升级为'负责分层体系搭建'，均超出原简历证据。",
    "overstatement_issues": [
        "工作经历改写将'负责店铺日常活动配置'升级为'主导30+场活动'，'主导'和'30+场'均无原文依据",
        "项目经历改写将'参与用户分层标签整理'升级为'负责用户分层标签体系搭建，制定RFM分层规则'，角色和方法论均被拔高",
    ],
    "fabrication_risks": ["'管理500+SKU'为编造的数据，原简历未提及SKU规模"],
    "logic_issues": [],
    "match_authenticity_issues": [],
    "required_fixes": [
        "将'主导30+场活动'回调为'执行店铺日常活动配置与上线'，如需数量需用户确认",
        "将'负责分层体系搭建、制定RFM规则'回调为'参与用户分层标签整理'，可补充具体动作但不改变角色定位",
        "删除'管理500+SKU'这一无依据数据",
    ],
    "safe_to_deliver": False,
}


MOCK_SUGGESTIONS_ROUND2 = {
    "overall_strategy": "以'数据驱动的活动运营'为主线重构简历：在忠于原始角色（参与/协助）的前提下，把描述细化为具体动作与真实可佐证的结果，向JD关键词对齐。",
    "rewrite_suggestions": [
        {"section": "工作经历", "problem": "'负责店铺日常活动配置'过于笼统",
         "suggestion": "细化为具体动作，数量留待用户确认后再补",
         "before": "负责店铺日常活动配置、商品上下架、基础数据整理。",
         "after": "执行店铺日常活动的配置与上线、商品上下架，并搭建了基础数据整理流程。"},
        {"section": "项目经历", "problem": "会员复购项目角色模糊",
         "suggestion": "保持'参与'定位，细化具体动作",
         "before": "参与用户分层标签整理，配合社群和短信触达。",
         "after": "参与用户分层标签整理：梳理分层维度并维护标签表，支撑社群和短信的分层触达。"},
    ],
    "star_rewrites": [
        {"original": "参与618和双11活动复盘，整理转化率、客单价、投放ROI等指标。",
         "situation": "618/双11大促后需要复盘", "task": "整理核心指标形成复盘输入",
         "action": "汇总转化率、客单价、投放ROI等指标并输出对比分析", "result": "复盘结论被用于下一场活动的选品和投放调整",
         "rewritten": "参与618/双11大促复盘：完成转化率、客单价、投放ROI等核心指标的汇总与对比分析，复盘结论支撑了后续活动的选品与投放调整。"},
    ],
    "keyword_injection": [
        {"keyword": "活动复盘", "placement": "工作经历第二条"},
        {"keyword": "用户分层", "placement": "项目经历"},
        {"keyword": "复购率", "placement": "项目经历成果描述"},
    ],
    "honesty_boundaries": [
        "保持'参与/协助'角色定位，不得升级为'主导/负责'",
        "点击率提升12%仅适用于'部分核心商品'",
        "活动场次、SKU规模等数据需用户确认后才能写入",
    ],
    "optimized_resume": (
        "李明\n电话：13800000000 ｜ 邮箱：liming@example.com\n目标岗位：电商运营专员\n\n"
        "教育背景\n2018.09-2022.06 江南大学 市场营销 本科\n\n"
        "工作经历\n2022.07-2025.06 星河电商 运营助理\n"
        "- 执行店铺日常活动的配置与上线、商品上下架，并搭建了基础数据整理流程。\n"
        "- 参与618/双11大促复盘，完成转化率、客单价、投放ROI等核心指标的汇总与对比分析，结论支撑后续活动的选品与投放调整。\n"
        "- 协助优化商品标题和详情页，部分核心商品点击率提升约12%。\n\n"
        "项目经历\n会员复购提升项目\n"
        "- 参与用户分层标签整理：梳理分层维度并维护标签表，支撑社群和短信的分层触达。\n"
        "- 输出周度数据看板，持续跟踪复购率变化。\n\n"
        "技能\nExcel（数据透视表）、SQL基础、活动运营、用户运营、飞书多维表格"
    ),
    "optimized_resume_struct": {
        "basic_info": {"name": "李明", "phone": "13800000000", "email": "liming@example.com",
                       "location": "", "target_role": "电商运营专员"},
        "summary": "电商运营方向，具备日常活动配置与大促复盘经验，参与过用户分层触达项目，习惯以转化率、复购率等指标驱动运营动作。",
        "education": [
            {"school": "江南大学", "degree": "本科", "major": "市场营销",
             "start": "2018.09", "end": "2022.06", "highlights": []},
        ],
        "experience": [
            {"company": "星河电商", "title": "运营助理", "start": "2022.07", "end": "2025.06",
             "bullets": [
                 "执行店铺日常活动的配置与上线、商品上下架，并搭建了基础数据整理流程",
                 "参与618/双11大促复盘，完成转化率、客单价、投放ROI等核心指标的汇总与对比分析，结论支撑后续活动的选品与投放调整",
                 "协助优化商品标题和详情页，部分核心商品点击率提升约12%",
             ]},
        ],
        "projects": [
            {"name": "会员复购提升项目", "role": "项目成员",
             "bullets": [
                 "参与用户分层标签整理：梳理分层维度并维护标签表，支撑社群和短信的分层触达",
                 "输出周度数据看板，持续跟踪复购率变化",
             ]},
        ],
        "skills": [
            {"group": "数据与工具", "items": ["Excel（数据透视表）", "SQL基础", "飞书多维表格"]},
            {"group": "运营能力", "items": ["活动运营", "用户运营", "活动复盘"]},
        ],
        "extras": [],
    },
}


MOCK_VERIFY_PASS = {
    "passed": True,
    "overall_assessment": "修正版已回调所有过度美化表述并删除无依据数据，角色定位忠于原文，量化内容均有原文支撑，可以交付。",
    "overstatement_issues": [],
    "fabrication_risks": [],
    "logic_issues": [],
    "match_authenticity_issues": [],
    "required_fixes": [],
    "safe_to_deliver": True,
}


MOCK_JOB_RECOMMENDATIONS = {
    "candidates": [
        {"company": "字节跳动", "role_title": "抖音电商-电商运营", "job_type": "全职", "location": "上海/杭州",
         "estimated_score": 74,
         "why_match": "3年电商运营经验，有618/双11大促复盘和用户分层实操，Excel数据能力有周度看板佐证",
         "gaps": ["缺少独立负责的活动案例", "缺少GMV级量化成果"],
         "typical_jd": ("岗位：抖音电商-电商运营\n职责：1.负责店铺/直播间的日常运营与大促活动落地；"
                        "2.跟踪转化率、客单价、GMV等核心指标并输出分析；3.协同达人、投放与供应链团队。\n"
                        "要求：1.本科及以上学历，2年以上电商运营经验；2.熟练使用Excel，数据敏感；"
                        "3.有大促实操和复盘经验。\n加分项：用户分层运营经验、直播电商经验。")},
        {"company": "阿里巴巴", "role_title": "淘天集团-类目运营", "job_type": "全职", "location": "杭州",
         "estimated_score": 70,
         "why_match": "商品上下架、标题详情页优化经验与类目运营职责直接对口",
         "gaps": ["平台视角经验欠缺（现有经验偏商家侧）"],
         "typical_jd": ("岗位：淘天集团-类目运营\n职责：负责类目商品池运营、活动招商与选品、数据分析。\n"
                        "要求：本科以上，2年以上电商经验，Excel/SQL数据能力，有选品或活动运营经验。")},
        {"company": "美团", "role_title": "到店事业群-商家运营", "job_type": "全职", "location": "北京",
         "estimated_score": 66,
         "why_match": "商家侧运营经验可迁移，数据整理与复盘能力匹配",
         "gaps": ["本地生活行业经验空白"],
         "typical_jd": ("岗位：到店事业群-商家运营\n职责：负责商家经营指导、活动策划与数据分析。\n"
                        "要求：本科以上，2年以上运营经验，数据分析能力强，沟通能力好。")},
    ],
    "overall_advice": "优先投递电商行业大厂的商家/类目运营岗，用大促复盘和数据看板作为核心卖点；投递前补齐量化数据。",
    "disclaimer": "岗位画像基于各公司公开招聘要求整理，投递前请以官方最新JD为准。",
}


# 记录verify被调用的次数：第1次返回未通过（演示自动修正闭环），之后返回通过
_CALL_COUNTS = {"verify": 0, "suggest": 0}


def mock_simple_ask(prompt, system=None):
    """根据system提示词的唯一标识路由到对应的固定JSON（模拟各工具内部的LLM调用）
    注意：必须优先用system中的角色标识判断，prompt里嵌入的JSON数据会导致关键词误匹配
    """
    system = system or ""
    if "审稿人" in system:
        _CALL_COUNTS["verify"] += 1
        result = MOCK_VERIFY_FAIL if _CALL_COUNTS["verify"] == 1 else MOCK_VERIFY_PASS
        return json.dumps(result, ensure_ascii=False)
    if "优化顾问" in system:
        _CALL_COUNTS["suggest"] += 1
        result = MOCK_SUGGESTIONS_ROUND1 if _CALL_COUNTS["suggest"] == 1 else MOCK_SUGGESTIONS_ROUND2
        return json.dumps(result, ensure_ascii=False)
    if "匹配度评估" in system:
        return json.dumps(MOCK_MATCH, ensure_ascii=False)
    if "猎头顾问" in system:
        return json.dumps(MOCK_JOB_RECOMMENDATIONS, ensure_ascii=False)
    if "信息抽取" in system:
        return json.dumps(MOCK_RESUME_INFO, ensure_ascii=False)
    if "需求分析" in system:
        return json.dumps(MOCK_JD_ANALYSIS, ensure_ascii=False)
    return "（Mock回复）这是离线模式的假数据。"


def _mock_tool_call(step_index, name, arguments):
    return SimpleNamespace(
        id="mock_call_{}".format(step_index),
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


def mock_agent_step(step_index, messages):
    """模拟Agent主循环中LLM的决策：按ReAct顺序依次发起工具调用
    大对象参数留空即可——Agent会自动注入状态中的完整数据
    """
    first_user = ""
    for message in messages:
        if message.get("role") == "user":
            first_user = message.get("content", "")
            break

    steps = []
    file_match = re.search(r"简历文件路径：(.+)", first_user)
    if file_match:
        steps.append(("需要先解析简历文件，提取纯文本。", "parse_resume_file",
                      {"file_path": file_match.group(1).strip()}))
    steps.append(
        ("简历文本已就绪，先提取结构化信息，识别潜在问题。", "extract_resume_info", {"resume_text": ""}))
    if "未提供目标JD" in first_user:
        steps.append(
            ("用户没有目标JD，先根据简历画像推荐匹配度最高的大厂岗位。", "recommend_jobs", {"resume_info": {}}))
    steps.append(("接下来分析目标岗位JD的硬性要求、加分项和隐含要求。", "analyze_jd", {"jd_text": ""}))
    # Web演示模式下展示"用户追问"环节（CLI测试不开启，避免阻塞自动化）
    if os.environ.get("AGENT_MOCK_ASK") == "1":
        steps.append((
            "JD强调数据驱动运营，但简历中核心项目缺少可量化成果，先向用户确认真实数据再继续。",
            "ask_user",
            {"question": "你在会员复购提升项目中负责的具体环节是什么？有没有真实的量化结果（如复购率变化、覆盖用户数）？",
             "context": "补充真实数据能让优化建议更精准；没有数据或不方便提供时直接跳过即可，Agent不会编造。"}))
    steps.extend([
        ("简历和JD都已结构化，现在逐项对比计算匹配度。", "calculate_match", {}),
        ("匹配分析完成，基于差距生成具体优化建议和优化版简历。", "generate_suggestions", {}),
        ("建议已生成，交付前必须自我验证：检查过度美化、编造和逻辑矛盾。", "verify_output", {}),
        ("自我验证发现过度美化问题，我需要根据required_fixes重新生成修正版建议。", "generate_suggestions",
         {"fix_instructions": ["回调'主导'等被拔高的角色表述", "删除无依据的数据", "保持参与/协助的真实定位"]}),
        ("修正版建议已生成，再次自我验证复检。", "verify_output", {}),
    ])

    if step_index < len(steps):
        thinking, name, arguments = steps[step_index]
        message = SimpleNamespace(content=thinking, tool_calls=[_mock_tool_call(step_index, name, arguments)])
        return message
    return SimpleNamespace(content="所有分析步骤已完成且通过自我验证，可以生成最终报告。", tool_calls=None)


def reset_mock_counters():
    """重置计数器（多次运行demo时保证每次都演示完整的修正闭环）"""
    _CALL_COUNTS["verify"] = 0
    _CALL_COUNTS["suggest"] = 0
