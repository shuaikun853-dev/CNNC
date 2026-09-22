"""Remote intents, crawl work and recovery transitions; cross-table updates stay atomic."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Sequence
from app.services.crawl_url_identity import is_placeholder_code
from app.services.page_price_semantics import valid_page_price_result
from app.services.audit_state_kernel import (
    AuditStateKernel,
    DEFAULT_TENANT_ID,
    DEFAULT_POOL_ID,
    _REQUIRED_FINAL_METRICS,
    _REQUIRED_FORCE_FINAL_METRICS,
    FINALIZATION_INTENT_KEY,
    LeaseUnavailable,
    StaleLease,
    FrozenSnapshotConflict,
    RemoteIntentConflict,
    LeaseToken,
    ProcessLock,
)

class RemoteExecutionStore(AuditStateKernel):

    def prepare_remote_intent(
        self,
        token: LeaseToken,
        logical_key: str,
        task_name: str,
        payload_hash: str,
        *,
        mall_key: str | None = None,
        input_path: str | None = None,
        result_path: str | None = None,
        payload_json: Any | None = None,
    ) -> dict[str, Any]:
        now = self._utcnow()
        encoded_payload = self._canonical_json(payload_json) if payload_json is not None else None
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = connection.execute("SELECT * FROM remote_jobs WHERE run_id=? AND logical_key=?", (token.run_id, logical_key)).fetchone()
            if job is not None:
                expected = {
                    "task_name": task_name,
                    "payload_hash": payload_hash,
                    "mall_key": mall_key,
                    "input_path": input_path,
                    "result_path": result_path,
                    "payload_json": encoded_payload,
                }
                if any(job[name] != value for name, value in expected.items()):
                    raise RemoteIntentConflict("logical remote job already has different immutable intent evidence")
                return self._row(job)  # type: ignore[return-value]
            try:
                cursor = connection.execute(
                    """INSERT INTO remote_jobs(run_id, logical_key, task_name, payload_hash, mall_key, input_path, result_path, payload_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (token.run_id, logical_key, task_name, payload_hash, mall_key, input_path, result_path, encoded_payload, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise RemoteIntentConflict("task name is already reserved by another logical remote job") from exc
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (cursor.lastrowid,)).fetchone())  # type: ignore[return-value]

    def get_remote_job(self, job_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())

    def list_remote_stall_evidence(self, run_id: int) -> list[dict[str, Any]]:
        """Lease-free read of pending/manual remote error evidence for the stall breaker."""
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT id, state, error_text, updated_at FROM remote_jobs
                   WHERE run_id=? AND state IN ('PRIORITY_PENDING', 'MANUAL_REQUIRED')
                     AND error_text IS NOT NULL
                   ORDER BY id""",
                (run_id,),
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def list_remote_jobs(self, token: LeaseToken, *, states: set[str] | None = None) -> list[dict[str, Any]]:
        with self._transaction(immediate=False) as connection:
            self._validate_lease(connection, token)
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = connection.execute(f"SELECT * FROM remote_jobs WHERE run_id=? AND state IN ({placeholders}) ORDER BY id", (token.run_id, *sorted(states))).fetchall()
            else:
                rows = connection.execute("SELECT * FROM remote_jobs WHERE run_id=? ORDER BY id", (token.run_id,)).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def supersede_remote_job_to_replacement(
        self,
        token: LeaseToken,
        *,
        old_job_id: int,
        replacement_logical_key: str,
        replacement_task_name: str,
        replacement_payload_hash: str,
        replacement_input_path: str,
        replacement_result_path: str,
        replacement_payload_json: Any,
        mall_key: str,
        action_id: str,
        reason: str,
        old_batch_number: str,
        new_batch_number: str,
        stop_evidence: Mapping[str, Any],
        eligible_crawl_ids: Sequence[int],
        rebuild_action_id: str | None = None,
    ) -> dict[str, Any]:
        """Retire one manual-rebuild target by recording its durable replacement.

        One BEGIN IMMEDIATE transaction moves the non-terminal crawl evidence
        of the old job to a fresh SUBMIT_INTENT job, records the replacement
        lineage (action id, batch numbers, stop evidence), marks the old
        job SUPERSEDED without touching its accumulated error/recovery
        evidence and -- when ``rebuild_action_id`` is given -- advances the
        write-ahead rebuild action to its SUPERSEDED phase bound to the new
        job.  Every precondition is checked inside the transaction, so a
        rejected rebuild leaves zero durable writes behind.
        """
        ids = list(dict.fromkeys(int(crawl_id) for crawl_id in eligible_crawl_ids))
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            old_job = connection.execute("SELECT * FROM remote_jobs WHERE id=?", (old_job_id,)).fetchone()
            if old_job is None or int(old_job["run_id"]) != token.run_id:
                raise ValueError("old remote job must belong to the leased daily run")
            existing_replacement = connection.execute(
                "SELECT id FROM remote_job_replacements WHERE run_id=? AND old_remote_job_id=?",
                (token.run_id, old_job_id),
            ).fetchone()
            if existing_replacement is not None or str(old_job["state"]) == "SUPERSEDED":
                raise RemoteIntentConflict("remote job already has a replacement")
            reserved = connection.execute(
                "SELECT id FROM remote_jobs WHERE run_id=? AND task_name=?",
                (token.run_id, replacement_task_name),
            ).fetchone()
            if reserved is not None:
                raise RemoteIntentConflict("replacement task name is already reserved")
            if not ids:
                raise ValueError("no eligible crawl work to migrate")
            placeholders = ",".join("?" for _ in ids)
            crawl_rows = connection.execute(f"SELECT * FROM crawl_work WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
            if len(crawl_rows) != len(ids) or any(int(row["run_id"]) != token.run_id for row in crawl_rows):
                raise ValueError("eligible crawl work must belong to the leased daily run")
            for row in crawl_rows:
                if row["remote_job_id"] is None or int(row["remote_job_id"]) != old_job_id:
                    raise RemoteIntentConflict("crawl work is not bound to the superseded remote job")
                if str(row["state"]) in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
                    raise RemoteIntentConflict("terminal crawl evidence cannot be migrated")
            now = self._utcnow()
            try:
                cursor = connection.execute(
                    """INSERT INTO remote_jobs(
                           run_id, logical_key, task_name, payload_hash, mall_key,
                           input_path, result_path, payload_json, state, post_attempted,
                           created_at, updated_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SUBMIT_INTENT', 0, ?, ?)""",
                    (
                        token.run_id,
                        replacement_logical_key,
                        replacement_task_name,
                        replacement_payload_hash,
                        mall_key,
                        replacement_input_path,
                        replacement_result_path,
                        self._canonical_json(replacement_payload_json),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RemoteIntentConflict(
                    "replacement intent conflicts with existing remote job evidence"
                ) from exc
            new_job_id = int(cursor.lastrowid)
            cursor = connection.execute(
                """INSERT INTO remote_job_replacements(
                       run_id, old_remote_job_id, new_remote_job_id, rebuild_action_id,
                       reason, status, old_batch_number, new_batch_number,
                       stop_evidence_json, created_at, updated_at
                   )
                   VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?)""",
                (
                    token.run_id,
                    old_job_id,
                    new_job_id,
                    action_id,
                    reason,
                    old_batch_number,
                    new_batch_number,
                    self._canonical_json(stop_evidence),
                    now,
                    now,
                ),
            )
            replacement_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE remote_jobs SET state='SUPERSEDED', updated_at=? WHERE id=?",
                (now, old_job_id),
            )
            connection.execute(
                f"""UPDATE crawl_work SET remote_job_id=?, state='PENDING',
                       error_text=NULL, result_json=NULL, updated_at=?
                   WHERE id IN ({placeholders})""",
                (new_job_id, now, *ids),
            )
            if rebuild_action_id is not None:
                action = connection.execute(
                    "SELECT * FROM remote_job_rebuild_actions WHERE action_id=? AND run_id=?",
                    (str(rebuild_action_id), token.run_id),
                ).fetchone()
                if action is None or int(action["old_remote_job_id"]) != old_job_id:
                    raise RemoteIntentConflict("rebuild action does not match the superseded job")
                connection.execute(
                    """UPDATE remote_job_rebuild_actions
                       SET phase='SUPERSEDED', new_remote_job_id=?, updated_at=?
                       WHERE id=?""",
                    (new_job_id, now, int(action["id"])),
                )
            return {
                "old_job": self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (old_job_id,)).fetchone()),
                "new_job": self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (new_job_id,)).fetchone()),
                "replacement": self._row(connection.execute("SELECT * FROM remote_job_replacements WHERE id=?", (replacement_id,)).fetchone()),
                "migrated_crawl_count": len(ids),
            }

    def get_replacement_for_job(self, old_job_id: int) -> dict[str, Any] | None:
        """Lease-free read of the replacement that retired this old job."""
        with self._reader() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM remote_job_replacements WHERE old_remote_job_id=? ORDER BY id LIMIT 1",
                    (old_job_id,),
                ).fetchone()
            )

    def get_replacement_of_job(self, new_job_id: int) -> dict[str, Any] | None:
        """Lease-free read of the replacement lineage behind this new job."""
        with self._reader() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM remote_job_replacements WHERE new_remote_job_id=? ORDER BY id LIMIT 1",
                    (new_job_id,),
                ).fetchone()
            )

    def list_remote_replacements(self, run_id: int) -> list[dict[str, Any]]:
        """Lease-free read of every replacement recorded for one daily run."""
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_job_replacements WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    _REBUILD_ACTION_ACTIVE_PHASES_SQL = (
        "'INTENT','STOP_SENT','STOP_CONFIRMED','TERMINAL_PROOF','CREATE_SENT'"
    )

    def create_rebuild_action(
        self,
        token: LeaseToken,
        *,
        action_id: str,
        old_remote_job_id: int,
        remote_identity: str,
        replacement_task_name: str,
    ) -> dict[str, Any]:
        """Persist the write-ahead INTENT row of one manual-rebuild protocol.

        The row is the durable evidence that a rebuild protocol owns this old
        remote job before any remote disable/create write leaves the process.
        One BEGIN IMMEDIATE transaction admits the action only when no other
        active protocol exists for the old job, no replacement was recorded
        yet and the action id is globally unique (a FAILED action never
        blocks a fresh protocol).
        """
        action_id = str(action_id)
        identity = str(remote_identity or "").strip()
        parts = identity.split("|")
        if len(parts) != 3 or not all(parts):
            raise ValueError("remote identity must have exactly three non-empty parts")
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = connection.execute(
                "SELECT * FROM remote_jobs WHERE id=?", (int(old_remote_job_id),)
            ).fetchone()
            if job is None or int(job["run_id"]) != token.run_id:
                raise ValueError("old remote job must belong to the leased daily run")
            active = connection.execute(
                f"""SELECT id FROM remote_job_rebuild_actions
                    WHERE run_id=? AND old_remote_job_id=?
                      AND phase IN ({self._REBUILD_ACTION_ACTIVE_PHASES_SQL})""",
                (token.run_id, int(old_remote_job_id)),
            ).fetchone()
            if active is not None:
                raise RemoteIntentConflict(
                    "an active rebuild action already exists for this remote job"
                )
            replaced = connection.execute(
                "SELECT id FROM remote_job_replacements WHERE run_id=? AND old_remote_job_id=?",
                (token.run_id, int(old_remote_job_id)),
            ).fetchone()
            if replaced is not None:
                raise RemoteIntentConflict("remote job already has a replacement")
            try:
                cursor = connection.execute(
                    """INSERT INTO remote_job_rebuild_actions(
                           run_id, action_id, old_remote_job_id, remote_identity,
                           replacement_task_name, phase, stop_evidence_json,
                           created_at, updated_at
                       )
                       VALUES (?, ?, ?, ?, ?, 'INTENT', '{}', ?, ?)""",
                    (
                        token.run_id,
                        action_id,
                        int(old_remote_job_id),
                        identity,
                        str(replacement_task_name),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RemoteIntentConflict("rebuild action id already exists") from exc
            return self._row(
                connection.execute(
                    "SELECT * FROM remote_job_rebuild_actions WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
            )  # type: ignore[return-value]

    def get_rebuild_action(self, action_id: str) -> dict[str, Any] | None:
        """Lease-free read of one durable rebuild action row."""
        with self._reader() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM remote_job_rebuild_actions WHERE action_id=?",
                    (str(action_id),),
                ).fetchone()
            )

    def get_latest_rebuild_action_for_job(self, old_job_id: int) -> dict[str, Any] | None:
        """Lease-free read of the newest rebuild action for one old job."""
        with self._reader() as connection:
            return self._row(
                connection.execute(
                    """SELECT * FROM remote_job_rebuild_actions
                       WHERE old_remote_job_id=? ORDER BY id DESC LIMIT 1""",
                    (int(old_job_id),),
                ).fetchone()
            )

    def update_rebuild_action_phase(
        self,
        token: LeaseToken,
        *,
        action_id: str,
        phase: str,
        stop_evidence: Mapping[str, Any] | None = None,
        new_remote_job_id: int | None = None,
        error_text: str | None = None,
    ) -> dict[str, Any]:
        """Advance the write-ahead phase of one durable rebuild action.

        ``stop_evidence`` merges into the persisted evidence document (it
        never replaces prior evidence); ``new_remote_job_id`` and
        ``error_text`` are written only when provided -- ``error_text=""``
        clears the diagnostic, ``None`` keeps it.
        """
        phase = str(phase)
        if not phase.strip():
            raise ValueError("rebuild action phase must be a non-empty string")
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            action = connection.execute(
                "SELECT * FROM remote_job_rebuild_actions WHERE action_id=? AND run_id=?",
                (str(action_id), token.run_id),
            ).fetchone()
            if action is None:
                raise ValueError(
                    f"rebuild action does not exist in this run: {action_id}"
                )
            merged: dict[str, Any] | None = None
            if stop_evidence is not None:
                try:
                    restored = json.loads(str(action["stop_evidence_json"] or "{}"))
                except (TypeError, ValueError):
                    restored = {}
                merged = dict(restored) if isinstance(restored, dict) else {}
                merged.update(dict(stop_evidence))
            sets = ["phase=?", "updated_at=?"]
            values: list[Any] = [phase, now]
            if merged is not None:
                sets.append("stop_evidence_json=?")
                values.append(self._canonical_json(merged))
            if new_remote_job_id is not None:
                sets.append("new_remote_job_id=?")
                values.append(int(new_remote_job_id))
            if error_text is not None:
                sets.append("error_text=?")
                values.append(str(error_text))
            values.append(int(action["id"]))
            connection.execute(
                f"UPDATE remote_job_rebuild_actions SET {', '.join(sets)} WHERE id=?",
                values,
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM remote_job_rebuild_actions WHERE id=?",
                    (int(action["id"]),),
                ).fetchone()
            )  # type: ignore[return-value]

    def claim_remote_submission(self, token: LeaseToken, job_id: int) -> dict[str, Any] | None:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if job["state"] != "SUBMIT_INTENT" or int(job["post_attempted"]):
                return None
            connection.execute("UPDATE remote_jobs SET post_attempted=1, updated_at=? WHERE id=?", (self._utcnow(), job_id))
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())

    def mark_submit_unknown(self, token: LeaseToken, job_id: int, error: str) -> dict[str, Any]:
        from app.services.remote_diagnostics import sanitize_remote_diagnostic

        return self._set_job_state(
            token,
            job_id,
            "SUBMIT_UNKNOWN",
            error=sanitize_remote_diagnostic(error),
            require_post_attempted=True,
        )

    def bump_remote_job_recovery_attempt(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        diagnostic: str | None = None,
    ) -> int:
        """Count one failed SUBMIT_UNKNOWN reconciliation attempt (narrow).

        Returns the new durable ``recovery_attempts`` value.  Legal only while
        the job is exactly ``SUBMIT_UNKNOWN``: a job the tick already pushed to
        ``MANUAL_REQUIRED`` (or any other state) raises
        :class:`RemoteIntentConflict` instead of being silently bumped.
        """
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "SUBMIT_UNKNOWN":
                raise RemoteIntentConflict("recovery attempt bump requires the SUBMIT_UNKNOWN state")
            now = self._utcnow()
            if diagnostic is None:
                connection.execute(
                    "UPDATE remote_jobs SET recovery_attempts=recovery_attempts+1, updated_at=? WHERE id=?",
                    (now, job_id),
                )
            else:
                from app.services.remote_diagnostics import sanitize_remote_diagnostic
                from app.services.remote_execution_policy import (
                    decode_recovery_evidence,
                    encode_recovery_evidence,
                )

                new_attempts = int(job["recovery_attempts"] or 0) + 1
                evidence = decode_recovery_evidence(job["recovery_evidence_json"])
                evidence["submit_reconcile"] = {
                    "status": "NOT_FOUND",
                    "last_diagnostic": sanitize_remote_diagnostic(diagnostic),
                    "last_reconcile_at": now,
                    "recovery_attempts": new_attempts,
                }
                connection.execute(
                    "UPDATE remote_jobs SET recovery_attempts=?, recovery_evidence_json=?, updated_at=? WHERE id=?",
                    (new_attempts, encode_recovery_evidence(evidence), now, job_id),
                )
            refreshed = connection.execute(
                "SELECT recovery_attempts FROM remote_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return int(refreshed["recovery_attempts"] or 0)

    def record_submit_reconcile_diagnostic(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        status: str,
        diagnostic: str,
    ) -> dict[str, Any]:
        """Persist one sanitized read-only reconcile observation atomically."""
        from app.services.remote_diagnostics import sanitize_remote_diagnostic
        from app.services.remote_execution_policy import (
            decode_recovery_evidence,
            encode_recovery_evidence,
        )

        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "SUBMIT_UNKNOWN":
                raise RemoteIntentConflict("submit reconcile diagnostic requires the SUBMIT_UNKNOWN state")
            now = self._utcnow()
            evidence = decode_recovery_evidence(job["recovery_evidence_json"])
            evidence["submit_reconcile"] = {
                "status": sanitize_remote_diagnostic(status, max_length=80),
                "last_diagnostic": sanitize_remote_diagnostic(diagnostic),
                "last_reconcile_at": now,
                "recovery_attempts": int(job["recovery_attempts"] or 0),
            }
            connection.execute(
                "UPDATE remote_jobs SET recovery_evidence_json=?, updated_at=? WHERE id=?",
                (encode_recovery_evidence(evidence), now, job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def release_remote_submission_claim(self, token: LeaseToken, job_id: int, error: str) -> dict[str, Any]:
        """Retreat a burned claim only with proof the POST never started.

        Only an un-dispatched submit intent with no confirmed remote identity
        may return to claimable state; the immutable intent, bindings and
        uniqueness stay untouched and the durable diagnostic keeps the
        pre-send reason auditable.
        """
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "SUBMIT_INTENT":
                raise RemoteIntentConflict("pre-send release requires the un-dispatched submit intent state")
            if str(job["remote_identity"] or "").strip():
                raise RemoteIntentConflict("pre-send release requires no confirmed remote identity")
            if int(job["post_attempted"]):
                connection.execute(
                    "UPDATE remote_jobs SET post_attempted=0, error_text=?, updated_at=? WHERE id=?",
                    (error, self._utcnow(), job_id),
                )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())

    def supersede_unposted_prewarm_intent(
        self, token: LeaseToken, job_id: int, survivor_crawl_ids: Sequence[int], reason: str,
    ) -> bool:
        """Retire a never-posted placeholder job and free its exact crawl rows."""
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if (
                str(job["state"]) != "SUBMIT_INTENT"
                or int(job["post_attempted"] or 0)
                or str(job["remote_identity"] or "").strip()
            ):
                return False
            try:
                payload = json.loads(str(job["payload_json"] or ""))
                requests = payload["requests"]
                expected = [
                    (
                        str(request["mall_key"]), str(request["sku_id"]),
                        str(request["url"]), str(request["backend_version"]),
                    )
                    for request in requests
                ]
            except (KeyError, TypeError, ValueError):
                return False
            if not expected or len(expected) != len(set(expected)):
                return False
            rows = connection.execute(
                """SELECT id, mall_key, sku_id, exact_url, backend_version,
                          state, result_json
                   FROM crawl_work WHERE run_id=? AND remote_job_id=? ORDER BY id""",
                (token.run_id, job_id),
            ).fetchall()
            actual = [
                (str(row["mall_key"]), str(row["sku_id"]),
                 str(row["exact_url"]), str(row["backend_version"]))
                for row in rows
            ]
            survivor_ids = {int(work_id) for work_id in survivor_crawl_ids}
            row_ids = {int(row["id"]) for row in rows}
            if (
                len(rows) != len(expected)
                or set(actual) != set(expected)
                or not survivor_ids
                or not survivor_ids < row_ids
                or any(
                    str(row["state"]) != "PENDING"
                    or row["result_json"] is not None
                    or not str(row["sku_id"]).startswith("zhpq-")
                    or not is_placeholder_code(str(row["sku_id"]))
                    for row in rows
                )
            ):
                return False
            now = self._utcnow()
            detached = connection.execute(
                "UPDATE crawl_work SET remote_job_id=NULL, updated_at=? WHERE run_id=? AND remote_job_id=?",
                (now, token.run_id, job_id),
            )
            if detached.rowcount != len(rows):
                raise RemoteIntentConflict("unposted crawl detachment changed during retirement")
            retired_ids = sorted(row_ids - survivor_ids)
            placeholders = ",".join("?" for _ in retired_ids)
            retired = connection.execute(
                f"UPDATE crawl_work SET state='SUPERSEDED', error_text=?, updated_at=? "
                f"WHERE id IN ({placeholders}) AND remote_job_id IS NULL AND state='PENDING'",
                (str(reason), now, *retired_ids),
            )
            if retired.rowcount != len(retired_ids):
                raise RemoteIntentConflict("retired crawl set changed during unposted retirement")
            connection.execute(
                "UPDATE remote_jobs SET state='SUPERSEDED', error_text=?, updated_at=? WHERE id=?",
                (str(reason), now, job_id),
            )
            return True

    def mark_remote_manual(self, token: LeaseToken, job_id: int, error: str) -> dict[str, Any]:
        return self._set_job_state(token, job_id, "MANUAL_REQUIRED", error=error)

    def mark_remote_zero_write_manual(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        evidence: Mapping[str, Any],
        error: str,
    ) -> dict[str, Any]:
        """Terminalize a proven zero-write recovery outcome atomically.

        The truthful evidence document and the MANUAL_REQUIRED state must
        land in ONE transaction: writing evidence and state separately could
        crash between them and strand the write-ahead DISABLE_SENT phase (a
        pending remote write that never happened) on a terminal job — after
        a human requeue that fake pending phase would bypass the
        EXTERNALLY_DISABLED guard and could auto-enable a task a human
        deliberately disabled.  Legal only from RUNNING with an unburned
        attempt and the exact confirmed remote identity.
        """
        from app.services.remote_execution_policy import encode_recovery_evidence

        normalized_identity = str(remote_identity or "").strip()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "RUNNING":
                raise RemoteIntentConflict("zero-write manual requires the RUNNING state")
            if int(job["recovery_attempts"] or 0) != 0:
                raise RemoteIntentConflict("zero-write manual requires an unburned recovery attempt")
            if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
                raise RemoteIntentConflict("zero-write manual requires the exact confirmed remote identity")
            connection.execute(
                "UPDATE remote_jobs SET state='MANUAL_REQUIRED', recovery_evidence_json=?, error_text=?, updated_at=? WHERE id=?",
                (encode_recovery_evidence(evidence), str(error), self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def mark_remote_recovery_probe_identity_manual(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        evidence: Mapping[str, Any],
        error: str,
    ) -> dict[str, Any]:
        """Terminalize a probe-proven remote identity conflict atomically.

        The truthful probe evidence (``probe_health.identity_conflict``), the
        MANUAL_REQUIRED state, and the error text land in ONE transaction.
        Legal from RUNNING with the exact confirmed remote identity —
        REGARDLESS of ``recovery_attempts``: a post-restart probe may also
        prove the conflict, and the caller's evidence document must preserve
        the confirmed restart proof untouched (this method never derives or
        overwrites ``restart``).  Zero remote writes by construction: the
        transition records the outcome of a read-only probe.  The durable
        ``remote_identity`` is never re-bound (exact-match precondition).
        """
        from app.services.remote_execution_policy import encode_recovery_evidence

        normalized_identity = str(remote_identity or "").strip()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "RUNNING":
                raise RemoteIntentConflict(
                    "recovery probe identity manual requires the RUNNING state"
                )
            if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
                raise RemoteIntentConflict(
                    "recovery probe identity manual requires the exact confirmed remote identity"
                )
            connection.execute(
                "UPDATE remote_jobs SET state='MANUAL_REQUIRED', recovery_evidence_json=?, error_text=?, updated_at=? WHERE id=?",
                (encode_recovery_evidence(evidence), str(error), self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def requeue_manual_priority_job(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
    ) -> dict[str, Any]:
        """Explicitly requeue a confirmed remote task after a priority-only repair."""
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            remote_identity = str(remote_identity or "").strip()
            if (
                job["state"] != "MANUAL_REQUIRED"
                or not int(job["post_attempted"])
                or not remote_identity
                or str(job["remote_identity"] or "").strip() != remote_identity
            ):
                raise RemoteIntentConflict("manual priority requeue requires the exact confirmed remote identity")
            connection.execute(
                "UPDATE remote_jobs SET state='PRIORITY_PENDING', error_text=NULL, updated_at=? WHERE id=?",
                (self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def requeue_manual_harvest_job(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
    ) -> dict[str, Any]:
        """Explicitly retry export mapping for one confirmed remote task."""
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            remote_identity = str(remote_identity or "").strip()
            if (
                job["state"] != "MANUAL_REQUIRED"
                or not int(job["post_attempted"])
                or not remote_identity
                or str(job["remote_identity"] or "").strip() != remote_identity
            ):
                raise RemoteIntentConflict(
                    "manual harvest requeue requires the exact confirmed remote identity"
                )
            connection.execute(
                "UPDATE remote_jobs SET state='SUBMITTED', error_text=NULL, updated_at=? WHERE id=?",
                (self._utcnow(), job_id),
            )
            return self._row(
                connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone()
            )  # type: ignore[return-value]

    def recover_ambiguous_remote_submissions(self, token: LeaseToken) -> int:
        """Fail closed after a crash between POST dispatch and durable outcome."""
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            result = connection.execute(
                """UPDATE remote_jobs SET state='SUBMIT_UNKNOWN', error_text=COALESCE(error_text, 'interrupted after POST dispatch'), updated_at=?
                   WHERE run_id=? AND state='SUBMIT_INTENT' AND post_attempted=1""",
                (self._utcnow(), token.run_id),
            )
            return result.rowcount

    def reconcile_remote_job(self, token: LeaseToken, job_id: int, *, remote_identity: str, state: str = "SUBMITTED", export_path: str | None = None, result: Any | None = None) -> dict[str, Any]:
        if state not in {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"}:
            raise ValueError("remote reconciliation must use a known non-create state")
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if not int(job["post_attempted"]):
                raise RemoteIntentConflict("remote reconciliation requires durable post_attempted evidence")
            remote_identity = str(remote_identity or "").strip()
            if not remote_identity:
                raise RemoteIntentConflict("remote reconciliation requires an exact remote identity")
            existing_identity = str(job["remote_identity"] or "").strip()
            if existing_identity and existing_identity != remote_identity:
                raise RemoteIntentConflict("remote identity is immutable once recorded")
            legal = {
                "SUBMIT_INTENT": {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"},
                "SUBMIT_UNKNOWN": {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"},
                "PRIORITY_PENDING": {"PRIORITY_PENDING", "SUBMITTED"},
                "SUBMITTED": {"SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"},
                "RUNNING": {"RUNNING", "EXPORTED", "COMPLETED"},
                "EXPORTED": {"EXPORTED", "COMPLETED"},
                "COMPLETED": {"COMPLETED"},
            }
            if state not in legal.get(str(job["state"]), set()):
                raise RemoteIntentConflict(f"illegal remote state transition {job['state']} -> {state}")
            try:
                connection.execute("UPDATE remote_jobs SET state=?, remote_identity=?, export_path=COALESCE(?, export_path), result_json=COALESCE(?, result_json), error_text=NULL, updated_at=? WHERE id=?", (state, remote_identity, export_path, self._canonical_json(result) if result is not None else None, self._utcnow(), job_id))
            except sqlite3.IntegrityError as exc:
                raise RemoteIntentConflict(
                    "remote identity is already bound to another logical job in this daily run"
                ) from exc
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def reconcile_remote_job_and_crawl_work(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        state: str = "PRIORITY_PENDING",
    ) -> dict[str, Any]:
        """Atomically restore a found unknown job and its linked crawl rows.

        This narrow cross-day transition prevents an identity-bearing job from
        being stranded with stale crawl rows if the process exits between two
        otherwise separate store calls.
        """
        if state != "PRIORITY_PENDING":
            raise ValueError("atomic unknown reconciliation requires PRIORITY_PENDING")
        identity = str(remote_identity or "").strip()
        parts = identity.split("|")
        if len(parts) != 3 or not all(parts):
            raise RemoteIntentConflict("remote reconciliation requires an exact remote identity")
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "SUBMIT_UNKNOWN" or str(job["remote_identity"] or "").strip():
                raise RemoteIntentConflict("atomic unknown reconciliation requires an identity-less SUBMIT_UNKNOWN job")
            if not int(job["post_attempted"]):
                raise RemoteIntentConflict("remote reconciliation requires durable post_attempted evidence")
            try:
                connection.execute(
                    """UPDATE remote_jobs SET state='PRIORITY_PENDING', remote_identity=?,
                       error_text=NULL, updated_at=? WHERE id=?""",
                    (identity, self._utcnow(), job_id),
                )
            except sqlite3.IntegrityError as exc:
                raise RemoteIntentConflict(
                    "remote identity is already bound to another logical job in this daily run"
                ) from exc
            connection.execute(
                """UPDATE crawl_work SET state='PRIORITY_PENDING', updated_at=?
                   WHERE run_id=? AND remote_job_id=?
                     AND state NOT IN ('SUCCESS', 'NO_RESULT', 'TERMINAL_FAILED')""",
                (self._utcnow(), token.run_id, job_id),
            )
            return self._row(
                connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone()
            )  # type: ignore[return-value]

    def confirm_remote_job_priority_and_crawl_work(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
    ) -> dict[str, Any]:
        """Atomically advance a proven priority job and its linked crawl rows."""
        identity = str(remote_identity or "").strip()
        parts = identity.split("|")
        if len(parts) != 3 or not all(parts):
            raise RemoteIntentConflict("priority confirmation requires an exact remote identity")
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if (
                str(job["state"]) != "PRIORITY_PENDING"
                or str(job["remote_identity"] or "").strip() != identity
            ):
                raise RemoteIntentConflict(
                    "atomic priority confirmation requires the exact PRIORITY_PENDING job"
                )
            connection.execute(
                """UPDATE remote_jobs SET state='SUBMITTED', error_text=NULL, updated_at=?
                   WHERE id=?""",
                (self._utcnow(), job_id),
            )
            connection.execute(
                """UPDATE crawl_work SET state='SUBMITTED', updated_at=?
                   WHERE run_id=? AND remote_job_id=?
                     AND state NOT IN ('SUCCESS', 'NO_RESULT', 'TERMINAL_FAILED')""",
                (self._utcnow(), token.run_id, job_id),
            )
            return self._row(
                connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone()
            )  # type: ignore[return-value]

    def claim_harvest(self, token: LeaseToken, job_id: int, *, max_attempts: int, max_elapsed_seconds: float) -> dict[str, Any] | None:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if job["state"] not in {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "SUBMIT_UNKNOWN"}:
                return None
            if int(job["harvest_attempts"]) >= max_attempts or float(job["harvest_elapsed_seconds"]) >= max_elapsed_seconds:
                connection.execute("UPDATE remote_jobs SET state='MANUAL_REQUIRED', error_text=COALESCE(error_text, 'harvest limit reached'), updated_at=? WHERE id=?", (self._utcnow(), job_id))
                return None
            connection.execute("UPDATE remote_jobs SET harvest_attempts=harvest_attempts+1, updated_at=? WHERE id=?", (self._utcnow(), job_id))
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())

    def record_harvest(self, token: LeaseToken, job_id: int, *, elapsed_seconds: float, max_attempts: int, max_elapsed_seconds: float, error: str | None = None) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            elapsed = float(job["harvest_elapsed_seconds"]) + max(0.0, elapsed_seconds)
            attempts = int(job["harvest_attempts"])
            # The last permitted harvest may return terminal evidence. That
            # evidence must win over the retry cap instead of being discarded.
            manual = str(job["state"]) not in {"EXPORTED", "COMPLETED"} and (
                attempts >= max_attempts or elapsed >= max_elapsed_seconds
            )
            state = "MANUAL_REQUIRED" if manual else str(job["state"])
            stored_error = error
            if state == "SUBMIT_UNKNOWN" and error is None:
                stored_error = job["error_text"]
            connection.execute("UPDATE remote_jobs SET state=?, harvest_elapsed_seconds=?, error_text=?, updated_at=? WHERE id=?", (state, elapsed, stored_error, self._utcnow(), job_id))
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def update_remote_recovery_evidence(self, token: LeaseToken, job_id: int, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the RUNNING-stall recovery evidence document for one job.

        The evidence document is the only authority for progress observations
        and restart phases; it never mutates ``result_json``/``error_text``
        semantics.  Any state may record probe evidence, but the update is
        fenced by the daily lease like every other durable write.
        """
        from app.services.remote_execution_policy import encode_recovery_evidence

        encoded = encode_recovery_evidence(evidence)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) == "COMPLETED":
                raise RemoteIntentConflict("completed remote job evidence is immutable")
            connection.execute(
                "UPDATE remote_jobs SET recovery_evidence_json=?, updated_at=? WHERE id=?",
                (encoded, self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def record_remote_recovery_probe_failure(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        failed_at: str,
        failure_reason: str,
    ) -> dict[str, Any]:
        """Durably count one RUNNING-stall recovery-probe failure.

        The increment is recomputed from the STORE's durable evidence
        document (never a caller-held copy) and, once the consecutive count
        reaches ``RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES``, the
        MANUAL_REQUIRED state and its error land in the SAME transaction —
        no crash window can leave a terminal manual job whose evidence
        shows fewer failures, or a RUNNING job whose evidence already hit
        the cap.  Probe health is stored beside (never inside)
        ``restart``: a probe failure has not entered the restart protocol,
        and reaching the cap must never fabricate a pending write phase.
        """
        from app.services.remote_execution_policy import (
            RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES,
            decode_recovery_evidence,
            encode_recovery_evidence,
        )

        normalized_identity = str(remote_identity or "").strip()
        reason = str(failure_reason or "").strip() or "probe callback returned no evidence"
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "RUNNING":
                raise RemoteIntentConflict("recovery probe failure requires the RUNNING state")
            if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
                raise RemoteIntentConflict("recovery probe failure requires the exact confirmed remote identity")
            document = decode_recovery_evidence(job["recovery_evidence_json"])
            health = dict(document.get("probe_health") or {})
            try:
                consecutive = int(health.get("consecutive_failures") or 0) + 1
            except (TypeError, ValueError):
                consecutive = 1
            document["probe_health"] = {
                "consecutive_failures": consecutive,
                "last_failure_at": str(failed_at or "").strip(),
                "last_failure_reason": reason,
            }
            if consecutive >= RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES:
                # The terminal error must match the durable restart history
                # (review round 7): recovery_attempts==1 with a confirmed
                # restart means post-restart probes failed — claiming "no
                # restart was attempted" there would contradict the durable
                # evidence.  Any other recovery_attempts/phase combination is
                # an invariant anomaly and fails closed with the anomaly
                # named, never with a fabricated history.
                recovery_attempts = int(job["recovery_attempts"] or 0)
                restart_phase = str((document.get("restart") or {}).get("phase") or "").strip()
                if recovery_attempts == 0:
                    headline = (
                        f"remote stall recovery probe failed {consecutive} consecutive "
                        f"times; no restart was attempted"
                    )
                elif recovery_attempts == 1 and restart_phase == "ENABLE_CONFIRMED":
                    headline = (
                        f"remote stall recovery probe failed {consecutive} consecutive "
                        f"times; the single restart was already confirmed; "
                        f"post-restart recovery probes did not produce usable evidence"
                    )
                else:
                    headline = (
                        f"remote stall recovery probe failed {consecutive} consecutive "
                        f"times; inconsistent restart evidence "
                        f"(recovery_attempts={recovery_attempts}, "
                        f"restart phase={restart_phase or 'missing'})"
                    )
                connection.execute(
                    "UPDATE remote_jobs SET state='MANUAL_REQUIRED', recovery_evidence_json=?, error_text=?, updated_at=? WHERE id=?",
                    (
                        encode_recovery_evidence(document),
                        f"{headline}; last failure: {reason}",
                        self._utcnow(),
                        job_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE remote_jobs SET recovery_evidence_json=?, updated_at=? WHERE id=?",
                    (encode_recovery_evidence(document), self._utcnow(), job_id),
                )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def mark_remote_recovery_attempted(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Burn the single automatic restart slot for one exact remote job.

        The transition is legal only from ``RUNNING`` with a matching exact
        remote identity and a previously unburned attempt, so a resumed
        process can never authorize a second disable->enable.
        """
        from app.services.remote_execution_policy import encode_recovery_evidence, decode_recovery_evidence

        normalized_identity = str(remote_identity or "").strip()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "RUNNING":
                raise RemoteIntentConflict("recovery attempt requires the RUNNING state")
            if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
                raise RemoteIntentConflict("recovery attempt requires the exact confirmed remote identity")
            if int(job["recovery_attempts"] or 0) != 0:
                raise RemoteIntentConflict("remote job already consumed its single recovery attempt")
            document = decode_recovery_evidence(job["recovery_evidence_json"])
            merged_restart = dict(document.get("restart") or {})
            merged_restart.update(
                {
                    "phase": "ENABLE_CONFIRMED",
                    "recovery_attempts": 1,
                }
            )
            document["restart"] = merged_restart
            connection.execute(
                "UPDATE remote_jobs SET recovery_attempts=1, recovery_evidence_json=?, updated_at=? WHERE id=?",
                (encode_recovery_evidence(document), self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def mark_remote_recovery_exhausted(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Move one RUNNING job to the local RECOVERY_EXHAUSTED terminal state.

        This is a local business give-up, never a claim that the remote task
        finished: the durable restart evidence must prove the single exact
        disable->enable already ran and was confirmed before the day stops
        waiting on this job.
        """
        from app.services.remote_execution_policy import (
            decode_recovery_evidence,
            encode_recovery_evidence,
        )

        normalized_identity = str(remote_identity or "").strip()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            document = decode_recovery_evidence(job["recovery_evidence_json"])
            restart = document.get("restart") if isinstance(document.get("restart"), dict) else {}
            if str(job["state"]) != "RUNNING":
                raise RemoteIntentConflict("recovery exhaustion requires the RUNNING state")
            if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
                raise RemoteIntentConflict("recovery exhaustion requires the exact confirmed remote identity")
            if int(job["recovery_attempts"] or 0) != 1:
                raise RemoteIntentConflict("recovery exhaustion requires exactly one consumed recovery attempt")
            if str(restart.get("phase") or "") != "ENABLE_CONFIRMED":
                raise RemoteIntentConflict("recovery exhaustion requires confirmed disable and enable evidence")
            disable_evidence = restart.get("disable_evidence")
            enable_evidence = restart.get("enable_evidence")
            if not (
                isinstance(disable_evidence, dict)
                and disable_evidence.get("confirmed_disabled") is True
                and isinstance(enable_evidence, dict)
                and enable_evidence.get("confirmed_enabled") is True
            ):
                raise RemoteIntentConflict(
                    "recovery exhaustion requires disable_evidence.confirmed_disabled and "
                    "enable_evidence.confirmed_enabled to both be true"
                )
            recovery_at = str(restart.get("recovery_at") or "").strip()
            try:
                datetime.fromisoformat(recovery_at)
            except ValueError as exc:
                raise RemoteIntentConflict(
                    "recovery exhaustion requires a valid recovery timestamp"
                ) from exc
            merged = dict(evidence)
            merged_restart = dict(merged.get("restart") or {})
            merged_restart.setdefault("phase", "ENABLE_CONFIRMED")
            merged_restart.setdefault("recovery_attempts", 1)
            merged["restart"] = merged_restart
            connection.execute(
                "UPDATE remote_jobs SET state='RECOVERY_EXHAUSTED', recovery_evidence_json=?, error_text=?, updated_at=? WHERE id=?",
                (
                    encode_recovery_evidence(merged),
                    "remote stall recovery exhausted after one confirmed disable->enable restart",
                    self._utcnow(),
                    job_id,
                ),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def complete_crawl_work_recovery_exhausted(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        reason: str,
        restart_phase: str,
        recovery_attempt: int,
        recovery_at: str,
        disable_confirmed: bool,
        enable_confirmed: bool,
        last_progress_signature: object,
        export_count: object,
        export_rsp_desc: str,
    ) -> dict[str, Any]:
        """Persist the auditable no-price outcome for one abandoned crawl."""
        from app.services.remote_execution_policy import encode_recovery_evidence

        normalized_reason = str(reason or "").strip()
        normalized_identity = str(remote_identity or "").strip()
        if not normalized_reason or not normalized_identity:
            raise RemoteIntentConflict("recovery-exhausted crawl resolution requires exact evidence")
        if int(recovery_attempt) != 1 or str(restart_phase or "").strip() != "ENABLE_CONFIRMED":
            raise RemoteIntentConflict("recovery-exhausted crawl resolution requires the completed restart proof")
        if not disable_confirmed or not enable_confirmed:
            raise RemoteIntentConflict("recovery-exhausted crawl resolution requires confirmed disable and enable evidence")
        evidence = encode_recovery_evidence(
            {
                "evidence_type": "REMOTE_STALL_RECOVERY_EXHAUSTED",
                "no_result_reason": normalized_reason,
                "page_price_source": "招商局（无采集结果）",
                "page_price_status": "NO_RESULT",
                "page_price_value": None,
                "remote_identity": normalized_identity,
                "remote_state": "RECOVERY_EXHAUSTED",
                "recovery_attempt": 1,
                "recovery_at": str(recovery_at or ""),
                "disable_confirmed": True,
                "enable_confirmed": True,
                "post_recovery_harvest_checked": True,
                "last_progress_signature": last_progress_signature,
                "export_count": export_count,
                "export_rspDesc": str(export_rsp_desc or ""),
            }
        )
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip() != normalized_identity
                or job["state"] != "RECOVERY_EXHAUSTED"
            ):
                raise RemoteIntentConflict(
                    "recovery-exhausted crawl resolution requires the exact abandoned remote job"
                )
            if work["state"] == "NO_RESULT":
                if work["result_json"] != evidence or work["error_text"] != normalized_reason:
                    raise RemoteIntentConflict("no-result crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"SUCCESS", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT', result_json=?, error_text=?, updated_at=? WHERE id=?",
                (evidence, normalized_reason, self._utcnow(), crawl_work_id),
            )
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def _set_job_state(self, token: LeaseToken, job_id: int, state: str, *, error: str | None = None, require_post_attempted: bool = False) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if require_post_attempted and not int(job["post_attempted"]):
                raise ValueError("cannot record uncertain submit before durable post_attempted evidence")
            connection.execute("UPDATE remote_jobs SET state=?, error_text=?, updated_at=? WHERE id=?", (state, error, self._utcnow(), job_id))
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    @staticmethod
    def _job_for_lease(connection: sqlite3.Connection, token: LeaseToken, job_id: int) -> sqlite3.Row:
        job = connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or int(job["run_id"]) != token.run_id:
            raise ValueError("remote job must belong to the leased daily run")
        return job

    def ensure_crawl_work(
        self,
        token: LeaseToken,
        mall_key: str,
        sku_id: str,
        exact_url: str,
        backend_version: str,
        fingerprint: str,
        *,
        source_id: str = "",
    ) -> dict[str, Any]:
        normalized_url = exact_url.strip()
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            work = connection.execute("SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND source_id=? AND sku_id=? AND exact_url=? AND backend_version=?", (token.run_id, mall_key, source_id, sku_id, normalized_url, backend_version)).fetchone()
            if work is None:
                cursor = connection.execute("INSERT INTO crawl_work(run_id, mall_key, source_id, sku_id, exact_url, backend_version, fingerprint, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (token.run_id, mall_key, source_id, sku_id, normalized_url, backend_version, fingerprint, now))
                work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (cursor.lastrowid,)).fetchone()
            elif (
                work["remote_job_id"] is None
                and work["state"] == "SUPERSEDED"
                and sku_id.startswith("zhpq-")
                and is_placeholder_code(sku_id)
            ):
                connection.execute(
                    "UPDATE crawl_work SET fingerprint=?, state='PENDING', result_json=NULL, error_text=NULL, updated_at=? WHERE id=?",
                    (fingerprint, now, work["id"]),
                )
                work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (work["id"],)).fetchone()
            elif work["fingerprint"] != fingerprint:
                if work["remote_job_id"] is None and work["state"] in {"PENDING", "RETRYABLE_FAILED"}:
                    connection.execute("UPDATE crawl_work SET fingerprint=?, state='PENDING', result_json=NULL, error_text=NULL, updated_at=? WHERE id=?", (fingerprint, now, work["id"]))
                work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (work["id"],)).fetchone()
            return self._row(work)  # type: ignore[return-value]

    def link_crawl_work_to_remote_job(self, token: LeaseToken, crawl_work_id: int, job_id: int) -> dict[str, Any]:
        return self.link_crawl_work_batch_to_remote_job(token, [crawl_work_id], job_id)[0]

    def link_crawl_work_batch_to_remote_job(
        self, token: LeaseToken, crawl_work_ids: Sequence[int], job_id: int,
    ) -> list[dict[str, Any]]:
        """Bind one immutable crawl intent atomically, or leave every row untouched."""
        ids = list(dict.fromkeys(int(work_id) for work_id in crawl_work_ids))
        if not ids:
            return []
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            self._job_for_lease(connection, token, job_id)
            placeholders = ",".join("?" for _ in ids)
            rows = connection.execute(f"SELECT * FROM crawl_work WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
            if len(rows) != len(ids) or any(int(row["run_id"]) != token.run_id for row in rows):
                raise ValueError("crawl work must belong to the leased daily run")
            for work in rows:
                if work["remote_job_id"] is not None and int(work["remote_job_id"]) != job_id:
                    raise RemoteIntentConflict("crawl work is already bound to another remote job")
                if work["remote_job_id"] is None and work["state"] in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
                    raise RemoteIntentConflict("terminal crawl work cannot be rebound")
            connection.execute(
                f"UPDATE crawl_work SET remote_job_id=?, updated_at=? WHERE id IN ({placeholders}) AND remote_job_id IS NULL",
                (job_id, self._utcnow(), *ids),
            )
            bound = connection.execute(f"SELECT * FROM crawl_work WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
            by_id = {int(row["id"]): self._row(row) for row in bound}
            return [by_id[work_id] for work_id in ids]  # type: ignore[list-item]

    def get_crawl_work(self, crawl_work_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())

    def reuse_successful_crawl_work(self, token: LeaseToken, crawl_work_id: int) -> dict[str, Any] | None:
        """Reuse or follow exact backend SKU/URL work for a new source document.

        ``source_id`` remains part of the durable business crawl identity, but
        the legacy backend request contains only mall/SKU/URL.  A new source
        relationship therefore follows an already submitted backend request
        instead of placing that same request into a second mixed-delta job.
        """
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            target = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            if target is None or int(target["run_id"]) != token.run_id:
                raise ValueError("crawl work must belong to the leased daily run")
            if target["state"] in {"SUCCESS", "NO_RESULT"}:
                return self._row(target)
            if target["remote_job_id"] is not None or target["state"] not in {"PENDING", "RETRYABLE_FAILED"}:
                return None
            source = connection.execute(
                """SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND sku_id=? AND exact_url=?
                   AND backend_version=? AND state IN ('SUCCESS', 'NO_RESULT')
                   AND result_json IS NOT NULL AND id<>?
                   ORDER BY CASE state WHEN 'SUCCESS' THEN 0 ELSE 1 END, id LIMIT 1""",
                (token.run_id, target["mall_key"], target["sku_id"], target["exact_url"], target["backend_version"], crawl_work_id),
            ).fetchone()
            if source is None:
                source = connection.execute(
                    """SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND sku_id=? AND exact_url=?
                       AND backend_version=? AND remote_job_id IS NOT NULL AND id<>?
                       AND state IN ('PENDING', 'PRIORITY_PENDING', 'SUBMITTED', 'RUNNING', 'EXPORTED', 'RETRYABLE_FAILED', 'SUBMIT_UNKNOWN')
                       ORDER BY id LIMIT 1""",
                    (
                        token.run_id,
                        target["mall_key"],
                        target["sku_id"],
                        target["exact_url"],
                        target["backend_version"],
                        crawl_work_id,
                    ),
                ).fetchone()
                if source is None:
                    source = connection.execute(
                        """SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND sku_id=? AND exact_url=?
                           AND backend_version=? AND state='TERMINAL_FAILED' AND id<>?
                           ORDER BY id LIMIT 1""",
                        (
                            token.run_id,
                            target["mall_key"],
                            target["sku_id"],
                            target["exact_url"],
                            target["backend_version"],
                            crawl_work_id,
                        ),
                    ).fetchone()
                    if source is None:
                        # Issue #10: URL-only placeholder-sibling tiers.  A prewarm
                        # placeholder crawl (``zhpq-`` sku, semantic guard
                        # ``is_placeholder_code``) proves the same (mall, url, backend)
                        # work already exists, so a new source document follows the very
                        # same remote job / result instead of duplicating it.  Matched
                        # WITHOUT sku (five-tuple semantics of the tiers above stay
                        # untouched), and a failed placeholder never terminal-fails a
                        # real STEP1 row (no TERMINAL_FAILED propagation).
                        placeholder = connection.execute(
                            """SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND exact_url=?
                               AND backend_version=? AND remote_job_id IS NOT NULL AND id<>?
                               AND state IN ('PENDING', 'PRIORITY_PENDING', 'SUBMITTED', 'RUNNING', 'EXPORTED', 'RETRYABLE_FAILED', 'SUBMIT_UNKNOWN')
                               ORDER BY id""",
                            (token.run_id, target["mall_key"], target["exact_url"], target["backend_version"], crawl_work_id),
                        ).fetchall()
                        source = next((row for row in placeholder if is_placeholder_code(row["sku_id"])), None)
                        if source is not None:
                            connection.execute(
                                """UPDATE crawl_work SET state=?, remote_job_id=?, error_text=NULL, updated_at=?
                                   WHERE id=? AND remote_job_id IS NULL""",
                                (
                                    source["state"],
                                    source["remote_job_id"],
                                    self._utcnow(),
                                    crawl_work_id,
                                ),
                            )
                            return self._row(
                                connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
                            )
                        placeholder = connection.execute(
                            """SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND exact_url=?
                               AND backend_version=? AND state IN ('SUCCESS', 'NO_RESULT')
                               AND result_json IS NOT NULL AND id<>?
                               ORDER BY CASE state WHEN 'SUCCESS' THEN 0 ELSE 1 END, id""",
                            (token.run_id, target["mall_key"], target["exact_url"], target["backend_version"], crawl_work_id),
                        ).fetchall()
                        source = next((row for row in placeholder if is_placeholder_code(row["sku_id"])), None)
                        if source is not None:
                            connection.execute(
                                """UPDATE crawl_work SET state=?, result_json=?, error_text=?, updated_at=? WHERE id=?""",
                                (
                                    source["state"],
                                    source["result_json"],
                                    source["error_text"] if source["state"] == "NO_RESULT" else None,
                                    self._utcnow(),
                                    crawl_work_id,
                                ),
                            )
                            return self._row(
                                connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
                            )
                        return None
                    connection.execute(
                        """UPDATE crawl_work SET state='TERMINAL_FAILED', result_json=?, error_text=?, updated_at=?
                           WHERE id=? AND remote_job_id IS NULL""",
                        (
                            source["result_json"],
                            source["error_text"],
                            self._utcnow(),
                            crawl_work_id,
                        ),
                    )
                    return self._row(
                        connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
                    )
                connection.execute(
                    """UPDATE crawl_work SET state=?, remote_job_id=?, error_text=NULL, updated_at=?
                       WHERE id=? AND remote_job_id IS NULL""",
                    (
                        source["state"],
                        source["remote_job_id"],
                        self._utcnow(),
                        crawl_work_id,
                    ),
                )
                return self._row(
                    connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
                )
            connection.execute(
                """UPDATE crawl_work SET state=?, result_json=?, error_text=?, updated_at=? WHERE id=?""",
                (
                    source["state"],
                    source["result_json"],
                    source["error_text"] if source["state"] == "NO_RESULT" else None,
                    self._utcnow(),
                    crawl_work_id,
                ),
            )
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def list_crawl_work(self, token: LeaseToken, *, states: set[str] | None = None) -> list[dict[str, Any]]:
        with self._transaction(immediate=False) as connection:
            self._validate_lease(connection, token)
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = connection.execute(f"SELECT * FROM crawl_work WHERE run_id=? AND state IN ({placeholders}) ORDER BY id", (token.run_id, *sorted(states))).fetchall()
            else:
                rows = connection.execute("SELECT * FROM crawl_work WHERE run_id=? ORDER BY id", (token.run_id,)).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def observe_local_fallback_remote_checks(
        self,
        token: LeaseToken,
        checked_at: str,
        supported_malls: set[str] | frozenset[str],
        *,
        checked_remote_job_ids: set[int] | frozenset[int],
    ) -> list[dict[str, Any]]:
        """Ensure fallback rows and stamp only remote jobs actually checked.

        ``crawl_work.updated_at`` is deliberately not used: it may change for
        unrelated lifecycle transitions and cannot prove a harvest/reconcile.
        """
        checked = self._utc_timestamp(checked_at, field="checked_at")
        assert checked is not None
        malls = sorted({str(value).strip() for value in supported_malls if str(value).strip()})
        checked_job_ids = {int(value) for value in checked_remote_job_ids}
        if not malls:
            return []
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            placeholders = ",".join("?" for _ in malls)
            rows = connection.execute(
                f"""SELECT c.*, r.created_at AS remote_started_at,
                            f.id AS fallback_id, f.state AS fallback_state,
                            f.last_remote_check_at, f.attempt_count,
                            f.last_attempt_tick_id
                       FROM crawl_work c
                       JOIN remote_jobs r ON r.id=c.remote_job_id AND r.run_id=c.run_id
                       LEFT JOIN local_fallback_work f ON f.crawl_work_id=c.id
                      WHERE c.run_id=? AND c.mall_key IN ({placeholders})
                        AND c.state<>'SUCCESS'
                      ORDER BY c.id""",
                (token.run_id, *malls),
            ).fetchall()
            now = self._utcnow()
            observed: list[dict[str, Any]] = []
            for row in rows:
                previous = self._row(row)
                assert previous is not None
                if row["fallback_id"] is None:
                    connection.execute(
                        """INSERT INTO local_fallback_work(
                               run_id, crawl_work_id, mall_key, exact_url,
                               last_remote_check_at, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            token.run_id, row["id"], row["mall_key"], row["exact_url"],
                            checked if int(row["remote_job_id"]) in checked_job_ids else None,
                            now, now,
                        ),
                    )
                elif int(row["remote_job_id"]) in checked_job_ids:
                    connection.execute(
                        "UPDATE local_fallback_work SET last_remote_check_at=?, updated_at=? WHERE id=?",
                        (checked, now, row["fallback_id"]),
                    )
                observed.append(previous)
            return observed

    def claim_local_fallback(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        eligible_reason: str,
        tick_id: str,
        claimed_at: str,
        *,
        max_attempts: int = 2,
    ) -> dict[str, Any] | None:
        """Atomically claim one exact URL; a second process receives ``None``."""
        if eligible_reason not in {"DAYTIME_60M", "PEAK_3M", "AFTER_1800"}:
            raise ValueError("unknown local fallback eligibility reason")
        claimed = self._utc_timestamp(claimed_at, field="claimed_at")
        assert claimed is not None
        normalized_tick = str(tick_id or "").strip()
        if not normalized_tick:
            raise ValueError("local fallback tick_id is required")
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            crawl = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert crawl is not None
            if crawl["state"] == "SUCCESS":
                return None
            work = connection.execute(
                "SELECT * FROM local_fallback_work WHERE crawl_work_id=?", (crawl_work_id,)
            ).fetchone()
            if work is None:
                raise RemoteIntentConflict("local fallback requires a persisted remote check")
            if work["state"] == "CLAIMED":
                return None
            if work["state"] in {"SUCCESS", "TERMINAL_FAILED"}:
                return None
            if work["last_attempt_tick_id"] == normalized_tick or int(work["attempt_count"]) >= max_attempts:
                return None
            try:
                changed = connection.execute(
                    """UPDATE local_fallback_work
                          SET state='CLAIMED', eligible_reason=?, claim_at=?, claim_tick_id=?,
                              last_attempt_tick_id=?, attempt_count=attempt_count+1,
                              error_text=NULL, updated_at=?
                        WHERE id=? AND state IN ('PENDING','RETRYABLE_FAILED')""",
                    (eligible_reason, claimed, normalized_tick, normalized_tick, self._utcnow(), work["id"]),
                )
            except sqlite3.IntegrityError:
                return None
            if changed.rowcount != 1:
                return None
            row = connection.execute(
                """SELECT f.*, c.sku_id, c.source_id, c.backend_version, c.remote_job_id
                     FROM local_fallback_work f JOIN crawl_work c ON c.id=f.crawl_work_id
                    WHERE f.id=?""",
                (work["id"],),
            ).fetchone()
            return self._row(row)

    def defer_unstarted_local_fallback(
        self, token: LeaseToken, fallback_id: int, tick_id: str
    ) -> dict[str, Any]:
        """Return an unlaunched claim to the next tick without charging an attempt."""
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            work = connection.execute(
                "SELECT * FROM local_fallback_work WHERE id=? AND run_id=?", (fallback_id, token.run_id)
            ).fetchone()
            if work is None or work["state"] != "CLAIMED" or work["claim_tick_id"] != str(tick_id):
                raise RemoteIntentConflict("budget deferral requires this tick's unstarted claim")
            prior_attempts = int(work["attempt_count"]) - 1
            connection.execute(
                """UPDATE local_fallback_work
                      SET state=?, attempt_count=?, claim_at=NULL, claim_tick_id=NULL,
                          error_text='budget deferred before child launch', updated_at=?
                    WHERE id=?""",
                (
                    "PENDING" if prior_attempts == 0 else "RETRYABLE_FAILED",
                    prior_attempts, self._utcnow(), fallback_id,
                ),
            )
            return self._row(connection.execute("SELECT * FROM local_fallback_work WHERE id=?", (fallback_id,)).fetchone())  # type: ignore[return-value]

    def recover_interrupted_local_fallback_claims(
        self,
        token: LeaseToken,
        tick_id: str,
        recovered_at: str,
        *,
        max_attempts: int = 2,
    ) -> int:
        """Release old crashed claims, never claims from the same/current instant."""
        recovered = self._utc_timestamp(recovered_at, field="recovered_at")
        assert recovered is not None
        current = datetime.fromisoformat(recovered)
        normalized_tick = str(tick_id or "").strip()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            rows = connection.execute(
                "SELECT * FROM local_fallback_work WHERE run_id=? AND state='CLAIMED' AND claim_tick_id<>?",
                (token.run_id, normalized_tick),
            ).fetchall()
            count = 0
            for row in rows:
                claim_at = datetime.fromisoformat(str(row["claim_at"]))
                if (current - claim_at).total_seconds() < 1:
                    continue
                state = "RETRYABLE_FAILED" if int(row["attempt_count"]) < max_attempts else "TERMINAL_FAILED"
                connection.execute(
                    "UPDATE local_fallback_work SET state=?, error_text='interrupted local fallback claim', updated_at=? WHERE id=?",
                    (state, self._utcnow(), row["id"]),
                )
                if state == "TERMINAL_FAILED":
                    connection.execute(
                        """UPDATE crawl_work SET state='MANUAL_REQUIRED',
                                  error_text='local fallback: interrupted claim exhausted retry budget',
                                  updated_at=?
                            WHERE id=?
                              AND state NOT IN ('SUCCESS','NO_RESULT','TERMINAL_FAILED')""",
                        (self._utcnow(), row["crawl_work_id"]),
                    )
                count += 1
            return count

    def complete_local_fallback(
        self,
        token: LeaseToken,
        fallback_id: int,
        result: Any,
        *,
        crawler_revision: str,
        completed_at: str,
    ) -> dict[str, Any]:
        """Commit local evidence and fan it out to duplicate exact URLs."""
        completed = self._utc_timestamp(completed_at, field="completed_at")
        assert completed is not None
        encoded = self._canonical_json(result)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            work = connection.execute(
                "SELECT * FROM local_fallback_work WHERE id=? AND run_id=?", (fallback_id, token.run_id)
            ).fetchone()
            if work is None or work["state"] != "CLAIMED":
                raise RemoteIntentConflict("local fallback completion requires its active claim")
            connection.execute(
                """UPDATE crawl_work SET state='SUCCESS', result_json=?, error_text=NULL, updated_at=?
                    WHERE run_id=? AND mall_key=? AND exact_url=? AND state<>'SUCCESS'""",
                (encoded, self._utcnow(), token.run_id, work["mall_key"], work["exact_url"]),
            )
            connection.execute(
                """UPDATE local_fallback_work
                      SET state='SUCCESS', completed_at=?, crawler_revision=?, result_json=?,
                          error_text=NULL, updated_at=?
                    WHERE run_id=? AND mall_key=? AND exact_url=?
                      AND state NOT IN ('SUCCESS','TERMINAL_FAILED')""",
                (
                    completed, crawler_revision, encoded, self._utcnow(), token.run_id,
                    work["mall_key"], work["exact_url"],
                ),
            )
            return self._row(connection.execute("SELECT * FROM local_fallback_work WHERE id=?", (fallback_id,)).fetchone())  # type: ignore[return-value]

    def fail_local_fallback(
        self,
        token: LeaseToken,
        fallback_id: int,
        error: str,
        *,
        retryable: bool,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            work = connection.execute(
                "SELECT * FROM local_fallback_work WHERE id=? AND run_id=?", (fallback_id, token.run_id)
            ).fetchone()
            if work is None or work["state"] != "CLAIMED":
                raise RemoteIntentConflict("local fallback failure requires its active claim")
            state = "RETRYABLE_FAILED" if retryable and int(work["attempt_count"]) < max_attempts else "TERMINAL_FAILED"
            connection.execute(
                "UPDATE local_fallback_work SET state=?, error_text=?, updated_at=? WHERE id=?",
                (state, str(error), self._utcnow(), fallback_id),
            )
            if state == "TERMINAL_FAILED":
                connection.execute(
                    """UPDATE crawl_work SET state='MANUAL_REQUIRED', error_text=?, updated_at=?
                        WHERE run_id=? AND mall_key=? AND exact_url=?
                          AND state NOT IN ('SUCCESS','NO_RESULT','TERMINAL_FAILED')""",
                    (f"local fallback: {error}", self._utcnow(), token.run_id, work["mall_key"], work["exact_url"]),
                )
            return self._row(connection.execute("SELECT * FROM local_fallback_work WHERE id=?", (fallback_id,)).fetchone())  # type: ignore[return-value]

    def list_local_fallback_work(self, token: LeaseToken) -> list[dict[str, Any]]:
        with self._transaction(immediate=False) as connection:
            self._validate_lease(connection, token)
            return [self._row(row) for row in connection.execute(
                """SELECT f.*, c.state AS crawl_state,
                           c.result_json AS crawl_result_json, c.remote_job_id
                      FROM local_fallback_work f
                      JOIN crawl_work c ON c.id=f.crawl_work_id
                     WHERE f.run_id=? ORDER BY f.id""",
                (token.run_id,)
            ).fetchall()]  # type: ignore[list-item]

    def complete_crawl_work(self, token: LeaseToken, crawl_work_id: int, result: Any) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            encoded = self._canonical_json(result)
            if work["state"] == "SUCCESS":
                if work["result_json"] != encoded:
                    raise RemoteIntentConflict("successful crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"NO_RESULT", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("terminal crawl cannot become successful")
            connection.execute("UPDATE crawl_work SET state='SUCCESS', result_json=?, error_text=NULL, updated_at=? WHERE id=?", (encoded, self._utcnow(), crawl_work_id))
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def complete_terminal_crawl_from_final_remote_check(
        self, token: LeaseToken, crawl_work_id: int, remote_job_id: int, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """A newly checked remote price may resolve an earlier remote terminal result."""
        if not valid_page_price_result(result) or not str(result.get("page_price_source") or "").startswith("招商局"):
            raise ValueError("final remote result must be a valid Zhaoshang price")
        encoded = self._canonical_json(result)
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            if int(work["remote_job_id"] or 0) != int(remote_job_id):
                raise RemoteIntentConflict("final remote price requires the exact linked job")
            if work["state"] not in {"NO_RESULT", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("final remote terminal supersession requires remote terminal state")
            connection.execute(
                "UPDATE crawl_work SET state='SUCCESS', result_json=?, error_text=NULL, updated_at=? WHERE id=?",
                (encoded, self._utcnow(), crawl_work_id),
            )
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def complete_partial_exact_crawl_work(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        result: Any,
    ) -> dict[str, Any]:
        """Persist one exact partial-export result bound to one confirmed job."""
        encoded = self._canonical_json(result)
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute(
                "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
            ).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip()
                != str(remote_identity or "").strip()
                or not int(job["post_attempted"])
                or job["state"] not in {"MANUAL_REQUIRED", "COMPLETED"}
            ):
                raise RemoteIntentConflict(
                    "partial exact result requires the exact confirmed manual/completed job"
                )
            if work["state"] == "SUCCESS":
                if work["result_json"] != encoded:
                    raise RemoteIntentConflict("successful crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"NO_RESULT", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            connection.execute(
                "UPDATE crawl_work SET state='SUCCESS', result_json=?, error_text=NULL, updated_at=? WHERE id=?",
                (encoded, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
                ).fetchone()
            )  # type: ignore[return-value]

    def complete_partial_exact_no_result(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        result_path: str,
        reason: str = "legacy export contains no matching durable identity row",
    ) -> dict[str, Any]:
        """Persist exact absence from a completed, provenance-valid export."""
        normalized_reason = str(reason or "").strip()
        normalized_path = str(result_path or "").strip()
        if not normalized_reason or not normalized_path:
            raise RemoteIntentConflict("partial no-result requires exact export evidence")
        evidence = self._canonical_json(
            {
                "evidence_type": "EXPORTED_NO_MATCHING_ROW",
                "no_result_reason": normalized_reason,
                "page_price_source": "招商局（无采集结果）",
                "page_price_status": "NO_RESULT",
                "page_price_value": None,
                "remote_identity": str(remote_identity or "").strip(),
                "remote_state": "COMPLETED",
                "result_path": normalized_path,
            }
        )
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute(
                "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
            ).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip()
                != str(remote_identity or "").strip()
                or not int(job["post_attempted"])
                or job["state"] not in {
                    "PRIORITY_PENDING",
                    "SUBMITTED",
                    "RUNNING",
                    "EXPORTED",
                    "MANUAL_REQUIRED",
                    "COMPLETED",
                }
                or str(job["result_path"] or "").strip() != normalized_path
            ):
                raise RemoteIntentConflict(
                    "partial no-result requires the exact confirmed export job"
                )
            if work["state"] == "NO_RESULT":
                if work["result_json"] != evidence or work["error_text"] != normalized_reason:
                    raise RemoteIntentConflict("no-result crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"SUCCESS", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT', result_json=?, error_text=?, updated_at=? WHERE id=?",
                (evidence, normalized_reason, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
                ).fetchone()
            )  # type: ignore[return-value]

    def mark_partial_exact_crawl_manual(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        error: str,
    ) -> dict[str, Any]:
        normalized_error = str(error or "").strip()
        if not normalized_error:
            raise RemoteIntentConflict("partial crawl manual state requires a reason")
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute(
                "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
            ).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip()
                != str(remote_identity or "").strip()
                or job["state"] != "MANUAL_REQUIRED"
            ):
                raise RemoteIntentConflict("partial crawl manual state requires the exact manual job")
            if work["state"] in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
                return self._row(work)  # type: ignore[return-value]
            if work["state"] == "MANUAL_REQUIRED" and work["error_text"] == normalized_error:
                return self._row(work)  # type: ignore[return-value]
            connection.execute(
                "UPDATE crawl_work SET state='MANUAL_REQUIRED', result_json=NULL, error_text=?, updated_at=? WHERE id=?",
                (normalized_error, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
                ).fetchone()
            )  # type: ignore[return-value]

    def finish_partial_exact_harvest_job(
        self,
        token: LeaseToken,
        remote_job_id: int,
        *,
        remote_identity: str,
        export_path: str,
        result: Any,
    ) -> dict[str, Any]:
        """Complete the job only when every linked crawl is deterministically resolved."""
        normalized_identity = str(remote_identity or "").strip()
        normalized_path = str(export_path or "").strip()
        encoded_result = self._canonical_json(result)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                str(job["remote_identity"] or "").strip() != normalized_identity
                or not int(job["post_attempted"])
                or job["state"] not in {"MANUAL_REQUIRED", "COMPLETED"}
                or str(job["result_path"] or "").strip() != normalized_path
            ):
                raise RemoteIntentConflict(
                    "partial harvest finish requires the exact confirmed export job"
                )
            states = [
                str(row["state"])
                for row in connection.execute(
                    "SELECT state FROM crawl_work WHERE run_id=? AND remote_job_id=? ORDER BY id",
                    (token.run_id, int(remote_job_id)),
                ).fetchall()
            ]
            if not states:
                raise RemoteIntentConflict("partial harvest job has no linked crawl work")
            completed = all(state in {"SUCCESS", "NO_RESULT"} for state in states)
            state = "COMPLETED" if completed else "MANUAL_REQUIRED"
            error = None if completed else "partial exact harvest has unresolved crawl identities"
            if (
                str(job["state"]) == state
                and str(job["export_path"] or "") == normalized_path
                and str(job["result_json"] or "") == encoded_result
                and str(job["error_text"] or "") == str(error or "")
            ):
                return self._row(job)  # type: ignore[return-value]
            connection.execute(
                "UPDATE remote_jobs SET state=?, export_path=?, result_json=?, error_text=?, updated_at=? WHERE id=?",
                (state, normalized_path, encoded_result, error, self._utcnow(), int(remote_job_id)),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM remote_jobs WHERE id=?", (int(remote_job_id),)
                ).fetchone()
            )  # type: ignore[return-value]

    def complete_crawl_work_without_result(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Record an explicit no-price business outcome from one exact completed export."""
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise RemoteIntentConflict("no-result resolution requires the persisted failure reason")
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            if work["state"] == "NO_RESULT":
                try:
                    evidence = json.loads(str(work["result_json"] or ""))
                except (TypeError, ValueError) as exc:
                    raise RemoteIntentConflict("no-result crawl evidence is immutable") from exc
                if (
                    str(work["error_text"] or "").strip() != normalized_reason
                    or not isinstance(evidence, dict)
                    or evidence.get("evidence_type") != "EXPORTED_NO_MATCHING_ROW"
                    or evidence.get("no_result_reason") != normalized_reason
                    or evidence.get("page_price_status") != "NO_RESULT"
                    or evidence.get("page_price_value") is not None
                ):
                    raise RemoteIntentConflict("no-result crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] != "TERMINAL_FAILED" or str(work["error_text"] or "").strip() != normalized_reason:
                raise RemoteIntentConflict("no-result resolution requires the exact terminal failure evidence")
            if work["remote_job_id"] is None:
                raise RemoteIntentConflict("no-result resolution requires completed export evidence")
            job = self._job_for_lease(connection, token, int(work["remote_job_id"]))
            remote_identity = str(job["remote_identity"] or "").strip()
            result_path = str(job["result_path"] or "").strip()
            if (
                job["state"] != "COMPLETED"
                or not int(job["post_attempted"])
                or not remote_identity
                or not result_path
                or job["result_json"] is None
            ):
                raise RemoteIntentConflict("no-result resolution requires completed export evidence")
            evidence = self._canonical_json(
                {
                    "evidence_type": "EXPORTED_NO_MATCHING_ROW",
                    "no_result_reason": normalized_reason,
                    "page_price_source": "招商局（无采集结果）",
                    "page_price_status": "NO_RESULT",
                    "page_price_value": None,
                    "remote_identity": remote_identity,
                    "remote_state": "COMPLETED",
                    "result_path": result_path,
                }
            )
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT', result_json=?, updated_at=? WHERE id=?",
                (evidence, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            )  # type: ignore[return-value]

    def complete_crawl_work_export_no_price(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        reason: str,
        export_rsp_desc: str,
        export_count: int,
    ) -> dict[str, Any]:
        """Persist terminal export-stage no-price evidence without a result workbook."""
        normalized_reason = str(reason or "").strip()
        normalized_rsp_desc = str(export_rsp_desc or "").strip()
        if not normalized_reason or not normalized_rsp_desc:
            raise RemoteIntentConflict("export no-price resolution requires exact export evidence")
        if int(export_count) != 0:
            raise RemoteIntentConflict("export no-price resolution requires a zero export count")
        evidence = self._canonical_json(
            {
                "evidence_type": "EXPORT_NO_PRICE_TERMINAL",
                "no_result_reason": normalized_reason,
                "page_price_source": "招商局（无采集结果）",
                "page_price_status": "NO_RESULT",
                "page_price_value": None,
                "remote_identity": str(remote_identity or "").strip(),
                "remote_state": "COMPLETED",
                "export_rspDesc": normalized_rsp_desc,
                "export_count": 0,
            }
        )
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip() != str(remote_identity or "").strip()
                or not int(job["post_attempted"])
                or job["state"] not in {"MANUAL_REQUIRED", "COMPLETED"}
            ):
                raise RemoteIntentConflict(
                    "export no-price resolution requires the exact confirmed remote job"
                )
            if work["state"] == "NO_RESULT":
                if work["result_json"] != evidence or work["error_text"] != normalized_reason:
                    raise RemoteIntentConflict("no-result crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"SUCCESS", "TERMINAL_FAILED", "MANUAL_REQUIRED"}:
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            if job["state"] != "MANUAL_REQUIRED":
                raise RemoteIntentConflict(
                    "export no-price crawl transition requires the manual remote job"
                )
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT', result_json=?, error_text=?, updated_at=? WHERE id=?",
                (evidence, normalized_reason, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            )  # type: ignore[return-value]

    def complete_manual_export_no_price_job(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        result: Any,
    ) -> dict[str, Any]:
        """Complete one MANUAL wait-export job whose export stage proved terminal no-price."""
        normalized_identity = str(remote_identity or "").strip()
        if not normalized_identity:
            raise RemoteIntentConflict("export no-price completion requires an exact remote identity")
        encoded_result = self._canonical_json(result)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, int(job_id))
            if (
                str(job["remote_identity"] or "").strip() != normalized_identity
                or not int(job["post_attempted"])
            ):
                raise RemoteIntentConflict(
                    "export no-price completion requires the exact confirmed remote job"
                )
            if job["state"] == "COMPLETED":
                if str(job["result_json"] or "") != encoded_result:
                    raise RemoteIntentConflict("completed remote job evidence is immutable")
                return self._row(job)  # type: ignore[return-value]
            if job["state"] != "MANUAL_REQUIRED":
                raise RemoteIntentConflict(
                    "export no-price completion requires the manual wait-export job"
                )
            connection.execute(
                "UPDATE remote_jobs SET state='COMPLETED', result_json=?, error_text=NULL, updated_at=? WHERE id=?",
                (encoded_result, self._utcnow(), int(job_id)),
            )
            return self._row(
                connection.execute("SELECT * FROM remote_jobs WHERE id=?", (int(job_id),)).fetchone()
            )  # type: ignore[return-value]

    def fail_crawl_work(self, token: LeaseToken, crawl_work_id: int, error: str, *, retryable: bool) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            state = "RETRYABLE_FAILED" if retryable else "TERMINAL_FAILED"
            if work["state"] in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
                if work["state"] == state and str(work["error_text"] or "") == error:
                    return self._row(work)  # type: ignore[return-value]
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            connection.execute("UPDATE crawl_work SET state=?, error_text=?, updated_at=? WHERE id=?", (state, error, self._utcnow(), crawl_work_id))
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def advance_crawl_work_for_job(self, token: LeaseToken, job_id: int, state: str) -> int:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            self._job_for_lease(connection, token, job_id)
            result = connection.execute("UPDATE crawl_work SET state=?, updated_at=? WHERE run_id=? AND remote_job_id=? AND state NOT IN ('SUCCESS', 'NO_RESULT', 'TERMINAL_FAILED')", (state, self._utcnow(), token.run_id, job_id))
            return result.rowcount

    def _validate_crawl_lease(self, connection: sqlite3.Connection, token: LeaseToken, crawl_work_id: int) -> None:
        self._validate_lease(connection, token)
        row = connection.execute("SELECT run_id FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
        if row is None or int(row["run_id"]) != token.run_id:
            raise ValueError("crawl work must belong to the leased daily run")

    def crawl_work_for_job(self, job_id: int) -> list[dict[str, Any]]:
        with self._reader() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM crawl_work WHERE remote_job_id=? ORDER BY id", (job_id,))]
