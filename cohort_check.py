"""用明确的JD届别与已解析教育日期提示差异，不猜测企业招聘窗口。"""

import re


def job_grounded_risks(jd_text, risks):
    """JD未提及成绩时，不把模型自行推断的GPA门槛呈现为岗位风险。"""
    has_grade_requirement = bool(re.search(r"gpa|绩点|成绩", str(jd_text or ""), re.I))
    return [risk for risk in risks if isinstance(risk, str) and (
        has_grade_requirement or not re.search(r"gpa|绩点", risk, re.I)
    )] if isinstance(risks, list) else []


def graduation_warning(jd_text, resume_info):
    cohorts = []
    for match in re.finditer(r"((?:20\d{2}\s*(?:[/、,，及和\-–]\s*)?)+)\s*届", str(jd_text or "")):
        cohorts.extend(int(year) for year in re.findall(r"20\d{2}", match.group(1)))
    cohorts = sorted(set(cohorts))
    if not cohorts:
        return "", False
    education = resume_info.get("education") if isinstance(resume_info, dict) else []
    years = []
    unknown = False
    for item in education if isinstance(education, list) else []:
        if not isinstance(item, dict):
            continue
        end = str(item.get("end_date") or "")
        match = re.search(r"(?:19|20)\d{2}", end)
        if match:
            years.append(int(match.group()))
        else:
            unknown = True
    required = "/".join(map(str, cohorts))
    if not years or unknown:
        return f"JD标示{required}届；部分教育记录未提供明确毕业年份，请核对应届资格，不能由匹配分数推断。", False
    latest = max(years)
    if latest in cohorts:
        return "", False
    return (f"JD标示{required}届，简历列出的最近毕业年份为{latest}，存在年份差异。"
            "请核对企业实际应届招聘窗口或例外政策；保留真实学校与毕业日期。"), True
