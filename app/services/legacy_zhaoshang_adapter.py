"""Narrow create-only / wait-export-only seam for the legacy Zhaoshang tool."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

from openpyxl import Workbook, load_workbook

from app.services.zhaoshang_result_adapter import (
    read_zhaoshang_result_xlsx,
    read_zhaoshang_result_xlsx_by_identity,
)
from scripts.auth_manager import AuthManager
from scripts.curl_utils import internal_subprocess_env
from scripts.zhaoshang_mall_mapping import (
    ZhaoshangMallMapping,
    load_zhaoshang_mall_mappings,
)


EXTERNAL_TOOL_PATH = Path(
    r"E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py"
)
TEMPLATE_HEADERS = ("商品编码", "url")
RESULT_URL_HEADERS = frozenset({"原始链接", "商品详情地址", "url", "URL", "商品链接", "链接", "商品URL"})
RESULT_CODE_HEADERS = frozenset({"商品编码", "商品最小品类单元Id", "skuId", "sku_id", "code"})


class SubmitStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class HarvestStatus(str, Enum):
    EXPORTED = "EXPORTED"
    RUNNING = "RUNNING"
    MANUAL = "MANUAL"
    FAILED = "FAILED"


class ReconcileStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    MANUAL = "MANUAL"


class LegacyZhaoshangAdapterError(RuntimeError):
    pass


class TemplateConflictError(LegacyZhaoshangAdapterError):
    pass


class PriorityEvidenceConflictError(LegacyZhaoshangAdapterError):
    pass


@dataclass(frozen=True)
class CrawlRequest:
    mall_key: str
    mall_source_id: str
    mall_name: str
    sku_id: str
    url: str
    backend_version: str

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (self.mall_key, self.mall_source_id, self.sku_id, self.url.strip(), self.backend_version)


@dataclass(frozen=True)
class PreparedLegacyJob:
    business_date: str
    mall_key: str
    mall_source_id: str
    mall_name: str
    task_name: str
    template_path: Path
    result_path: Path
    batch_state_path: Path
    requests: tuple[CrawlRequest, ...]
    logical_job_key: str
    payload_sha256: str


@dataclass(frozen=True)
class LegacySubmitOutcome:
    status: SubmitStatus
    task_id: str | None = None
    task_number: str | None = None
    priority_evidence: str | None = None
    diagnostic: str = ""


@dataclass(frozen=True)
class LegacyHarvestOutcome:
    status: HarvestStatus
    result_map: Mapping[str, Mapping[str, Any]] | None = None
    # URL-only maps remain for legacy callers. Incremental consumers must use
    # this source identity to avoid merging two SKUs with one exact URL.
    result_by_identity: Mapping[tuple[str, str], Mapping[str, Any]] | None = None
    reused_existing: bool = False
    diagnostic: str = ""


@dataclass(frozen=True)
class LegacyReconcileOutcome:
    status: ReconcileStatus
    task_id: str | None = None
    task_number: str | None = None
    diagnostic: str = ""


class _BoundedReadOnlySession:
    """Clamp legacy helper HTTP calls to the tick's remaining reconcile budget."""

    def __init__(
        self,
        session: Any,
        timeout_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        deadline: float | None = None,
    ) -> None:
        self._session = session
        self._monotonic = monotonic
        self._deadline = (
            float(deadline)
            if deadline is not None
            else self._monotonic() + float(timeout_seconds)
        )

    def _bounded_timeout(self, requested: Any) -> float:
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy read-only reconciliation deadline is exhausted")
        try:
            requested_timeout = float(requested)
        except (TypeError, ValueError):
            requested_timeout = remaining
        return min(remaining, max(requested_timeout, 0.001))

    def post(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = self._bounded_timeout(kwargs.get("timeout"))
        return self._session.post(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = self._bounded_timeout(kwargs.get("timeout"))
        return self._session.get(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def _text(value: object) -> str:
    return str(value or "").strip()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _temporary_environment(values: Mapping[str, str]):
    """Expose state-backed auth only while constructing the legacy session."""
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update({key: str(value) for key, value in values.items()})
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    host = _text(host).lower().lstrip(".")
    return bool(host) and any(
        host == str(domain).lower().lstrip(".")
        or host.endswith("." + str(domain).lower().lstrip("."))
        for domain in domains
        if str(domain).strip()
    )


class CnncLegacyZhaoshangAdapter:
    def __init__(
        self,
        *,
        mappings: Iterable[ZhaoshangMallMapping] | None = None,
        runner: Callable[[list[str], float, Mapping[str, str]], Any] | None = None,
        reconciler: Callable[[str, str, float], Mapping[str, Any] | None] | None = None,
        auth_manager: AuthManager | None = None,
        external_tool_path: Path = EXTERNAL_TOOL_PATH,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mappings = tuple(mappings) if mappings is not None else tuple(load_zhaoshang_mall_mappings())
        self._runner = runner
        self._reconciler = reconciler
        self._auth_manager = auth_manager
        self._external_tool_path = Path(external_tool_path)
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
            os.replace(temp_path, path)
            self._verify_existing_template(path, requests, payload_sha256)
        finally:
            temp_path.unlink(missing_ok=True)

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
    def _priority_evidence(events: Iterable[Mapping[str, Any]]) -> tuple[str | None, str | None, str | None]:
        selected: tuple[str, str, str] | None = None
        incomplete_evidence: str | None = None
        for event in events:
            action = _text(event.get("action"))
            evidence: str | None = None
            if action == "PRIORITY_UPDATED" and int(event.get("affected_rows") or 0) > 0:
                evidence = "updated"
            elif action == "PRIORITY_ALREADY_HIGHEST" and int(event.get("already_highest_count") or 0) > 0:
                evidence = "already_highest_priority"
            elif action == "PRIORITY_UNCONFIRMED":
                evidence = "completed_unconfirmed"
            if evidence is None:
                continue
            task_id = _text(event.get("task_id"))
            task_number = _text(event.get("task_number"))
            if not task_id or not task_number:
                incomplete_evidence = evidence
                continue
            candidate = (task_id, task_number, evidence)
            if selected is not None and selected[:2] != candidate[:2]:
                raise PriorityEvidenceConflictError(
                    "conflicting accepted priority evidence identities"
                )
            selected = candidate
        if selected is not None:
            return selected
        return None, None, incomplete_evidence

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

    @staticmethod
    def _harvest_args(job: PreparedLegacyJob, timeout_seconds: float) -> list[str]:
        return [
            sys.executable,
            str(EXTERNAL_TOOL_PATH),
            "--task-name", job.task_name,
            "--excel", str(job.template_path),
            "--mall", job.mall_key,
            "--output", str(job.result_path),
            "--task-weight", "0",
            "--wait-timeout", str(max(1, int(timeout_seconds))),
            "--wait-export-only",
            "--batch-state-file", str(job.batch_state_path),
        ]

    def _production_runner(self, args: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        if not self._external_tool_path.is_file():
            raise FileNotFoundError("legacy Zhaoshang external tool is unavailable")
        deadline = self._monotonic() + timeout_seconds
        auth_manager = self._auth_manager or AuthManager()
        environment = internal_subprocess_env()
        environment.update(auth_manager.export_environment("zhaoshang", refresh=True))
        for key in tuple(environment):
            if key.startswith("TITLE_MATCH_MODEL_"):
                environment.pop(key, None)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy Zhaoshang auth refresh exhausted the command deadline")
        args = [str(self._external_tool_path) if value == str(EXTERNAL_TOOL_PATH) else value for value in args]
        return subprocess.run(
            args,
            cwd=str(self._external_tool_path.parent),
            capture_output=True,
            text=True,
            timeout=remaining,
            env=environment,
            shell=False,
            check=False,
        )

    def _run(self, args: list[str], timeout_seconds: float) -> Any:
        if self._runner is not None:
            return self._runner(args, timeout_seconds, {})
        return self._production_runner(args, timeout_seconds)

    def submit(self, job: PreparedLegacyJob, *, timeout_seconds: float) -> LegacySubmitOutcome:
        invalid_reason = self._validate_submit_inputs(job, timeout_seconds)
        if invalid_reason:
            return LegacySubmitOutcome(SubmitStatus.NOT_ATTEMPTED, diagnostic=invalid_reason)
        try:
            process = self._run(self._create_args(job), timeout_seconds)
        except (subprocess.TimeoutExpired, TimeoutError, OSError):
            return LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic="legacy create-only completion is unknown")
        if int(getattr(process, "returncode", -1)) != 0:
            return LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic="legacy create-only returned a nonzero exit")
        try:
            task_id, task_number, evidence = self._priority_evidence(self._read_batch_events(job))
        except PriorityEvidenceConflictError as exc:
            return LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic=str(exc))
        if evidence is None or not task_id or not task_number:
            return LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, task_id, task_number, diagnostic="legacy create-only lacks accepted priority evidence")
        return LegacySubmitOutcome(SubmitStatus.SUBMITTED, task_id, task_number, evidence)

    @staticmethod
    def _identity_from_remote(remote: Mapping[str, Any]) -> tuple[str | None, str | None]:
        task_id = _text(remote.get("taskId") or remote.get("id") or remote.get("task_id")) or None
        task_number = _text(remote.get("taskNumber") or remote.get("taskNo") or remote.get("task_number")) or None
        return task_id, task_number

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
        with _temporary_environment(auth_environment):
            session = _BoundedReadOnlySession(
                module.build_session(),
                remaining,
                monotonic=self._monotonic,
                deadline=deadline,
            )
            mall_code = module.priority_mall_code_for_key(mall_key)
            return module.check_existing_task(session, task_name, mall_code)

    def reconcile_unknown(self, job: PreparedLegacyJob, *, timeout_seconds: float) -> LegacyReconcileOutcome:
        if timeout_seconds <= 0:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="reconciliation timeout must be positive")
        reconciler = self._reconciler or self._default_reconciler
        try:
            remote = reconciler(job.task_name, job.mall_key, timeout_seconds)
        except (FileNotFoundError, ImportError, AttributeError):
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="exact task reconciliation tooling is unavailable")
        except Exception:
            # A read timeout, authentication refresh race, or other transient
            # failure proves neither absence nor existence. Keep the durable
            # submit unknown until the outer harvest attempts/elapsed cap.
            return LegacyReconcileOutcome(ReconcileStatus.NOT_FOUND, diagnostic="exact task reconciliation is not yet conclusive")
        if remote is None:
            return LegacyReconcileOutcome(ReconcileStatus.NOT_FOUND)
        task_id, task_number = self._identity_from_remote(remote)
        if not task_id or not task_number:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="exact task reconciliation returned incomplete identity")
        try:
            event_task_id, event_task_number, evidence = self._priority_evidence(self._read_batch_events(job))
        except PriorityEvidenceConflictError as exc:
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic=str(exc))
        if evidence is None:
            return LegacyReconcileOutcome(ReconcileStatus.NOT_FOUND, diagnostic="remote task is not yet backed by priority evidence")
        if not event_task_id or not event_task_number:
            return LegacyReconcileOutcome(ReconcileStatus.NOT_FOUND, diagnostic="priority evidence has no complete remote identity yet")
        if (event_task_id, event_task_number) != (task_id, task_number):
            return LegacyReconcileOutcome(ReconcileStatus.MANUAL, diagnostic="priority evidence conflicts with exact remote identity")
        return LegacyReconcileOutcome(ReconcileStatus.FOUND, task_id, task_number)

    @staticmethod
    def _result_column_index(headers: list[str], allowed: frozenset[str]) -> int:
        return next((index for index, value in enumerate(headers) if value in allowed), -1)

    @classmethod
    def _validate_result_workbook(
        cls, job: PreparedLegacyJob
    ) -> tuple[Mapping[str, Mapping[str, Any]], Mapping[tuple[str, str], Mapping[str, Any]]]:
        if not job.result_path.is_file():
            raise ValueError("result workbook does not exist")
        workbook = load_workbook(job.result_path, read_only=True, data_only=True)
        try:
            sheet = workbook.active
            if sheet is None:
                raise ValueError("result workbook has no active sheet")
            iterator = sheet.iter_rows(values_only=True)
            try:
                headers = [_text(value) for value in next(iterator)]
            except StopIteration as exc:
                raise ValueError("result workbook is empty") from exc
            url_index = cls._result_column_index(headers, RESULT_URL_HEADERS)
            code_index = cls._result_column_index(headers, RESULT_CODE_HEADERS)
            if url_index < 0 and code_index < 0:
                raise ValueError("result workbook header is not recognized")
            result_rows = [row for row in iterator if any(_text(value) for value in row)]
        finally:
            workbook.close()
        if len(result_rows) != len(job.requests):
            raise ValueError("result workbook row count does not match request count")

        expected = Counter((request.sku_id, request.url.strip()) for request in job.requests)
        by_sku: dict[str, set[str]] = {}
        by_url: dict[str, set[str]] = {}
        for sku_id, exact_url in expected:
            by_sku.setdefault(sku_id, set()).add(exact_url)
            by_url.setdefault(exact_url, set()).add(sku_id)

        observed: list[tuple[str, str]] = []
        for row in result_rows:
            sku_id = _text(row[code_index] if 0 <= code_index < len(row) else "")
            exact_url = _text(row[url_index] if 0 <= url_index < len(row) else "")
            if sku_id and exact_url:
                identity = (sku_id, exact_url)
                if identity not in expected:
                    # Some legacy exports fill a URL for a request originally
                    # submitted with a blank URL.  The SKU may establish that
                    # identity only when it is unique in this job.
                    if (sku_id, "") in expected and by_sku.get(sku_id) == {""}:
                        identity = (sku_id, "")
                    else:
                        raise ValueError("result workbook contains an unexpected skuId / exact URL identity")
            elif sku_id:
                urls = by_sku.get(sku_id, set())
                if len(urls) != 1:
                    raise ValueError("result workbook skuId fallback is ambiguous")
                identity = (sku_id, next(iter(urls)))
            elif exact_url:
                skus = by_url.get(exact_url, set())
                if len(skus) != 1:
                    raise ValueError("result workbook exact URL fallback is ambiguous")
                identity = (next(iter(skus)), exact_url)
            else:
                raise ValueError("result workbook row has no parseable crawl identity")
            observed.append(identity)
        if Counter(observed) != expected:
            raise ValueError("result workbook crawl identities do not match durable request identities")

        observed_result_by_identity = read_zhaoshang_result_xlsx_by_identity(
            job.result_path, template_path=job.template_path
        )
        result_by_identity: dict[tuple[str, str], Mapping[str, Any]] = {}
        for observed_identity, value in observed_result_by_identity.items():
            sku_id, exact_url = observed_identity
            durable_identity = observed_identity
            if durable_identity not in expected:
                if (sku_id, "") in expected and by_sku.get(sku_id) == {""}:
                    durable_identity = (sku_id, "")
                else:
                    raise ValueError("result workbook result identity is not a durable request identity")
            if durable_identity in result_by_identity:
                raise ValueError("result workbook result identity is ambiguous")
            result_by_identity[durable_identity] = value
        if Counter(result_by_identity.keys()) != expected:
            raise ValueError("result workbook has no complete parseable crawl identity mapping")
        result_map = read_zhaoshang_result_xlsx(job.result_path, template_path=job.template_path)
        expected_urls = {url for _sku_id, url in expected if url}
        if expected_urls and (not result_map or not expected_urls.issubset(result_map)):
            raise ValueError("result workbook has no complete parseable URL result mapping")
        return result_map, result_by_identity

    def harvest(self, job: PreparedLegacyJob, *, timeout_seconds: float) -> LegacyHarvestOutcome:
        if timeout_seconds <= 0:
            return LegacyHarvestOutcome(HarvestStatus.FAILED, diagnostic="harvest timeout must be positive")
        if job.result_path.exists():
            try:
                result_map, result_by_identity = self._validate_result_workbook(job)
            except Exception:
                return LegacyHarvestOutcome(HarvestStatus.FAILED, diagnostic="existing result workbook is invalid")
            return LegacyHarvestOutcome(
                HarvestStatus.EXPORTED,
                result_map=result_map,
                result_by_identity=result_by_identity,
                reused_existing=True,
            )
        try:
            process = self._run(self._harvest_args(job, timeout_seconds), timeout_seconds)
        except (subprocess.TimeoutExpired, TimeoutError):
            return LegacyHarvestOutcome(HarvestStatus.RUNNING, diagnostic="legacy wait-export-only is still running")
        except OSError:
            return LegacyHarvestOutcome(HarvestStatus.MANUAL, diagnostic="legacy wait-export-only could not be started")
        if int(getattr(process, "returncode", -1)) != 0:
            return LegacyHarvestOutcome(HarvestStatus.MANUAL, diagnostic="legacy wait-export-only did not produce an export")
        if not job.result_path.exists():
            return LegacyHarvestOutcome(HarvestStatus.RUNNING, diagnostic="legacy export is not available yet")
        try:
            result_map, result_by_identity = self._validate_result_workbook(job)
        except Exception:
            return LegacyHarvestOutcome(HarvestStatus.FAILED, diagnostic="exported result workbook is invalid")
        return LegacyHarvestOutcome(
            HarvestStatus.EXPORTED,
            result_map=result_map,
            result_by_identity=result_by_identity,
        )
