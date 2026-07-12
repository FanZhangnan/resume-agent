import json

from tools.analysis import calculate_match, generate_suggestions
from tools.file_parser import parse_resume_file
from tools.interaction import ask_user
from tools.recommendation import recommend_jobs
from tools.resume_tools import analyze_jd, extract_resume_info
from tools.verification import verify_output


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "parse_resume_file",
            "description": "读取PDF、Word或txt简历文件，并提取纯文本内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "简历文件路径，支持.pdf、.docx、.txt、.md"}
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_resume_info",
            "description": "将简历文本结构化为JSON，提取教育、工作、技能、项目，并标注潜在问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resume_text": {"type": "string", "description": "简历纯文本"}
                },
                "required": ["resume_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_jd",
            "description": "分析职位JD，提取硬性要求、加分项、隐含要求、关键词和风险点。",
            "parameters": {
                "type": "object",
                "properties": {
                    "jd_text": {"type": "string", "description": "职位JD文本"}
                },
                "required": ["jd_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_match",
            "description": "对比结构化简历与JD分析，输出匹配分类清单、评分和依据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resume_info": {"type": "object", "description": "extract_resume_info返回的resume_info对象（可传空对象，系统会自动注入完整数据）"},
                    "jd_analysis": {"type": "object", "description": "analyze_jd返回的jd_analysis对象（可传空对象，系统会自动注入完整数据）"},
                    "preferences": {"type": "string", "description": "可选。用户明确表达的求职偏好，仅作证据传递，不从自由文本推断门槛"},
                },
                "required": ["resume_info", "jd_analysis"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_suggestions",
            "description": "基于简历、JD和匹配分析，用STAR法则生成优化建议和优化版简历。自我验证未通过时，把required_fixes作为fix_instructions传入以生成修正版。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resume_info": {"type": "object", "description": "结构化简历信息（可传空对象，系统会自动注入完整数据）"},
                    "jd_analysis": {"type": "object", "description": "JD分析结果（可传空对象，系统会自动注入完整数据）"},
                    "match_result": {"type": "object", "description": "calculate_match返回的match_result对象（可传空对象，系统会自动注入完整数据）"},
                    "fix_instructions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选。自我验证未通过时必须修复的问题清单，修正轮传入",
                    },
                },
                "required": ["resume_info", "jd_analysis", "match_result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_output",
            "description": "用批判性提示词审查优化产出，检查过度美化、编造、逻辑矛盾和强行匹配。必须在生成最终报告前调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resume_info": {"type": "object", "description": "结构化简历信息（可传空对象，系统会自动注入完整数据）"},
                    "jd_analysis": {"type": "object", "description": "JD分析结果（可传空对象，系统会自动注入完整数据）"},
                    "match_result": {"type": "object", "description": "匹配分析结果（可传空对象，系统会自动注入完整数据）"},
                    "suggestions": {"type": "object", "description": "优化建议和优化版简历（可传空对象，系统会自动注入完整数据）"},
                },
                "required": ["resume_info", "jd_analysis", "match_result", "suggestions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_jobs",
            "description": "用户没有提供目标JD时使用：根据简历画像推荐匹配度最高的大厂岗位（实习或全职），按预估匹配度排序，每个岗位附带完整典型JD。推荐后应对排名第一的岗位继续标准分析流程。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resume_info": {"type": "object", "description": "结构化简历信息（可传空对象，系统会自动注入完整数据）"},
                    "preferences": {"type": "string", "description": "可选。用户明确表达过的偏好（城市、方向、公司类型等）"},
                },
                "required": ["resume_info"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "当关键信息不足时，暂停Agent循环并向用户提问。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要询问用户的问题"},
                    "context": {"type": "string", "description": "提问背景，可选"},
                },
                "required": ["question"],
            },
        },
    },
]


_TOOL_FUNCTIONS = {
    "parse_resume_file": parse_resume_file,
    "extract_resume_info": extract_resume_info,
    "analyze_jd": analyze_jd,
    "calculate_match": calculate_match,
    "generate_suggestions": generate_suggestions,
    "verify_output": verify_output,
    "recommend_jobs": recommend_jobs,
    "ask_user": ask_user,
}


def get_tool_definitions():
    return TOOL_DEFINITIONS


def get_tool_function(tool_name):
    return _TOOL_FUNCTIONS.get(tool_name)


def execute_tool(tool_name, arguments):
    function = get_tool_function(tool_name)
    if function is None:
        return {"success": False, "error": f"未知工具：{tool_name}"}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as error:
            return {"success": False, "error": f"工具参数不是合法JSON：{error}", "raw_arguments": arguments}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"success": False, "error": "工具参数必须是对象", "raw_arguments": arguments}
    try:
        return function(**arguments)
    except Exception as error:
        if getattr(error, "is_run_deadline", False):
            raise
        return {"success": False, "error": str(error), "tool_name": tool_name}
