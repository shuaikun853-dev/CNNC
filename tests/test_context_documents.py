import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROOT_DOCS = {
    "AGENTS.md": 180,
    "CURRENT_STATE.md": 150,
    "TESTING.md": 244,
    "README.md": 180,
}
RUNBOOKS = {
    "docs/runbooks/STEP_1_PRICE_AUDIT.md": 180,
    "docs/runbooks/STEP_2_MONITOR_STATUS.md": 120,
    "docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md": 180,
}


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_context_documents_stay_within_line_budgets():
    for relative_path, maximum in {**ROOT_DOCS, **RUNBOOKS}.items():
        line_count = len(_read(relative_path).splitlines())
        assert line_count <= maximum, (
            f"{relative_path} has {line_count} lines; context budget is {maximum}"
        )


def test_agents_routes_to_one_runbook_instead_of_all_history():
    agents = _read("AGENTS.md")
    for runbook in RUNBOOKS:
        assert runbook in agents
    assert "不要再默认通读四份大文档" in agents
    assert "约 20 轮" in agents
    assert "约 30 轮" in agents
    assert "CURRENT_STATE.md" in agents
    assert "同一步骤可以同时存在多个活跃批次" in agents
    assert "其他批次不得成为当前批次的阻塞条件" in agents
    assert "上下文轮数本身不得作为暂停、拒绝或延后执行的理由" in agents
    assert "强制交接" not in agents
    assert "不再开始新的生产动作" not in agents


def test_current_state_is_a_parallel_batch_protocol():
    state = _read("CURRENT_STATE.md")
    assert "## 当前交接卡" not in state
    assert "BATCH_STATE.md" in state
    assert "runtime/**/BATCH_STATE.md" in state
    assert "ACTIVE | WAITING_USER | BLOCKED | COMPLETE | ABANDONED" in state
    assert "已完成且不得重复" in state
    assert "后台进程" in state
    assert "## 批次状态模板" in state
    assert "## 接管提示词" in state


def test_runbooks_keep_scope_and_authorization_gates():
    step_1 = _read("docs/runbooks/STEP_1_PRICE_AUDIT.md")
    step_2 = _read("docs/runbooks/STEP_2_MONITOR_STATUS.md")
    step_3 = _read("docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md")

    assert "实际行数 == count" in step_1
    assert "--auto-stbchina-fallback" in step_1
    assert "affected_rows>0" in step_1
    assert 'sassCheckStatuss=["0"]' in step_2
    assert "每批最多 200" in step_2
    assert "syncPriceStatus!=5" in step_2
    assert "--skip-zhaoshang" in step_3
    assert "--zs-wait-concurrency" in step_3
    assert "顺序创建、启用和受控提优" in step_3
    assert "有界并发等待和导出" in step_3
    assert "--detail-reference-fallback" in step_3
    assert "documentId + 完整 URL" in step_3

    for runbook in (step_1, step_2, step_3):
        assert "BATCH_STATE.md" in runbook
        assert "更新 `CURRENT_STATE.md`" not in runbook
        assert "把 `CURRENT_STATE.md`" not in runbook


def test_readme_local_markdown_links_resolve():
    readme = _read("README.md")
    links = re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", readme)
    assert links
    for link in links:
        assert (ROOT / link).exists(), f"README link does not exist: {link}"
