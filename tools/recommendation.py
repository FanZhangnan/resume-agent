"""
岗位推荐工具：用户只提供简历、没有目标JD时，
根据简历画像推荐匹配度最高的大厂岗位（实习或全职），并给出典型JD供后续深入分析。

诚实边界：推荐的是各大厂真实长期开设的岗位类型，JD为基于公开招聘要求整理的"典型岗位画像"，
不是实时在招职位——报告中必须附带提醒：投递前以公司官方最新JD为准。
"""
import config
from tools.common import ask_json
from utils import compact_text, to_pretty_json

_RECO_SCHEMA = {
    "candidates": [],
    "overall_advice": "",
    "disclaimer": "岗位画像基于各公司公开招聘要求整理，投递前请以官方最新JD为准。",
}


def recommend_jobs(resume_info, preferences=None):
    if not resume_info:
        return {"success": False, "error": "缺少简历结构化信息，请先调用extract_resume_info"}

    system = (
        "你是资深猎头顾问，深谙国内外知名大厂的校招、实习和社招岗位体系与典型招聘要求，"
        "包括：字节跳动、腾讯、阿里巴巴、华为、美团、拼多多、小红书、京东、网易、百度，"
        "以及Google、Microsoft、Amazon、Meta、Apple、Atlassian、Canva、TikTok等。"
        "只输出JSON。诚实原则：只推荐这些公司真实长期开设的岗位类型；"
        "预估匹配度必须基于简历中的具体证据，不许为了讨好而虚高；"
        "候选人明显够不着的岗位不要硬推。"
    )
    prefs_block = f"\n用户补充偏好：{preferences}\n" if preferences else ""
    prompt = f"""
请根据候选人的简历结构化信息，推荐匹配度最高的大厂岗位。要求：
1. 先判断候选人当前身份：在读学生优先推荐实习/校招岗位，有工作经验者推荐对应职级的全职岗位
2. 结合候选人所在地理位置：如在海外，需兼顾当地有办公室的国际大厂和中国大厂的海外办公室/远程/回国校招机会
3. 推荐5个岗位，按预估匹配度从高到低排序，公司尽量多样（不要5个都是同一家）
4. 每个岗位的typical_jd必须是一段完整可直接分析的典型JD文本（含岗位职责、硬性要求、加分项），
   基于该公司该类岗位长期、公开、稳定的招聘要求撰写，200-400字
5. 预估匹配度要诚实：简历中有证据的技能才算数，主要差距如实列出

输出JSON，字段：
candidates: 数组，每项包含：
  company: 公司名
  role_title: 岗位名
  job_type: "实习"或"全职"
  location: 工作地点（城市/远程/多地）
  estimated_score: 0-100的预估匹配度整数
  why_match: 为什么匹配，必须引用简历中的具体证据
  gaps: 数组，相对该岗位的主要差距
  typical_jd: 完整典型JD文本
overall_advice: 100字以内的总体投递策略
disclaimer: 一句话提醒（岗位画像基于公开招聘要求整理，投递前以官方最新JD为准）

候选人简历信息：
{compact_text(to_pretty_json(resume_info))}
{prefs_block}"""

    result = ask_json(prompt, system, _RECO_SCHEMA, temperature=0.3,
                      label="搜寻匹配的大厂岗位并按匹配度排序", max_tokens=config.REPORT_MAX_TOKENS)
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试recommend_jobs"}

    candidates = [c for c in (result.get("candidates") or []) if isinstance(c, dict)]
    if not candidates:
        return {"success": False, "error": "未能生成有效的岗位推荐，请重试recommend_jobs"}
    candidates.sort(key=lambda c: c.get("estimated_score") or 0, reverse=True)
    result["candidates"] = candidates
    return {"success": True, "recommendations": result}
