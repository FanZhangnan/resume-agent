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
