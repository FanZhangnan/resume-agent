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
