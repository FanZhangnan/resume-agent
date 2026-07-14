import json
import re


def extract_json_text(text):
    if text is None:
        return ""
    text = str(text).strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [idx for idx in [start_obj, start_arr] if idx != -1]
    if starts:
        start = min(starts)
        end_char = "}" if text[start] == "{" else "]"
        end = text.rfind(end_char)
        if end != -1 and end >= start:
            text = text[start:end + 1]
    return text.strip()


def _remove_trailing_commas(text):
    return re.sub(r",\s*([}\]])", r"\1", text)


def _normalize_quotes(text):
    replacements = {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def parse_json_safely(text, default=None):
    if default is None:
        default = {}
    raw = extract_json_text(text)
    if not raw:
        return default
    candidates = [raw]
    normalized = _remove_trailing_commas(_normalize_quotes(raw))
    if normalized != raw:
        candidates.append(normalized)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    repaired = normalized.replace("\n", " ")
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return default


def to_pretty_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2)


def compact_text(text, max_chars=12000):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[内容过长已截断]"


def clip_text(text, max_chars=12000):
    """只做长度截断、保留换行结构（用于简历/JD这类格式敏感的原文）"""
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[内容过长已截断]"


_SECTION_MAP = {
    "个人简介": "summary", "自我评价": "summary", "简介": "summary",
    "教育背景": "education", "教育经历": "education",
    "工作经历": "experience", "工作经验": "experience", "实习经历": "experience",
    "项目经验": "projects", "项目经历": "projects",
    "核心技能": "skills", "技能": "skills", "专业技能": "skills",
    "其他": "extras", "证书": "extras", "奖项": "extras", "语言": "extras",
}

_PERIOD_RE = re.compile(r"[（(]?\s*(\d{4}(?:\.\d{1,2})?)\s*[-–~至]+\s*(\d{4}(?:\.\d{1,2})?|至今|在读|现在)\s*[)）]?")


def parse_resume_text_to_struct(text):
    """确定性兜底：把纯文本简历尽力解析为结构化数据（识别【段落】标题、｜分隔头行、-要点）
    解析不出有效结构时返回{}，调用方据此继续降级
    """
    text = str(text or "").strip()
    if not text:
        return {}
    struct = {"basic_info": {}, "summary": "", "education": [],
              "experience": [], "projects": [], "skills": [], "extras": []}
    lines = [line.strip() for line in text.splitlines()]
    section, head_done, entry = None, False, None
    basic_fields = {
        "姓名": "name", "电话": "phone", "手机": "phone",
        "邮箱": "email", "邮件": "email", "所在地": "location",
        "城市": "location", "目标岗位": "target_role",
        "求职意向": "target_role",
    }

    def _new_entry(line, keys):
        period = _PERIOD_RE.search(line)
        start, end = (period.group(1), period.group(2)) if period else ("", "")
        clean = _PERIOD_RE.sub("", line).strip(" ｜|（()）")
        parts = [p.strip() for p in re.split(r"[｜|]", clean) if p.strip()]
        item = {k: (parts[i] if i < len(parts) else "") for i, k in enumerate(keys)}
        if start:
            item["start"], item["end"] = start, end
        item["bullets"] = []
        return item

    for line in lines:
        if not line:
            continue
        field_match = re.match(r"^([^:：]+)[:：]\s*(.*)$", line)
        if section is None and field_match and field_match.group(1) in basic_fields:
            struct["basic_info"][basic_fields[field_match.group(1)]] = (
                field_match.group(2).strip()
            )
            continue

        bracket_header = re.match(r"^【(.+?)】$", line)
        section_name = bracket_header.group(1) if bracket_header else None
        section_body = ""
        if section_name is None and line in _SECTION_MAP:
            section_name = line
        elif section_name is None and field_match and field_match.group(1) in _SECTION_MAP:
            section_name = field_match.group(1)
            section_body = field_match.group(2).strip()
        if section_name in _SECTION_MAP:
            section, entry = _SECTION_MAP[section_name], None
            head_done = True
            if not section_body:
                continue
            line = (
                f"{section_name}：{section_body}"
                if section == "skills" else section_body
            )
        if not head_done and section is None:
            basic = struct["basic_info"]
            if line.startswith("求职意向：") or line.startswith("目标岗位："):
                basic["target_role"] = line.split("：", 1)[1].strip()
            elif "@" in line or re.search(r"\d{7,}", line):
                for part in re.split(r"[｜|]", line):
                    part = part.strip()
                    if "@" in part:
                        basic["email"] = part
                    elif re.search(r"\d{7,}", part):
                        basic["phone"] = part
                    elif part:
                        basic["location"] = part
            elif not basic.get("name") and len(line) <= 20:
                basic["name"] = line
            continue
        if section == "summary":
            struct["summary"] = (struct["summary"] + " " + line).strip()
        elif section in ("education", "experience", "projects"):
            if line.startswith(("-", "•", "·")):
                if entry is not None:
                    entry["bullets"].append(line.lstrip("-•· ").strip())
            else:
                keys = {"education": ("school", "degree", "major"),
                        "experience": ("company", "title"),
                        "projects": ("name", "role")}[section]
                entry = _new_entry(line, keys)
                struct[section].append(entry)
        elif section == "skills":
            body = line.lstrip("-•· ").strip()
            if "：" in body:
                group, _, items = body.partition("：")
                struct["skills"].append({"group": group.strip(),
                                         "items": [s.strip() for s in re.split(r"[、,，;；]", items) if s.strip()]})
            elif body:
                struct["skills"].append(body)
        elif section == "extras":
            struct["extras"].append(line.lstrip("-•· ").strip())

    has_content = any(struct[k] for k in ("education", "experience", "projects"))
    if not (struct["basic_info"].get("name") or has_content):
        return {}
    return struct


def render_resume_text(struct):
    """把结构化优化简历渲染成可读的纯文本简历（报告与CLI使用；Web端另有排版模板）"""
    if not isinstance(struct, dict) or not struct:
        return ""
    lines = []
    basic = struct.get("basic_info") or {}
    if basic.get("name"):
        lines.append(str(basic["name"]))
    contact = " ｜ ".join(str(basic[k]) for k in ("phone", "email", "location") if basic.get(k))
    if contact:
        lines.append(contact)
    if basic.get("target_role"):
        lines.append(f"求职意向：{basic['target_role']}")
    if struct.get("summary"):
        lines.extend(["", "【个人简介】", str(struct["summary"])])

    def _period(item):
        start, end = item.get("start") or "", item.get("end") or ""
        return f"{start} - {end}".strip(" -")

    def _entry(items, title, head_keys):
        if not items:
            return
        lines.extend(["", f"【{title}】"])
        for item in items:
            if not isinstance(item, dict):
                lines.append(f"- {item}")
                continue
            head = " ｜ ".join(str(item[k]) for k in head_keys if item.get(k))
            period = _period(item)
            lines.append(f"{head}（{period}）" if period else head)
            for point in (item.get("bullets") or item.get("highlights") or []):
                lines.append(f"- {point}")

    _entry(struct.get("education") or [], "教育背景", ("school", "degree", "major"))
    _entry(struct.get("experience") or [], "工作经历", ("company", "title"))
    _entry(struct.get("projects") or [], "项目经验", ("name", "role"))

    skills = struct.get("skills") or []
    if skills:
        lines.extend(["", "【核心技能】"])
        for item in skills:
            if isinstance(item, dict):
                items = "、".join(str(s) for s in item.get("items") or [])
                lines.append(f"{item.get('group', '技能')}：{items}" if items else str(item.get("group", "")))
            else:
                lines.append(f"- {item}")
    extras = struct.get("extras") or []
    if extras:
        lines.extend(["", "【其他】"])
        for item in extras:
            lines.append(f"- {item}")
    return "\n".join(lines).strip()
