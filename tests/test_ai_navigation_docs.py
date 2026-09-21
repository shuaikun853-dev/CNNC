import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NAVIGATION_DOCS = (
    ROOT / "ARCHITECTURE.md",
    ROOT / "docs/debug/BUG_ROUTING.md",
    ROOT / "docs/debug/STATE_OWNERSHIP.md",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_ai_navigation_entrypoints_exist_and_are_linked_from_agents():
    agents = _read(ROOT / "AGENTS.md")

    for path in NAVIGATION_DOCS:
        assert path.is_file(), f"missing AI navigation document: {path}"

    assert "ARCHITECTURE.md" in agents
    assert "docs/debug/BUG_ROUTING.md" in agents
    assert "docs/debug/STATE_OWNERSHIP.md" in agents


def test_navigation_markdown_links_resolve():
    link_pattern = re.compile(r"\[[^]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)")

    for document in NAVIGATION_DOCS:
        for target in link_pattern.findall(_read(document)):
            resolved = (document.parent / target).resolve()
            assert resolved.is_file(), f"broken link in {document}: {target}"


def test_bug_routing_is_guidance_not_a_false_oracle():
    routing = _read(ROOT / "docs/debug/BUG_ROUTING.md")

    assert "优先导航，不是真理" in routing
    assert "扩大搜索范围" in routing
    assert "招商局任务已经完成，但 CNNC 没有及时回收" in routing
    assert "state.sqlite3" in routing
    assert "remote_jobs" in routing


def test_state_ownership_separates_authority_from_projections():
    ownership = _read(ROOT / "docs/debug/STATE_OWNERSHIP.md")

    for state_carrier in (
        "state.sqlite3",
        "customer_prewarm.sqlite3",
        "shared_crawl_cache.sqlite3",
        "daily_status.json",
        "BATCH_STATE.md",
        "batch_state_snapshots",
        "Dashboard projection",
        "Live remote progress",
    ):
        assert state_carrier in ownership

    assert "都不是增量 STEP1 的业务 System of Record" in ownership
    assert "CURRENTLY AMBIGUOUS" in ownership


def test_architecture_mermaid_maps_have_parseable_entrypoints():
    architecture = _read(ROOT / "ARCHITECTURE.md")
    diagrams = re.findall(r"```mermaid\s*\n(.*?)```", architecture, flags=re.DOTALL)

    assert len(diagrams) >= 2
    assert all(diagram.lstrip().startswith("flowchart") for diagram in diagrams)
