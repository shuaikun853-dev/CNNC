"""Run from the repository root: python -m pytest tests/test_offline_price_audit.py -q"""
import json

import pytest

from examples.offline_price_audit import EXAMPLE_INPUT, evaluate, main


def test_synthetic_sample_uses_existing_rules():
    data = json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8"))
    out = evaluate(data["items"])
    assert out["mode"] == "offline_demo"
    assert out["readonly"] is True
    assert [r["decision"] for r in out["items"]] == [
        "PASS", "REJECT_BOTH", "REJECT_PRICE_CHANGED", "REVIEW_REQUIRED"
    ]
    assert all(r["advisory_only"] for r in out["items"])


def test_reject_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate item id"):
        evaluate([{"id": "a"}, {"id": "a"}])


def test_missing_price_requires_review():
    out = evaluate([{"id": "x", "customer_price": "10", "saas_price": "10",
                     "page_price_status": "NO_RESULT"}])
    assert out["items"][0]["decision"] == "REVIEW_REQUIRED"


def test_invalid_state_rejected():
    with pytest.raises(ValueError, match="page_price_status"):
        evaluate([{"id": "x", "page_price_status": "UNKNOWN"}])


def test_cli_local_fixture_only(capsys):
    assert main([]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 4


def test_nonfinite_price_requires_review():
    out = evaluate([{"id": "x", "customer_price": "NaN", "saas_price": "10",
                     "page_price": "10", "page_price_status": "SUCCESS"}])
    assert out["items"][0]["decision"] == "REVIEW_REQUIRED"
