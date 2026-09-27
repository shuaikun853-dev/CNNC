import json

from app.services.customer_prewarm_auth_pause import PrewarmAuthPause


def test_pause_is_durable_idempotent_and_manual_resume_clears_it(tmp_path):
    path = tmp_path / "auth_pause.json"
    pause = PrewarmAuthPause(path)
    assert pause.active() is False
    assert pause.pause("harvest_deadline") is True
    first = path.read_bytes()
    assert PrewarmAuthPause(path).active() is True
    assert pause.pause("harvest_deadline") is False
    assert path.read_bytes() == first
    assert pause.resume() is True
    assert PrewarmAuthPause(path).active() is False
    assert pause.resume() is False


def test_corrupt_pause_fails_closed_until_manual_resume(tmp_path):
    path = tmp_path / "auth_pause.json"
    path.write_text("{bad json", encoding="utf-8")
    pause = PrewarmAuthPause(path)
    assert pause.active() is True
    assert pause.resume() is True
    assert json.loads(path.read_text(encoding="utf-8"))["paused"] is False


def test_manual_resume_fences_an_older_deadline_event(tmp_path):
    pause = PrewarmAuthPause(tmp_path / "auth_pause.json")
    pause.resume()
    assert pause.pause("harvest_deadline", event_at="2026-01-01T00:00:00+00:00") is False
    assert PrewarmAuthPause(pause.path).active() is False


def test_transient_defer_is_not_durable(tmp_path):
    pause = PrewarmAuthPause(tmp_path / "auth_pause.json")
    pause.defer()
    assert pause.active() is True
    assert PrewarmAuthPause(pause.path).active() is False
