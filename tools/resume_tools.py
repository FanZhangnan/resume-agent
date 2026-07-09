import config
from tools.common import ask_json
from utils import clip_text

_RESUME_SCHEMA = {
    "basic_info": {"name": "", "phone": "", "email": "", "location": "", "target_role": ""},
    "education": [],
    "work_experience": [],
    "projects": [],
    "skills": [],
    "certificates": [],
    "achievements": [],
    "potential_issues": [],
    "raw_summary": "",
}


_JD_SCHEMA = {
    "job_title": "",
    "company_or_industry": "",
    "hard_requirements": [],
    "bonus_points": [],
    "implicit_requirements": [],
    "keywords": [],
    "responsibilities": [],
    "risk_points": [],
    "raw_summary": "",
}


def extract_resume_info(resume_text):
    if not str(resume_text or "").strip():
        return {
            "success": False,
            "error": "简历文本为空。如果用户提供的是文件路径，请先调用parse_resume_file解析出文本。",
        }
    system = "你是严谨的简历信息抽取专家。只输出JSON，不要输出解释。不得编造简历中没有的信息；缺失信息用空字符串或空数组表示。"
    prompt = f"""
请将下面的简历文本结构化为JSON，字段必须包含：
basic_info: name, phone, email, location, target_role
education: 数组，每项包含 school, degree, major, start_date, end_date, details
work_experience: 数组，每项包含 company, title, start_date, end_date, responsibilities, achievements
projects: 数组，每项包含 name, role, start_date, end_date, description, achievements, technologies
skills: 数组
certificates: 数组
achievements: 数组
potential_issues: 数组，指出数据缺失、表述模糊、缺少量化成果、时间线不清等问题
raw_summary: 150字以内总结

简历文本：
{clip_text(resume_text, max_chars=12000)}
"""
    # 长简历的结构化JSON输出可能超过默认上限，给大token预算
    result = ask_json(prompt, system, _RESUME_SCHEMA, temperature=0.1,
                      label="提取简历结构化信息", max_tokens=config.REPORT_MAX_TOKENS)
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试extract_resume_info"}
    return {"success": True, "resume_info": result}


def analyze_jd(jd_text):
    if not str(jd_text or "").strip():
        return {"success": False, "error": "JD文本为空，无法分析。"}
    system = "你是资深招聘需求分析专家。只输出JSON，不要输出解释。要求区分硬性要求、加分项和隐含要求。"
    prompt = f"""
请分析下面的职位JD并输出JSON，字段必须包含：
job_title: 岗位名称
company_or_industry: 公司或行业线索
hard_requirements: 数组，必须满足的学历、年限、技能、经验要求
bonus_points: 数组，加分但非硬性的要求
implicit_requirements: 数组，从职责中推断出的隐含能力要求
keywords: 数组，ATS或HR筛选关键词
responsibilities: 数组，岗位核心职责
risk_points: 数组，候选人容易忽略或容易错配的点
raw_summary: 150字以内总结

JD文本：
{clip_text(jd_text, max_chars=6000)}
"""
    result = ask_json(prompt, system, _JD_SCHEMA, temperature=0.1, label="分析职位JD要求")
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试analyze_jd"}
    return {"success": True, "jd_analysis": result}
