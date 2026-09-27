"""Durable, prewarm-owned pause for unattended CNNC network reads."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.services.daily_audit_store import LeaseUnavailable, ProcessLock


DEFAULT_PAUSE_PATH = (
    Path(__file__).resolve().parents[2]
    / "runtime"
    / "incremental_price_audit"
    / "customer_prewarm_auth_pause.json"
)


class PrewarmAuthPause:
    def __init__(self, path: Path | str = DEFAULT_PAUSE_PATH):
        self.path = Path(path).absolute()
        if os.name == "nt" and self.path.drive.casefold() != "e:":
            raise ValueError("prewarm auth pause must stay on E drive")
        self._deferred = False

    def _read(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"paused": True}
        return payload if isinstance(payload, dict) else {"paused": True}

    def active(self) -> bool:
        payload = self._read()
        return self._deferred or (payload is not None and payload.get("paused") is not False)

    def defer(self) -> None:
        self._deferred = True

    def pause(self, reason: str, *, event_at: str = "") -> bool:
        with self._locked():
            payload = self._read()
            if payload is not None and payload.get("paused") is not False:
                return False
            if event_at and payload and str(payload.get("resumed_at") or "") >= event_at:
                return False
            self._write({
                "schema": 1,
                "paused": True,
                "reason": reason,
                "paused_at": datetime.now(timezone.utc).isoformat(),
                "source_event_at": event_at,
            })
            return True

    def resume(self) -> bool:
        with self._locked():
            was_active = self.active()
            self._deferred = False
            self._write({
                "schema": 1,
                "paused": False,
                "resumed_at": datetime.now(timezone.utc).isoformat(),
            })
            return was_active

    @contextmanager
    def _locked(self):
        lock = ProcessLock(self.path.with_name(f"{self.path.name}.lock"))
        for attempt in range(50):
            try:
                lock.acquire()
                break
            except LeaseUnavailable:
                if attempt == 49:
                    raise OSError("prewarm auth pause lock unavailable")
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.release()

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
