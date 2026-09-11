"""customer_reference_links 等价性与行为测试(Issue #9 Phase 1)。

等价基准:STEP3 ``scripts/process_monitor_no_push_zhaoshang.py`` 365-377 行的
内联提取逻辑,逐字拷贝为本文件的 oracle;新纯函数必须对同一批输入产出完全
一致的结果(顺序、去重、过滤全部一致)。
"""

from __future__ import annotations

import re

import pytest

from app.services.customer_reference_links import URL_RE, extract_reference_links

# ---- oracle:STEP3 内联实现逐字拷贝(仅把非 dict 输入归一为空 dict,
# 对应 STEP3 在进入提取前已有的 isinstance(data, dict) 守卫) ----

STEP3_URL_RE = re.compile(r"https?://[^\s,，;；\n\r\t]+")


def _step3_oracle(payload):
    data = payload if isinstance(payload, dict) else {}
    d = data.get("data", {})
    if not isinstance(d, dict):
        d = {}
    links = []
    for item in d.get("referenceInfos", []) if isinstance(d.get("referenceInfos", []), list) else []:
        links += [u for u in STEP3_URL_RE.findall(str(item.get("referenceLink", "") or "")) if u.startswith("http")]
    links += [u for u in STEP3_URL_RE.findall(str(d.get("referenceLink", "") or "")) if u.startswith("http")]
    seen, unique = set(), []
    for u in links:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


_EQUIVALENCE_FIXTURES = [
    pytest.param({"data": {"referenceLink": "https://mall.example/item/1"}}, id="single-link"),
    pytest.param(
        {
            "data": {
                "referenceInfos": [
                    {"referenceLink": ""},
                    {"referenceLink": "https://a.example/1"},
                    {"referenceLink": "https://b.example/2 https://c.example/3，https://d.example/4；"},
                ],
                "referenceLink": "https://e.example/5",
            }
        },
        id="infos-0-1-many-with-chinese-punct",
    ),
    pytest.param(
        {
            "data": {
                "referenceInfos": [
                    {"referenceLink": "https://dup.example/1"},
                    {"referenceLink": "https://dup.example/1 https://dup.example/2"},
                ],
                "referenceLink": "https://dup.example/1",
            }
        },
        id="duplicates-across-infos-and-top",
    ),
    pytest.param(
        {"data": {"referenceLink": "ftp://files.example/a //cdn.example/b"}},
        id="non-http-filtered",
    ),
    pytest.param(
        {"data": {"referenceInfos": [{"referenceLink": "see https://keep.example/1 and ftp://drop.example/2"}]}},
        id="mixed-schemes-in-prose",
    ),
    pytest.param({"data": {}}, id="empty-data"),
    pytest.param({}, id="missing-data"),
    pytest.param({"data": [1, 2]}, id="data-not-dict-list"),
    pytest.param({"data": "https://string.example/1"}, id="data-not-dict-string"),
    pytest.param({"data": None}, id="data-null"),
    pytest.param(
        {"data": {"referenceInfos": "https://ignored.example/1", "referenceLink": "https://kept.example/2"}},
        id="reference-infos-not-list",
    ),
    pytest.param({"data": {"referenceLink": "https://top.example/1"}}, id="reference-infos-missing"),
    pytest.param(
        {"data": {"referenceInfos": None, "referenceLink": "https://top.example/2"}},
        id="reference-infos-null",
    ),
    pytest.param(
        {"data": {"referenceInfos": [{"referenceLink": None}, {}]}},
        id="item-reference-link-null",
    ),
    pytest.param(
        {
            "data": {
                "referenceInfos": [{"referenceLink": "https://x.example/p?a=1&b=2#frag"}],
                "referenceLink": "https://x.example/p?a=1&b=2#frag",
            }
        },
        id="query-and-fragment-preserved",
    ),
    pytest.param(
        {"data": {"referenceLink": "https://m.example/1，https://m.example/2；https://m.example/1"}},
        id="top-level-multi-url-with-dup",
    ),
    pytest.param(
        {"data": {"referenceInfos": [{"referenceLink": 42}], "referenceLink": 7}},
        id="numeric-link-fields",
    ),
    pytest.param(None, id="none-payload"),
    pytest.param("not a dict", id="garbage-string"),
    pytest.param([{"data": {}}], id="garbage-list"),
]


@pytest.mark.parametrize("payload", _EQUIVALENCE_FIXTURES)
def test_equivalence_with_step3_oracle(payload):
    assert extract_reference_links(payload) == _step3_oracle(payload)


@pytest.mark.parametrize("bad_item", [123, "https://raw.example/1", None, ["x"]])
def test_non_dict_items_are_skipped_and_never_raise(bad_item):
    payload = {"data": {"referenceInfos": [bad_item, {"referenceLink": "https://ok.example/1"}]}}
    # STEP3 逐字版遇到非 dict 项会抛 AttributeError(生产中由外层 try/except 兜底,
    # 整篇文档计为失败);新纯函数契约:逐项跳过、永不抛出。
    with pytest.raises(AttributeError):
        _step3_oracle(payload)
    assert extract_reference_links(payload) == ["https://ok.example/1"]


def test_url_re_matches_step3_pattern():
    assert isinstance(URL_RE, re.Pattern)
    assert URL_RE.pattern == r"https?://[^\s,，;；\n\r\t]+"


def test_order_info_links_before_top_level():
    payload = {
        "data": {
            "referenceInfos": [{"referenceLink": "https://info.example/1"}],
            "referenceLink": "https://top.example/1",
        }
    }
    assert extract_reference_links(payload) == ["https://info.example/1", "https://top.example/1"]


def test_dedupe_keeps_first_occurrence_position():
    payload = {
        "data": {
            "referenceInfos": [{"referenceLink": "https://a.example/1 https://b.example/2"}],
            "referenceLink": "https://a.example/1",
        }
    }
    assert extract_reference_links(payload) == ["https://a.example/1", "https://b.example/2"]


def test_url_verbatim_including_query_and_fragment():
    payload = {"data": {"referenceLink": "https://x.example/p?a=1&b=2#frag"}}
    assert extract_reference_links(payload) == ["https://x.example/p?a=1&b=2#frag"]
