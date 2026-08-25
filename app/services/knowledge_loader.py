import re
import html
from pathlib import Path
from app.config import KNOWLEDGE_MD_PATH

WRITE_SQL_KEYWORDS = [
    "UPDATE", "DELETE", "INSERT", "TRUNCATE", "DROP", "ALTER",
    "CREATE", "REPLACE", "MERGE", "CALL", "EXEC",
]

_FENCED_CODE_BLOCK_RE = re.compile(r"(```\s*[a-zA-Z0-9]*\s*\n)(.*?)(```)", re.DOTALL)

_HTML_PRE_CODE_RE = re.compile(r"<pre><code>(.*?)</code></pre>", re.DOTALL | re.IGNORECASE)

_HTML_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)

_INDENT_CODE_LINE_RE = re.compile(r"^( {4,}|\t)(\S.*)$")

_HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")

_WRITE_SQL_NOTICE = (
    "【高风险写库 SQL 已脱敏：第一阶段不展示具体 SQL，需人工确认后处理】"
)

_sections: list[dict] | None = None
_load_error: str | None = None


def _decode_html_entities(text: str) -> str:
    return html.unescape(text)


def _contains_write_sql(text: str) -> bool:
    decoded = _decode_html_entities(text)
    upper = decoded.upper()
    tokens = re.findall(r"[A-Z_]+", upper)
    for kw in WRITE_SQL_KEYWORDS:
        if kw in tokens:
            return True
    for pattern in [r"\bINTO\s+OUTFILE\b", r"\bINTO\s+DUMPFILE\b", r"\bLOAD\s+DATA\b"]:
        if re.search(pattern, upper):
            return True
    return False


def _sanitize_fenced_blocks(text: str) -> str:
    def _replace_block(m):
        prefix = m.group(1)
        code_content = m.group(2)
        suffix = m.group(3)
        if _contains_write_sql(code_content):
            return f"{prefix}{_WRITE_SQL_NOTICE}\n{suffix}"
        return m.group(0)

    return _FENCED_CODE_BLOCK_RE.sub(_replace_block, text)


def _sanitize_html_pre_code(text: str) -> str:
    def _replace_pre_code(m):
        code_content = m.group(1)
        if _contains_write_sql(code_content):
            return _WRITE_SQL_NOTICE
        return m.group(0)

    text = _HTML_PRE_CODE_RE.sub(_replace_pre_code, text)
    return text


def _sanitize_html_pre(text: str) -> str:
    def _replace_pre(m):
        code_content = m.group(1)
        if _contains_write_sql(code_content):
            return _WRITE_SQL_NOTICE
        return m.group(0)

    text = _HTML_PRE_RE.sub(_replace_pre, text)
    return text


def _sanitize_indented_code_blocks(text: str) -> str:
    lines = text.split("\n")
    result = []
    buffer = []
    in_block = False

    for line in lines:
        m = _INDENT_CODE_LINE_RE.match(line)
        if m:
            if not in_block:
                in_block = True
                buffer = []
            buffer.append(m.group(2))
        else:
            if in_block:
                block_text = "\n".join(buffer)
                if _contains_write_sql(block_text):
                    result.append(_WRITE_SQL_NOTICE)
                else:
                    for bline in buffer:
                        result.append("    " + bline)
                buffer = []
                in_block = False
            result.append(line)

    if in_block and buffer:
        block_text = "\n".join(buffer)
        if _contains_write_sql(block_text):
            result.append(_WRITE_SQL_NOTICE)
        else:
            for bline in buffer:
                result.append("    " + bline)

    return "\n".join(result)


def _sanitize_content(content: str) -> str:
    content = _sanitize_fenced_blocks(content)
    content = _sanitize_html_pre_code(content)
    content = _sanitize_html_pre(content)
    content = _sanitize_indented_code_blocks(content)
    return content


def _parse_markdown(text: str) -> list[dict]:
    sections = []
    current_title = ""
    current_level = 0
    current_lines: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            if current_title:
                sections.append({
                    "title": current_title,
                    "level": current_level,
                    "content": _sanitize_content("\n".join(current_lines).strip()),
                })
            current_level = len(m.group(1))
            current_title = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_title:
        sections.append({
            "title": current_title,
            "level": current_level,
            "content": _sanitize_content("\n".join(current_lines).strip()),
        })

    return sections


class KnowledgeLoadError(Exception):
    pass


def load_knowledge(path: str | Path | None = None) -> list[dict]:
    global _sections, _load_error
    if _sections is not None:
        return _sections

    md_path = Path(path) if path else KNOWLEDGE_MD_PATH
    if not md_path.exists():
        _load_error = "知识库文件不存在"
        raise KnowledgeLoadError(_load_error)

    try:
        text = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        _load_error = "知识库文件编码错误"
        raise KnowledgeLoadError(_load_error)
    except OSError as e:
        _load_error = "知识库文件读取失败"
        raise KnowledgeLoadError(_load_error)

    _sections = _parse_markdown(text)
    _load_error = None
    return _sections


def get_knowledge_status() -> tuple[str, str]:
    global _sections, _load_error
    if _sections is not None:
        return "ok", ""
    if _load_error:
        return "error", _load_error
    md_path = KNOWLEDGE_MD_PATH
    if not md_path.exists():
        return "degraded", "知识库文件不存在"
    try:
        md_path.read_text(encoding="utf-8")
        return "ok", ""
    except Exception:
        return "degraded", "知识库文件不可读"


def _extract_substrings(text: str, min_len: int = 2, max_len: int = 4) -> list[str]:
    chunks = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text)
    subs = set()
    for chunk in chunks:
        for length in range(min_len, min(len(chunk), max_len) + 1):
            for i in range(len(chunk) - length + 1):
                subs.add(chunk[i:i + length])
    return list(subs)


def search_knowledge(keyword: str, top_k: int = 3) -> list[dict]:
    sections = load_knowledge()
    if not keyword:
        return sections[:top_k]

    subs = _extract_substrings(keyword)
    scored = []
    for sec in sections:
        score = 0
        title_lower = sec["title"].lower()
        content_lower = sec["content"].lower()
        for sub in subs:
            if sub.lower() in title_lower:
                score += 5
            cnt = content_lower.count(sub.lower())
            if cnt > 0:
                score += min(cnt, 5)
        scored.append((score, sec))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for score, s in scored if score > 0][:top_k]


def reset_knowledge_cache():
    global _sections, _load_error
    _sections = None
    _load_error = None
