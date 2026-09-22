"""Control capability of the existing Zhaoshang adapter; original contracts retained."""
from __future__ import annotations
from app.services.zhaoshang_adapter_contract import (
    Any,
    AuthManager,
    LegacyRestartOutcome,
    LegacyZhaoshangAdapterError,
    Mapping,
    PreparedLegacyJob,
    RestartStatus,
    _BoundedReadOnlySession,
    _locked_temporary_environment,
    _text,
    internal_subprocess_env,
)
from app.services.zhaoshang_adapter_base import ZhaoshangAdapterBase


class ZhaoshangControlOperations(ZhaoshangAdapterBase):

    def restart_running_task(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
        *,
        resume_phase: str | None = None,
    ) -> LegacyRestartOutcome:
        """Execute the single exact disable->enable restart for one remote task.

        ``resume_phase`` reconciles an already-sent direction: ``DISABLE_SENT``
        never issues another disable, and ``ENABLE_SENT`` issues no remote
        write at all.  Unknown write outcomes return the re-observed live
        state so the caller can reconcile before any further decision.
        """
        if self._restart_runner is not None:
            return self._restart_runner(
                job, task_id, task_number, timeout_seconds, resume_phase=resume_phase
            )
        return self._production_restart_running_task(
            job, task_id, task_number, timeout_seconds, resume_phase=resume_phase
        )

    def _production_restart_running_task(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
        *,
        resume_phase: str | None = None,
    ) -> LegacyRestartOutcome:
        if timeout_seconds <= 0:
            raise TimeoutError("restart timeout must be positive")
        if resume_phase not in {None, "DISABLE_SENT", "DISABLE_CONFIRMED", "ENABLE_SENT"}:
            raise ValueError("restart resume phase is not recognized")
        # The adapter owns the side-effect boundary: the protocol body marks
        # the moment each remote write starts, and any exception escaping it
        # is converted into an explicit outcome that names whether a write
        # may have left — never left for the runtime bridge to guess from
        # the exception type alone.
        writes = {"disable": False, "enable": False}
        try:
            return self._restart_protocol(
                job,
                task_id,
                task_number,
                timeout_seconds,
                resume_phase=resume_phase,
                writes=writes,
            )
        except Exception as exc:  # noqa: BLE001 - converted into an outcome
            return self._restart_escape_outcome(exc, resume_phase=resume_phase, writes=writes)

    def _restart_escape_outcome(
        self,
        exc: BaseException,
        *,
        resume_phase: str | None,
        writes: Mapping[str, bool],
    ) -> LegacyRestartOutcome:
        """Translate one escaped protocol exception into a boundary outcome.

        The write flags are set immediately before the disable/enable calls,
        so this always knows whether a side effect may have left: after the
        enable call the outcome is ENABLE_UNKNOWN, after the disable call
        DISABLE_UNKNOWN, and before both a provable zero-write failure
        (pre-write transient, or permanent identity conflict).  Resumed
        phases reconcile the already-durable direction.
        """
        diagnostic = f"restart raised {type(exc).__name__}: {exc}"
        if writes.get("enable") or resume_phase == "ENABLE_SENT":
            return LegacyRestartOutcome(
                RestartStatus.ENABLE_UNKNOWN,
                disable_confirmed=True,
                observed_enabled=None,
                diagnostic=diagnostic,
            )
        if writes.get("disable") or resume_phase in {"DISABLE_SENT", "DISABLE_CONFIRMED"}:
            return LegacyRestartOutcome(
                RestartStatus.DISABLE_UNKNOWN,
                observed_enabled=None,
                diagnostic=diagnostic,
            )
        from app.services.remote_execution_policy import _pre_write_restart_failure_status

        return LegacyRestartOutcome(
            RestartStatus(_pre_write_restart_failure_status(exc)),
            observed_enabled=None,
            diagnostic=diagnostic,
        )

    def _restart_protocol(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
        *,
        resume_phase: str | None = None,
        writes: dict[str, bool],
    ) -> LegacyRestartOutcome:
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang external tool is unavailable")
        deadline = self._monotonic() + float(timeout_seconds)
        tool = self._load_module(self._external_tool_path, "legacy_zhaoshang_running_restart")
        auth_manager = self._auth_manager or AuthManager()
        environment = internal_subprocess_env()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy Zhaoshang restart deadline exhausted before auth refresh")
        # The token-refresh HTTP is part of this callback's budget: it runs
        # before the bounded session exists, so it may only use the live
        # remaining time — never the AuthManager's full 30-second default.
        environment.update(
            auth_manager.export_environment(
                "zhaoshang",
                refresh=True,
                timeout_seconds=remaining,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        )
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy Zhaoshang auth refresh exhausted the restart deadline")
        session_environment = {
            key: str(environment[key])
            for key in ("BLADE_AUTH", "COOKIE")
            if environment.get(key)
        }
        if len(session_environment) != 2:
            raise LegacyZhaoshangAdapterError("legacy Zhaoshang session auth is unavailable")

        def lookup() -> tuple[Any, str, str, str]:
            task = tool.check_existing_task(session, resolved_task_name, mall_code)
            if task is None:
                raise LegacyZhaoshangAdapterError("restart found no exact existing task")
            live_task_id, live_task_number, mall_logo = tool.get_task_identity(task, mall_code)
            if str(live_task_id) != str(task_id) or str(live_task_number) != str(task_number):
                raise LegacyZhaoshangAdapterError(
                    "live task identity does not match the durable remote identity"
                )
            return task, str(live_task_id), str(live_task_number), mall_logo

        with _locked_temporary_environment(session_environment):
            # The deadline must reach the real HTTP requests: the legacy tool
            # hard-codes timeout=60 inside its helpers, so every call flows
            # through this clamping session wrapper instead.
            session = _BoundedReadOnlySession(
                tool.build_session(),
                timeout_seconds,
                monotonic=self._monotonic,
                deadline=deadline,
            )
            mall_code = tool.priority_mall_code_for_key(job.mall_key)
            resolved_task_name, _batch_number = tool.build_browser_task_identifiers(
                job.task_name, job.mall_key
            )
            try:
                task, live_task_id, live_task_number, _mall_logo = lookup()
            except LegacyZhaoshangAdapterError as exc:
                message = str(exc)
                if "found no exact existing task" in message:
                    return LegacyRestartOutcome(RestartStatus.NOT_RUNNING, diagnostic=message)
                return LegacyRestartOutcome(RestartStatus.IDENTITY_CONFLICT, diagnostic=message)

            enabled_now = self._remote_is_enabled(task)

            if resume_phase == "ENABLE_SENT":
                # The single enable already left this process: reconciliation
                # only.  No further remote write may be dispatched.
                if enabled_now:
                    return LegacyRestartOutcome(
                        RestartStatus.RESTARTED,
                        disable_confirmed=True,
                        enable_confirmed=True,
                        observed_enabled=True,
                        diagnostic="reconciled from durable ENABLE_SENT phase",
                    )
                return LegacyRestartOutcome(
                    RestartStatus.ENABLE_UNCONFIRMED,
                    observed_enabled=False,
                    diagnostic="enable was sent but the exact task is still disabled",
                )

            if enabled_now is False and resume_phase is None:
                # Fresh recovery found the exact task already disabled with no
                # durable phase proving the disable belongs to this protocol:
                # it may be a human, an operator, or another system.  Zero
                # remote writes; enabling it here is forbidden.
                return LegacyRestartOutcome(
                    RestartStatus.EXTERNALLY_DISABLED,
                    observed_enabled=False,
                    diagnostic=(
                        "exact task is already disabled without durable recovery "
                        "evidence; refusing to enable an externally disabled task"
                    ),
                )

            if resume_phase is None:
                # Write-time revalidation (race guard): the probe may have seen
                # an active task minutes ago, but the task may have completed
                # since.  The first fresh remote write requires a fresh status
                # answer AND a fresh structured progress payload to prove the
                # exact task is still ACTIVE; a terminal marker, terminal
                # progress, or anything unprovable declines with zero writes
                # and leaves the job to ordinary harvest.
                from app.services.remote_execution_policy import (
                    RemoteExecutionState,
                    classify_remote_execution_state,
                )

                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TimeoutError("restart deadline exhausted before status revalidation")
                status = tool.query_task_status(session, live_task_id)
                # Business-response gate: a failed status response's marker
                # text is not trusted (same rule as the probe path).
                from app.services.zhaoshang_remote_task import remote_business_response_ok

                status_desc = _text(status.get("rspDesc")) if remote_business_response_ok(status) else ""
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TimeoutError("restart deadline exhausted before progress revalidation")
                write_time_batch_number = _text(task.get("batchNumber")) or resolved_task_name
                progress_json = tool.query_task_progress(session, write_time_batch_number)
                # Business-response gate: counters from a failed progress
                # response are unprovable and must not authorize the write.
                progress_desc = (
                    _text(progress_json.get("rspDesc")) if remote_business_response_ok(progress_json) else ""
                )
                execution_state = classify_remote_execution_state(
                    tool.parse_task_progress_desc(progress_desc), status_desc
                )
                if execution_state is not RemoteExecutionState.ACTIVE:
                    return LegacyRestartOutcome(
                        RestartStatus.NO_LONGER_RUNNING,
                        observed_enabled=enabled_now,
                        diagnostic=(
                            f"write-time execution state {execution_state.value} "
                            f"(status={status_desc or '(empty)'})"
                        ),
                    )

            disable_response: Mapping[str, Any] | None = None
            if resume_phase in {None, "DISABLE_SENT", "DISABLE_CONFIRMED"}:
                # A fresh protocol may disable once; a resumed DISABLE_SENT
                # phase re-checks the world first and treats an observed
                # disabled state as the completed disable.
                if enabled_now and resume_phase is None:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        raise TimeoutError("restart deadline exhausted before disable write")
                    try:
                        # Side-effect boundary: from this line on, an escape
                        # may mean the disable POST left this process.
                        writes["disable"] = True
                        disable_response = tool.disable_task(session, live_task_id)
                    except Exception:
                        try:
                            retask, _rid, _rnum, _rmall = lookup()
                            return LegacyRestartOutcome(
                                RestartStatus.DISABLE_UNKNOWN,
                                observed_enabled=self._remote_is_enabled(retask),
                                diagnostic="disable request outcome unknown; re-observed live state",
                            )
                        except LegacyZhaoshangAdapterError as reexc:
                            return LegacyRestartOutcome(
                                RestartStatus.DISABLE_UNKNOWN,
                                observed_enabled=None,
                                diagnostic=f"disable request outcome unknown; reconcile failed: {reexc}",
                            )
                try:
                    task, live_task_id, live_task_number, _mall_logo = lookup()
                except LegacyZhaoshangAdapterError as exc:
                    return LegacyRestartOutcome(
                        RestartStatus.IDENTITY_CONFLICT,
                        disable_response=disable_response,
                        diagnostic=str(exc),
                    )
                if self._remote_is_enabled(task):
                    return LegacyRestartOutcome(
                        RestartStatus.DISABLE_UNCONFIRMED,
                        observed_enabled=True,
                        disable_response=disable_response,
                        diagnostic="disable did not take effect on the exact task",
                    )

            enable_response: Mapping[str, Any] | None = None
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("restart deadline exhausted before enable write")
            try:
                # Side-effect boundary: from this line on, an escape may mean
                # the enable POST left this process.
                writes["enable"] = True
                enable_response = tool.enable_task(session, live_task_id)
            except Exception:
                try:
                    retask, _rid, _rnum, _rmall = lookup()
                    return LegacyRestartOutcome(
                        RestartStatus.ENABLE_UNKNOWN,
                        disable_confirmed=True,
                        observed_enabled=self._remote_is_enabled(retask),
                        diagnostic="enable request outcome unknown; re-observed live state",
                    )
                except LegacyZhaoshangAdapterError as reexc:
                    return LegacyRestartOutcome(
                        RestartStatus.ENABLE_UNKNOWN,
                        disable_confirmed=True,
                        observed_enabled=None,
                        diagnostic=f"enable request outcome unknown; reconcile failed: {reexc}",
                    )
            try:
                task, live_task_id, live_task_number, _mall_logo = lookup()
            except LegacyZhaoshangAdapterError as exc:
                return LegacyRestartOutcome(
                    RestartStatus.IDENTITY_CONFLICT,
                    disable_confirmed=True,
                    enable_response=enable_response,
                    diagnostic=str(exc),
                )
            if not self._remote_is_enabled(task):
                return LegacyRestartOutcome(
                    RestartStatus.ENABLE_UNCONFIRMED,
                    disable_confirmed=True,
                    observed_enabled=False,
                    enable_response=enable_response,
                    diagnostic="enable did not take effect on the exact task",
                )
            return LegacyRestartOutcome(
                RestartStatus.RESTARTED,
                disable_confirmed=True,
                enable_confirmed=True,
                observed_enabled=True,
                disable_response=disable_response,
                enable_response=enable_response,
                diagnostic="exact task disabled and re-enabled with confirmed evidence",
            )

    def stop_running_task(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
        *,
        resume_phase: str | None = None,
    ) -> LegacyRestartOutcome:
        """Execute the single exact disable (stop) for one remote task.

        Mirrors the first half of the restart protocol — auth, exact lookup,
        identity verification, the externally-disabled refusal, the write-time
        ACTIVE revalidation, one disable write, and the re-lookup confirmation
        — and NEVER issues an enable.  ``resume_phase`` reconciles an
        already-sent disable idempotently: ``DISABLE_SENT`` and
        ``DISABLE_CONFIRMED`` only re-confirm the observed remote state and
        never dispatch a second disable write.
        """
        if self._stop_runner is not None:
            return self._stop_runner(
                job, task_id, task_number, timeout_seconds, resume_phase=resume_phase
            )
        return self._production_stop_running_task(
            job, task_id, task_number, timeout_seconds, resume_phase=resume_phase
        )

    def _production_stop_running_task(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
        *,
        resume_phase: str | None = None,
    ) -> LegacyRestartOutcome:
        if timeout_seconds <= 0:
            raise TimeoutError("stop timeout must be positive")
        if resume_phase not in {None, "DISABLE_SENT", "DISABLE_CONFIRMED"}:
            raise ValueError("stop resume phase is not recognized")
        # Same side-effect boundary contract as the restart protocol: the
        # writes flag marks the moment the disable starts, and any escape is
        # converted into an outcome that names whether the write may have left
        # (the shared escape translator has no enable branch reachable here:
        # stop never sets writes["enable"] and never resumes from ENABLE_SENT).
        writes = {"disable": False}
        try:
            return self._stop_protocol(
                job,
                task_id,
                task_number,
                timeout_seconds,
                resume_phase=resume_phase,
                writes=writes,
            )
        except Exception as exc:  # noqa: BLE001 - converted into an outcome
            return self._restart_escape_outcome(exc, resume_phase=resume_phase, writes=writes)

    def _stop_protocol(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
        *,
        resume_phase: str | None = None,
        writes: dict[str, bool],
    ) -> LegacyRestartOutcome:
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang external tool is unavailable")
        deadline = self._monotonic() + float(timeout_seconds)
        tool = self._load_module(self._external_tool_path, "legacy_zhaoshang_running_stop")
        auth_manager = self._auth_manager or AuthManager()
        environment = internal_subprocess_env()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy Zhaoshang stop deadline exhausted before auth refresh")
        environment.update(
            auth_manager.export_environment(
                "zhaoshang",
                refresh=True,
                timeout_seconds=remaining,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        )
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy Zhaoshang auth refresh exhausted the stop deadline")
        session_environment = {
            key: str(environment[key])
            for key in ("BLADE_AUTH", "COOKIE")
            if environment.get(key)
        }
        if len(session_environment) != 2:
            raise LegacyZhaoshangAdapterError("legacy Zhaoshang session auth is unavailable")

        def lookup() -> tuple[Any, str, str, str]:
            task = tool.check_existing_task(session, resolved_task_name, mall_code)
            if task is None:
                raise LegacyZhaoshangAdapterError("stop found no exact existing task")
            live_task_id, live_task_number, mall_logo = tool.get_task_identity(task, mall_code)
            if str(live_task_id) != str(task_id) or str(live_task_number) != str(task_number):
                raise LegacyZhaoshangAdapterError(
                    "live task identity does not match the durable remote identity"
                )
            return task, str(live_task_id), str(live_task_number), mall_logo

        with _locked_temporary_environment(session_environment):
            session = _BoundedReadOnlySession(
                tool.build_session(),
                timeout_seconds,
                monotonic=self._monotonic,
                deadline=deadline,
            )
            mall_code = tool.priority_mall_code_for_key(job.mall_key)
            resolved_task_name, _batch_number = tool.build_browser_task_identifiers(
                job.task_name, job.mall_key
            )
            try:
                task, live_task_id, live_task_number, _mall_logo = lookup()
            except LegacyZhaoshangAdapterError as exc:
                message = str(exc)
                if "found no exact existing task" in message:
                    return LegacyRestartOutcome(RestartStatus.NOT_RUNNING, diagnostic=message)
                return LegacyRestartOutcome(RestartStatus.IDENTITY_CONFLICT, diagnostic=message)

            enabled_now = self._remote_is_enabled(task)

            if enabled_now is False and resume_phase is None:
                # Fresh stop found the exact task already disabled with no
                # durable phase proving the disable belongs to this protocol:
                # it may be a human, an operator, or another system.  Zero
                # remote writes.
                return LegacyRestartOutcome(
                    RestartStatus.EXTERNALLY_DISABLED,
                    observed_enabled=False,
                    diagnostic=(
                        "exact task is already disabled without durable stop "
                        "evidence; refusing to claim an external disable"
                    ),
                )

            if resume_phase is None:
                # Write-time revalidation (same race guard as the restart
                # protocol): the first fresh remote write requires a fresh
                # status answer AND a fresh structured progress payload to
                # prove the exact task is still ACTIVE; a terminal marker,
                # terminal progress, or anything unprovable declines with zero
                # writes.
                from app.services.remote_execution_policy import (
                    RemoteExecutionState,
                    classify_remote_execution_state,
                )

                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TimeoutError("stop deadline exhausted before status revalidation")
                status = tool.query_task_status(session, live_task_id)
                from app.services.zhaoshang_remote_task import remote_business_response_ok

                status_desc = _text(status.get("rspDesc")) if remote_business_response_ok(status) else ""
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TimeoutError("stop deadline exhausted before progress revalidation")
                write_time_batch_number = _text(task.get("batchNumber")) or resolved_task_name
                progress_json = tool.query_task_progress(session, write_time_batch_number)
                progress_desc = (
                    _text(progress_json.get("rspDesc")) if remote_business_response_ok(progress_json) else ""
                )
                execution_state = classify_remote_execution_state(
                    tool.parse_task_progress_desc(progress_desc), status_desc
                )
                if execution_state is not RemoteExecutionState.ACTIVE:
                    return LegacyRestartOutcome(
                        RestartStatus.NO_LONGER_RUNNING,
                        observed_enabled=enabled_now,
                        diagnostic=(
                            f"write-time execution state {execution_state.value} "
                            f"(status={status_desc or '(empty)'})"
                        ),
                    )

            disable_response: Mapping[str, Any] | None = None
            # A fresh stop may disable once; a resumed DISABLE_SENT /
            # DISABLE_CONFIRMED phase only re-checks the world and treats an
            # observed disabled state as the completed disable.
            if enabled_now and resume_phase is None:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TimeoutError("stop deadline exhausted before disable write")
                try:
                    # Side-effect boundary: from this line on, an escape may
                    # mean the disable POST left this process.
                    writes["disable"] = True
                    disable_response = tool.disable_task(session, live_task_id)
                except Exception:
                    try:
                        retask, _rid, _rnum, _rmall = lookup()
                        return LegacyRestartOutcome(
                            RestartStatus.DISABLE_UNKNOWN,
                            observed_enabled=self._remote_is_enabled(retask),
                            diagnostic="disable request outcome unknown; re-observed live state",
                        )
                    except LegacyZhaoshangAdapterError as reexc:
                        return LegacyRestartOutcome(
                            RestartStatus.DISABLE_UNKNOWN,
                            observed_enabled=None,
                            diagnostic=f"disable request outcome unknown; reconcile failed: {reexc}",
                        )
            try:
                task, live_task_id, live_task_number, _mall_logo = lookup()
            except LegacyZhaoshangAdapterError as exc:
                return LegacyRestartOutcome(
                    RestartStatus.IDENTITY_CONFLICT,
                    disable_response=disable_response,
                    diagnostic=str(exc),
                )
            if self._remote_is_enabled(task):
                return LegacyRestartOutcome(
                    RestartStatus.DISABLE_UNCONFIRMED,
                    observed_enabled=True,
                    disable_response=disable_response,
                    diagnostic="disable did not take effect on the exact task",
                )
            return LegacyRestartOutcome(
                RestartStatus.STOPPED,
                disable_confirmed=True,
                observed_enabled=False,
                disable_response=disable_response,
                diagnostic="exact task disabled with confirmed evidence",
            )
