"""Fail-closed adapter for the documented Zhaoshang batch create API.

The adapter deliberately covers only the locally evidenced create and progress
contracts.  The supplied batch skill contains no export API contract; the
separately evidenced read-only SQL result path lives in
``batch_sql_result_adapter`` and is not an HTTP capability of this adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from app.services.legacy_zhaoshang_adapter import CrawlRequest
from app.services.zhaoshang_url_identity import is_absolute_http_url


CREATE_URL = "http://zhaosj.iioprod.com/improxy/iio/crawl/crawlTaskManage/batch/autoAddTask"
PROGRESS_URL = "http://zsz.iio.com/improxy/iio/crawl/crawlTaskManage/queryTaskProgress"
CONTRACT_VERSION = "zhaoshang-batch-url-v1"
MAX_SLICE_SIZE = 100
PURPOSE_PRICE_AUDIT = "price-audit"
PURPOSE_CUSTOMER_PREWARM = "customer-prewarm"
PURPOSE_LEGACY_STEP1_MANUAL = "legacy-step1-manual"
PURPOSE_STEP3_NO_PUSH = "step3-no-push"
_SUPPORTED_PURPOSES = frozenset(
    {
        PURPOSE_PRICE_AUDIT,
        PURPOSE_CUSTOMER_PREWARM,
        PURPOSE_LEGACY_STEP1_MANUAL,
        PURPOSE_STEP3_NO_PUSH,
    }
)


class BatchSubmitStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    REJECTED = "REJECTED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class BatchReconcileStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    MANUAL = "MANUAL"


class BatchProgressStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class BatchRemoteIdentity:
    batch_number: str
    mall_logo: str
    execute_numbers: tuple[str, ...]
    task_id: str | None = None
    task_number: str | None = None

    def durable_value(self) -> str:
        return json.dumps(
            {
                "batchNumber": self.batch_number,
                "executeNumbers": list(self.execute_numbers),
                "mallLogo": self.mall_logo,
                "taskId": self.task_id,
                "taskNumber": self.task_number,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class PreparedBatchJob:
    business_date: str
    parent_job_key: str
    logical_job_key: str
    mall_key: str
    mall_source_id: str
    mall_name: str
    slice_index: int
    slice_count: int
    batch_number: str
    task_name: str
    requests: tuple[CrawlRequest, ...]
    source_ids: tuple[str, ...]
    execute_numbers: tuple[str, ...]
    payload: Mapping[str, Any]
    payload_sha256: str
    purpose: str = PURPOSE_PRICE_AUDIT

    @property
    def expected_count(self) -> int:
        return len(self.requests)

    @property
    def remote_identity(self) -> BatchRemoteIdentity:
        return BatchRemoteIdentity(
            batch_number=self.batch_number,
            mall_logo=self.mall_key,
            execute_numbers=self.execute_numbers,
        )

    def durable_payload(self) -> Mapping[str, Any]:
        """Return the complete immutable envelope needed after a crash."""
        value = {
            "contract_version": CONTRACT_VERSION,
            "purpose": self.purpose,
            "business_date": self.business_date,
            "parent_job_key": self.parent_job_key,
            "logical_job_key": self.logical_job_key,
            "mall_key": self.mall_key,
            "mall_source_id": self.mall_source_id,
            "mall_name": self.mall_name,
            "slice_index": self.slice_index,
            "slice_count": self.slice_count,
            "batch_number": self.batch_number,
            "task_name": self.task_name,
            "requests": [request.__dict__ for request in self.requests],
            "execute_numbers": list(self.execute_numbers),
            "post_body": self.payload,
        }
        if any(self.source_ids):
            value["source_ids"] = list(self.source_ids)
        return value


@dataclass(frozen=True)
class PreparedBatchPlan:
    parent_job_key: str
    jobs: tuple[PreparedBatchJob, ...]


@dataclass(frozen=True)
class BatchSubmitOutcome:
    status: BatchSubmitStatus
    remote_identity: BatchRemoteIdentity | None = None
    diagnostic: str = ""


@dataclass(frozen=True)
class BatchProgress:
    status: BatchProgressStatus
    total: int | None
    completed: int | None
    pending: int | None
    running: int | None
    failed: int | None
    pending_store: int | None


@dataclass(frozen=True)
class BatchReconcileOutcome:
    status: BatchReconcileStatus
    remote_identity: BatchRemoteIdentity | None = None
    progress: BatchProgress | None = None
    diagnostic: str = ""


PostJson = Callable[[str, Mapping[str, Any], float], Any]
GetJson = Callable[[str, Mapping[str, str], float], Any]


def _text(value: object) -> str:
    return str(value or "").strip()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _walk_mappings(value: object) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            yield from _walk_mappings(nested)


def _first_value(value: object, keys: set[str]) -> object | None:
    normalized = {key.casefold() for key in keys}
    for mapping in _walk_mappings(value):
        for key, candidate in mapping.items():
            if str(key).casefold() in normalized and candidate not in (None, ""):
                return candidate
    return None


def _integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


_PROGRESS_FIELDS = {
    "total": ({"total", "totalCount", "totalTask", "taskTotal", "allCount", "总任务量"}, ("总任务量", "任务总量")),
    "completed": ({"completed", "completedCount", "completeCount", "finishCount", "successCount", "执行完成"}, ("执行完成", "已完成")),
    "pending": ({"pending", "pendingCount", "waitCount", "waitingCount", "待执行"}, ("待执行",)),
    "running": ({"running", "runningCount", "executingCount", "执行中"}, ("执行中",)),
    "failed": ({"failed", "failedCount", "failureCount", "失败"}, ("失败",)),
    "pending_store": ({"pendingStore", "pendingStoreCount", "waitStorageCount", "待入库"}, ("待入库",)),
}


def _description(response: Mapping[str, Any]) -> str:
    return _text(_first_value(response, {"rspDesc", "description", "message", "msg"}))


def _progress_value(response: Mapping[str, Any], field: str) -> int | None:
    keys, labels = _PROGRESS_FIELDS[field]
    direct = _integer(_first_value(response, keys))
    if direct is not None:
        return direct
    description = _description(response)
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：=]?\s*(\d+)", description)
        if match:
            return int(match.group(1))
    return None


def parse_progress(response: Mapping[str, Any]) -> BatchProgress:
    values = {name: _progress_value(response, name) for name in _PROGRESS_FIELDS}
    total = values["total"]
    completed = values["completed"]
    pending = values["pending"]
    running = values["running"]
    failed = values["failed"]
    pending_store = values["pending_store"]
    unfinished = (pending, running, pending_store)
    if total is not None and completed == total and all(value == 0 for value in unfinished) and failed == 0:
        status = BatchProgressStatus.COMPLETED
    elif failed is not None and failed > 0 and all(value == 0 for value in unfinished if value is not None):
        status = BatchProgressStatus.FAILED
    elif any(value is not None for value in values.values()):
        status = BatchProgressStatus.RUNNING
    else:
        status = BatchProgressStatus.UNKNOWN
    return BatchProgress(status, total, completed, pending, running, failed, pending_store)


class CnncBatchZhaoshangAdapter:
    """Prepare deterministic one-POST slices and reconcile them by batchNumber.

    Writes are disabled by default.  A caller must persist an immutable remote
    intent and then opt in explicitly before calling :meth:`submit`.
    """

    def __init__(
        self,
        *,
        write_enabled: bool = False,
        post_json: PostJson | None = None,
        get_json: GetJson | None = None,
        create_url: str = CREATE_URL,
        progress_url: str = PROGRESS_URL,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.write_enabled = bool(write_enabled)
        self._post_json = post_json or self._default_post_json
        self._get_json = get_json or self._default_get_json
        self.create_url = create_url
        self.progress_url = progress_url
        self._monotonic = monotonic
        self._attempted: set[str] = set()
        self._attempt_lock = Lock()

    @staticmethod
    def _canonical_requests(requests: Iterable[CrawlRequest]) -> tuple[CrawlRequest, ...]:
        unique: dict[tuple[str, str, str, str, str], CrawlRequest] = {}
        for request in requests:
            normalized = CrawlRequest(
                mall_key=_text(request.mall_key),
                mall_source_id=_text(request.mall_source_id),
                mall_name=_text(request.mall_name),
                sku_id=_text(request.sku_id),
                url=_text(request.url),
                backend_version=_text(request.backend_version),
            )
            if not all((normalized.mall_key, normalized.mall_source_id, normalized.mall_name, normalized.sku_id, normalized.url, normalized.backend_version)):
                raise ValueError("batch URL crawl requires complete mall identity, skuId, URL and backend version")
            if not is_absolute_http_url(normalized.url):
                raise ValueError("batch URL crawl requires an absolute HTTP(S) URL")
            unique.setdefault(normalized.identity, normalized)
        if not unique:
            raise ValueError("batch Zhaoshang plan must contain at least one crawl request")
        return tuple(unique[key] for key in sorted(unique))

    @classmethod
    def prepare_jobs(
        cls,
        business_date: str,
        requests: Iterable[CrawlRequest],
        *,
        batch_size: int = MAX_SLICE_SIZE,
        source_ids: Sequence[str] | None = None,
        purpose: str = PURPOSE_PRICE_AUDIT,
    ) -> PreparedBatchPlan:
        business_date = _text(business_date)
        if not business_date:
            raise ValueError("business_date is required")
        if batch_size <= 0 or batch_size > MAX_SLICE_SIZE:
            raise ValueError("batch_size must be between 1 and 100")
        purpose = _text(purpose)
        if purpose not in _SUPPORTED_PURPOSES:
            raise ValueError("unsupported Zhaoshang batch purpose")
        raw_requests = tuple(requests)
        source_scoped = source_ids is not None
        if source_ids is None:
            canonical = cls._canonical_requests(raw_requests)
            canonical_source_ids = tuple("" for _ in canonical)
        else:
            raw_source_ids = tuple(_text(value) for value in source_ids)
            if len(raw_source_ids) != len(raw_requests) or any(
                not value for value in raw_source_ids
            ):
                raise ValueError("source_ids must exactly cover every batch request")
            members: dict[
                tuple[tuple[str, str, str, str, str], str],
                tuple[CrawlRequest, str],
            ] = {}
            for request, source_id in zip(
                raw_requests, raw_source_ids, strict=True
            ):
                normalized = cls._canonical_requests((request,))[0]
                members.setdefault(
                    (normalized.identity, source_id), (normalized, source_id)
                )
            ordered = tuple(members[key] for key in sorted(members))
            canonical = tuple(request for request, _source_id in ordered)
            canonical_source_ids = tuple(source_id for _request, source_id in ordered)
        parent_payload = {
            "business_date": business_date,
            "contract_version": CONTRACT_VERSION,
            "purpose": purpose,
            "requests": [request.__dict__ for request in canonical],
        }
        if source_scoped:
            parent_payload["source_ids"] = list(canonical_source_ids)
        parent_key = _canonical_hash(parent_payload)
        grouped: dict[
            tuple[str, str, str], list[tuple[CrawlRequest, str]]
        ] = {}
        for request, source_id in zip(
            canonical, canonical_source_ids, strict=True
        ):
            grouped.setdefault(
                (request.mall_key, request.mall_source_id, request.mall_name), []
            ).append((request, source_id))

        jobs: list[PreparedBatchJob] = []
        for (mall_key, mall_source_id, mall_name), mall_members in sorted(grouped.items()):
            chunks = [mall_members[index:index + batch_size] for index in range(0, len(mall_members), batch_size)]
            for offset, member_chunk in enumerate(chunks, start=1):
                chunk = [request for request, _source_id in member_chunk]
                chunk_source_ids = tuple(
                    source_id for _request, source_id in member_chunk
                )
                slice_seed = {
                    "parent_job_key": parent_key,
                    "purpose": purpose,
                    "mall_key": mall_key,
                    "slice_index": offset,
                    "requests": [request.__dict__ for request in chunk],
                }
                if source_scoped:
                    slice_seed["source_ids"] = list(chunk_source_ids)
                logical_key = _canonical_hash(slice_seed)
                batch_number = f"{business_date}-{parent_key[:10]}-{mall_key}-{offset:03d}"
                execute_numbers = tuple(
                    "mrwang-" + _canonical_hash(
                        {
                            "logical_job_key": logical_key,
                            "item_index": index,
                            "identity": request.identity,
                            **(
                                {"source_id": source_id}
                                if source_scoped
                                else {}
                            ),
                        }
                    )[:16]
                    for index, (request, source_id) in enumerate(
                        member_chunk, start=1
                    )
                )
                reptile_list = [
                    {
                        "mallLogo": mall_key,
                        "executeNumber": execute_number,
                        "url": request.url,
                        "productCode": request.sku_id,
                    }
                    for request, execute_number in zip(chunk, execute_numbers, strict=True)
                ]
                payload = {
                    "batchNumber": batch_number,
                    "taskName": batch_number,
                    "crawlMode": "3",
                    "triggerFrequency": "1",
                    "commodityPoolId": "777",
                    "tenantId": "000000",
                    "taskSource": "mrwang",
                    "taskWeight": "0",
                    "subtask": len(chunk),
                    "isScreenshot": "0",
                    "isDetailPic": "0",
                    "isHostPic": "0",
                    "reptileList": reptile_list,
                }
                jobs.append(
                    PreparedBatchJob(
                        business_date=business_date,
                        parent_job_key=parent_key,
                        logical_job_key=logical_key,
                        mall_key=mall_key,
                        mall_source_id=mall_source_id,
                        mall_name=mall_name,
                        slice_index=offset,
                        slice_count=len(chunks),
                        batch_number=batch_number,
                        task_name=batch_number,
                        requests=tuple(chunk),
                        source_ids=chunk_source_ids,
                        execute_numbers=execute_numbers,
                        payload=payload,
                        payload_sha256=_canonical_hash(payload),
                        purpose=purpose,
                    )
                )
        return PreparedBatchPlan(parent_key, tuple(jobs))

    @classmethod
    def restore_job(
        cls,
        value: Mapping[str, Any],
        *,
        expected_logical_job_key: str | None = None,
        expected_payload_sha256: str | None = None,
    ) -> PreparedBatchJob:
        """Rebuild and verify one exact slice from SQLite ``payload_json``."""
        if value.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("unsupported Zhaoshang batch contract version")
        purpose_present = "purpose" in value
        purpose = _text(value.get("purpose") or PURPOSE_PRICE_AUDIT)
        if purpose not in _SUPPORTED_PURPOSES:
            raise ValueError("unsupported Zhaoshang batch purpose")
        try:
            requests = tuple(CrawlRequest(**dict(item)) for item in value["requests"])
            execute_numbers = tuple(_text(item) for item in value["execute_numbers"])
            source_scoped = "source_ids" in value
            source_ids = (
                tuple(_text(item) for item in value["source_ids"])
                if source_scoped
                else tuple("" for _ in requests)
            )
            post_body = dict(value["post_body"])
            job = PreparedBatchJob(
                business_date=_text(value["business_date"]),
                parent_job_key=_text(value["parent_job_key"]),
                logical_job_key=_text(value["logical_job_key"]),
                mall_key=_text(value["mall_key"]),
                mall_source_id=_text(value["mall_source_id"]),
                mall_name=_text(value["mall_name"]),
                slice_index=int(value["slice_index"]),
                slice_count=int(value["slice_count"]),
                batch_number=_text(value["batch_number"]),
                task_name=_text(value["task_name"]),
                requests=requests,
                source_ids=source_ids,
                execute_numbers=execute_numbers,
                payload=post_body,
                payload_sha256=_canonical_hash(post_body),
                purpose=purpose,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid durable Zhaoshang batch envelope") from exc
        if source_scoped:
            member_keys = tuple(
                (request.identity, source_id)
                for request, source_id in zip(
                    job.requests, job.source_ids, strict=True
                )
            )
            if (
                len(job.source_ids) != len(job.requests)
                or any(not value for value in job.source_ids)
                or len(set(member_keys)) != len(member_keys)
            ):
                raise ValueError("durable source identity does not match batch requests")
            ordered_members = tuple(
                sorted(
                    zip(job.requests, job.source_ids, strict=True),
                    key=lambda item: (item[0].identity, item[1]),
                )
            )
            canonical = tuple(request for request, _source_id in ordered_members)
            canonical_source_ids = tuple(
                source_id for _request, source_id in ordered_members
            )
        else:
            canonical = cls._canonical_requests(job.requests)
            canonical_source_ids = tuple("" for _ in canonical)
        if (
            canonical != job.requests
            or canonical_source_ids != job.source_ids
            or not (0 < len(job.requests) <= MAX_SLICE_SIZE)
        ):
            raise ValueError("durable Zhaoshang batch requests are not canonical")
        if len(job.execute_numbers) != len(job.requests):
            raise ValueError("durable executeNumber count does not match requests")
        seed = {
            "parent_job_key": job.parent_job_key,
            "mall_key": job.mall_key,
            "slice_index": job.slice_index,
            "requests": [request.__dict__ for request in job.requests],
        }
        if purpose_present:
            seed["purpose"] = job.purpose
        if source_scoped:
            seed["source_ids"] = list(job.source_ids)
        logical_key = _canonical_hash(seed)
        expected_batch_number = f"{job.business_date}-{job.parent_job_key[:10]}-{job.mall_key}-{job.slice_index:03d}"
        expected_execute_numbers = tuple(
            "mrwang-"
            + _canonical_hash(
                {
                    "logical_job_key": logical_key,
                    "item_index": index,
                    "identity": request.identity,
                    **(
                        {"source_id": source_id}
                        if source_scoped
                        else {}
                    ),
                }
            )[:16]
            for index, (request, source_id) in enumerate(
                zip(job.requests, job.source_ids, strict=True), start=1
            )
        )
        expected_items = [
            {
                "mallLogo": job.mall_key,
                "executeNumber": execute_number,
                "url": request.url,
                "productCode": request.sku_id,
            }
            for request, execute_number in zip(job.requests, expected_execute_numbers, strict=True)
        ]
        expected_body = {
            "batchNumber": job.batch_number,
            "taskName": job.task_name,
            "crawlMode": "3",
            "triggerFrequency": "1",
            "commodityPoolId": "777",
            "tenantId": "000000",
            "taskSource": "mrwang",
            "taskWeight": "0",
            "subtask": len(job.requests),
            "isScreenshot": "0",
            "isDetailPic": "0",
            "isHostPic": "0",
            "reptileList": expected_items,
        }
        if (
            logical_key != job.logical_job_key
            or job.batch_number != expected_batch_number
            or job.task_name != job.batch_number
            or job.execute_numbers != expected_execute_numbers
            or post_body != expected_body
        ):
            raise ValueError("durable Zhaoshang batch envelope failed identity verification")
        if expected_logical_job_key is not None and logical_key != expected_logical_job_key:
            raise ValueError("durable logical job identity differs from SQLite")
        if expected_payload_sha256 is not None and job.payload_sha256 != expected_payload_sha256:
            raise ValueError("durable POST body hash differs from SQLite")
        return job

    @staticmethod
    def validate_remote_identity(
        job: PreparedBatchJob, value: str
    ) -> BatchRemoteIdentity:
        """Parse and verify the exact typed identity bound to one durable job."""
        try:
            raw = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("batch remote identity is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("batch remote identity must be an object")

        batch_number = raw.get("batchNumber")
        mall_logo = raw.get("mallLogo")
        execute_numbers = raw.get("executeNumbers")
        task_id = raw.get("taskId")
        task_number = raw.get("taskNumber")
        if not isinstance(batch_number, str) or not batch_number.strip():
            raise ValueError("batchNumber must be a non-empty string")
        if not isinstance(mall_logo, str) or not mall_logo.strip():
            raise ValueError("mallLogo must be a non-empty string")
        if (
            not isinstance(execute_numbers, list)
            or not execute_numbers
            or any(not isinstance(item, str) or not item.strip() for item in execute_numbers)
        ):
            raise ValueError("executeNumbers must contain non-empty strings")
        if task_id is not None and not isinstance(task_id, str):
            raise ValueError("taskId must be a string or null")
        if task_number is not None and not isinstance(task_number, str):
            raise ValueError("taskNumber must be a string or null")

        normalized_execute_numbers = tuple(item.strip() for item in execute_numbers)
        if (
            batch_number.strip() != job.batch_number
            or mall_logo.strip() != job.mall_key
            or len(normalized_execute_numbers) != len(set(normalized_execute_numbers))
            or set(normalized_execute_numbers) != set(job.execute_numbers)
        ):
            raise ValueError("batch remote identity differs from durable intent")
        return BatchRemoteIdentity(
            batch_number=batch_number.strip(),
            mall_logo=mall_logo.strip(),
            execute_numbers=normalized_execute_numbers,
            task_id=task_id.strip() if task_id is not None else None,
            task_number=task_number.strip() if task_number is not None else None,
        )

    @staticmethod
    def _default_post_json(url: str, body: Mapping[str, Any], timeout_seconds: float) -> tuple[int, Mapping[str, Any]]:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=max(0.001, timeout_seconds)) as client:
            response = client.post(url, json=body, headers={"Content-Type": "application/json"})
            return response.status_code, response.json()

    @staticmethod
    def _default_get_json(url: str, params: Mapping[str, str], timeout_seconds: float) -> tuple[int, Mapping[str, Any]]:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=max(0.001, timeout_seconds)) as client:
            response = client.get(url, params=params)
            return response.status_code, response.json()

    @staticmethod
    def _normalize_response(value: Any) -> tuple[int, Mapping[str, Any]]:
        if isinstance(value, tuple) and len(value) == 2:
            status, payload = value
        else:
            status, payload = 200, value
        if not isinstance(payload, Mapping):
            raise ValueError("Zhaoshang batch response is not a JSON object")
        return int(status), payload

    @staticmethod
    def _identity(job: PreparedBatchJob, response: Mapping[str, Any]) -> BatchRemoteIdentity:
        batch_number = _text(_first_value(response, {"batchNumber", "batch_number"})) or job.batch_number
        return BatchRemoteIdentity(
            batch_number=batch_number,
            mall_logo=_text(_first_value(response, {"mallLogo", "mall_logo"})) or job.mall_key,
            execute_numbers=job.execute_numbers,
            task_id=_text(_first_value(response, {"taskId", "task_id"})) or None,
            task_number=_text(_first_value(response, {"taskNumber", "taskNo", "task_number"})) or None,
        )

    def submit(self, job: PreparedBatchJob, *, timeout_seconds: float) -> BatchSubmitOutcome:
        if timeout_seconds <= 0:
            return BatchSubmitOutcome(BatchSubmitStatus.NOT_ATTEMPTED, diagnostic="submit timeout must be positive")
        if not self.write_enabled:
            return BatchSubmitOutcome(BatchSubmitStatus.NOT_ATTEMPTED, diagnostic="batch writes are disabled")
        with self._attempt_lock:
            if job.logical_job_key in self._attempted:
                return BatchSubmitOutcome(
                    BatchSubmitStatus.SUBMIT_UNKNOWN,
                    remote_identity=job.remote_identity,
                    diagnostic="POST was already attempted; reconcile exact batchNumber instead of resubmitting",
                )
            self._attempted.add(job.logical_job_key)
        started = self._monotonic()
        try:
            status, response = self._normalize_response(self._post_json(self.create_url, job.payload, timeout_seconds))
        except Exception:
            return BatchSubmitOutcome(
                BatchSubmitStatus.SUBMIT_UNKNOWN,
                remote_identity=job.remote_identity,
                diagnostic="batch create completion is unknown",
            )
        if self._monotonic() - started > timeout_seconds:
            return BatchSubmitOutcome(
                BatchSubmitStatus.SUBMIT_UNKNOWN,
                remote_identity=job.remote_identity,
                diagnostic="batch create exceeded its elapsed limit",
            )
        code = _text(_first_value(response, {"rspCode", "code"}))
        success = _first_value(response, {"success"})
        identity = self._identity(job, response)
        if 200 <= status < 300 and code == "0000" and success is True:
            if identity.batch_number != job.batch_number or identity.mall_logo != job.mall_key:
                return BatchSubmitOutcome(
                    BatchSubmitStatus.SUBMIT_UNKNOWN,
                    remote_identity=identity,
                    diagnostic="batch create response conflicts with durable identity",
                )
            return BatchSubmitOutcome(BatchSubmitStatus.SUBMITTED, remote_identity=identity)
        # A deterministic validation rejection proves the server did not
        # accept this POST. Auth, throttling and server failures do not.
        if status == 400 or (200 <= status < 300 and code and code != "0000"):
            return BatchSubmitOutcome(BatchSubmitStatus.REJECTED, remote_identity=job.remote_identity, diagnostic="batch create was explicitly rejected")
        return BatchSubmitOutcome(
            BatchSubmitStatus.SUBMIT_UNKNOWN,
            remote_identity=job.remote_identity,
            diagnostic="batch create response did not prove acceptance",
        )

    def query_progress(self, job: PreparedBatchJob, *, timeout_seconds: float) -> tuple[Mapping[str, Any], BatchProgress]:
        if timeout_seconds <= 0:
            raise ValueError("progress timeout must be positive")
        status, response = self._normalize_response(
            self._get_json(self.progress_url, {"batchNumber": job.batch_number}, timeout_seconds)
        )
        if not 200 <= status < 300:
            raise RuntimeError("batch progress request did not return HTTP success")
        return response, parse_progress(response)

    def reconcile_unknown(self, job: PreparedBatchJob, *, timeout_seconds: float) -> BatchReconcileOutcome:
        try:
            response, progress = self.query_progress(job, timeout_seconds=timeout_seconds)
        except Exception:
            return BatchReconcileOutcome(
                BatchReconcileStatus.NOT_FOUND,
                diagnostic="exact batchNumber reconciliation is not yet conclusive",
            )
        code = _text(_first_value(response, {"rspCode", "code"}))
        description = _description(response)
        if code != "0000":
            if any(marker in description for marker in ("不存在", "未查询到", "为空")):
                return BatchReconcileOutcome(BatchReconcileStatus.NOT_FOUND, progress=progress)
            return BatchReconcileOutcome(
                BatchReconcileStatus.NOT_FOUND,
                progress=progress,
                diagnostic="progress endpoint has not proved exact task existence",
            )
        echoed_batch = _first_value(response, {"batchNumber", "batch_number"})
        echoed_mall = _first_value(response, {"mallLogo", "mall_logo"})
        echoed_execute_numbers = _first_value(
            response, {"executeNumbers", "execute_numbers"}
        )
        echoed_identity_conflict = (
            echoed_batch not in (None, "")
            and _text(echoed_batch) != job.batch_number
        ) or (
            echoed_mall not in (None, "")
            and _text(echoed_mall) != job.mall_key
        )
        if echoed_execute_numbers not in (None, ""):
            echoed_identity_conflict = echoed_identity_conflict or (
                not isinstance(echoed_execute_numbers, list)
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in echoed_execute_numbers
                )
                or len(echoed_execute_numbers)
                != len(set(value.strip() for value in echoed_execute_numbers))
                or set(value.strip() for value in echoed_execute_numbers)
                != set(job.execute_numbers)
            )
        identity = BatchRemoteIdentity(
            batch_number=job.batch_number,
            mall_logo=job.mall_key,
            execute_numbers=job.execute_numbers,
            task_id=_text(_first_value(response, {"taskId", "task_id"})) or None,
            task_number=_text(
                _first_value(response, {"taskNumber", "taskNo", "task_number"})
            )
            or None,
        )
        if echoed_identity_conflict:
            return BatchReconcileOutcome(
                BatchReconcileStatus.MANUAL,
                remote_identity=identity,
                progress=progress,
                diagnostic="progress identity conflicts with durable batch slice",
            )
        if progress.total is None:
            return BatchReconcileOutcome(
                BatchReconcileStatus.NOT_FOUND,
                remote_identity=identity,
                progress=progress,
                diagnostic="progress response has no total count evidence",
            )
        if progress.total == 0:
            return BatchReconcileOutcome(BatchReconcileStatus.NOT_FOUND, remote_identity=identity, progress=progress)
        if progress.total != job.expected_count:
            return BatchReconcileOutcome(
                BatchReconcileStatus.MANUAL,
                remote_identity=identity,
                progress=progress,
                diagnostic="progress total conflicts with the durable slice size",
            )
        return BatchReconcileOutcome(BatchReconcileStatus.FOUND, remote_identity=identity, progress=progress)
