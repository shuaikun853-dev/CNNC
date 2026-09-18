"""Durable timeout policy and orchestration for incremental local fallback."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, time as datetime_time
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook

from app.services.daily_audit_store import DailyAuditStore, LeaseToken
from app.services.page_price_semantics import valid_page_price_result

DAYTIME_60M = "DAYTIME_60M"
PEAK_3M = "PEAK_3M"
AFTER_1800 = "AFTER_1800"
SHANGHAI = ZoneInfo("Asia/Shanghai")
SUPPORTED_MALLS = frozenset({
    "nbdeli", "guangbo", "ofs", "lxwlmall", "qstbg", "ycw", "comix", "tima",
})
MAX_ATTEMPTS = 2
MIN_EXECUTION_WINDOW_SECONDS = 60
MAX_CHILD_TIMEOUT_SECONDS = 600
EXPECTED_CRAWLER_REVISION = "28870666aeb99a10494ebb91419aadf3bff4c0a8"
DEFAULT_CRAWLER_ROOT = Path(r"E:\.A中核运维\cnnc-mall-crawler")
SITE_ALIASES = {"ofs": "officemate"}
SITE_HOSTS = {
    "nbdeli": {"b2b.nbdeli.com"},
    "guangbo": {"btob.guangbo.net"},
    "officemate": {"officemate.cn", "www.officemate.cn"},
    "lxwlmall": {"www.lxwlmall.com"},
    "qstbg": {"qstbg.com", "www.qstbg.com"},
    "ycw": {"ycw.cn", "www.ycw.cn"},
    "comix": {"qx.com", "www.qx.com", "comix.com.cn", "www.comix.com.cn"},
    "tima": {"tima365.com", "www.tima365.com"},
}


def _moment(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("fallback timestamps must include an explicit offset")
    return parsed.astimezone(UTC)


def eligibility_reason(
    now: datetime,
    remote_started_at: datetime | str,
    last_remote_check_at: datetime | str | None,
) -> str | None:
    """Return the audited reason for the current window, or ``None``."""
    current = _moment(now)
    started = _moment(remote_started_at)
    previous = _moment(last_remote_check_at)
    assert current is not None and started is not None
    local = current.astimezone(SHANGHAI)
    local_time = local.timetz().replace(tzinfo=None)
    if local_time >= datetime_time(18, 0):
        return AFTER_1800
    if local_time >= datetime_time(17, 0):
        peak_start = local.replace(hour=17, minute=0, second=0, microsecond=0).astimezone(UTC)
        if previous is not None and previous >= peak_start and (current - previous).total_seconds() >= 180:
            return PEAK_3M
        return None
    if (current - started).total_seconds() >= 3600:
        return DAYTIME_60M
    return None


def _metrics(store: DailyAuditStore, token: LeaseToken) -> dict[str, Any]:
    rows = store.list_local_fallback_work(token)
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["mall_key"]), str(row["exact_url"]))
        item = unique.setdefault(key, dict(row))
        reason = item.get("eligible_reason") or row.get("eligible_reason")
        if str(row.get("crawl_state")) == "SUCCESS":
            item.update(row)
        if reason:
            item["eligible_reason"] = reason
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    sites: dict[str, dict[str, int]] = {}
    links: list[dict[str, Any]] = []
    for row in unique.values():
        source = ""
        if str(row.get("crawl_state")) == "SUCCESS":
            try:
                result = json.loads(str(row.get("crawl_result_json") or "{}"))
            except (TypeError, ValueError):
                result = {}
            if isinstance(result, Mapping):
                source = str(result.get("page_price_source") or "")
        if str(row.get("crawl_state")) == "SUCCESS":
            state = "SUCCESS" if source.startswith("本地兜底:") else "REMOTE_SUCCESS"
        else:
            state = str(row["state"])
        states[state] += 1
        if row.get("eligible_reason"):
            reasons[str(row["eligible_reason"])] += 1
        site = sites.setdefault(
            str(row["mall_key"]),
            {"pending": 0, "running": 0, "success": 0, "remote_success": 0, "failed": 0},
        )
        effective_state = state
        if effective_state == "CLAIMED":
            site["running"] += 1
        elif effective_state == "SUCCESS":
            site["success"] += 1
        elif effective_state == "REMOTE_SUCCESS":
            site["remote_success"] += 1
        elif effective_state in {"RETRYABLE_FAILED", "TERMINAL_FAILED"}:
            site["failed"] += 1
        else:
            site["pending"] += 1
        links.append({
            "mall_key": str(row["mall_key"]),
            "exact_url": str(row["exact_url"]),
            "state": effective_state,
            "source": source or "待定",
            "remote_job_id": row.get("remote_job_id"),
        })
    return {
        "total": len(unique),
        "pending": states["PENDING"],
        "running": states["CLAIMED"],
        "success": states["SUCCESS"],
        "remote_success": states["REMOTE_SUCCESS"],
        "failed": states["RETRYABLE_FAILED"] + states["TERMINAL_FAILED"],
        "reasons": dict(reasons),
        "sites": sites,
        "links": links,
    }


def _normalized_site(mall_key: str) -> str:
    return SITE_ALIASES.get(mall_key, mall_key)


def _url_matches_site(url: str, site: str) -> bool:
    return (urlparse(url).hostname or "").lower() in SITE_HOSTS.get(site, set())


def read_crawler_output(
    path: Path,
    site: str,
    expected_urls: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Strictly reconcile one child workbook with its locked exact URLs."""
    expected = list(expected_urls)
    if len(expected) != len(set(expected)):
        raise ValueError("锁定输入 URL 重复")
    if not path.is_file():
        raise ValueError(f"本地 crawler 输出不存在: {path}")
    try:
        workbook = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        raise ValueError(f"本地 crawler 输出不是有效 XLSX: {path}") from exc
    try:
        rows = workbook.active.iter_rows(values_only=True)
        headers = [str(value or "").strip() for value in (next(rows, ()) or ())]
        required = {"商品价格", "商品链接", "采集状态", "失败原因"}
        if not required.issubset(headers):
            raise ValueError("本地 crawler 输出表头不完整")
        index = {name: headers.index(name) for name in required}
        results: dict[str, dict[str, Any]] = {}
        for values in rows:
            url = str(values[index["商品链接"]] or "").strip()
            if not url:
                continue
            if url in results:
                raise ValueError(f"本地 crawler 输出 URL 重复: {url}")
            if not _url_matches_site(url, site):
                raise ValueError(f"本地 crawler 输出 URL 与站点不匹配: {site} {url}")
            status = str(values[index["采集状态"]] or "").strip().upper()
            reason = str(values[index["失败原因"]] or "").strip()
            if status == "SUCCESS":
                try:
                    price = Decimal(str(values[index["商品价格"]]))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError(f"本地 crawler SUCCESS 价格非法: {url}") from exc
                if not price.is_finite() or price < 0:
                    raise ValueError(f"本地 crawler SUCCESS 价格非法: {url}")
                results[url] = {
                    "result": {
                        "page_price_value": price,
                        "page_price_status": "PRICE_IS_ZERO" if price == 0 else "OK",
                        "page_price_source": f"本地兜底:{site}",
                    }
                }
            else:
                risk = status == "SKIPPED_RISK" or any(
                    marker in reason.upper() for marker in ("RISK", "CAPTCHA", "LOGIN", "验证码", "登录")
                )
                retryable = status in {"FAILED", "UNKNOWN"} and any(
                    marker in reason.upper() for marker in ("NETWORK", "TIMEOUT", "TEMPORARY", "网络", "超时")
                )
                results[url] = {
                    "error": ("RISK_STOP" if risk else status or "INVALID_RESPONSE") + (f": {reason}" if reason else ""),
                    "retryable": retryable and not risk,
                }
        if set(results) != set(expected):
            raise ValueError(
                "本地 crawler 输出未与锁定 URL 集合逐项对账: "
                f"missing={len(set(expected) - set(results))} foreign={len(set(results) - set(expected))}"
            )
        return results
    finally:
        workbook.close()


def _child_env(run_dir: Path) -> dict[str, str]:
    allowed = {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMDATA", "HOMEDRIVE", "HOMEPATH",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    temp = run_dir / "_tmp"
    pycache = run_dir / "_pycache"
    temp.mkdir(parents=True, exist_ok=True)
    pycache.mkdir(parents=True, exist_ok=True)
    env.update({
        "TEMP": str(temp.resolve()), "TMP": str(temp.resolve()),
        "PYTHONPYCACHEPREFIX": str(pycache.resolve()), "PYTHONIOENCODING": "utf-8",
    })
    return env


def verify_crawler_revision(
    crawler_root: Path,
    *,
    expected_revision: str = EXPECTED_CRAWLER_REVISION,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    root = Path(crawler_root).resolve()
    if not (root / "mall_crawler" / "cli.py").is_file():
        raise ValueError(f"固定 crawler 入口不存在: {root}")
    env = _child_env(root / "_run" / "revision-check")
    revision = runner(
        ["git", "rev-parse", "HEAD"], cwd=str(root), env=env, check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    actual = str(revision.stdout or "").strip().lower()
    status = runner(
        ["git", "status", "--porcelain"], cwd=str(root), env=env, check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if revision.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", actual):
        raise ValueError("固定 crawler Git revision 无法验证")
    if status.returncode != 0 or str(status.stdout or "").strip():
        raise ValueError("固定 crawler 工作区不干净")
    if actual != expected_revision.lower():
        raise ValueError(f"固定 crawler revision 不匹配: expected={expected_revision} actual={actual}")
    return actual


def execute_pinned_crawler(
    claims: Sequence[Mapping[str, Any]],
    *,
    run_dir: Path,
    tick_id: str,
    crawler_root: Path = DEFAULT_CRAWLER_ROOT,
    timeout_seconds: float = MAX_CHILD_TIMEOUT_SECONDS,
) -> Mapping[int, Mapping[str, Any]]:
    """Run one locked multi-site child with an allowlisted environment."""
    claim_list = [dict(claim) for claim in claims]
    if not claim_list:
        return {}
    revision = verify_crawler_revision(crawler_root)
    urls = [str(claim["exact_url"]) for claim in claim_list]
    if len(urls) != len(set(urls)):
        return {int(claim["id"]): {"error": "MANIFEST_MISMATCH: duplicate exact URL", "retryable": False} for claim in claim_list}
    folder = Path(run_dir) / "local_fallback" / re.sub(r"[^A-Za-z0-9_.-]+", "_", tick_id)
    folder.mkdir(parents=True, exist_ok=True)
    by_site: dict[str, list[dict[str, Any]]] = {}
    for claim in claim_list:
        site = _normalized_site(str(claim["mall_key"]))
        url = str(claim["exact_url"])
        if site not in SITE_HOSTS or not _url_matches_site(url, site):
            return {int(item["id"]): {"error": "INVALID_SCOPE", "retryable": False} for item in claim_list}
        by_site.setdefault(site, []).append(claim)
    manifest = Workbook()
    sheet = manifest.active
    sheet.title = "jobs"
    sheet.append(["site", "input", "output"])
    outputs: dict[str, Path] = {}
    for site, site_claims in sorted(by_site.items()):
        input_path = (folder / f"{site}_input.xlsx").resolve()
        output_path = (folder / f"{site}_output.xlsx").resolve()
        book = Workbook()
        input_sheet = book.active
        input_sheet.append(["URL"])
        for claim in site_claims:
            input_sheet.append([str(claim["exact_url"])])
        book.save(input_path)
        sheet.append([site, str(input_path), str(output_path)])
        outputs[site] = output_path
    manifest_path = (folder / "manifest.xlsx").resolve()
    manifest.save(manifest_path)
    env = _child_env(folder)
    if timeout_seconds <= 0:
        return {int(claim["id"]): {"error": "TEMPORARY_TIMEOUT", "retryable": True} for claim in claim_list}
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "mall_crawler.cli", "--manifest", str(manifest_path)],
            cwd=str(Path(crawler_root).resolve()), env=env, check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=min(MAX_CHILD_TIMEOUT_SECONDS, timeout_seconds),
        )
    except subprocess.TimeoutExpired:
        return {int(claim["id"]): {"error": "TEMPORARY_TIMEOUT", "retryable": True} for claim in claim_list}
    outcomes: dict[int, Mapping[str, Any]] = {}
    for site, site_claims in by_site.items():
        expected = [str(claim["exact_url"]) for claim in site_claims]
        try:
            parsed = read_crawler_output(outputs[site], site, expected)
        except Exception as exc:
            if not outputs[site].is_file() and completed.returncode != 0:
                risk = completed.returncode == 3
                error = "RISK_STOP" if risk else f"PROCESS_FAILED: exit={completed.returncode}"
                retryable = not risk
            else:
                error = f"MANIFEST_MISMATCH: {exc}"
                retryable = False
            for claim in site_claims:
                outcomes[int(claim["id"])] = {"error": error, "retryable": retryable}
            continue
        for claim in site_claims:
            outcome = dict(parsed[str(claim["exact_url"])])
            if "result" in outcome:
                outcome["crawler_revision"] = revision
            outcomes[int(claim["id"])] = outcome
    return outcomes


def run_incremental_local_fallback(
    store: DailyAuditStore,
    token: LeaseToken,
    *,
    now: datetime,
    tick_id: str,
    final_remote_check: Callable[[Sequence[Mapping[str, Any]]], set[int]],
    execute: Callable[[Sequence[Mapping[str, Any]]], Mapping[int, Mapping[str, Any]]],
    checked_remote_job_ids: set[int] | frozenset[int] = frozenset(),
    execution_budget_seconds: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Observe, final-check, claim, execute and settle exact unresolved URLs."""
    current = _moment(now)
    assert current is not None
    store.recover_interrupted_local_fallback_claims(
        token, tick_id, current.isoformat(timespec="seconds"), max_attempts=MAX_ATTEMPTS
    )
    opening_checked = {int(value) for value in checked_remote_job_ids}
    observed = store.observe_local_fallback_remote_checks(
        token,
        current.isoformat(timespec="seconds"),
        SUPPORTED_MALLS,
        checked_remote_job_ids=opening_checked,
    )
    eligible: list[dict[str, Any]] = []
    peak_initial_checks: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    local_time = current.astimezone(SHANGHAI)
    peak_start = local_time.replace(hour=17, minute=0, second=0, microsecond=0).astimezone(UTC)
    in_peak = datetime_time(17, 0) <= local_time.timetz().replace(tzinfo=None) < datetime_time(18, 0)
    for row in observed:
        reason = eligibility_reason(current, row["remote_started_at"], row.get("last_remote_check_at"))
        exact_url = str(row["exact_url"])
        if exact_url in seen_urls:
            continue
        previous = _moment(row.get("last_remote_check_at"))
        remote_job_id = int(row["remote_job_id"])
        if (
            reason is None
            and in_peak
            and (previous is None or previous < peak_start)
            and remote_job_id not in opening_checked
        ):
            seen_urls.add(exact_url)
            peak_initial_checks.append(dict(row))
            continue
        if reason is None:
            continue
        seen_urls.add(exact_url)
        eligible.append({**row, "eligible_reason": reason})
    if peak_initial_checks:
        try:
            checked = {int(value) for value in final_remote_check(peak_initial_checks)}
        except Exception:
            return _metrics(store, token)
        if checked:
            store.observe_local_fallback_remote_checks(
                token,
                current.isoformat(timespec="seconds"),
                SUPPORTED_MALLS,
                checked_remote_job_ids=checked,
            )
    if not eligible:
        return _metrics(store, token)

    # The final check is separate from the opening sweep. A local claim
    # requires proof that this exact remote job completed that final query.
    final_candidates = eligible
    final_checked: set[int] = set()
    if final_candidates:
        try:
            final_checked = {int(value) for value in final_remote_check(final_candidates)}
        except Exception:
            return _metrics(store, token)
        if final_checked:
            store.observe_local_fallback_remote_checks(
                token,
                current.isoformat(timespec="seconds"),
                SUPPORTED_MALLS,
                checked_remote_job_ids=final_checked,
            )
    if execution_budget_seconds is not None and execution_budget_seconds() < MIN_EXECUTION_WINDOW_SECONDS:
        return _metrics(store, token)
    claims: list[dict[str, Any]] = []
    for row in eligible:
        if int(row["remote_job_id"]) not in final_checked:
            continue
        current_work = store.get_crawl_work(int(row["id"]))
        if current_work is None or current_work["state"] == "SUCCESS":
            continue
        claim = store.claim_local_fallback(
            token,
            int(row["id"]),
            str(row["eligible_reason"]),
            tick_id,
            current.isoformat(timespec="seconds"),
            max_attempts=MAX_ATTEMPTS,
        )
        if claim is not None:
            claims.append(claim)
    if not claims:
        return _metrics(store, token)
    try:
        outcomes = execute(tuple(claims))
    except Exception as exc:
        outcomes = {
            int(claim["id"]): {"error": f"PROCESS_FAILED: {type(exc).__name__}: {exc}", "retryable": True}
            for claim in claims
        }
    for claim in claims:
        fallback_id = int(claim["id"])
        outcome = outcomes.get(fallback_id) or outcomes.get(str(fallback_id))  # type: ignore[arg-type]
        if not isinstance(outcome, Mapping):
            store.fail_local_fallback(
                token, fallback_id, "PROCESS_FAILED: missing child outcome", retryable=True,
                max_attempts=MAX_ATTEMPTS,
            )
            continue
        result = outcome.get("result")
        if isinstance(result, Mapping) and valid_page_price_result(result):
            store.complete_local_fallback(
                token,
                fallback_id,
                result,
                crawler_revision=str(outcome.get("crawler_revision") or "injected-test-executor"),
                completed_at=current.isoformat(timespec="seconds"),
            )
            continue
        store.fail_local_fallback(
            token,
            fallback_id,
            str(outcome.get("error") or "INVALID_RESPONSE: no valid exact price"),
            retryable=bool(outcome.get("retryable", False)),
            max_attempts=MAX_ATTEMPTS,
        )
    return _metrics(store, token)
