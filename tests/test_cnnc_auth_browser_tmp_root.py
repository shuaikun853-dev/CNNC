"""E-only profile creation; lifecycle safety cases live in test_cnnc_browser_lifecycle."""
import inspect
import json
import time

from scripts import cnnc_auth_browser as browser


def test_profile_constants_and_routing():
    assert browser.BROWSER_TMP_ROOT == browser.ROOT / "runtime" / "browser_tmp"
    assert browser.STALE_BROWSER_TMP_MAX_AGE_SECONDS == 6 * 3600
    assert browser.STALE_HARVEST_TMP_MAX_AGE_SECONDS == 10 * 60
    for function in (browser.cmd_login, browser.cmd_harvest, browser.cmd_smoke):
        source = inspect.getsource(function)
        assert "_make_browser_profile_dir(" in source
        assert source.index("_close_cdp(") < source.index("_remove_browser_profile(")


def test_login_keeps_six_hour_window(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", tmp_path)
    old = browser._make_browser_profile_dir("cnnc_login_")
    marker = old / browser.PROFILE_OWNER_FILE
    metadata = json.loads(marker.read_text())
    metadata.update(pid=999999, created_at=time.time() - 700)
    marker.write_text(json.dumps(metadata))
    monkeypatch.setattr(browser, "_process_inventory", lambda: [])
    assert not browser._remove_browser_profile(old, orphan=True)
    metadata["created_at"] = time.time() - 7 * 3600
    marker.write_text(json.dumps(metadata))
    assert browser._remove_browser_profile(old, orphan=True)
