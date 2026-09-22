"""Export capability of the existing Zhaoshang adapter; original contracts retained."""
from __future__ import annotations
from typing import Sequence
from app.services.zhaoshang_adapter_contract import (
    Any,
    Counter,
    CrawlRequest,
    EXTERNAL_TOOL_PATH,
    HarvestStatus,
    LegacyHarvestOutcome,
    LegacyPreSendFailure,
    Mapping,
    PartialExactHarvestOutcome,
    PreparedLegacyJob,
    RESULT_BATCH_HEADERS,
    RESULT_CODE_HEADERS,
    RESULT_COMMENT_HEADERS,
    RESULT_EXECUTION_HEADERS,
    RESULT_MALL_HEADERS,
    RESULT_PAGE_SKU_HEADERS,
    RESULT_TITLE_HEADERS,
    RESULT_URL_HEADERS,
    SubmissionEvidenceConflictError,
    _parse_export_comment_count,
    _text,
    _wait_export_startup_failure_is_transient,
    load_workbook,
    parse_decimal,
    parse_qsl,
    re,
    read_zhaoshang_result_xlsx,
    read_zhaoshang_result_xlsx_by_identity,
    replace,
    subprocess,
    sys,
    urlparse,
)
from app.services.zhaoshang_adapter_base import ZhaoshangAdapterBase


class ZhaoshangExportOperations(ZhaoshangAdapterBase):

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

    @staticmethod
    def _result_column_index(headers: list[str], allowed: frozenset[str]) -> int:
        return next((index for index, value in enumerate(headers) if value in allowed), -1)

    @staticmethod
    def _result_column_indices(headers: list[str], allowed: frozenset[str]) -> list[int]:
        return [index for index, value in enumerate(headers) if value in allowed]

    @staticmethod
    def _matches_legacy_export_url(durable_url: str, exported_url: str) -> bool:
        """Match exact URLs plus strict, allowlisted legacy export rewrites."""
        if durable_url == exported_url:
            return True
        if not durable_url or not exported_url:
            return False
        durable = urlparse(durable_url)
        exported = urlparse(exported_url)
        durable_query = parse_qsl(durable.query, keep_blank_values=True)
        exported_query = parse_qsl(exported.query, keep_blank_values=True)
        if (
            durable.scheme == exported.scheme == "https"
            and durable.netloc == exported.netloc == "www.qstbg.com"
            and durable.path == "/gd/detail.do"
            and exported.path == "/gd/initParameter.do"
            and not durable.fragment
            and exported.fragment == "title_detail"
            and len({key for key, _value in durable_query}) == len(durable_query)
            and len({key for key, _value in exported_query}) == len(exported_query)
        ):
            durable_params = dict(durable_query)
            exported_params = dict(exported_query)
            goods_id = durable_params.get("goods_id", "")
            if (
                goods_id
                and set(durable_params).issubset({"goods_id", "formid"})
                and durable_params.get("formid", "searchform") == "searchform"
                and exported_params == {"goods_id": goods_id, "formid": "form1"}
            ):
                return True
        jd_hosts = {"i-item.jd.com", "item.jd.com"}
        durable_jd_product = re.fullmatch(r"/([0-9]+)\.html", durable.path)
        exported_jd_product = re.fullmatch(r"/([0-9]+)\.html", exported.path)
        if (
            durable.scheme == exported.scheme == "https"
            and {durable.netloc, exported.netloc} == jd_hosts
            and not durable.query
            and not exported.query
            and not durable.fragment
            and not exported.fragment
            and durable_jd_product is not None
            and exported_jd_product is not None
            and durable_jd_product.group(1) == exported_jd_product.group(1)
        ):
            return True
        if not durable.path.endswith("/detail/inner"):
            return False
        canonical_path = durable.path[: -len("/inner")]
        return exported == durable._replace(path=canonical_path)

    @classmethod
    def _validate_result_workbook(cls, job: PreparedLegacyJob) -> tuple[
        Mapping[str, Mapping[str, Any]],
        Mapping[tuple[str, str], Mapping[str, Any]],
        Mapping[str, int],
    ]:
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
            code_indices = cls._result_column_indices(headers, RESULT_CODE_HEADERS)
            if url_index < 0 and not code_indices:
                raise ValueError("result workbook header is not recognized")
            workbook_rows = [
                (excel_row, row)
                for excel_row, row in enumerate(iterator, start=2)
                if any(_text(value) for value in row)
            ]
        finally:
            workbook.close()
        execution_index = cls._result_column_index(headers, RESULT_EXECUTION_HEADERS)
        batch_index = cls._result_column_index(headers, RESULT_BATCH_HEADERS)
        mall_index = cls._result_column_index(headers, RESULT_MALL_HEADERS)
        provenance_present = min(execution_index, batch_index, mall_index) >= 0
        if not provenance_present:
            raise ValueError("result workbook provenance columns are required")
        foreign_filtered = 0
        current_rows: list[tuple[int, tuple[Any, ...]]] = []
        current_executions: set[str] = set()
        for excel_row, row in workbook_rows:
            execution_number = _text(
                row[execution_index] if execution_index < len(row) else ""
            )
            if not execution_number:
                raise ValueError("result workbook row has no execution provenance")
            batch_number = _text(row[batch_index] if batch_index < len(row) else "")
            if not batch_number:
                raise ValueError("result workbook row has no batch provenance")
            if batch_number != job.task_name:
                foreign_filtered += 1
                continue
            mall_key = _text(row[mall_index] if mall_index < len(row) else "")
            if mall_key != job.mall_key:
                raise ValueError("result workbook current batch mall does not match durable job")
            if execution_number in current_executions:
                raise ValueError("result workbook current batch execution is duplicated")
            current_executions.add(execution_number)
            current_rows.append((excel_row, row))
        workbook_rows = current_rows
        included_excel_rows = {excel_row for excel_row, _row in workbook_rows}
        result_rows = [row for _excel_row, row in workbook_rows]
        if len(result_rows) != len(job.requests):
            raise ValueError("result workbook row count does not match request count")

        expected = Counter((request.sku_id, request.url.strip()) for request in job.requests)
        by_sku: dict[str, set[str]] = {}
        by_url: dict[str, set[str]] = {}
        for sku_id, exact_url in expected:
            by_sku.setdefault(sku_id, set()).add(exact_url)
            by_url.setdefault(exact_url, set()).add(sku_id)

        def durable_identity(sku_id: str, exact_url: str) -> tuple[str, str]:
            identity = (sku_id, exact_url)
            if identity in expected:
                return identity
            conflicting_skus = {
                durable_sku
                for durable_sku, durable_urls in by_sku.items()
                for durable_url in durable_urls
                if durable_sku != sku_id
                and cls._matches_legacy_export_url(durable_url, exact_url)
            }
            if conflicting_skus:
                raise ValueError(
                    "result workbook skuId conflicts with an exact durable URL identity"
                )
            urls = by_sku.get(sku_id, set())
            if sku_id and urls == {""}:
                return (sku_id, "")
            if sku_id and len(urls) == 1:
                durable_url = next(iter(urls))
                if cls._matches_legacy_export_url(durable_url, exact_url):
                    return (sku_id, durable_url)
            raise ValueError(
                "result workbook contains an unexpected skuId / exact URL identity"
            )

        def identities_for(code_index: int) -> list[tuple[str, str]]:
            observed: list[tuple[str, str]] = []
            for row in result_rows:
                sku_id = _text(row[code_index] if 0 <= code_index < len(row) else "")
                exact_url = _text(row[url_index] if 0 <= url_index < len(row) else "")
                if sku_id and exact_url:
                    # Canonicalized URLs remain safe only when the SKU is
                    # unique and the URL does not identify another request.
                    identity = durable_identity(sku_id, exact_url)
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
            observed_counter = Counter(observed)
            if observed_counter != expected:
                raise ValueError(
                    "result workbook crawl identities do not match durable request identities"
                )
            return observed

        matching_candidates: list[
            tuple[int, list[tuple[str, str]], tuple[str, ...]]
        ] = []
        for candidate_index in code_indices or [-1]:
            try:
                matching_candidates.append(
                    (
                        candidate_index,
                        identities_for(candidate_index),
                        tuple(
                            _text(
                                row[candidate_index]
                                if 0 <= candidate_index < len(row)
                                else ""
                            )
                            for row in result_rows
                        ),
                    )
                )
            except ValueError:
                continue
        if not matching_candidates:
            raise ValueError("no SKU candidate column matches durable request identities")
        equivalence_classes: dict[
            tuple[str, ...], list[tuple[int, list[tuple[str, str]]]]
        ] = {}
        for candidate_index, identities, value_vector in matching_candidates:
            equivalence_classes.setdefault(value_vector, []).append(
                (candidate_index, identities)
            )
        if len(equivalence_classes) != 1:
            raise ValueError("multiple SKU candidate columns match durable request identities")
        code_index, observed = next(iter(equivalence_classes.values()))[0]
        observed_counter = Counter(observed)

        observed_result_by_identity = read_zhaoshang_result_xlsx_by_identity(
            job.result_path,
            template_path=job.template_path,
            code_index=code_index,
            include_excel_rows=included_excel_rows,
        )
        result_by_identity: dict[tuple[str, str], Mapping[str, Any]] = {}
        for observed_identity, value in observed_result_by_identity.items():
            sku_id, exact_url = observed_identity
            mapped_identity = durable_identity(sku_id, exact_url)
            if mapped_identity in result_by_identity:
                raise ValueError("result workbook result identity is ambiguous")
            result_by_identity[mapped_identity] = value
        if Counter(result_by_identity.keys()) != observed_counter:
            raise ValueError("result workbook has no complete parseable crawl identity mapping")
        result_map = read_zhaoshang_result_xlsx(
            job.result_path,
            template_path=job.template_path,
            code_index=code_index,
            include_excel_rows=included_excel_rows,
        )
        for (_sku_id, durable_url), value in result_by_identity.items():
            if durable_url:
                result_map.setdefault(durable_url, value)
        expected_urls = {url for _sku_id, url in expected if url}
        if expected_urls and (
            not result_map or not expected_urls.issubset(result_map)
        ):
            raise ValueError("result workbook has no complete parseable URL result mapping")
        return result_map, result_by_identity, {
            "expected": len(job.requests),
            "matched": len(result_by_identity),
            "missing": 0,
            "duplicate": 0,
            "unexpected": 0,
            "foreign_filtered": foreign_filtered,
        }

    def harvest_existing(self, job: PreparedLegacyJob) -> LegacyHarvestOutcome:
        """Validate one existing export without making a remote call."""
        if not job.result_path.is_file():
            return LegacyHarvestOutcome(
                HarvestStatus.FAILED,
                diagnostic="existing result workbook is unavailable",
            )
        if self.exact_export_remote_identity(job) is None:
            try:
                result_map, result_by_identity, validation_summary = (
                    self._validate_result_workbook(job)
                )
            except Exception:
                return LegacyHarvestOutcome(
                    HarvestStatus.FAILED,
                    diagnostic="existing result workbook is invalid",
                )
            return LegacyHarvestOutcome(
                HarvestStatus.EXPORTED,
                result_map=result_map,
                result_by_identity=result_by_identity,
                reused_existing=True,
                validation_summary=validation_summary,
            )
        try:
            classification = self.classify_existing_export_partial(job)
        except Exception:
            return LegacyHarvestOutcome(
                HarvestStatus.FAILED,
                diagnostic="existing result workbook is invalid",
            )
        result_by_identity = dict(classification.exact_matches)
        result_map: dict[str, Mapping[str, Any]] = {}
        for (_sku_id, durable_url), result in result_by_identity.items():
            exported_url = _text(result.get("exported_url"))
            if exported_url:
                result_map.setdefault(exported_url, result)
            if durable_url:
                result_map.setdefault(durable_url, result)
        return LegacyHarvestOutcome(
            HarvestStatus.EXPORTED,
            result_map=result_map,
            result_by_identity=result_by_identity,
            reused_existing=True,
            validation_summary=classification.validation_summary,
            no_result_identities=classification.exact_absent,
            manual_identities=classification.ambiguous,
        )

    def validate_existing_export_exact_requests(
        self,
        job: PreparedLegacyJob,
        requests: Sequence[CrawlRequest],
    ) -> Mapping[tuple[str, str], Mapping[str, Any]]:
        """Validate that an existing export contains exactly the supplied requests."""
        selected = tuple(requests)
        if not selected:
            raise ValueError("exact export validation requires at least one request")
        full = Counter(request.identity for request in job.requests)
        subset = Counter(request.identity for request in selected)
        if subset - full:
            raise ValueError("exact export validation requests are outside the durable job")
        scoped_job = replace(job, requests=selected)
        _result_map, result_by_identity, _validation_summary = self._validate_result_workbook(scoped_job)
        return result_by_identity

    def classify_existing_export_partial(
        self,
        job: PreparedLegacyJob,
    ) -> PartialExactHarvestOutcome:
        """Map task-owned result payloads onto immutable remote request groups."""
        if not job.result_path.is_file():
            raise ValueError("result workbook does not exist")
        events = self._read_batch_events(job)
        try:
            _task_id, task_number = self._submission_evidence(job, events)
        except SubmissionEvidenceConflictError as exc:
            raise ValueError(str(exc)) from exc
        if not task_number:
            raise ValueError("result workbook requires exact remote task provenance")

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
            workbook_rows = [
                (excel_row, row)
                for excel_row, row in enumerate(iterator, start=2)
                if any(_text(value) for value in row)
            ]
        finally:
            workbook.close()

        url_index = self._result_column_index(headers, RESULT_URL_HEADERS)
        code_indices = self._result_column_indices(headers, RESULT_CODE_HEADERS)
        execution_index = self._result_column_index(headers, RESULT_EXECUTION_HEADERS)
        batch_index = self._result_column_index(headers, RESULT_BATCH_HEADERS)
        mall_index = self._result_column_index(headers, RESULT_MALL_HEADERS)
        price_index = self._result_column_index(
            headers,
            frozenset({"\u5546\u54c1\u4ef7\u683c", "\u4ef7\u683c", "\u91c7\u96c6\u4ef7\u683c", "page_price"}),
        )
        page_sku_index = self._result_column_index(headers, RESULT_PAGE_SKU_HEADERS)
        title_index = self._result_column_index(headers, RESULT_TITLE_HEADERS)
        comment_index = self._result_column_index(headers, RESULT_COMMENT_HEADERS)
        status_index = self._result_column_index(
            headers, frozenset({"status", "crawl_status"})
        )
        reason_index = self._result_column_index(
            headers, frozenset({"reason", "failure_reason"})
        )
        if min(execution_index, batch_index, mall_index) < 0:
            raise ValueError("result workbook provenance columns are required")
        if url_index < 0 and not code_indices:
            raise ValueError("result workbook header is not recognized")

        foreign_filtered = 0
        current_rows: list[tuple[int, tuple[Any, ...], str]] = []
        current_executions: dict[str, Any] = {}
        for excel_row, row in workbook_rows:
            execution_number = _text(
                row[execution_index] if execution_index < len(row) else ""
            )
            batch_number = _text(row[batch_index] if batch_index < len(row) else "")
            if not execution_number:
                raise ValueError("result workbook row has no execution provenance")
            if not batch_number:
                raise ValueError("result workbook row has no batch provenance")
            if batch_number != job.task_name:
                foreign_filtered += 1
                continue
            if not execution_number.startswith(f"{task_number}-"):
                raise ValueError(
                    "result workbook execution does not belong to the exact remote task"
                )
            mall_key = _text(row[mall_index] if mall_index < len(row) else "")
            if mall_key != job.mall_key:
                raise ValueError("result workbook current batch mall does not match durable job")
            price = parse_decimal(row[price_index]) if 0 <= price_index < len(row) else None
            if execution_number in current_executions:
                if current_executions[execution_number] != price:
                    raise ValueError("result workbook execution has conflicting prices")
                raise ValueError("result workbook current batch execution is duplicated")
            current_executions[execution_number] = price
            current_rows.append((excel_row, row, execution_number))

        expected_counter = Counter(
            (request.sku_id, request.url.strip()) for request in job.requests
        )
        if not expected_counter or any(count != 1 for count in expected_counter.values()):
            raise ValueError("partial export requires unique durable request identities")
        expected = set(expected_counter)
        primary_code_index = code_indices[0] if code_indices else -1
        exact_matches: dict[tuple[str, str], Mapping[str, Any]] = {}
        ambiguous: dict[tuple[str, str], str] = {}
        unassigned_rows = 0

        for excel_row, row, execution_number in current_rows:
            exported_codes = {
                _text(row[index] if index < len(row) else "")
                for index in code_indices
                if _text(row[index] if index < len(row) else "")
            }
            exported_url = _text(row[url_index] if 0 <= url_index < len(row) else "")
            code_matches = {
                identity for identity in expected if identity[0] in exported_codes
            }
            url_matches = {
                identity
                for identity in expected
                if self._matches_legacy_export_url(identity[1], exported_url)
            }
            if code_matches:
                if url_matches and code_matches.isdisjoint(url_matches):
                    for identity in code_matches | url_matches:
                        ambiguous[identity] = (
                            "exported business fields map to conflicting durable request groups"
                        )
                    continue
                targets = code_matches
            elif len(url_matches) == 1:
                targets = url_matches
            elif len(url_matches) > 1:
                for identity in url_matches:
                    ambiguous[identity] = "exported URL maps to multiple durable request groups"
                continue
            elif len(expected) == 1:
                targets = expected
            else:
                unassigned_rows += 1
                continue

            price = parse_decimal(row[price_index]) if 0 <= price_index < len(row) else None
            status = _text(row[status_index] if 0 <= status_index < len(row) else "")
            reason = _text(row[reason_index] if 0 <= reason_index < len(row) else "")
            if price is not None and price > 0:
                page_status, source = "OK", "\u62db\u5546\u5c40"
            elif price is not None and price == 0:
                page_status, source = "PRICE_IS_ZERO", "\u62db\u5546\u5c40"
            elif "\u6210\u529f" in status:
                page_status, source = (
                    "PRICE_MISSING",
                    "\u62db\u5546\u5c40-\u4ef7\u683c\u4e0d\u53ef\u89c1",
                )
            else:
                page_status = "FAILED"
                source = "\u62db\u5546\u5c40-" + (reason or status or "\u672a\u77e5")
            result = {
                "product_code": _text(
                    row[primary_code_index]
                    if 0 <= primary_code_index < len(row)
                    else ""
                ),
                "exported_sku_candidates": sorted(exported_codes),
                "exported_url": exported_url,
                "page_sku_id": _text(
                    row[page_sku_index] if 0 <= page_sku_index < len(row) else ""
                ),
                "page_title": _text(
                    row[title_index] if 0 <= title_index < len(row) else ""
                ),
                "comment_count": _parse_export_comment_count(
                    row[comment_index] if 0 <= comment_index < len(row) else None
                ),
                "comment_count_raw": _text(
                    row[comment_index] if 0 <= comment_index < len(row) else ""
                ),
                "channel": job.mall_key,
                "page_price_value": price,
                "page_price_status": page_status,
                "page_price_source": source,
                "remote_execution": execution_number,
                "remote_batch": job.task_name,
                "remote_mall": job.mall_key,
                "export_excel_row": excel_row,
            }
            for identity in targets:
                if identity in ambiguous:
                    continue
                if identity in exact_matches:
                    exact_matches.pop(identity, None)
                    ambiguous[identity] = (
                        "multiple remote executions compete for one durable request"
                    )
                    continue
                exact_matches[identity] = result

        unresolved = expected - set(exact_matches) - set(ambiguous)
        if unassigned_rows:
            if not unresolved:
                raise ValueError(
                    "provenance-valid export has rows outside durable request groups"
                )
            for identity in unresolved:
                ambiguous[identity] = (
                    "provenance-valid result row lacks durable request-group mapping"
                )
        exact_absent = expected - set(exact_matches) - set(ambiguous)
        return PartialExactHarvestOutcome(
            exact_matches=exact_matches,
            exact_absent=frozenset(exact_absent),
            ambiguous=ambiguous,
            validation_summary={
                "expected": len(expected),
                "matched": len(exact_matches),
                "missing": len(exact_absent),
                "ambiguous": len(ambiguous),
                "candidate_classes": len(code_indices),
                "foreign_filtered": foreign_filtered,
            },
        )

    def harvest(self, job: PreparedLegacyJob, *, timeout_seconds: float) -> LegacyHarvestOutcome:
        if timeout_seconds <= 0:
            # Budget exhaustion is transient: the same exact remote identity
            # stays harvestable and the next sweep retries the bounded wait.
            return LegacyHarvestOutcome(
                HarvestStatus.RUNNING,
                diagnostic="legacy wait-export-only had no budget this pass",
            )
        if job.result_path.exists():
            return self.harvest_existing(job)

        def validated_export_after_runner_failure() -> LegacyHarvestOutcome | None:
            if not job.result_path.is_file():
                return None
            outcome = self.harvest_existing(job)
            return outcome if outcome.status is HarvestStatus.EXPORTED else None

        try:
            process = self._run(self._harvest_args(job, timeout_seconds), timeout_seconds)
        except (subprocess.TimeoutExpired, TimeoutError):
            validated = validated_export_after_runner_failure()
            if validated is not None:
                return validated
            return LegacyHarvestOutcome(HarvestStatus.RUNNING, diagnostic="legacy wait-export-only is still running")
        except (OSError, LegacyPreSendFailure) as exc:
            if _wait_export_startup_failure_is_transient(exc):
                # A provably transient startup failure against an
                # already-confirmed remote task: the retry only ever
                # re-addresses the same identity.
                return LegacyHarvestOutcome(
                    HarvestStatus.RUNNING,
                    diagnostic="legacy wait-export-only could not be started; retry scheduled on the same remote identity",
                )
            # Deterministic or unrecognized local spawn failure: fail closed
            # so the job reaches manual review instead of burning the
            # harvest budget on an identical re-run.
            return LegacyHarvestOutcome(HarvestStatus.MANUAL, diagnostic="legacy wait-export-only could not be started")
        if int(getattr(process, "returncode", -1)) != 0:
            validated = validated_export_after_runner_failure()
            if validated is not None:
                return validated
            if job.result_path.is_file():
                # The child failed AND left a workbook that does not validate:
                # deterministic evidence, a rerun cannot fix it.
                return LegacyHarvestOutcome(HarvestStatus.MANUAL, diagnostic="legacy wait-export-only did not produce an export")
            return LegacyHarvestOutcome(
                HarvestStatus.RUNNING,
                diagnostic="legacy wait-export-only failed transiently; retry scheduled on the same remote identity",
            )
        if not job.result_path.exists():
            return LegacyHarvestOutcome(HarvestStatus.RUNNING, diagnostic="legacy export is not available yet")
        return self.harvest_existing(job)
