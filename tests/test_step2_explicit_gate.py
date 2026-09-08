"""STEP2 只能显式触发：仓库代码不得出现自动调用方；STEP2 不得拉起 STEP3。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEP2_MODULE = "export_current_monitor_status"
STEP3_MODULE = "process_monitor_no_push_zhaoshang"


def _python_sources():
    for folder in (ROOT / "app", ROOT / "scripts"):
        for path in folder.rglob("*.py"):
            if path.name in {f"{STEP2_MODULE}.py", f"{STEP3_MODULE}.py"}:
                continue
            yield path


def test_no_automatic_step2_callers_in_repo_code():
    offenders = [
        str(path.relative_to(ROOT))
        for path in _python_sources()
        if STEP2_MODULE in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"AUTO_STEP2_CALLERS must stay NONE, found: {offenders}"


def test_step2_script_never_references_step3():
    source = (ROOT / "scripts" / f"{STEP2_MODULE}.py").read_text(encoding="utf-8")
    assert STEP3_MODULE not in source
    assert "zhImport" not in source
