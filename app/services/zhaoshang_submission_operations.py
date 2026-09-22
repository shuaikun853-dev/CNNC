"""Submission capability of the existing Zhaoshang adapter; original contracts retained."""
from __future__ import annotations
from app.services.zhaoshang_adapter_contract import (
    Any,
    AuthManager,
    EXTERNAL_TOOL_PATH,
    LegacyPreSendFailure,
    LegacyPriorityOutcome,
    LegacyReconcileOutcome,
    LegacySubmitOutcome,
    Mapping,
    PreparedLegacyJob,
    PriorityEvidenceConflictError,
    PriorityStatus,
    ReconcileStatus,
    SubmissionEvidenceConflictError,
    SubmitStatus,
    _BoundedReadOnlySession,
    _locked_session_construction,
    _locked_temporary_environment,
    _text,
    datetime,
    importlib,
    load_zhaoshang_db_env,
    remote_not_materialized_diagnostic,
    retryable_priority_error_diagnostic,
    subprocess,
    sys,
    timedelta,
)
from app.services.zhaoshang_adapter_base import ZhaoshangAdapterBase


class ZhaoshangSubmissionOperations(ZhaoshangAdapterBase):

    @staticmethod
    def _create_args(job: PreparedLegacyJob) -> list[str]:
        return [
            sys.executable,
            str(EXTERNAL_TOOL_PATH),
            "--task-name", job.task_name,
            "--excel", str(job.template_path),
            "--mall", job.mall_key,
            "--output", str(job.result_path),
            "--task-weight", "0",
            "--create-only",
            "--batch-state-file", str(job.batch_state_path),
        ]

    def _production_priority_runner(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        if timeout_seconds <= 0:
            raise TimeoutError("legacy priority deadline is exhausted")
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang external tool is unavailable")
        if self._db_env_path is None:
            raise RuntimeError("legacy Zhaoshang priority database environment is unavailable")
        auto_module = self._load_module(self._external_tool_path, "legacy_zhaoshang_priority_events")
        try:
            priority_module = self._load_module(
                self._external_tool_path.with_name("zhaoshang_priority_boost.py"),
                "legacy_zhaoshang_priority_core",
            )
            db_environment = load_zhaoshang_db_env(self._db_env_path)
            with _locked_temporary_environment(db_environment):
                marker_time = self._submission_marker_time(
                    self._read_batch_events(job),
                    task_id,
                    task_number,
                )
                # A re-check must find the same already-created task, not guess
                # by widening the identity: task_num and mall stay exact and
                # window_start keeps guarding against earlier tasks. Only the
                # upper bound stretches to now so a subtask materialized after
                # marker+15min is still recoverable on a later probe.
                window_end = max(
                    marker_time + timedelta(minutes=15),
                    datetime.now() + timedelta(seconds=30),
                )
                target = priority_module.PriorityTarget(
                    task_name=job.task_name,
                    task_number=task_number,
                    mall_code=auto_module.priority_mall_code_for_key(job.mall_key),
                    marker_time=marker_time,
                    window_start=marker_time - timedelta(minutes=2),
                    window_end=window_end,
                )
                connection = priority_module.connect_from_env()
                read_timeout_seconds = max(120, min(300, int(timeout_seconds)))
                if not hasattr(connection, "_read_timeout"):
                    raise RuntimeError("legacy priority connection has no bounded read timeout")
                connection._read_timeout = read_timeout_seconds
                try:
                    result = priority_module.boost_zhaoshang_task_priority(
                        connection,
                        target,
                        expected_subtask_count=len(job.requests),
                        max_select_retries=max(1, min(60, int(timeout_seconds / 2) or 1)),
                        retry_sleep_seconds=min(2.0, max(0.0, timeout_seconds / 60)),
                    )
                finally:
                    if hasattr(connection, "close"):
                        connection.close()
        except Exception as exc:
            retryable_diagnostic = retryable_priority_error_diagnostic(exc)
            stall_diagnostic = remote_not_materialized_diagnostic(exc)
            try:
                auto_module.record_batch_state_event(
                    job.batch_state_path,
                    mall_key=job.mall_key,
                    task_name=job.task_name,
                    action="PRIORITY_UNCONFIRMED",
                    task_id=task_id,
                    task_number=task_number,
                    reason=retryable_diagnostic or stall_diagnostic or type(exc).__name__,
                    retryable=retryable_diagnostic is not None or stall_diagnostic is not None,
                    remote_stall=stall_diagnostic,
                )
            except Exception:
                pass
            raise

        priority_state = _text(result.get("priority_state"))
        details: dict[str, Any] = {}
        if priority_state == "updated" and int(result.get("affected_rows") or 0) > 0:
            action = "PRIORITY_UPDATED"
            details["affected_rows"] = int(result["affected_rows"])
        elif priority_state == "already_highest_priority" and int(result.get("already_highest_count") or 0) > 0:
            action = "PRIORITY_ALREADY_HIGHEST"
            details["already_highest_count"] = int(result["already_highest_count"])
        else:
            raise RuntimeError("legacy priority core returned no accepted verification evidence")
        auto_module.record_batch_state_event(
            job.batch_state_path,
            mall_key=job.mall_key,
            task_name=job.task_name,
            action=action,
            task_id=task_id,
            task_number=task_number,
            **details,
        )
        return result

    def submit(self, job: PreparedLegacyJob, *, timeout_seconds: float) -> LegacySubmitOutcome:
        invalid_reason = self._validate_submit_inputs(job, timeout_seconds)
        if invalid_reason:
            return LegacySubmitOutcome(SubmitStatus.NOT_ATTEMPTED, diagnostic=invalid_reason)
        process = None
        run_diagnostic = ""
        pre_send_diagnostic = ""
        try:
            process = self._run(self._create_args(job), timeout_seconds)
        except LegacyPreSendFailure as exc:
            pre_send_diagnostic = str(exc)
        except (subprocess.TimeoutExpired, TimeoutError, OSError):
            run_diagnostic = "legacy create-only completion is unknown"
        events = self._read_batch_events(job)
        try:
            task_id, task_number = self._submission_evidence(job, events)
        except SubmissionEvidenceConflictError as exc:
            return LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic=str(exc))
        if task_id and task_number:
            priority_evidence = None
            diagnostic = run_diagnostic
            try:
                (
                    priority_task_id,
                    priority_task_number,
                    priority_evidence,
                    _priority_retryable,
                ) = self._priority_evidence(events)
            except PriorityEvidenceConflictError as exc:
                diagnostic = str(exc)
            if priority_evidence is not None and (priority_task_id, priority_task_number) != (task_id, task_number):
                priority_evidence = None
                diagnostic = "priority evidence conflicts with confirmed remote identity"
            return LegacySubmitOutcome(
                SubmitStatus.SUBMITTED,
                task_id,
                task_number,
                priority_evidence,
                diagnostic,
            )
        if pre_send_diagnostic:
            # Durable CREATED+ENABLED evidence already won above. With no such
            # evidence and a provably un-started subprocess, no POST began.
            return LegacySubmitOutcome(SubmitStatus.NOT_ATTEMPTED, diagnostic=pre_send_diagnostic)
        if run_diagnostic:
            return LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic=run_diagnostic)
        if int(getattr(process, "returncode", -1)) != 0:
            return LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic="legacy create-only returned a nonzero exit")
        return LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic="legacy create-only lacks CREATED + ENABLED evidence")

    @staticmethod
    def _identity_from_remote(remote: Mapping[str, Any]) -> tuple[str | None, str | None]:
        task_id = _text(remote.get("taskId") or remote.get("id") or remote.get("task_id")) or None
        task_number = _text(remote.get("taskNumber") or remote.get("taskNo") or remote.get("task_number")) or None
        return task_id, task_number

    @staticmethod
    def _one_reconciled_remote(remote: Any) -> Mapping[str, Any] | None:
        if remote is None:
            return None
        if isinstance(remote, Mapping):
            return remote
        if isinstance(remote, (list, tuple)):
            if not remote:
                return None
            if len(remote) != 1 or not isinstance(remote[0], Mapping):
                raise SubmissionEvidenceConflictError(
                    "exact task reconciliation returned multiple or malformed matches"
                )
            return remote[0]
        raise SubmissionEvidenceConflictError(
            "exact task reconciliation returned an invalid result shape"
        )

    @staticmethod
    def _remote_takeover_conflict(
        job: PreparedLegacyJob,
        remote: Mapping[str, Any],
    ) -> str:
        remote_task_name = _text(
            remote.get("taskName") or remote.get("task_name")
        )
        if remote_task_name and remote_task_name != job.task_name:
            return "exact task reconciliation returned a different task name"
        remote_mall = _text(
            remote.get("mallLogo")
            or remote.get("identification")
            or remote.get("sourceIdentification")
            or remote.get("mall_key")
        )
        if remote_mall and remote_mall != job.mall_key:
            return "exact task reconciliation returned a different mall"
        for field in (
            "expectedSubtaskCount",
            "expected_subtask_count",
            "subTaskCount",
            "subtaskCount",
            "taskCount",
        ):
            value = remote.get(field)
            if value in (None, ""):
                continue
            try:
                count = int(value)
            except (TypeError, ValueError):
                return "exact task reconciliation returned an invalid subtask count"
            if count != len(job.requests):
                return "exact task reconciliation returned a different subtask count"
            break
        return ""

    def _default_reconciler(self, task_name: str, mall_key: str, timeout_seconds: float) -> Mapping[str, Any] | None:
        if timeout_seconds <= 0:
            raise ValueError("reconciliation timeout must be positive")
        deadline = self._monotonic() + timeout_seconds
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang external tool is unavailable")
        spec = importlib.util.spec_from_file_location("legacy_zhaoshang_read_only", self._external_tool_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load legacy Zhaoshang reconciliation helpers")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        auth_manager = self._auth_manager or AuthManager()
        auth_environment = auth_manager.export_environment("zhaoshang", refresh=True)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy reconciliation auth refresh exhausted the deadline")
        with _locked_session_construction(auth_environment, module.build_session) as _raw:
            session = _BoundedReadOnlySession(
                _raw,
                remaining,
                monotonic=self._monotonic,
                deadline=deadline,
            )
        mall_code = module.priority_mall_code_for_key(mall_key)
        try:
            return module.check_existing_task(session, task_name, mall_code)
        except RuntimeError as exc:
            raise SubmissionEvidenceConflictError(str(exc)) from exc

    def reconcile_unknown(self, job: PreparedLegacyJob, *, timeout_seconds: float) -> LegacyReconcileOutcome:
        if timeout_seconds <= 0:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="reconciliation timeout must be positive")
        reconciler = self._reconciler or self._default_reconciler
        try:
            remote = self._one_reconciled_remote(
                reconciler(job.task_name, job.mall_key, timeout_seconds)
            )
        except SubmissionEvidenceConflictError as exc:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic=str(exc))
        except (FileNotFoundError, ImportError, AttributeError):
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="exact task reconciliation tooling is unavailable")
        except Exception:
            # A read timeout, authentication refresh race, or other transient
            # failure proves neither absence nor existence. Keep the durable
            # submit unknown until the outer harvest attempts/elapsed cap.
            return LegacyReconcileOutcome(ReconcileStatus.NOT_FOUND, diagnostic="exact task reconciliation is not yet conclusive")
        if remote is None:
            return LegacyReconcileOutcome(ReconcileStatus.NOT_FOUND)
        conflict = self._remote_takeover_conflict(job, remote)
        if conflict:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic=conflict)
        task_id, task_number = self._identity_from_remote(remote)
        if not task_id or not task_number:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="exact task reconciliation returned incomplete identity")
        try:
            events = self._read_batch_events(job)
            event_task_id, event_task_number = self._submission_evidence(job, events)
        except SubmissionEvidenceConflictError as exc:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic=str(exc))
        if event_task_id and event_task_number:
            if (event_task_id, event_task_number) != (task_id, task_number):
                return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="submission evidence conflicts with exact remote identity")
            return LegacyReconcileOutcome(ReconcileStatus.FOUND, task_id, task_number)

        partial_identities = {
            (_text(event.get("task_id")), _text(event.get("task_number")))
            for event in events
            if _text(event.get("action")) in {"CREATED", "ENABLED"}
        }
        partial_identities.discard(("", ""))
        if partial_identities and partial_identities != {(task_id, task_number)}:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="submission evidence conflicts with exact remote identity")
        if not self._remote_is_enabled(remote):
            return LegacyReconcileOutcome(
                ReconcileStatus.MANUAL,
                diagnostic="exact remote task is not durably confirmed enabled",
            )
        try:
            marker_time = self._submission_intent_marker(job, events)
            common = {
                "mall_key": job.mall_key,
                "task_name": job.task_name,
                "task_id": task_id,
                "task_number": task_number,
                "submission_marker_time": marker_time,
                "recovered_from": "SUBMIT_UNKNOWN_EXACT_LOOKUP",
            }
            self._record_batch_event(
                job.batch_state_path,
                action="CREATED",
                expected_subtask_count=len(job.requests),
                **common,
            )
            self._record_batch_event(
                job.batch_state_path,
                action="ENABLED",
                **common,
            )
            adopted_task_id, adopted_task_number = self._submission_evidence(
                job,
                self._read_batch_events(job),
            )
        except SubmissionEvidenceConflictError as exc:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic=str(exc))
        except Exception:
            return LegacyReconcileOutcome(
                ReconcileStatus.NOT_FOUND,
                diagnostic="exact remote task was found but durable takeover evidence could not be written",
            )
        if (adopted_task_id, adopted_task_number) != (task_id, task_number):
            return LegacyReconcileOutcome(
                ReconcileStatus.NOT_FOUND,
                diagnostic="exact remote task was found but durable takeover evidence is incomplete",
            )
        return LegacyReconcileOutcome(ReconcileStatus.FOUND, task_id, task_number)

    def prioritize(
        self,
        job: PreparedLegacyJob,
        *,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
        retry_unconfirmed: bool = False,
    ) -> LegacyPriorityOutcome:
        if timeout_seconds <= 0:
            return LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="priority timeout must be positive")
        events = self._read_batch_events(job)
        try:
            created_task_id, created_task_number = self._submission_evidence(job, events)
        except SubmissionEvidenceConflictError as exc:
            return LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic=str(exc))
        if (created_task_id, created_task_number) != (_text(task_id), _text(task_number)):
            return LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="priority identity does not match confirmed submission")

        def existing_outcome() -> LegacyPriorityOutcome | None:
            try:
                (
                    event_task_id,
                    event_task_number,
                    evidence,
                    evidence_retryable,
                ) = self._priority_evidence(self._read_batch_events(job))
            except PriorityEvidenceConflictError as exc:
                return LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic=str(exc))
            if evidence is None:
                return None
            if (event_task_id, event_task_number) != (created_task_id, created_task_number):
                return LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="priority evidence conflicts with confirmed submission")
            if evidence == "updated":
                return LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1)
            if evidence == "already_highest_priority":
                return LegacyPriorityOutcome(PriorityStatus.ALREADY_HIGHEST, weight_type=-1)
            if retry_unconfirmed or evidence_retryable:
                return None
            return LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="highest priority was not confirmed")

        existing = existing_outcome()
        if existing is not None:
            return existing
        priority_runner = self._priority_runner or self._production_priority_runner
        try:
            priority_runner(job, _text(task_id), _text(task_number), timeout_seconds)
        except Exception as exc:
            retryable_diagnostic = retryable_priority_error_diagnostic(exc)
            stall_diagnostic = remote_not_materialized_diagnostic(exc)
            return LegacyPriorityOutcome(
                PriorityStatus.UNCONFIRMED,
                diagnostic=retryable_diagnostic or stall_diagnostic or type(exc).__name__,
                retryable=retryable_diagnostic is not None or stall_diagnostic is not None,
            )
        return existing_outcome() or LegacyPriorityOutcome(
            PriorityStatus.UNCONFIRMED,
            diagnostic="priority runner returned without durable evidence",
        )
