"""Read capability of the existing Zhaoshang adapter; original contracts retained."""
from __future__ import annotations
from app.services.zhaoshang_adapter_contract import (
    Any,
    AuthManager,
    ExportNoPriceProbe,
    LegacyProbeEvidenceError,
    LegacyZhaoshangAdapterError,
    Mapping,
    PreparedLegacyJob,
    RemoteTaskIdentityConflictError,
    RunningTaskProbe,
    _BoundedReadOnlySession,
    _locked_session_construction,
    _locked_temporary_environment,
    _text,
    internal_subprocess_env,
    query_task_progress,
)
from app.services.zhaoshang_adapter_base import ZhaoshangAdapterBase


class ZhaoshangReadOperations(ZhaoshangAdapterBase):

    def probe_export_no_price(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
    ) -> ExportNoPriceProbe:
        """Re-verify live terminal no-price evidence for one exact existing task.

        Read-only against the crawl platform: the exact existing task is looked
        up by durable name, its identity must match, and the status / progress /
        export endpoints are queried directly.  The export response body is
        inspected but never written anywhere.
        """
        if self._export_probe is not None:
            return self._export_probe(job, task_id, task_number, timeout_seconds)
        return self._production_export_no_price_probe(job, task_id, task_number, timeout_seconds)

    def _production_export_no_price_probe(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
    ) -> ExportNoPriceProbe:
        if timeout_seconds <= 0:
            raise TimeoutError("export no-price probe timeout must be positive")
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang external tool is unavailable")
        deadline = self._monotonic() + float(timeout_seconds)
        tool = self._load_module(self._external_tool_path, "legacy_zhaoshang_export_no_price_probe")
        auth_manager = self._auth_manager or AuthManager()
        environment = internal_subprocess_env()
        environment.update(auth_manager.export_environment("zhaoshang", refresh=True))
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy Zhaoshang auth refresh exhausted the probe deadline")
        # The legacy session constructor reads exactly these two auth variables.
        session_environment = {
            key: str(environment[key])
            for key in ("BLADE_AUTH", "COOKIE")
            if environment.get(key)
        }
        if len(session_environment) != 2:
            raise LegacyZhaoshangAdapterError("legacy Zhaoshang session auth is unavailable")
        with _locked_temporary_environment(session_environment):
            session = tool.build_session()
            mall_code = tool.priority_mall_code_for_key(job.mall_key)
            resolved_task_name, _batch_number = tool.build_browser_task_identifiers(
                job.task_name,
                job.mall_key,
            )
            task = tool.check_existing_task(session, resolved_task_name, mall_code)
            remote_calls = 1
            if task is None:
                raise LegacyZhaoshangAdapterError(
                    "export no-price probe found no exact existing task"
                )
            live_task_id, live_task_number, mall_logo = tool.get_task_identity(task, mall_code)
            if str(live_task_id) != str(task_id) or str(live_task_number) != str(task_number):
                raise LegacyZhaoshangAdapterError(
                    "live task identity does not match the durable remote identity"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("export no-price probe deadline exhausted before status query")
            status = tool.query_task_status(session, str(live_task_id))
            remote_calls += 1
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("export no-price probe deadline exhausted before progress query")
            # The progress endpoint keys on the task record's batchNumber, not
            # the RW- taskNumber; mirror run_auto_task's identifier choice.
            batch_number = _text(task.get("batchNumber")) or resolved_task_name
            progress_json = tool.query_task_progress(session, batch_number)
            remote_calls += 1
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("export no-price probe deadline exhausted before export query")
            response = session.post(
                tool.EXPORT_TASK_URL,
                json={
                    "taskNumber": str(live_task_number),
                    "taskId": str(live_task_id),
                    "mallLogo": mall_logo,
                },
                timeout=min(60.0, remaining),
            )
            response.raise_for_status()
            remote_calls += 1
        content_type = response.headers.get("Content-Type", "") or ""
        content_disposition = response.headers.get("Content-Disposition", "") or ""
        if (
            "spreadsheetml" in content_type
            or "application/vnd.ms-excel" in content_type
            or "attachment" in content_disposition
            or response.content[:2] == b"PK"
        ):
            raise LegacyZhaoshangAdapterError(
                "export endpoint returned a result workbook; terminal no-price resolution does not apply"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise LegacyZhaoshangAdapterError(
                "export endpoint returned neither a workbook nor JSON"
            ) from exc
        if not isinstance(data, dict):
            raise LegacyZhaoshangAdapterError("export endpoint returned an unexpected payload")
        embedded = data.get("data")
        if isinstance(embedded, dict) and any(
            _text(embedded.get(key)) for key in ("url", "fileUrl", "downloadUrl", "exportUrl")
        ):
            raise LegacyZhaoshangAdapterError(
                "export endpoint returned a result download; terminal no-price resolution does not apply"
            )
        if isinstance(embedded, str) and any(token in embedded for token in (".xlsx", "download", "export")):
            raise LegacyZhaoshangAdapterError(
                "export endpoint returned a result download; terminal no-price resolution does not apply"
            )
        raw_count = data.get("count")
        if raw_count is None:
            # An exact no-price answer may omit the count field; only an
            # empty data payload keeps that shape admissible.
            if embedded not in (None, {}, []):
                raise LegacyZhaoshangAdapterError(
                    "export endpoint returned rows without a count"
                )
            export_count = 0
        else:
            try:
                export_count = int(raw_count)
            except (TypeError, ValueError) as exc:
                raise LegacyZhaoshangAdapterError(
                    "export endpoint returned an unparseable count"
                ) from exc
        return ExportNoPriceProbe(
            task_status_rsp_desc=_text(status.get("rspDesc")),
            progress=dict(tool.parse_task_progress_desc(_text(progress_json.get("rspDesc")))),
            export_rsp_desc=_text(data.get("rspDesc")),
            export_count=export_count,
            remote_calls=remote_calls,
        )

    def probe_running_task(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
    ) -> RunningTaskProbe:
        """Read-only status/progress/export probe for one exact RUNNING task.

        The exact task is re-looked-up by durable name and its live identity
        must match the supplied task_id/task_number; any conflict raises so
        callers can fail closed instead of probing the wrong remote task.
        """
        if self._running_probe is not None:
            return self._running_probe(job, task_id, task_number, timeout_seconds)
        return self._production_probe_running_task(job, task_id, task_number, timeout_seconds)

    def _production_probe_running_task(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
    ) -> RunningTaskProbe:
        if timeout_seconds <= 0:
            raise TimeoutError("running task probe timeout must be positive")
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang external tool is unavailable")
        deadline = self._monotonic() + float(timeout_seconds)
        tool = self._load_module(self._external_tool_path, "legacy_zhaoshang_running_probe")
        auth_manager = self._auth_manager or AuthManager()
        environment = internal_subprocess_env()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy Zhaoshang probe deadline exhausted before auth refresh")
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
            raise TimeoutError("legacy Zhaoshang auth refresh exhausted the probe deadline")
        session_environment = {
            key: str(environment[key])
            for key in ("BLADE_AUTH", "COOKIE")
            if environment.get(key)
        }
        if len(session_environment) != 2:
            raise LegacyZhaoshangAdapterError("legacy Zhaoshang session auth is unavailable")
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
            # Identity verification boundary (review round 7): the external
            # tool's exact-task lookup fails with bare RuntimeError only for
            # its two documented identity failures (_one_exact_task:
            # 精确任务不唯一 / 精确任务商城不匹配 — extract_records never
            # raises), and get_task_identity's 商城标识不一致 marker is the
            # mall-mismatch proof.  Those are PROVEN conflicts with the
            # durable remote identity and surface as the typed conflict; a
            # malformed payload (没有拿到 ...) is unprovable, not a
            # mismatch, and stays untyped so it keeps counting transient.
            try:
                task = tool.check_existing_task(session, resolved_task_name, mall_code)
            except RuntimeError as exc:
                raise RemoteTaskIdentityConflictError(str(exc)) from exc
            remote_calls = 1
            if task is None:
                raise LegacyZhaoshangAdapterError("running task probe found no exact existing task")
            try:
                live_task_id, live_task_number, mall_logo = tool.get_task_identity(task, mall_code)
            except RuntimeError as exc:
                if "商城标识不一致" in str(exc):
                    raise RemoteTaskIdentityConflictError(str(exc)) from exc
                raise
            if str(live_task_id) != str(task_id) or str(live_task_number) != str(task_number):
                raise RemoteTaskIdentityConflictError(
                    "live task identity does not match the durable remote identity"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("running task probe deadline exhausted before status query")
            status = tool.query_task_status(session, str(live_task_id))
            remote_calls += 1
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("running task probe deadline exhausted before progress query")
            batch_number = _text(task.get("batchNumber")) or resolved_task_name
            progress_json = tool.query_task_progress(session, batch_number)
            remote_calls += 1
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("running task probe deadline exhausted before export query")
            response = session.post(
                tool.EXPORT_TASK_URL,
                json={
                    "taskNumber": str(live_task_number),
                    "taskId": str(live_task_id),
                    "mallLogo": mall_logo,
                },
                timeout=min(60.0, remaining),
            )
            response.raise_for_status()
            remote_calls += 1
        # Business-response gate (review round 5): a business-failed status
        # or progress response (success=false / non-0000 rspCode) donates no
        # evidence — its terminal marker text and ACTIVE-looking counters
        # are stripped to the unprovable form before parsing.  Non-mapping
        # payloads are stripped by the same check.
        from app.services.zhaoshang_remote_task import remote_business_response_ok

        status_desc = _text(status.get("rspDesc")) if remote_business_response_ok(status) else ""
        parsed_progress = dict(
            tool.parse_task_progress_desc(
                _text(progress_json.get("rspDesc")) if remote_business_response_ok(progress_json) else ""
            )
        )
        content_type = response.headers.get("Content-Type", "") or ""
        content_disposition = response.headers.get("Content-Disposition", "") or ""
        if (
            "spreadsheetml" in content_type
            or "application/vnd.ms-excel" in content_type
            or "attachment" in content_disposition
            or response.content[:2] == b"PK"
        ):
            return RunningTaskProbe(
                task_id=str(live_task_id),
                task_number=str(live_task_number),
                mall=mall_logo,
                enabled=self._remote_is_enabled(task),
                task_status_rsp_desc=status_desc,
                progress=dict(parsed_progress),
                export_had_result=True,
                export_count=None,
                export_rsp_desc="",
                remote_calls=remote_calls,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise LegacyZhaoshangAdapterError(
                "export endpoint returned neither a workbook nor JSON"
            ) from exc
        if not isinstance(data, dict):
            raise LegacyZhaoshangAdapterError("export endpoint returned an unexpected payload")
        embedded = data.get("data")
        if isinstance(embedded, dict) and any(
            _text(embedded.get(key)) for key in ("url", "fileUrl", "downloadUrl", "exportUrl")
        ):
            return RunningTaskProbe(
                task_id=str(live_task_id),
                task_number=str(live_task_number),
                mall=mall_logo,
                enabled=self._remote_is_enabled(task),
                task_status_rsp_desc=status_desc,
                progress=dict(parsed_progress),
                export_had_result=True,
                export_count=None,
                export_rsp_desc="",
                remote_calls=remote_calls,
            )
        if isinstance(embedded, str) and any(token in embedded for token in (".xlsx", "download", "export")):
            return RunningTaskProbe(
                task_id=str(live_task_id),
                task_number=str(live_task_number),
                mall=mall_logo,
                enabled=self._remote_is_enabled(task),
                task_status_rsp_desc=status_desc,
                progress=dict(parsed_progress),
                export_had_result=True,
                export_count=None,
                export_rsp_desc="",
                remote_calls=remote_calls,
            )
        raw_count = data.get("count")
        if raw_count is None:
            # An exact no-price answer may omit the count field; only an
            # empty data payload keeps that shape admissible.
            if embedded not in (None, {}, []):
                raise LegacyZhaoshangAdapterError(
                    "export endpoint returned rows without a count"
                )
            export_count = 0
        else:
            try:
                export_count = int(raw_count)
            except (TypeError, ValueError) as exc:
                raise LegacyZhaoshangAdapterError(
                    "export endpoint returned an unparseable count"
                ) from exc
        # Probe usability contract (review round 7): reaching this point means
        # the export endpoint answered with NO actual result, so the only
        # usable recovery evidence left is a provable execution state.  A
        # business-failed/malformed/contradictory progress payload whose
        # status carries no trusted terminal marker leaves the state UNKNOWN:
        # returning a normal probe would let the runtime count transport
        # success as probe success and keep the job RUNNING forever.  The
        # three export_had_result=True early returns above are strong success
        # evidence and never reach this gate.
        from app.services.remote_execution_policy import (
            RemoteExecutionState,
            classify_remote_execution_state,
        )

        execution_state = classify_remote_execution_state(parsed_progress, status_desc)
        if execution_state not in (RemoteExecutionState.ACTIVE, RemoteExecutionState.TERMINAL):
            raise LegacyProbeEvidenceError(
                "running task probe returned no usable execution evidence "
                f"(execution_state={execution_state.value})"
            )
        return RunningTaskProbe(
            task_id=str(live_task_id),
            task_number=str(live_task_number),
            mall=mall_logo,
            enabled=self._remote_is_enabled(task),
            task_status_rsp_desc=status_desc,
            progress=dict(parsed_progress),
            export_had_result=False,
            export_count=export_count,
            export_rsp_desc=_text(data.get("rspDesc")),
            remote_calls=remote_calls,
        )

    def query_task_progress_for_job(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """Read-only exact lookup + progress query for one durable remote job.

        Strictly read-only: no disable/enable/export/add call ever leaves this
        method.  The exact task is re-looked-up by durable name and its live
        identity must match the supplied task_id/task_number (conflicts raise
        ``RemoteTaskIdentityConflictError``).  A business-failed progress
        response is NOT an exception: the result returns ``progress=None`` so
        dashboard callers can classify the state as REMOTE_UNKNOWN.
        """
        if self._progress_probe is not None:
            return self._progress_probe(job, task_id, task_number, timeout_seconds)
        return self._production_query_task_progress_for_job(
            job, task_id, task_number, timeout_seconds
        )

    def _production_query_task_progress_for_job(
        self,
        job: PreparedLegacyJob,
        task_id: str,
        task_number: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        if timeout_seconds <= 0:
            raise TimeoutError("progress query timeout must be positive")
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang external tool is unavailable")
        deadline = self._monotonic() + float(timeout_seconds)
        tool = self._load_module(self._external_tool_path, "legacy_zhaoshang_progress_probe")
        auth_manager = self._auth_manager or AuthManager()
        environment = internal_subprocess_env()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy Zhaoshang progress query deadline exhausted before auth refresh")
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
            raise TimeoutError("legacy Zhaoshang auth refresh exhausted the progress query deadline")
        # The legacy session constructor reads exactly these two auth variables.
        session_environment = {
            key: str(environment[key])
            for key in ("BLADE_AUTH", "COOKIE")
            if environment.get(key)
        }
        if len(session_environment) != 2:
            raise LegacyZhaoshangAdapterError("legacy Zhaoshang session auth is unavailable")
        with _locked_session_construction(session_environment, tool.build_session) as _raw:
            session = _BoundedReadOnlySession(
                _raw,
                timeout_seconds,
                monotonic=self._monotonic,
                deadline=deadline,
            )
        return query_task_progress(
            session,
            tool,
            task_name=job.task_name,
            mall_key=job.mall_key,
            task_id=task_id,
            task_number=task_number,
            deadline=deadline,
            monotonic=self._monotonic,
        )
