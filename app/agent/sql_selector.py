import yaml
from pathlib import Path
from app.config import TEMPLATES_DIR
from app.agent.safety_guard import validate_sql


_loaded_cache: dict[str, dict] = {}


def _load_template(scenario: str) -> dict | None:
    if scenario in _loaded_cache:
        return _loaded_cache[scenario]

    path = TEMPLATES_DIR / f"{scenario}.yaml"
    if not path.exists():
        return None

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    _loaded_cache[scenario] = data
    return data


def get_sql_templates(scenario: str) -> list[dict]:
    data = _load_template(scenario)
    if not data:
        return []

    results = []
    for tpl in data.get("templates", []):
        sql = tpl.get("sql", "")
        safe, reason = validate_sql(sql)

        if safe:
            results.append({
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "params": tpl.get("params", []),
                "sql": sql,
                "safe": True,
                "safety_reason": reason,
                "result_hint": tpl.get("result_hint", ""),
            })
        else:
            results.append({
                "template_name": tpl.get("name", ""),
                "name": tpl.get("name", ""),
                "purpose": tpl.get("purpose", ""),
                "blocked": True,
                "blocked_reason": reason,
                "safe": False,
            })

    return results
