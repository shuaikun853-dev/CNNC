"""Daily audit inputs, staged work and finalization."""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Mapping, Sequence
from app.services.customer_price_semantics import CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES
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

class AuditRunStore(AuditStateKernel):

    def ensure_run(
        self,
        business_date: str,
        tenant_id: int = DEFAULT_TENANT_ID,
        pool_id: int = DEFAULT_POOL_ID,
        cutoff_at: str | None = None,
    ) -> dict[str, Any]:
        self._validate_scope(tenant_id, pool_id)
        business_date = self._business_date(business_date)
        cutoff_at = self._utc_timestamp(cutoff_at, field="cutoff_at")
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO daily_runs(business_date, tenant_id, pool_id, cutoff_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(business_date, tenant_id, pool_id) DO NOTHING""",
                (business_date, tenant_id, pool_id, cutoff_at, now, now),
            )
            row = connection.execute(
                "SELECT * FROM daily_runs WHERE business_date=? AND tenant_id=? AND pool_id=?",
                (business_date, tenant_id, pool_id),
            ).fetchone()
            assert row is not None
            if cutoff_at is not None and row["cutoff_at"] is None:
                connection.execute("UPDATE daily_runs SET cutoff_at=?, updated_at=? WHERE id=?", (cutoff_at, now, row["id"]))
                row = connection.execute("SELECT * FROM daily_runs WHERE id=?", (row["id"],)).fetchone()
                assert row is not None
            if cutoff_at is not None and row["cutoff_at"] is not None and row["cutoff_at"] != cutoff_at:
                raise ValueError("cutoff_at conflicts with the existing daily run")
            return self._row(row)  # type: ignore[return-value]

    def get_run(self, business_date: str, tenant_id: int = DEFAULT_TENANT_ID, pool_id: int = DEFAULT_POOL_ID) -> dict[str, Any] | None:
        self._validate_scope(tenant_id, pool_id)
        business_date = self._business_date(business_date)
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM daily_runs WHERE business_date=? AND tenant_id=? AND pool_id=?", (business_date, tenant_id, pool_id)).fetchone())

    def acquire_lease(
        self,
        business_date: str,
        owner: str,
        ttl_seconds: float,
        tenant_id: int = DEFAULT_TENANT_ID,
        pool_id: int = DEFAULT_POOL_ID,
        *,
        allow_unexpired_takeover: bool = False,
        process_lock: ProcessLock | None = None,
    ) -> LeaseToken:
        self._validate_scope(tenant_id, pool_id)
        business_date = self._business_date(business_date)
        self.ensure_run(business_date, tenant_id, pool_id)
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            run = connection.execute("SELECT * FROM daily_runs WHERE business_date=? AND tenant_id=? AND pool_id=?", (business_date, tenant_id, pool_id)).fetchone()
            assert run is not None
            expires = run["lease_expires_at"]
            if run["lease_owner"] and expires and expires > now:
                if not allow_unexpired_takeover:
                    raise LeaseUnavailable(f"daily run {business_date} is leased by another tick")
                if process_lock is None or not process_lock.held:
                    raise LeaseUnavailable("unexpired lease takeover requires a held ProcessLock")
            generation = int(run["lease_generation"]) + 1
            connection.execute(
                """UPDATE daily_runs SET lease_owner=?, lease_generation=?, lease_expires_at=?, updated_at=? WHERE id=?""",
                (owner, generation, self._expiry(ttl_seconds), now, run["id"]),
            )
            return LeaseToken(int(run["id"]), business_date, owner, generation)

    def renew_lease(self, token: LeaseToken, ttl_seconds: float) -> LeaseToken:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            connection.execute(
                "UPDATE daily_runs SET lease_expires_at=?, updated_at=? WHERE id=? AND lease_owner=? AND lease_generation=?",
                (self._expiry(ttl_seconds), self._utcnow(), token.run_id, token.owner, token.generation),
            )
        return token

    def release_lease(self, token: LeaseToken) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE daily_runs SET lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND lease_owner=? AND lease_generation=?""",
                (self._utcnow(), token.run_id, token.owner, token.generation),
            )

    def force_lease_expiry_for_test(self, business_date: str) -> None:
        """Test-only deterministic expiry hook; production ticks never call it."""
        with self._transaction(immediate=True) as connection:
            connection.execute("UPDATE daily_runs SET lease_expires_at=? WHERE business_date=?", ("1970-01-01T00:00:00+00:00", business_date))

    @staticmethod
    def _source_fields(row: Mapping[str, Any]) -> tuple[str, str]:
        v = row.get("vCommodityId", row.get("documentId"))
        sku = row.get("skuId")
        if v is None or sku is None or not str(v).strip() or not str(sku).strip():
            raise ValueError("vCommodityId/documentId and skuId are required for automatic audit identity")
        return str(v).strip(), str(sku).strip()

    def ingest_snapshot(
        self,
        token: LeaseToken,
        rows: Sequence[Mapping[str, Any]],
        observed_at: str | None = None,
        *,
        source_fingerprints: Sequence[str] | None = None,
        customer_fingerprints: Sequence[str] | None = None,
        headers: Sequence[str] | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_fps = list(source_fingerprints) if source_fingerprints is not None else [self._fingerprint(row) for row in rows]
        customer_fps = list(customer_fingerprints) if customer_fingerprints is not None else ["" for _ in rows]
        if len(source_fps) != len(rows) or len(customer_fps) != len(rows):
            raise ValueError("source_fingerprints and customer_fingerprints must match snapshot row count")
        if any(not isinstance(value, str) or not value for value in source_fps):
            raise ValueError("source_fingerprints must be non-empty strings")
        ordered_hash = self._fingerprint(list(zip(source_fps, customer_fps, strict=True)))
        now = self._utcnow()
        explicit_observation = self._utc_timestamp(observed_at, field="observed_at")
        observation = explicit_observation or now
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            if explicit_observation is None:
                # Direct store callers without authoritative observation evidence
                # retain the original content-idempotent behavior. Production
                # discovery always supplies observed_at, so an unchanged list
                # observed after cutoff is still recorded as a new observation.
                existing = connection.execute(
                    "SELECT * FROM snapshots WHERE run_id=? AND ordered_hash=? ORDER BY observed_at DESC, id DESC LIMIT 1",
                    (token.run_id, ordered_hash),
                ).fetchone()
            else:
                existing = connection.execute(
                    "SELECT * FROM snapshots WHERE run_id=? AND ordered_hash=? AND observed_at=?",
                    (token.run_id, ordered_hash, observation),
                ).fetchone()
            if existing is not None:
                members = self._snapshot_members(connection, int(existing["id"]))
                current_members = [{**member, "change_kind": "UNCHANGED"} for member in members]
                return {
                    "snapshot": self._row(existing), "members": current_members, "member_count": len(members), "reused": True,
                    "status_counts": {"UNCHANGED": len(members)} if members else {},
                }
            run = connection.execute("SELECT frozen_snapshot_id FROM daily_runs WHERE id=?", (token.run_id,)).fetchone()
            if run is not None and run["frozen_snapshot_id"] is not None:
                raise FrozenSnapshotConflict("a frozen daily run cannot accept different snapshot membership")
            previous = connection.execute("SELECT id FROM snapshots WHERE run_id=? ORDER BY id DESC LIMIT 1", (token.run_id,)).fetchone()
            previous_fps: dict[tuple[str, str], set[str]] = {}
            if previous is not None:
                for item in connection.execute(
                    """SELECT a.v_commodity_id, a.sku_id, a.source_fingerprint, a.customer_fingerprint
                       FROM snapshot_members m JOIN audit_items a ON a.id=m.audit_item_id
                       WHERE m.snapshot_id=?""",
                    (previous["id"],),
                ):
                    previous_fps.setdefault((item["v_commodity_id"], item["sku_id"]), set()).add(
                        f"{item['source_fingerprint']}\x00{item['customer_fingerprint']}"
                    )
            cursor = connection.execute(
                """INSERT INTO snapshots(run_id, ordered_hash, observed_at, row_count, headers_json, evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (token.run_id, ordered_hash, observation, len(rows), self._canonical_json(list(headers or [])), self._canonical_json(dict(evidence or {})), now),
            )
            snapshot_id = int(cursor.lastrowid)
            seen_occurrences: dict[tuple[str, str, str], int] = {}
            members: list[dict[str, Any]] = []
            for ordinal, (row, source_fp, customer_fp) in enumerate(zip(rows, source_fps, customer_fps, strict=True)):
                v, sku = self._source_fields(row)
                item = connection.execute(
                    "SELECT * FROM audit_items WHERE run_id=? AND v_commodity_id=? AND sku_id=? AND source_fingerprint=? AND customer_fingerprint=?",
                    (token.run_id, v, sku, source_fp, customer_fp),
                ).fetchone()
                if item is None:
                    cursor = connection.execute(
                        """INSERT INTO audit_items(run_id, v_commodity_id, sku_id, source_fingerprint, customer_fingerprint, row_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (token.run_id, v, sku, source_fp, customer_fp, self._canonical_json(row), now),
                    )
                    item_id = int(cursor.lastrowid)
                else:
                    item_id = int(item["id"])
                prior = previous_fps.get((v, sku))
                version = f"{source_fp}\x00{customer_fp}"
                kind = "NEW" if prior is None else ("UNCHANGED" if version in prior else "CHANGED")
                url = str(row.get("extLink") or "").strip()
                occurrence = (v, sku, url)
                duplicate_ordinal = seen_occurrences.get(occurrence, 0)
                seen_occurrences[occurrence] = duplicate_ordinal + 1
                connection.execute(
                    """INSERT INTO snapshot_members(snapshot_id, row_ordinal, audit_item_id, duplicate_ordinal, change_kind)
                       VALUES (?, ?, ?, ?, ?)""",
                    (snapshot_id, ordinal, item_id, duplicate_ordinal, kind),
                )
                members.append({"snapshot_id": snapshot_id, "row_ordinal": ordinal, "audit_item_id": item_id, "duplicate_ordinal": duplicate_ordinal, "change_kind": kind})
            snapshot = self._row(connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone())
            return {"snapshot": snapshot, "members": members, "member_count": len(members), "reused": False, "status_counts": self._count_kinds(members)}

    @staticmethod
    def _count_kinds(members: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for member in members:
            kind = str(member["change_kind"])
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    @staticmethod
    def _snapshot_members(connection: sqlite3.Connection, snapshot_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute("SELECT * FROM snapshot_members WHERE snapshot_id=? ORDER BY row_ordinal", (snapshot_id,))]

    def snapshot_members(self, snapshot_id: int) -> list[dict[str, Any]]:
        with self._reader() as connection:
            return self._snapshot_members(connection, snapshot_id)

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone())

    def list_snapshots(self, business_date: str) -> list[dict[str, Any]]:
        """Return that day's authoritative observations in chronological order."""
        business_date = self._business_date(business_date)
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT s.* FROM snapshots s
                   JOIN daily_runs r ON r.id=s.run_id
                   WHERE r.business_date=? AND r.tenant_id=? AND r.pool_id=?
                   ORDER BY s.observed_at, s.id""",
                (business_date, DEFAULT_TENANT_ID, DEFAULT_POOL_ID),
            ).fetchall()
        return [self._row(row) for row in rows]  # type: ignore[misc]

    def latest_snapshot(self, business_date: str) -> dict[str, Any] | None:
        run = self.get_run(business_date)
        if run is None:
            return None
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM snapshots WHERE run_id=? ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone())

    def first_snapshot_at_or_after(self, business_date: str, cutoff_at: str) -> dict[str, Any] | None:
        run = self.get_run(business_date)
        if run is None:
            return None
        cutoff = self._utc_timestamp(cutoff_at, field="cutoff_at")
        with self._reader() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM snapshots WHERE run_id=? AND observed_at>=? ORDER BY observed_at, id LIMIT 1",
                    (run["id"], cutoff),
                ).fetchone()
            )

    def ordered_snapshot_items(self, snapshot_id: int) -> list[dict[str, Any]]:
        """Return the preserved ordered multiset with the joined canonical row."""
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT m.row_ordinal, m.duplicate_ordinal, m.change_kind, a.*
                   FROM snapshot_members m JOIN audit_items a ON a.id=m.audit_item_id
                   WHERE m.snapshot_id=? ORDER BY m.row_ordinal""",
                (snapshot_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = self._row(row)
                assert item is not None
                item["row"] = json.loads(item["row_json"])
                result.append(item)
            return result

    def get_audit_item(self, audit_item_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM audit_items WHERE id=?", (audit_item_id,)).fetchone())

    def ensure_stage_work(self, token: LeaseToken, audit_item_id: int, stage: str, fingerprint: str) -> dict[str, Any]:
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            item = connection.execute("SELECT run_id FROM audit_items WHERE id=?", (audit_item_id,)).fetchone()
            if item is None or int(item["run_id"]) != token.run_id:
                raise ValueError("stage work must belong to the leased daily run")
            work = connection.execute("SELECT * FROM stage_work WHERE audit_item_id=? AND stage=?", (audit_item_id, stage)).fetchone()
            if work is None:
                cursor = connection.execute("INSERT INTO stage_work(audit_item_id, stage, fingerprint, updated_at) VALUES (?, ?, ?, ?)", (audit_item_id, stage, fingerprint, now))
                work = connection.execute("SELECT * FROM stage_work WHERE id=?", (cursor.lastrowid,)).fetchone()
            elif work["fingerprint"] != fingerprint:
                connection.execute(
                    """UPDATE stage_work SET fingerprint=?, state='PENDING', attempts=0, elapsed_seconds=0,
                       retryable=1, error_text=NULL, result_json=NULL, claimed_at=NULL, updated_at=? WHERE id=?""",
                    (fingerprint, now, work["id"]),
                )
                work = connection.execute("SELECT * FROM stage_work WHERE id=?", (work["id"],)).fetchone()
            return self._row(work)  # type: ignore[return-value]

    def reuse_successful_stage_work(self, token: LeaseToken, work_id: int) -> dict[str, Any] | None:
        """Copy same-run narrow-fingerprint success into a new item version."""
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            target = connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
            assert target is not None
            if target["state"] == "SUCCESS":
                return self._row(target)
            if target["state"] == "RECOVERED_PENDING_REVIEW":
                return None
            source = connection.execute(
                """SELECT s.* FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id
                   WHERE a.run_id=? AND s.stage=? AND s.fingerprint=? AND s.state='SUCCESS' AND s.id<>?
                   ORDER BY s.id LIMIT 1""",
                (token.run_id, target["stage"], target["fingerprint"], work_id),
            ).fetchone()
            if source is None:
                return None
            connection.execute(
                """UPDATE stage_work SET state='SUCCESS', retryable=0, result_json=?, error_text=NULL,
                   claimed_at=NULL, updated_at=? WHERE id=?""",
                (source["result_json"], self._utcnow(), work_id),
            )
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())

    def get_stage_work(self, work_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())

    def list_stage_work(
        self, token: LeaseToken, *, stage: str | None = None, states: set[str] | None = None
    ) -> list[dict[str, Any]]:
        with self._transaction(immediate=False) as connection:
            self._validate_lease(connection, token)
            clauses = ["a.run_id=?"]
            params: list[Any] = [token.run_id]
            if stage is not None:
                clauses.append("s.stage=?")
                params.append(stage)
            if states:
                placeholders = ",".join("?" for _ in states)
                clauses.append(f"s.state IN ({placeholders})")
                params.extend(sorted(states))
            rows = connection.execute(
                f"SELECT s.* FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id WHERE {' AND '.join(clauses)} ORDER BY s.id",
                params,
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def list_stage_work_for_business_date(
        self,
        business_date: str,
        *,
        stage: str | None = None,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read stage evidence without acquiring or mutating the daily lease."""
        clauses = ["r.business_date=?"]
        params: list[Any] = [self._business_date(business_date)]
        if stage is not None:
            clauses.append("s.stage=?")
            params.append(stage)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"s.state IN ({placeholders})")
            params.extend(sorted(states))
        with self._reader() as connection:
            rows = connection.execute(
                f"""SELECT s.* FROM stage_work s
                    JOIN audit_items a ON a.id=s.audit_item_id
                    JOIN daily_runs r ON r.id=a.run_id
                    WHERE {' AND '.join(clauses)} ORDER BY s.id""",
                params,
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def claim_stage_work(self, token: LeaseToken, work_id: int, *, max_attempts: int, max_elapsed_seconds: float) -> dict[str, Any] | None:
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            work = connection.execute("""SELECT s.*, a.run_id FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id WHERE s.id=?""", (work_id,)).fetchone()
            if work is None or int(work["run_id"]) != token.run_id:
                raise ValueError("stage work must belong to the leased daily run")
            if work["state"] not in {"PENDING", "RETRYABLE_FAILED"}:
                return None
            if int(work["attempts"]) >= max_attempts or float(work["elapsed_seconds"]) >= max_elapsed_seconds:
                connection.execute("UPDATE stage_work SET state='TERMINAL_FAILED', retryable=0, updated_at=? WHERE id=?", (now, work_id))
                return None
            connection.execute("UPDATE stage_work SET state='CLAIMED', attempts=attempts+1, claimed_at=?, updated_at=? WHERE id=?", (now, now, work_id))
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())

    def complete_stage_work(self, token: LeaseToken, work_id: int, result: Any) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            work = connection.execute("SELECT state FROM stage_work WHERE id=?", (work_id,)).fetchone()
            if work is None or work["state"] != "CLAIMED":
                raise ValueError("only claimed stage work can be completed")
            connection.execute("UPDATE stage_work SET state='SUCCESS', retryable=0, result_json=?, error_text=NULL, claimed_at=NULL, updated_at=? WHERE id=?", (self._canonical_json(result), self._utcnow(), work_id))
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())  # type: ignore[return-value]

    def complete_recovered_model_work(
        self, token: LeaseToken, work_id: int, result: Any
    ) -> dict[str, Any]:
        """Persist recovered model evidence behind an explicit review hold."""
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            work = connection.execute(
                "SELECT * FROM stage_work WHERE id=?", (work_id,)
            ).fetchone()
            if work is None or work["stage"] != "MODEL" or work["state"] != "CLAIMED":
                raise ValueError("only claimed MODEL work can enter recovery review")
            connection.execute(
                """UPDATE stage_work
                   SET state='RECOVERED_PENDING_REVIEW', retryable=0, result_json=?,
                       error_text=NULL, claimed_at=NULL, updated_at=? WHERE id=?""",
                (self._canonical_json(result), self._utcnow(), work_id),
            )
            return self._row(
                connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
            )  # type: ignore[return-value]

    def release_recovered_model_work_batch(
        self, token: LeaseToken, work_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Atomically release exact reviewed model results to normal workflow."""
        ids = [int(value) for value in work_ids]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("model recovery release ids must be non-empty and unique")
        placeholders = ",".join("?" for _ in ids)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            rows = connection.execute(
                f"""SELECT s.*, a.run_id FROM stage_work s
                    JOIN audit_items a ON a.id=s.audit_item_id
                    WHERE s.id IN ({placeholders}) ORDER BY s.id""",
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                raise ValueError("every model recovery release id must exist")
            by_id = {int(row["id"]): row for row in rows}
            for work_id in ids:
                work = by_id[work_id]
                if (
                    int(work["run_id"]) != token.run_id
                    or work["stage"] != "MODEL"
                    or work["state"] != "RECOVERED_PENDING_REVIEW"
                    or work["result_json"] is None
                ):
                    raise ValueError(
                        f"stage_work id {work_id} is not a reviewed recoverable MODEL result"
                    )
            connection.execute(
                f"UPDATE stage_work SET state='SUCCESS', updated_at=? WHERE id IN ({placeholders})",
                (self._utcnow(), *ids),
            )
            return [
                self._row(
                    connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
                )
                for work_id in ids
            ]  # type: ignore[list-item]

    def fail_stage_work(self, token: LeaseToken, work_id: int, *, elapsed_seconds: float, error: str, retryable: bool, max_attempts: int, max_elapsed_seconds: float) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            work = connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
            assert work is not None
            if work["state"] != "CLAIMED":
                raise ValueError("only claimed stage work can be failed")
            total = float(work["elapsed_seconds"]) + max(0, elapsed_seconds)
            terminal = not retryable or int(work["attempts"]) >= max_attempts or total >= max_elapsed_seconds
            state = "TERMINAL_FAILED" if terminal else "RETRYABLE_FAILED"
            connection.execute("UPDATE stage_work SET state=?, elapsed_seconds=?, retryable=?, error_text=?, claimed_at=NULL, updated_at=? WHERE id=?", (state, total, 0 if terminal else 1, error, self._utcnow(), work_id))
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())  # type: ignore[return-value]

    def requeue_invalid_business_success_details(self, token: LeaseToken, snapshot_id: int) -> int:
        """Requeue legacy DETAIL rows checkpointed as SUCCESS but carrying a
        retryable business failure (NOT_FOUND / DETAIL_REQUEST_FAILED /
        DETAIL_PARSE_FAILED) in ``result_json`` — only rows that are members of
        the given authoritative snapshot.

        Scope contract (round-2 fail-closed ruling): the requeue exists so the
        CURRENT tick's detail loop can redo legacy invalid successes it is
        about to process anyway.  It therefore only touches rows belonging to
        ``snapshot_id`` via ``snapshot_members`` — the frozen snapshot when the
        run is already frozen, otherwise the snapshot just ingested this tick.
        Rows outside that membership would be requeued into a PENDING state no
        tick ever revisits (FINALIZED runs exit ticks via ALREADY_FINALIZED
        before any detail work), permanently polluting the durable backlog.
        Historical data repair is out of scope and needs an explicit future
        recovery command.

        The new detail worker can no longer produce those states as SUCCESS, so
        each row is requeued at most once: attempts and elapsed budgets reset,
        the failure evidence survives verbatim in ``error_text`` and the
        misleading success payload is dropped.  Returns the number of rows
        actually requeued (idempotent under the ``state='SUCCESS'`` guard).
        """
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            rows = connection.execute(
                """SELECT s.id, s.result_json FROM stage_work s
                   JOIN audit_items a ON a.id=s.audit_item_id
                   JOIN (SELECT DISTINCT audit_item_id FROM snapshot_members WHERE snapshot_id=?)
                     m ON m.audit_item_id=s.audit_item_id
                   WHERE a.run_id=? AND s.stage='DETAIL' AND s.state='SUCCESS'""",
                (int(snapshot_id), token.run_id),
            ).fetchall()
            requeued = 0
            for row in rows:
                try:
                    decoded = json.loads(str(row["result_json"] or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(decoded, dict):
                    continue
                status = str(decoded.get("customerPriceStatus") or "")
                if status not in CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES:
                    continue
                cursor = connection.execute(
                    """UPDATE stage_work SET state='PENDING', attempts=0, elapsed_seconds=0,
                       retryable=1, error_text=?, result_json=NULL, claimed_at=NULL, updated_at=?
                       WHERE id=? AND state='SUCCESS'""",
                    (f"requeued invalid business success: customerPriceStatus={status}", now, row["id"]),
                )
                requeued += max(0, cursor.rowcount)
            return requeued

    # ---- Batched stage-work transitions -------------------------------------
    # Same per-row semantics and validations as the single-row methods above,
    # but the whole batch commits in one transaction.  The bounded-parallel
    # MODEL pass checkpoints hundreds of rows per tick; per-row fsync commits
    # dominate its wall clock otherwise.  All calls stay on the lease-owning
    # thread — batching never introduces a second writer.

    def ensure_stage_work_batch(self, token: LeaseToken, entries: Sequence[tuple[int, str, str]]) -> dict[int, dict[str, Any]]:
        """Batched ensure_stage_work keyed by audit_item_id."""
        now = self._utcnow()
        results: dict[int, dict[str, Any]] = {}
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            for audit_item_id, stage, fingerprint in entries:
                item = connection.execute("SELECT run_id FROM audit_items WHERE id=?", (audit_item_id,)).fetchone()
                if item is None or int(item["run_id"]) != token.run_id:
                    raise ValueError("stage work must belong to the leased daily run")
                work = connection.execute("SELECT * FROM stage_work WHERE audit_item_id=? AND stage=?", (audit_item_id, stage)).fetchone()
                if work is None:
                    cursor = connection.execute("INSERT INTO stage_work(audit_item_id, stage, fingerprint, updated_at) VALUES (?, ?, ?, ?)", (audit_item_id, stage, fingerprint, now))
                    work = connection.execute("SELECT * FROM stage_work WHERE id=?", (cursor.lastrowid,)).fetchone()
                elif work["fingerprint"] != fingerprint:
                    connection.execute(
                        """UPDATE stage_work SET fingerprint=?, state='PENDING', attempts=0, elapsed_seconds=0,
                           retryable=1, error_text=NULL, result_json=NULL, claimed_at=NULL, updated_at=? WHERE id=?""",
                        (fingerprint, now, work["id"]),
                    )
                    work = connection.execute("SELECT * FROM stage_work WHERE id=?", (work["id"],)).fetchone()
                results[audit_item_id] = self._row(work)  # type: ignore[index]
        return results

    def reuse_successful_stage_work_batch(self, token: LeaseToken, work_ids: Sequence[int]) -> dict[int, dict[str, Any] | None]:
        """Batched reuse_successful_stage_work keyed by work id."""
        results: dict[int, dict[str, Any] | None] = {}
        with self._transaction(immediate=True) as connection:
            for work_id in work_ids:
                self._validate_stage_lease(connection, token, work_id)
                target = connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
                assert target is not None
                if target["state"] == "SUCCESS":
                    results[work_id] = self._row(target)
                    continue
                if target["state"] == "RECOVERED_PENDING_REVIEW":
                    results[work_id] = None
                    continue
                source = connection.execute(
                    """SELECT s.* FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id
                       WHERE a.run_id=? AND s.stage=? AND s.fingerprint=? AND s.state='SUCCESS' AND s.id<>?
                       ORDER BY s.id LIMIT 1""",
                    (token.run_id, target["stage"], target["fingerprint"], work_id),
                ).fetchone()
                if source is None:
                    results[work_id] = None
                    continue
                connection.execute(
                    """UPDATE stage_work SET state='SUCCESS', retryable=0, result_json=?, error_text=NULL,
                       claimed_at=NULL, updated_at=? WHERE id=?""",
                    (source["result_json"], self._utcnow(), work_id),
                )
                results[work_id] = self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())
        return results

    def claim_stage_work_batch(self, token: LeaseToken, work_ids: Sequence[int], *, max_attempts: int, max_elapsed_seconds: float) -> list[dict[str, Any]]:
        """Batched claim_stage_work; returns only the rows actually claimed, in order."""
        now = self._utcnow()
        claimed: list[dict[str, Any]] = []
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            for work_id in work_ids:
                work = connection.execute("SELECT s.*, a.run_id FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id WHERE s.id=?", (work_id,)).fetchone()
                if work is None or int(work["run_id"]) != token.run_id:
                    raise ValueError("stage work must belong to the leased daily run")
                if work["state"] not in {"PENDING", "RETRYABLE_FAILED"}:
                    continue
                if int(work["attempts"]) >= max_attempts or float(work["elapsed_seconds"]) >= max_elapsed_seconds:
                    connection.execute("UPDATE stage_work SET state='TERMINAL_FAILED', retryable=0, updated_at=? WHERE id=?", (now, work_id))
                    continue
                connection.execute("UPDATE stage_work SET state='CLAIMED', attempts=attempts+1, claimed_at=?, updated_at=? WHERE id=?", (now, now, work_id))
                claimed.append(self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()))  # type: ignore[arg-type]
        return claimed

    def complete_stage_work_batch(self, token: LeaseToken, completions: Sequence[tuple[int, Any]]) -> None:
        """Batched complete_stage_work; every row must still be CLAIMED."""
        with self._transaction(immediate=True) as connection:
            for work_id, result in completions:
                self._validate_stage_lease(connection, token, work_id)
                work = connection.execute("SELECT state FROM stage_work WHERE id=?", (work_id,)).fetchone()
                if work is None or work["state"] != "CLAIMED":
                    raise ValueError("only claimed stage work can be completed")
                connection.execute("UPDATE stage_work SET state='SUCCESS', retryable=0, result_json=?, error_text=NULL, claimed_at=NULL, updated_at=? WHERE id=?", (self._canonical_json(result), self._utcnow(), work_id))

    def fail_stage_work_batch(self, token: LeaseToken, failures: Sequence[tuple[int, float, str, bool]], *, max_attempts: int, max_elapsed_seconds: float) -> None:
        """Batched fail_stage_work; entries are (work_id, elapsed, error, retryable)."""
        with self._transaction(immediate=True) as connection:
            for work_id, elapsed_seconds, error_text, retryable in failures:
                self._validate_stage_lease(connection, token, work_id)
                work = connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
                assert work is not None
                if work["state"] != "CLAIMED":
                    raise ValueError("only claimed stage work can be failed")
                total = float(work["elapsed_seconds"]) + max(0, elapsed_seconds)
                terminal = not retryable or int(work["attempts"]) >= max_attempts or total >= max_elapsed_seconds
                state = "TERMINAL_FAILED" if terminal else "RETRYABLE_FAILED"
                connection.execute("UPDATE stage_work SET state=?, elapsed_seconds=?, retryable=?, error_text=?, claimed_at=NULL, updated_at=? WHERE id=?", (state, total, 0 if terminal else 1, error_text, self._utcnow(), work_id))

    def recover_claimed_stage_work(self, token: LeaseToken) -> int:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            result = connection.execute("""UPDATE stage_work SET state='RETRYABLE_FAILED', error_text=COALESCE(error_text, 'interrupted before completion'), claimed_at=NULL, updated_at=?
                WHERE state='CLAIMED' AND audit_item_id IN (SELECT id FROM audit_items WHERE run_id=?)""", (self._utcnow(), token.run_id))
            return result.rowcount

    def requeue_terminal_stage_work(self, token: LeaseToken, work_id: int) -> dict[str, Any]:
        """Explicit operator/engine action; never silently retries terminal evidence."""
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            work = connection.execute("SELECT state FROM stage_work WHERE id=?", (work_id,)).fetchone()
            if work is None or work["state"] != "TERMINAL_FAILED":
                raise ValueError("only terminal stage work can be explicitly requeued")
            connection.execute("UPDATE stage_work SET state='PENDING', attempts=0, elapsed_seconds=0, retryable=1, error_text=NULL, claimed_at=NULL, updated_at=? WHERE id=?", (self._utcnow(), work_id))
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())  # type: ignore[return-value]

    def requeue_terminal_model_work_batch_preserving_evidence(
        self, token: LeaseToken, work_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Atomically make an exact terminal MODEL set claimable without resetting its budget."""
        ids = [int(work_id) for work_id in work_ids]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("stage work ids must be a non-empty unique list")
        placeholders = ",".join("?" for _ in ids)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            rows = connection.execute(
                f"""SELECT s.*, a.run_id FROM stage_work s
                    JOIN audit_items a ON a.id=s.audit_item_id
                    WHERE s.id IN ({placeholders}) ORDER BY s.id""",
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                raise ValueError("every selected stage work id must exist")
            if any(
                int(row["run_id"]) != token.run_id
                or row["stage"] != "MODEL"
                or row["state"] != "TERMINAL_FAILED"
                for row in rows
            ):
                raise ValueError("every selected stage must be terminal MODEL work in the leased run")
            result = connection.execute(
                f"""UPDATE stage_work
                    SET state='RETRYABLE_FAILED', retryable=1, claimed_at=NULL, updated_at=?
                    WHERE id IN ({placeholders}) AND state='TERMINAL_FAILED' AND stage='MODEL'""",
                [self._utcnow(), *ids],
            )
            if result.rowcount != len(ids):
                raise ValueError("terminal MODEL requeue did not update the exact selected set")
            updated = connection.execute(
                f"SELECT * FROM stage_work WHERE id IN ({placeholders}) ORDER BY id", ids
            ).fetchall()
            return [self._row(row) for row in updated]  # type: ignore[list-item]

    def _validate_stage_lease(self, connection: sqlite3.Connection, token: LeaseToken, work_id: int) -> None:
        self._validate_lease(connection, token)
        row = connection.execute("SELECT a.run_id FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id WHERE s.id=?", (work_id,)).fetchone()
        if row is None or int(row["run_id"]) != token.run_id:
            raise ValueError("stage work must belong to the leased daily run")

    def update_daily_metrics(self, token: LeaseToken, metrics: Mapping[str, Any]) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            existing = json.loads(run["metrics_json"] or "{}")
            if run["state"] == "FINALIZED":
                immutable = _REQUIRED_FINAL_METRICS | _REQUIRED_FORCE_FINAL_METRICS | {FINALIZATION_INTENT_KEY}
                if any(key in immutable and (key not in existing or existing[key] != value)
                       for key, value in metrics.items()):
                    raise FrozenSnapshotConflict("finalized metrics are immutable")
            existing.update(metrics)
            connection.execute("UPDATE daily_runs SET metrics_json=?, updated_at=? WHERE id=?", (self._canonical_json(existing), self._utcnow(), token.run_id))
            return self._row(connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone())  # type: ignore[return-value]

    def freeze_snapshot(self, token: LeaseToken, snapshot_id: int) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            snapshot = connection.execute("SELECT id FROM snapshots WHERE id=? AND run_id=?", (snapshot_id, token.run_id)).fetchone()
            if snapshot is None:
                raise ValueError("frozen snapshot must belong to the leased daily run")
            frozen = run["frozen_snapshot_id"]
            if frozen is not None and int(frozen) != snapshot_id:
                raise FrozenSnapshotConflict("daily run already froze a different authoritative snapshot")
            connection.execute("UPDATE daily_runs SET state=CASE WHEN state='FINALIZED' THEN state ELSE 'FINALIZING' END, frozen_snapshot_id=?, updated_at=? WHERE id=?", (snapshot_id, self._utcnow(), token.run_id))
            return self._row(connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone())  # type: ignore[return-value]

    def prepare_finalization(
        self,
        token: LeaseToken,
        output_path: str,
        *,
        input_manifest_hash: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        refresh_intent: bool = False,
    ) -> dict[str, Any]:
        """Durably reserve the one formal output path before publishing it."""
        output_path = str(output_path or "").strip()
        if not output_path:
            raise ValueError("finalization output path must be non-empty")
        if (input_manifest_hash is None) != (metrics is None):
            raise ValueError("finalization intent requires both input hash and metrics")
        intent = None
        if metrics is not None:
            if not str(input_manifest_hash or "").strip():
                raise ValueError("finalization intent requires a non-empty input hash")
            intent = {
                "input_manifest_hash": input_manifest_hash,
                "metrics": json.loads(self._canonical_json(dict(metrics))),
            }
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            if run["frozen_snapshot_id"] is None:
                raise FrozenSnapshotConflict("finalization requires a frozen authoritative snapshot")
            prepared_path = str(run["final_output_path"] or "")
            if run["state"] == "FINALIZED":
                if prepared_path == output_path:
                    return self._row(run)  # type: ignore[return-value]
                raise FrozenSnapshotConflict("daily run was already finalized for another output path")
            if prepared_path and prepared_path != output_path:
                raise FrozenSnapshotConflict("daily run already prepared another final output path")
            existing_metrics = json.loads(run["metrics_json"] or "{}")
            current_intent = existing_metrics.get(FINALIZATION_INTENT_KEY)
            if intent is not None and (current_intent is None or refresh_intent):
                existing_metrics[FINALIZATION_INTENT_KEY] = intent
            if not prepared_path or existing_metrics.get(FINALIZATION_INTENT_KEY) != current_intent:
                connection.execute(
                    "UPDATE daily_runs SET state='FINALIZING', final_output_path=?, metrics_json=?, updated_at=? WHERE id=?",
                    (output_path, self._canonical_json(existing_metrics), self._utcnow(), token.run_id),
                )
                run = connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone()
                assert run is not None
            return self._row(run)  # type: ignore[return-value]

    def mark_finalized(self, token: LeaseToken, output_path: str, manifest_hash: str) -> dict[str, Any]:
        """Legacy idempotence check; new transitions require commit_finalization."""
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            if run["frozen_snapshot_id"] is None:
                raise FrozenSnapshotConflict("finalization requires a frozen authoritative snapshot")
            if not run["final_output_path"]:
                raise FrozenSnapshotConflict("finalization requires a prepared output path")
            if run["final_output_path"] != output_path:
                raise FrozenSnapshotConflict("finalization output path differs from its prepared intent")
            if run["state"] == "FINALIZED":
                if run["final_manifest_hash"] == manifest_hash:
                    return self._row(run)  # type: ignore[return-value]
                raise FrozenSnapshotConflict("daily run was already finalized with different output evidence")
            raise FrozenSnapshotConflict("new finalization requires commit_finalization with final metrics")

    def commit_finalization(
        self,
        token: LeaseToken,
        output_path: str,
        manifest_hash: str,
        metrics: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Commit final evidence and its immutable metrics as one SQLite transition."""
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            if run["frozen_snapshot_id"] is None:
                raise FrozenSnapshotConflict("finalization requires a frozen authoritative snapshot")
            if not run["final_output_path"]:
                raise FrozenSnapshotConflict("finalization requires a prepared output path")
            if run["final_output_path"] != output_path:
                raise FrozenSnapshotConflict("finalization output path differs from its prepared intent")
            if not str(manifest_hash or "").strip():
                raise ValueError("finalization requires a manifest hash")
            final_metrics = json.loads(self._canonical_json(dict(metrics)))
            required = _REQUIRED_FINAL_METRICS
            if "forced" in final_metrics or "force_finalize" in final_metrics:
                if final_metrics.get("forced") is not True or final_metrics.get("force_finalize") is not True:
                    raise ValueError("force finalization markers must both be true")
                required = required | _REQUIRED_FORCE_FINAL_METRICS
            missing = required - final_metrics.keys()
            if missing:
                raise ValueError(f"finalization metrics missing required keys: {', '.join(sorted(missing))}")
            existing_metrics = json.loads(run["metrics_json"] or "{}")
            if not isinstance(existing_metrics, dict):
                raise ValueError("daily metrics must be a JSON object")
            intent = existing_metrics.get(FINALIZATION_INTENT_KEY)
            if intent is not None and intent.get("metrics") != final_metrics:
                raise FrozenSnapshotConflict("final metrics differ from prepared publication intent")
            if run["state"] == "FINALIZED":
                if run["final_manifest_hash"] != manifest_hash:
                    raise FrozenSnapshotConflict("daily run was already finalized with different output evidence")
                if any(key not in existing_metrics or existing_metrics[key] != value
                       for key, value in final_metrics.items()):
                    raise FrozenSnapshotConflict("daily run was already finalized with different final metrics")
                return self._row(run)  # type: ignore[return-value]
            existing_metrics.pop(FINALIZATION_INTENT_KEY, None)
            existing_metrics.update(final_metrics)
            connection.execute(
                "UPDATE daily_runs SET state='FINALIZED', final_manifest_hash=?, metrics_json=?, updated_at=? WHERE id=?",
                (manifest_hash, self._canonical_json(existing_metrics), self._utcnow(), token.run_id),
            )
            return self._row(connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone())  # type: ignore[return-value]
