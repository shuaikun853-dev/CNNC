"""Base capability of the existing Zhaoshang adapter; original contracts retained."""
from __future__ import annotations
from app.services.zhaoshang_adapter_contract import (
    Any,
    AuthManager,
    Callable,
    CrawlRequest,
    DEFAULT_DB_ENV_PATH,
    EXTERNAL_TOOL_PATH,
    Iterable,
    LegacyPreSendFailure,
    LegacyRestartOutcome,
    Mapping,
    Path,
    PreparedLegacyJob,
    PriorityEvidenceConflictError,
    RunningTaskProbe,
    SubmissionEvidenceConflictError,
    TEMPLATE_HEADERS,
    TemplateConflictError,
    Workbook,
    ZhaoshangMallMapping,
    _canonical_hash,
    _host_matches,
    _text,
    datetime,
    importlib,
    internal_subprocess_env,
    json,
    load_workbook,
    load_zhaoshang_db_env,
    load_zhaoshang_mall_mappings,
    os,
    remote_task_is_enabled,
    subprocess,
    sys,
    time,
    timedelta,
    timezone,
    urlparse,
    uuid,
)


class ZhaoshangAdapterBase:
    def __init__(
        self,
        *,
        mappings: Iterable[ZhaoshangMallMapping] | None = None,
        runner: Callable[[list[str], float, Mapping[str, str]], Any] | None = None,
        reconciler: Callable[[str, str, float], Mapping[str, Any] | None] | None = None,
        priority_runner: Callable[[PreparedLegacyJob, str, str, float], Any] | None = None,
        export_probe: Callable[[PreparedLegacyJob, str, str, float], Any] | None = None,
        running_probe: Callable[[PreparedLegacyJob, str, str, float], RunningTaskProbe] | None = None,
        restart_runner: Callable[..., LegacyRestartOutcome] | None = None,
        progress_probe: Callable[[PreparedLegacyJob, str, str, float], Mapping[str, Any]] | None = None,
        stop_runner: Callable[..., LegacyRestartOutcome] | None = None,
        event_recorder: Callable[..., Any] | None = None,
        auth_manager: AuthManager | None = None,
        external_tool_path: Path = EXTERNAL_TOOL_PATH,
        db_env_path: Path | None = DEFAULT_DB_ENV_PATH,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mappings = tuple(mappings) if mappings is not None else tuple(load_zhaoshang_mall_mappings())
        self._runner = runner
        self._reconciler = reconciler
        self._priority_runner = priority_runner
        self._export_probe = export_probe
        self._running_probe = running_probe
        self._restart_runner = restart_runner
        self._progress_probe = progress_probe
        self._stop_runner = stop_runner
        self._event_recorder = event_recorder
        self._auth_manager = auth_manager
        self._external_tool_path = Path(external_tool_path)
        self._db_env_path = Path(db_env_path) if db_env_path is not None else None
        self._monotonic = monotonic
        self._host_mapping_cache: dict[str, ZhaoshangMallMapping | None] = {}

    def _mapping_for_host(self, host: str) -> ZhaoshangMallMapping | None:
        normalized = _text(host).lower().lstrip(".")
        if not normalized:
            return None
        if normalized not in self._host_mapping_cache:
            self._host_mapping_cache[normalized] = next(
                (item for item in self._mappings if _host_matches(normalized, item.domains)),
                None,
            )
        return self._host_mapping_cache[normalized]

    def request_from_saas_row(self, row: Mapping[str, Any], *, backend_version: str) -> CrawlRequest | None:
        url = _text(row.get("extLink"))
        vendor = _text(row.get("vendorName")).lower()
        mapping: ZhaoshangMallMapping | None = None
        host = (urlparse(url).hostname or "").lower().lstrip(".")
        if host:
            mapping = self._mapping_for_host(host)
        elif vendor:
            mapping = next(
                (
                    item
                    for item in self._mappings
                    if any(keyword.lower() in vendor for keyword in item.vendor_keywords)
                ),
                None,
            )
        if mapping is None:
            return None
        return CrawlRequest(
            mall_key=mapping.mall_key,
            mall_source_id=mapping.source_id,
            mall_name=mapping.mall_name,
            sku_id=_text(row.get("skuId")),
            url=url,
            backend_version=_text(backend_version),
        )

    @staticmethod
    def _canonical_requests(requests: Iterable[CrawlRequest]) -> tuple[CrawlRequest, ...]:
        unique: dict[tuple[str, str, str, str, str], CrawlRequest] = {}
        for crawl_request in requests:
            normalized = CrawlRequest(
                mall_key=_text(crawl_request.mall_key),
                mall_source_id=_text(crawl_request.mall_source_id),
                mall_name=_text(crawl_request.mall_name),
                sku_id=_text(crawl_request.sku_id),
                url=_text(crawl_request.url),
                backend_version=_text(crawl_request.backend_version),
            )
            if not normalized.sku_id:
                raise ValueError("Zhaoshang crawl request skuId must not be blank")
            if not all((normalized.mall_key, normalized.mall_source_id, normalized.mall_name, normalized.backend_version)):
                raise ValueError("Zhaoshang crawl request mall identity and backend version are required")
            unique.setdefault(normalized.identity, normalized)
        if not unique:
            raise ValueError("Zhaoshang job must contain at least one crawl request")
        return tuple(unique[key] for key in sorted(unique))

    @staticmethod
    def _expected_template_rows(requests: Iterable[CrawlRequest]) -> list[tuple[str, str]]:
        return [(request.sku_id, request.url) for request in requests]

    @staticmethod
    def _read_template_rows(path: Path) -> tuple[tuple[str, ...], list[tuple[str, str]], str]:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.active
            if sheet is None:
                return (), [], ""
            rows = sheet.iter_rows(values_only=True)
            try:
                headers = tuple(_text(value) for value in next(rows))
            except StopIteration:
                return (), [], ""
            values = [(_text(row[0]) if len(row) > 0 else "", _text(row[1]) if len(row) > 1 else "") for row in rows]
            return headers, values, _text(workbook.properties.description)
        finally:
            workbook.close()

    @staticmethod
    def _template_manifest(payload_sha256: str) -> str:
        return f"legacy_zhaoshang_payload_sha256={payload_sha256}"

    @classmethod
    def _verify_existing_template(cls, path: Path, requests: tuple[CrawlRequest, ...], payload_sha256: str) -> None:
        try:
            headers, rows, description = cls._read_template_rows(path)
        except Exception as exc:
            # A zero-byte file may be a stale placeholder from the previous
            # implementation, but it carries no proof that it is ours.  Never
            # overwrite it blindly.
            raise TemplateConflictError("existing Zhaoshang template is unreadable and cannot be safely replaced") from exc
        if (
            headers != TEMPLATE_HEADERS
            or rows != cls._expected_template_rows(requests)
            or description != cls._template_manifest(payload_sha256)
        ):
            raise TemplateConflictError("existing Zhaoshang template differs from durable payload evidence")

    def _write_or_verify_template(self, path: Path, requests: tuple[CrawlRequest, ...], payload_sha256: str) -> None:
        expected_rows = self._expected_template_rows(requests)
        if path.exists():
            self._verify_existing_template(path, requests, payload_sha256)
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("xb"):
                pass
            workbook = Workbook()
            try:
                sheet = workbook.active
                sheet.title = "商品编码导入模板"
                sheet.append(list(TEMPLATE_HEADERS))
                for row in expected_rows:
                    sheet.append(list(row))
                workbook.properties.description = self._template_manifest(payload_sha256)
                workbook.save(temp_path)
            finally:
                workbook.close()
            # Workbook.save owns its file handle.  Flush the completed temp
            # file before the atomic publication step.
            with temp_path.open("r+b") as stream:
                os.fsync(stream.fileno())
            if path.exists():
                self._verify_existing_template(path, requests, payload_sha256)
                return
            for attempt in range(5):
                try:
                    os.replace(temp_path, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1 * (attempt + 1))
            self._verify_existing_template(path, requests, payload_sha256)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except PermissionError:
                # Preserve the original publication error if a Windows scanner
                # still holds the temporary workbook during cleanup.
                pass

    def prepare_job(
        self,
        business_date: str,
        requests: Iterable[CrawlRequest],
        template_path: Path | str,
        result_path: Path | str,
        task_name: str,
        batch_state_path: Path | str,
    ) -> PreparedLegacyJob:
        canonical_requests = self._canonical_requests(requests)
        mall_identities = {(item.mall_key, item.mall_source_id, item.mall_name) for item in canonical_requests}
        if len(mall_identities) != 1:
            raise ValueError("each legacy Zhaoshang job must contain exactly one mall")
        mall_key, mall_source_id, mall_name = next(iter(mall_identities))
        business_date = _text(business_date)
        task_name = _text(task_name)
        if not business_date or not task_name:
            raise ValueError("business_date and task_name are required")
        payload = {
            "business_date": business_date,
            "mall": {"mall_key": mall_key, "mall_source_id": mall_source_id, "mall_name": mall_name},
            "requests": [
                {
                    "mall_key": item.mall_key,
                    "sku_id": item.sku_id,
                    "url": item.url,
                    "backend_version": item.backend_version,
                }
                for item in canonical_requests
            ],
        }
        logical_job_key = _canonical_hash(payload)
        payload_sha256 = _canonical_hash({"headers": TEMPLATE_HEADERS, "payload": payload})
        prepared = PreparedLegacyJob(
            business_date=business_date,
            mall_key=mall_key,
            mall_source_id=mall_source_id,
            mall_name=mall_name,
            task_name=task_name,
            template_path=Path(template_path),
            result_path=Path(result_path),
            batch_state_path=Path(batch_state_path),
            requests=canonical_requests,
            logical_job_key=logical_job_key,
            payload_sha256=payload_sha256,
        )
        if not prepared.batch_state_path.is_file():
            raise ValueError("batch state file must exist before preparing a legacy job")
        self._write_or_verify_template(prepared.template_path, canonical_requests, payload_sha256)
        return prepared

    @staticmethod
    def _read_batch_events(job: PreparedLegacyJob) -> list[dict[str, Any]]:
        try:
            lines = job.batch_state_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            stripped = line.strip()
            if not (stripped.startswith("- `") and stripped.endswith("`")):
                continue
            try:
                event = json.loads(stripped[3:-1])
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict) and event.get("task_name") == job.task_name and event.get("mall") == job.mall_key:
                events.append(event)
        return events
    @staticmethod
    def _priority_evidence(
        events: Iterable[Mapping[str, Any]],
    ) -> tuple[str | None, str | None, str | None, bool]:
        selected: tuple[str, str, str, bool] | None = None
        incomplete_evidence: str | None = None
        incomplete_retryable = False
        for event in events:
            action = _text(event.get("action"))
            evidence: str | None = None
            retryable = False
            if action == "PRIORITY_UPDATED" and int(event.get("affected_rows") or 0) > 0:
                evidence = "updated"
            elif action == "PRIORITY_ALREADY_HIGHEST" and int(event.get("already_highest_count") or 0) > 0:
                evidence = "already_highest_priority"
            elif action == "PRIORITY_UNCONFIRMED":
                evidence = "completed_unconfirmed"
                retryable = event.get("retryable") is True
            if evidence is None:
                continue
            task_id = _text(event.get("task_id"))
            task_number = _text(event.get("task_number"))
            if not task_id or not task_number:
                incomplete_evidence = evidence
                incomplete_retryable = retryable
                continue
            candidate = (task_id, task_number, evidence, retryable)
            if selected is not None and selected[:2] != candidate[:2]:
                raise PriorityEvidenceConflictError(
                    "conflicting accepted priority evidence identities"
                )
            selected = candidate
        if selected is not None:
            return selected
        return None, None, incomplete_evidence, incomplete_retryable

    @staticmethod
    def _submission_evidence(
        job: PreparedLegacyJob,
        events: Iterable[Mapping[str, Any]],
    ) -> tuple[str | None, str | None]:
        created: set[tuple[str, str]] = set()
        enabled: set[tuple[str, str]] = set()
        for event in events:
            action = _text(event.get("action"))
            if action not in {"CREATED", "ENABLED"}:
                continue
            task_id = _text(event.get("task_id"))
            task_number = _text(event.get("task_number"))
            if not task_id or not task_number:
                raise SubmissionEvidenceConflictError(f"{action} evidence has incomplete remote identity")
            if action == "CREATED":
                expected_count = event.get("expected_subtask_count")
                if expected_count not in (None, "") and int(expected_count) != len(job.requests):
                    raise SubmissionEvidenceConflictError("CREATED evidence does not match immutable request count")
                created.add((task_id, task_number))
            else:
                enabled.add((task_id, task_number))
        if not created or not enabled:
            return None, None
        if len(created) != 1 or created != enabled:
            raise SubmissionEvidenceConflictError("CREATED and ENABLED evidence has conflicting remote identity")
        return next(iter(created))

    @staticmethod
    def _submission_marker_time(
        events: Iterable[Mapping[str, Any]],
        task_id: str,
        task_number: str,
    ) -> datetime:
        markers: set[datetime] = set()
        takeover_markers: set[datetime] = set()
        for event in events:
            if _text(event.get("action")) != "CREATED":
                continue
            if (_text(event.get("task_id")), _text(event.get("task_number"))) != (task_id, task_number):
                continue
            takeover_timestamp = _text(event.get("submission_marker_time"))
            timestamp = takeover_timestamp or _text(event.get("timestamp"))
            if not timestamp:
                raise SubmissionEvidenceConflictError("CREATED evidence has no durable timestamp")
            try:
                marker = datetime.fromisoformat(timestamp)
            except ValueError as exc:
                raise SubmissionEvidenceConflictError("CREATED evidence timestamp is invalid") from exc
            if marker.tzinfo is not None and marker.utcoffset() is not None:
                marker = marker.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
            (takeover_markers if takeover_timestamp else markers).add(marker)
        selected = takeover_markers or markers
        if len(selected) != 1:
            raise SubmissionEvidenceConflictError("CREATED evidence has conflicting durable timestamps")
        return next(iter(selected))

    @staticmethod
    def _submission_intent_marker(
        job: PreparedLegacyJob,
        events: Iterable[Mapping[str, Any]],
    ) -> str:
        markers: set[str] = set()
        for event in events:
            if _text(event.get("action")) != "ADD_INTENT":
                continue
            expected_count = event.get("expected_subtask_count")
            if expected_count not in (None, "") and int(expected_count) != len(job.requests):
                raise SubmissionEvidenceConflictError(
                    "ADD_INTENT evidence does not match immutable request count"
                )
            timestamp = _text(event.get("timestamp"))
            if not timestamp:
                raise SubmissionEvidenceConflictError(
                    "ADD_INTENT evidence has no durable timestamp"
                )
            try:
                datetime.fromisoformat(timestamp)
            except ValueError as exc:
                raise SubmissionEvidenceConflictError(
                    "ADD_INTENT evidence timestamp is invalid"
                ) from exc
            markers.add(timestamp)
        if len(markers) != 1:
            raise SubmissionEvidenceConflictError(
                "exact takeover requires one durable ADD_INTENT marker"
            )
        return next(iter(markers))

    def _record_batch_event(self, path: Path, **event: Any) -> None:
        if self._event_recorder is not None:
            self._event_recorder(path, **event)
            return
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang event recorder is unavailable")
        module = self._load_module(
            self._external_tool_path,
            "legacy_zhaoshang_reconcile_events",
        )
        module.record_batch_state_event(path, **event)

    def _validate_submit_inputs(self, job: PreparedLegacyJob, timeout_seconds: float) -> str | None:
        if timeout_seconds <= 0:
            return "submit timeout must be positive"
        if not job.batch_state_path.is_file():
            return "batch state file is missing"
        if not job.template_path.is_file():
            return "template is missing"
        try:
            headers, rows, description = self._read_template_rows(job.template_path)
        except Exception:
            return "template cannot be validated"
        if (
            headers != TEMPLATE_HEADERS
            or rows != self._expected_template_rows(job.requests)
            or description != self._template_manifest(job.payload_sha256)
        ):
            return "template no longer matches durable payload"
        return None

    def _production_runner(self, args: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        # Pre-send phase: everything before Popen() is provably local, so any
        # failure here means no child process exists and the remote /add
        # cannot have happened.
        deadline = self._monotonic() + timeout_seconds
        try:
            if not self._external_tool_path.is_file():
                raise LegacyPreSendFailure("legacy Zhaoshang external tool is unavailable")
            auth_manager = self._auth_manager or AuthManager()
            environment = internal_subprocess_env()
            environment.update(auth_manager.export_environment("zhaoshang", refresh=True))
            if self._db_env_path is not None:
                environment.update(load_zhaoshang_db_env(self._db_env_path))
            for key in tuple(environment):
                if key.startswith("TITLE_MATCH_MODEL_"):
                    environment.pop(key, None)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("legacy Zhaoshang auth refresh exhausted the command deadline")
            args = [str(self._external_tool_path) if value == str(EXTERNAL_TOOL_PATH) else value for value in args]
            try:
                process = subprocess.Popen(
                    args,
                    cwd=str(self._external_tool_path.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                    shell=False,
                )
            except OSError as exc:
                # Popen only raises here when the child could not be created.
                raise LegacyPreSendFailure(f"legacy Zhaoshang subprocess could not be started: {exc}") from exc
        except LegacyPreSendFailure:
            raise
        except Exception as exc:
            raise LegacyPreSendFailure(f"legacy Zhaoshang pre-send setup failed: {type(exc).__name__}") from exc
        # Post-send boundary: the child exists, so any failure below may
        # already have dispatched the remote create and must stay SUBMIT_UNKNOWN.
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            exc.stdout, exc.stderr = process.communicate()
            raise
        except BaseException:
            process.kill()
            process.wait()
            raise
        return subprocess.CompletedProcess(args, process.poll(), stdout, stderr)

    def _run(self, args: list[str], timeout_seconds: float) -> Any:
        if self._runner is not None:
            return self._runner(args, timeout_seconds, {})
        return self._production_runner(args, timeout_seconds)

    @staticmethod
    def _load_module(path: Path, name: str) -> Any:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load legacy Zhaoshang module: {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return module

    # Moved to zhaoshang_remote_task.remote_task_is_enabled; kept as a
    # staticmethod alias so existing self._remote_is_enabled call sites
    # (and subclass/test access) are unchanged.
    _remote_is_enabled = staticmethod(remote_task_is_enabled)

    def exact_export_remote_identity(self, job: PreparedLegacyJob) -> str | None:
        """Return the exact exported remote identity, or ``None`` on any conflict."""
        events = self._read_batch_events(job)
        try:
            task_id, task_number = self._submission_evidence(job, events)
        except SubmissionEvidenceConflictError:
            return None
        if not task_id or not task_number:
            return None
        expected_path = os.path.normcase(str(job.result_path.resolve()))
        for event in reversed(events):
            if _text(event.get("action")) != "EXPORTED":
                continue
            if (_text(event.get("task_id")), _text(event.get("task_number"))) != (
                task_id,
                task_number,
            ):
                continue
            raw_path = _text(event.get("output_path"))
            if raw_path and os.path.normcase(str(Path(raw_path).resolve())) == expected_path:
                return f"{job.mall_key}|{task_id}|{task_number}"
        return None

    def exact_priority_remote_identity(self, job: PreparedLegacyJob) -> str | None:
        """Return an exact identity only for accepted durable priority evidence."""
        events = self._read_batch_events(job)
        try:
            submitted = self._submission_evidence(job, events)
            task_id, task_number, evidence, _retryable = self._priority_evidence(events)
        except (PriorityEvidenceConflictError, SubmissionEvidenceConflictError):
            return None
        if (
            not task_id
            or not task_number
            or submitted != (task_id, task_number)
            or evidence not in {"updated", "already_highest_priority"}
        ):
            return None
        return f"{job.mall_key}|{task_id}|{task_number}"

    def exact_nonretryable_unconfirmed_priority_remote_identity(
        self, job: PreparedLegacyJob
    ) -> str | None:
        """Return the exact identity for the narrow legacy scope-safety incident.

        This is deliberately weaker than accepted priority evidence and must
        only be consumed together with an exact live TERMINAL proof.  It proves
        CREATED/ENABLED and the latest exact priority event all refer to the
        same task, and that the event was the known non-retryable
        PriorityScopeSafetyError family. Other non-retryable priority failures
        remain fail-closed.
        """
        events = self._read_batch_events(job)
        try:
            submitted = self._submission_evidence(job, events)
            task_id, task_number, evidence, retryable = self._priority_evidence(events)
        except (PriorityEvidenceConflictError, SubmissionEvidenceConflictError):
            return None
        if (
            not task_id
            or not task_number
            or submitted != (task_id, task_number)
            or evidence != "completed_unconfirmed"
            or retryable
        ):
            return None
        for event in reversed(events):
            action = _text(event.get("action"))
            if action not in {
                "PRIORITY_UPDATED",
                "PRIORITY_ALREADY_HIGHEST",
                "PRIORITY_UNCONFIRMED",
            }:
                continue
            event_identity = (
                _text(event.get("task_id")),
                _text(event.get("task_number")),
            )
            if event_identity != submitted:
                return None
            if (
                action != "PRIORITY_UNCONFIRMED"
                or "retryable" not in event
                or event.get("retryable") is not False
                or _text(event.get("reason")) != "PriorityScopeSafetyError"
            ):
                return None
            return f"{job.mall_key}|{task_id}|{task_number}"
        return None
