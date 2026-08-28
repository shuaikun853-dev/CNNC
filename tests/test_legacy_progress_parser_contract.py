r"""Legacy zhaoshang progress-parser contract: L1 double / L3 real-tool replay.

分层标记(与仓库 L0-L3 测试分层惯例一致):
- L1 替身契约:test_remote_running_recovery.py 与
  test_remote_export_no_price_resolution.py 中工厂函数内的 fake
  ``parse_task_progress_desc``(真实使用的替身,经工厂取回,不是第三份抄本)
  对下列语料逐条断言:六键恒在、未命中为 None、正则 ``[：:](\d+)条``
  不得放宽(冒号后空格阻断、缺"条"不匹配)。
- L3 真实工具 corpus replay:若外部真实工具
  ``E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py``
  存在,加载真实模块(已核对源码:顶层仅 import/常量/函数定义,唯一执行入口
  在 ``if __name__ == "__main__"`` 守卫之后,import 无网络无循环),把同一
  语料喂给真实 ``parse_task_progress_desc``,断言每个替身输出 == 真实输出
  (逐条相等)。文件缺失时 skip
  (ENVIRONMENT_INTEGRATION_NOT_CONFIGURED)。

方向性声明:REAL CONTRACT -> TEST DOUBLE。真实工具是唯一事实源;替身镜像
真实正则契约,禁止反向放宽(例如接受冒号后空格、省略未命中键、容忍非数字)。
若真实工具行为变化,替身与本语料随之更新,绝不用替身行为倒推真实契约。
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from app.services.remote_running_recovery import canonical_progress_signature
from tests import test_remote_export_no_price_resolution as export_test
from tests import test_remote_running_recovery as recovery_test

REAL_TOOL_PATH = Path(
    r"E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py"
)

SIX_KEYS = ("total", "pending", "running", "failed", "waiting_db", "finished")


def _all_none() -> dict:
    return {key: None for key in SIX_KEYS}


def _progress(total, pending, running, failed, waiting_db, finished) -> dict:
    return {
        "total": total,
        "pending": pending,
        "running": running,
        "failed": failed,
        "waiting_db": waiting_db,
        "finished": finished,
    }


def _doubles() -> dict:
    """Return the actual fake parsers used by the sibling suites.

    The factory functions only define and return the ``_Module`` class; the
    ``task``/``session`` arguments are captured lazily inside static methods
    and are never touched at class-definition time, so ``({}, None)`` is safe
    and side-effect free.
    """
    recovery_tool = recovery_test._restart_tool_module({}, None)
    export_tool = export_test._fake_tool_module({}, None)
    return {
        "recovery-double": recovery_tool.parse_task_progress_desc,
        "export-double": export_tool.parse_task_progress_desc,
    }


# (rsp_desc, expected six-key dict) — expectations come from the REAL tool's
# regex contract ``{label}[：:](\d+)条`` (auto_crawl_task.py:645-660).
CORPUS = [
    pytest.param(
        "总任务量：94条，待执行：0条，执行中：0条，失败：0条，待入库：0条，执行完成：94条",
        _progress(94, 0, 0, 0, 0, 94),
        id="fullwidth-colon-complete",
    ),
    pytest.param(
        "总任务量:94条,待执行:0条,执行中:0条,失败:0条,待入库:0条,执行完成:94条",
        _progress(94, 0, 0, 0, 0, 94),
        id="halfwidth-colon-complete",
    ),
    pytest.param(
        "总任务量：8条,待执行:3条，执行中：2条,失败：0条，待入库:1条，执行完成：2条",
        _progress(8, 3, 2, 0, 1, 2),
        id="mixed-colon-widths",
    ),
    pytest.param(
        # Space right after the colon breaks the real regex [：:](\d+)条;
        # the doubles must NOT be widened to tolerate it.
        "总任务量： 5条，待执行：1条，执行中：1条，失败：0条，待入库：1条，执行完成：2条",
        _progress(None, 1, 1, 0, 1, 2),
        id="space-after-colon-blocks-total",
    ),
    pytest.param(
        "总任务量：5条，执行完成：5条",
        _progress(5, None, None, None, None, 5),
        id="missing-fields-stay-none",
    ),
    pytest.param(
        # Unexpected wording: no label matches, but all six keys stay present.
        "总量：5条",
        _all_none(),
        id="unexpected-wording-all-none",
    ),
    pytest.param(
        "",
        _all_none(),
        id="empty-string-all-none",
    ),
    pytest.param(
        "总任务量：7条、待执行：2条、执行中：1条、失败：0条、待入库：2条、执行完成：2条",
        _progress(7, 2, 1, 0, 2, 2),
        id="separator-chinese-dunhoun",
    ),
    pytest.param(
        "总任务量：7条 待执行：2条 执行中：1条 失败：0条 待入库：2条 执行完成：2条",
        _progress(7, 2, 1, 0, 2, 2),
        id="separator-space-between-fields",
    ),
    pytest.param(
        # Inter-field separators (and spaces between fields) are irrelevant
        # to the regex; only label+colon+digits+条 matters.
        "总任务量:7条, 待执行:2条, 执行中:1条, 失败:0条, 待入库:2条, 执行完成:2条",
        _progress(7, 2, 1, 0, 2, 2),
        id="separator-english-comma-space",
    ),
    pytest.param(
        # Zero is a parsed integer 0, never None.
        "总任务量：5条，待执行：0条，执行中：0条，失败：0条，待入库：0条，执行完成：5条",
        _progress(5, 0, 0, 0, 0, 5),
        id="zero-counts-are-int-zero-not-none",
    ),
]


# ---------------------------------------------------------------------------
# L1 — double contract: the fakes used by the sibling suites mirror the real
# regex exactly; six keys always present, misses are None.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rsp_desc,expected", CORPUS)
def test_l1_doubles_match_real_contract(rsp_desc, expected):
    for name, parse in _doubles().items():
        summary = parse(rsp_desc)
        assert set(summary) == set(SIX_KEYS), f"{name}: six keys must always be present"
        assert summary == expected, f"{name}: diverged from the real regex contract"


# ---------------------------------------------------------------------------
# L3 — real-tool corpus replay: same corpus through the real external tool;
# every double must equal the real output entry for entry.
# ---------------------------------------------------------------------------


def _extract_real_parser_source():
    """Exec ONLY the real parser function's source (fallback path).

    Used when the real tool file exists but its heavy third-party imports
    (pandas/requests) are missing from this interpreter.  The function only
    needs ``re``, so executing its real source segment stays faithful.
    """
    source = REAL_TOOL_PATH.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "parse_task_progress_desc":
            namespace = {"re": __import__("re")}
            exec(
                compile(ast.get_source_segment(source, node), str(REAL_TOOL_PATH), "exec"),
                namespace,
            )
            return namespace["parse_task_progress_desc"]
    pytest.fail("real crawl tool present but parse_task_progress_desc not found in it")


@pytest.fixture(scope="module")
def real_parse():
    if not REAL_TOOL_PATH.is_file():
        pytest.skip("ENVIRONMENT_INTEGRATION_NOT_CONFIGURED: external crawl tool not present")
    spec = importlib.util.spec_from_file_location(
        "cnnc_real_auto_crawl_task_contract_probe", REAL_TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError:
        return _extract_real_parser_source()
    return module.parse_task_progress_desc


@pytest.mark.parametrize("rsp_desc,expected", CORPUS)
def test_l3_real_tool_replay_matches_doubles(real_parse, rsp_desc, expected):
    real_summary = real_parse(rsp_desc)
    assert real_summary == expected, "corpus expectation diverged from the REAL tool"
    for name, parse in _doubles().items():
        assert parse(rsp_desc) == real_summary, (
            f"{name}: fake output must equal the real tool output entry for entry"
        )


# ---------------------------------------------------------------------------
# canonical_progress_signature contract (remote_running_recovery.py:238-255).
# ---------------------------------------------------------------------------


def test_canonical_signature_accepts_six_int_dict():
    assert (
        canonical_progress_signature(_progress(10, 4, 3, 0, 1, 2))
        == (10, 4, 3, 0, 1, 2)
    )


def test_canonical_signature_rejects_any_none_field():
    assert canonical_progress_signature(_progress(10, None, 3, 0, 1, 2)) is None


def test_canonical_signature_pins_current_numeric_string_coercion():
    # Pin CURRENT behaviour: int() accepts numeric strings, so such rows
    # canonicalize today.  If this pin ever breaks, the change is deliberate
    # production behaviour, not silent drift.
    assert (
        canonical_progress_signature(_progress("10", "4", "3", "0", "1", "2"))
        == (10, 4, 3, 0, 1, 2)
    )


def test_canonical_signature_rejects_non_intable_value():
    assert canonical_progress_signature(_progress(10, "abc", 3, 0, 1, 2)) is None


def test_canonical_signature_rejects_bool_value():
    assert canonical_progress_signature(_progress(True, 4, 3, 0, 1, 2)) is None


def test_canonical_signature_rejects_total_zero():
    assert canonical_progress_signature(_progress(0, 0, 0, 0, 0, 0)) is None


@pytest.mark.parametrize("bad", [[], "总任务量：10条", 42, None])
def test_canonical_signature_rejects_non_mapping(bad):
    assert canonical_progress_signature(bad) is None
