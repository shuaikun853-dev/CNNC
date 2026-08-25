import re
import yaml
from pathlib import Path
from app.config import TEMPLATES_DIR
from app.agent.safety_guard import validate_sql


_loaded_cache: dict[str, dict] = {}

_COLON_PARAM_RE = re.compile(r":(\w+)")
_MUSTACHE_PARAM_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")
_DIGITS_ONLY_RE = re.compile(r"^\d+$")
_EXEC_NUMBER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TASK_TYPE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

_DANGEROUS_CHARS_RE = re.compile(r"[;\\#\n\r\x00]|/\*|\*/|--")
_SINGLE_QUOTE_RE = re.compile(r"'")


def _load_template(scenario: str) -> dict | None:
    if scenario in _loaded_cache:
        return _loaded_cache[scenario]

    path = TEMPLATES_DIR / f"{scenario}.yaml"
    if not path.exists():
        return None

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        return None

    _loaded_cache[scenario] = data
    return data


def _is_empty_value(val) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and val.strip() == "":
        return True
    if isinstance(val, (list, dict, tuple, set)) and len(val) == 0:
        return True
    return False


def _is_unsupported_container_type(val) -> bool:
    if val is None:
        return True
    if isinstance(val, (list, dict, tuple, set)):
        return True
    return False


def _validate_param_value(name: str, val) -> tuple[bool, str]:
    known_validators = {
        "start_time": _validate_datetime_param,
        "end_time": _validate_datetime_param,
        "commodity_id": _validate_digits_param,
        "my_commodity_id": _validate_digits_param,
        "request_id": _validate_digits_param,
        "execution_number": _validate_exec_number_param,
        "task_type": _validate_task_type_param,
        "hours": _validate_digits_param,
    }

    validator = known_validators.get(name, _validate_unknown_param)
    return validator(val)


def _validate_datetime_param(val) -> tuple[bool, str]:
    if isinstance(val, bool):
        return False, "时间参数不接受布尔值"
    if isinstance(val, (int, float)):
        return False, "时间参数不接受数值类型，必须为时间格式字符串"
    if not isinstance(val, str):
        return False, "时间参数必须为字符串"
    if not _DATETIME_RE.match(val):
        return False, "参数不符合时间格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"
    if _DANGEROUS_CHARS_RE.search(val):
        return False, "时间参数包含非法字符"
    return True, ""


def _validate_digits_param(val) -> tuple[bool, str]:
    if isinstance(val, bool):
        return False, "ID 参数不接受布尔值"
    if isinstance(val, float):
        return False, "ID 参数不接受浮点数"
    if isinstance(val, int):
        return True, ""
    if not isinstance(val, str):
        return False, "ID 参数必须为数字"
    if not _DIGITS_ONLY_RE.match(val):
        return False, "ID 参数必须为纯数字"
    return True, ""


def _validate_exec_number_param(val) -> tuple[bool, str]:
    if isinstance(val, bool):
        return False, "execution_number 参数不接受布尔值"
    if isinstance(val, (int, float)):
        return False, "execution_number 参数必须为字符串"
    if not isinstance(val, str):
        return False, "execution_number 参数必须为字符串"
    if not _EXEC_NUMBER_RE.match(val):
        return False, "execution_number 包含非法字符，仅允许字母、数字、下划线、横线"
    return True, ""


def _validate_task_type_param(val) -> tuple[bool, str]:
    if isinstance(val, bool):
        return False, "task_type 参数不接受布尔值"
    if isinstance(val, (int, float)):
        return False, "task_type 参数必须为字符串"
    if not isinstance(val, str):
        return False, "task_type 参数必须为字符串"
    if not _TASK_TYPE_RE.match(val):
        return False, "task_type 包含非法字符，仅允许字母、数字、下划线、横线、点"
    return True, ""


def _validate_unknown_param(val) -> tuple[bool, str]:
    if isinstance(val, bool):
        return False, "未知参数不接受布尔值"
    if isinstance(val, (list, dict, tuple, set)):
        return False, "未知参数不接受容器类型"
    if not isinstance(val, str):
        return True, ""
    if _DANGEROUS_CHARS_RE.search(val):
        return False, "参数包含非法字符（分号、注释、反斜杠、换行等）"
    if _SINGLE_QUOTE_RE.search(val):
        return False, "参数包含单引号，不允许"
    return True, ""


def _render_value(val, inside_quotes: bool = False) -> str:
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        if inside_quotes:
            return val
        return f"'{val}'"
    return str(val)


def _is_quoted_placeholder(sql: str, start: int, end: int) -> bool:
    before = sql[:start]
    after = sql[end:]
    before_stripped = before.rstrip()
    if before_stripped.endswith("'"):
        after_stripped = after.lstrip()
        if after_stripped.startswith("'"):
            return True
    return False


def _render_sql(sql: str, params: dict) -> tuple[str, list[str], bool]:
    remaining = []
    has_double_quote = False

    replacements_colon = []
    for m in _COLON_PARAM_RE.finditer(sql):
        name = m.group(1)
        if name in params and not _is_empty_value(params[name]) and not _is_unsupported_container_type(params[name]):
            val = params[name]
            ok, _ = _validate_param_value(name, val)
            if not ok:
                remaining.append(name)
                continue
            inside = _is_quoted_placeholder(sql, m.start(), m.end())
            if inside:
                if not isinstance(val, str):
                    has_double_quote = True
                replacements_colon.append((m.start(), m.end(), _render_value(val, inside_quotes=True)))
            else:
                replacements_colon.append((m.start(), m.end(), _render_value(val, inside_quotes=False)))
        else:
            remaining.append(name)

    rendered = sql
    for start, end, replacement in reversed(replacements_colon):
        rendered = rendered[:start] + replacement + rendered[end:]

    replacements_mustache = []
    for m in _MUSTACHE_PARAM_RE.finditer(rendered):
        name = m.group(1)
        if name in params and not _is_empty_value(params[name]) and not _is_unsupported_container_type(params[name]):
            val = params[name]
            ok, _ = _validate_param_value(name, val)
            if not ok:
                remaining.append(name)
                continue
            inside = _is_quoted_placeholder(rendered, m.start(), m.end())
            if inside:
                if not isinstance(val, str):
                    has_double_quote = True
                replacements_mustache.append((m.start(), m.end(), _render_value(val, inside_quotes=True)))
            else:
                replacements_mustache.append((m.start(), m.end(), _render_value(val, inside_quotes=False)))
        else:
            remaining.append(name)

    for start, end, replacement in reversed(replacements_mustache):
        rendered = rendered[:start] + replacement + rendered[end:]

    return rendered, remaining, has_double_quote


def _extract_param_names(sql: str) -> list[str]:
    seen = set()
    names = []
    for m in _COLON_PARAM_RE.finditer(sql):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            names.append(n)
    for m in _MUSTACHE_PARAM_RE.finditer(sql):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            names.append(n)
    return names


_REMAINING_COLON_RE_TEMPLATE = r"(?:^|[^.\w]):{name}(?!\w)"
_REMAINING_MUSTACHE_RE_TEMPLATE = r"\{{\{{\s*{name}\s*\}}\}}"


def _has_named_placeholder(sql: str, param_name: str) -> bool:
    pattern_colon = _REMAINING_COLON_RE_TEMPLATE.format(name=re.escape(param_name))
    if re.search(pattern_colon, sql):
        return True
    pattern_mustache = _REMAINING_MUSTACHE_RE_TEMPLATE.format(name=re.escape(param_name))
    if re.search(pattern_mustache, sql):
        return True
    return False


def _check_remaining_placeholders(rendered_sql: str, original_param_names: list[str]) -> list[str]:
    remaining = []
    for name in original_param_names:
        if _has_named_placeholder(rendered_sql, name):
            remaining.append(name)
    return remaining


_DOUBLE_QUOTE_RE = re.compile(r"''[^']*''")


def _has_double_quotes_pattern(sql: str) -> bool:
    return bool(_DOUBLE_QUOTE_RE.search(sql))


def get_sql_templates(scenario: str, provided_params: dict | None = None) -> list[dict]:
    data = _load_template(scenario)
    if not data:
        return []

    params = provided_params or {}
    results = []
    for tpl in data.get("templates", []):
        sql = tpl.get("sql", "")
        tpl_params = tpl.get("params", [])
        safe, safety_reason = validate_sql(sql)

        if not safe:
            results.append({
                "template_name": tpl.get("name", ""),
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "description": tpl.get("description", ""),
                "blocked": True,
                "blocked_reason": safety_reason,
                "safe": False,
                "reason": f"SQL 安全检查未通过: {safety_reason}",
            })
            continue

        missing = []
        invalid_params = []
        invalid_reasons = []

        for p in tpl_params:
            pname = p.get("name", "")
            if pname not in params or _is_empty_value(params.get(pname)):
                missing.append(pname)
            elif _is_unsupported_container_type(params.get(pname)):
                invalid_params.append(pname)
                type_name = type(params[pname]).__name__
                invalid_reasons.append(f"参数 {pname} 类型 {type_name} 不支持渲染，仅允许 str/int/float/bool")
            else:
                ok, reason = _validate_param_value(pname, params[pname])
                if not ok:
                    invalid_params.append(pname)
                    invalid_reasons.append(reason)

        if missing:
            results.append({
                "template_name": tpl.get("name", ""),
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "description": tpl.get("description", ""),
                "pending_params": True,
                "missing_params": missing,
                "params": tpl_params,
                "safe": True,
                "reason": f"缺少必要参数: {', '.join(missing)}",
                "result_hint": tpl.get("result_hint", ""),
            })
            continue

        if invalid_params:
            results.append({
                "template_name": tpl.get("name", ""),
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "description": tpl.get("description", ""),
                "blocked": True,
                "blocked_reason": "; ".join(invalid_reasons),
                "invalid_params": invalid_params,
                "safe": False,
                "reason": f"参数校验失败: {'; '.join(invalid_reasons)}",
            })
            continue

        rendered_sql, render_remaining, double_quote_issue = _render_sql(sql, params)

        original_param_names = _extract_param_names(sql)
        unresolved = _check_remaining_placeholders(rendered_sql, original_param_names)

        all_unresolved = list(set(render_remaining + unresolved))
        if all_unresolved:
            results.append({
                "template_name": tpl.get("name", ""),
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "description": tpl.get("description", ""),
                "pending_params": True,
                "missing_params": all_unresolved,
                "params": tpl_params,
                "safe": True,
                "reason": f"SQL 渲染后仍有未替换占位符: {', '.join(all_unresolved)}",
                "result_hint": tpl.get("result_hint", ""),
            })
            continue

        if double_quote_issue or _has_double_quotes_pattern(rendered_sql):
            results.append({
                "template_name": tpl.get("name", ""),
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "description": tpl.get("description", ""),
                "blocked": True,
                "blocked_reason": "SQL 渲染后出现双重引号",
                "safe": False,
                "reason": "SQL 渲染后出现双重引号，渲染中止",
            })
            continue

        render_safe, render_reason = validate_sql(rendered_sql)
        if render_safe:
            results.append({
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "description": tpl.get("description", ""),
                "params": tpl_params,
                "sql": rendered_sql,
                "safe": True,
                "safety_reason": render_reason,
                "reason": "参数已渲染，安全检查通过",
                "result_hint": tpl.get("result_hint", ""),
            })
        else:
            results.append({
                "template_name": tpl.get("name", ""),
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "description": tpl.get("description", ""),
                "blocked": True,
                "blocked_reason": render_reason,
                "safe": False,
                "reason": f"SQL 渲染后安全检查未通过: {render_reason}",
            })

    return results
