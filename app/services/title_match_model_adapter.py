import json
import hashlib
import math
import os
import time
from urllib import error, request

from pydantic import Field

from app.schemas.workflow import StrictModel


class TitleMatchModelRequest(StrictModel):
    query_str: str
    sku_name: str
    model_audit_result: str = ""
    is_match: str = ""


class TitleMatchModelResult(StrictModel):
    match_result: str
    confidence: float = 0.0
    brand_match: bool | None = None
    model_match: str = ""
    spec_match: bool | None = None
    risk_points: list[str] = Field(default_factory=list)
    reason: str = ""
    raw_response: str = ""


ALLOWED_RESULTS = frozenset({"SAME", "NOT_SAME", "MANUAL_REVIEW", "MODEL_UNAVAILABLE"})
ALLOWED_MODEL_MATCH = frozenset({"exact", "partial", "mismatch", "unknown"})
RISK_TERMS = ("替代款", "通用", "适用", "升级款")
DEFAULT_MODEL_MAX_ATTEMPTS = 3
DEFAULT_MODEL_MAX_ELAPSED_SECONDS = 300.0


class ModelCallRetryExhaustedError(RuntimeError):
    """A retryable model operation exhausted its bounded retry policy."""

    def __init__(
        self,
        operation: str,
        attempts: int,
        elapsed_seconds: float,
        last_cause: BaseException | None,
    ) -> None:
        self.operation = operation
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.last_cause = last_cause
        cause_name = _safe_failure_label(last_cause)
        super().__init__(
            f"{operation} exhausted after {attempts} attempts in {elapsed_seconds:.3f}s: "
            f"{cause_name}"
        )


def _safe_failure_label(exc: BaseException | None) -> str:
    if exc is None:
        return "deadline"
    if isinstance(exc, error.HTTPError):
        return f"HTTPError(status={exc.code})"
    return type(exc).__name__


def _unavailable(reason: str) -> TitleMatchModelResult:
    return TitleMatchModelResult(
        match_result="MODEL_UNAVAILABLE",
        confidence=0.0,
        model_match="unknown",
        reason=reason,
    )


def _build_prompt(req: TitleMatchModelRequest) -> str:
    return _PROMPT_TEMPLATE.format(
        query_str=req.query_str,
        sku_name=req.sku_name,
        model_audit_result=req.model_audit_result,
        is_match=req.is_match,
    )


_PROMPT_RULES = (
    "你是中核 SaaS 同款审批预审助手。你只做“标准商品描述 query_str 与候选商品标题 sku_name 是否为同款”的标题预审，不判断价格、库存、评论。必须只输出 JSON，禁止输出 Markdown、解释文本、代码块或多余字段。\n"
    "\n"
    "判定总原则：以 query_str（客户标题/标准商品描述）为基准，但不要机械逐字匹配。query_str 中品牌、型号、核心规格、容量、尺寸、功率、材质、颜色、包装数量、套装数量是强约束；sku_name 必须与这些强约束一致。sku_name 可以比 query_str 多正常商品属性，例如尺寸补充、颜色补充、接口、适用机型、打印页数、速度、标准配置、系列营销词、页面引流词，只要这些信息不改变目标商品本体、不改变包装数量、不与 query_str 冲突，就不能因为“多属性”直接判 MANUAL_REVIEW 或 NOT_SAME。\n"
    "\n"
    "核心判定优先级：品牌 > 型号/系列型号 > 商品本体 > 核心规格/容量/尺寸/功率/颜色/材质 > 包装数量/套装数量 > 销售单位。品牌、型号、核心规格明确一致时，不要因为普通销售单位差异、候选标题补充参数、页面营销词而误判非同款。\n"
    "\n"
    "归一化规则：忽略空格、大小写、全半角、普通标点、括号样式差异；100*70*3、100×70×3、100x70x3 等价；mm=毫米、cm=厘米、m=米、kg=千克、g=克、ml=毫升、L=升；1T=1TB，2T=2TB；50cm=500mm，0.5m=500mm；中文品牌与英文品牌别名可视为一致，例如 SATA/世达、KONICA MINOLTA/柯尼卡美能达、Loctek/乐歌、aigo/爱国者、SAWA/砂威。\n"
    "\n"
    "必须逐项判断：\n"
    "1. 品牌：如果 query_str 有品牌，sku_name 必须出现相同品牌或明确同义品牌。品牌明确不同直接 NOT_SAME。query_str 有品牌但 sku_name 缺品牌时，一般输出 MANUAL_REVIEW；但如果 sku_name 的型号极强且商品本体完全一致，可以输出 MANUAL_REVIEW，不得直接 SAME。query_str 无品牌而 sku_name 有品牌时，不因品牌多出直接否，继续核对其他属性。\n"
    "2. 型号：如果 query_str 有型号、订货号、物料型号、系列型号或明确编号，sku_name 必须完整包含同一型号或等价写法。型号明确不同直接 NOT_SAME。型号只缺少品牌前缀或连接符不同，例如 91403 与 SATA-91403、G50-2TB 与 G50-2T，可视为 exact。sku_name 前半部分出现多个同系列型号用于页面引流，但后半部分明确落到 query_str 的目标型号/目标规格时，不要仅因多型号营销词直接判人工复核；如果无法确认当前 SKU 选中的就是目标型号，则输出 MANUAL_REVIEW。\n"
    "3. 商品本体：商品本体必须一致，但允许更具体或同义表达。例如金属直板尺/钢直尺/钢板尺可视为同类，电脑桌/升降桌/电动电脑桌可视为同类，U盘/固态U盘/高速固态盘在品牌型号容量一致时可视为同类，彩色多功能一体机/彩色复合机/打印复印扫描一体机在品牌型号一致时可视为同类，切割片/砂轮切割片/不锈钢切割片在品牌尺寸规格一致且无明确冲突时可视为同类。主商品与明显配件、耗材、服务、赠品、替代件不是同款；如果标题同时包含主商品和可能的配件，但目标商品型号明确，需要判断该配件是否改变销售内容，不能简单否定。\n"
    "4. 核心规格：sku_name 必须与 query_str 中的核心规格一致，包括容量、尺寸、长度、宽度、厚度、颜色、功率、电压、转速、目数、打印规格、套装数量等。候选标题缺少非核心补充参数，但品牌型号唯一且无冲突时，可以输出 SAME；不要因为 sku_name 未写 query_str 括号中的全部技术参数就直接判非同款。若核心规格明确冲突，输出 NOT_SAME；若目标规格只部分命中且存在多规格可选，输出 MANUAL_REVIEW。\n"
    "5. 包装数量/套装数量：包装数量必须严格一致。例如 query_str 为 1片，sku_name 为 4片试用装、50片/盒、800片/箱，属于包装数量不一致，输出 NOT_SAME 或 MANUAL_REVIEW。query_str 为 四色套装 4支/套，sku_name 也表达四色套装、黑色+彩色组合、黑色1100页+彩色900页*3，可视为数量一致。\n"
    "6. 销售单位/计量单位：销售单位是辅助判断，不是商品本体。对于单件实物商品，个/把/件/台/张/片 这类销售单位差异，不得单独作为 NOT_SAME 理由；当品牌、型号、核心规格、包装数量一致时，可视为 spec_match=true。query_str 中经常同时出现“销售单位”和括号内“计量单位”，如果二者冲突，例如销售单位为台但计量单位为个、销售单位为张但计量单位为个、销售单位为片但计量单位为个，应优先参考更贴近商品本体的销售单位，不要因为 query_str 自身单位冲突直接判非同款。瓶、套、盒、箱、包等涉及包装口径的单位要谨慎，若包装数量不一致则不得 SAME。\n"
    "7. 风险词处理：sku_name 出现“替代、通用、兼容、升级、升级款、替换、改款、定制、客户定制、可选、随机、多规格、多型号”等词时，通常输出 MANUAL_REVIEW，除非标题已明确当前 SKU 就是 query_str 的目标型号和目标规格。sku_name 出现“适用”时不要一律判风险：如果商品是硒鼓、墨盒、耗材、配件，且品牌/系列/套装数量一致，“适用机型”通常是正常商品说明，可以输出 SAME；如果 query_str 是主机设备而 sku_name 只是适用某设备的配件，则输出 MANUAL_REVIEW 或 NOT_SAME。\n"
    "8. 配置/附件/套装：sku_name 中“标配、官方标配、输稿器、盖板、工作台、四纸盒、收纳袋、定制、赠、送、+美工刀、套餐、组合、套装”等信息要判断是否改变销售内容。如果 query_str 本身是套装或标准整机，且 sku_name 的配置是该商品正常标准配置，可以输出 SAME；如果 sku_name 明确额外增加 query_str 没有的赠品、工具、配件或套餐，例如“+美工刀”“4片试用版”“收纳袋+定制”，应输出 MANUAL_REVIEW；若额外内容导致商品本体或包装数量明确不同，输出 NOT_SAME。\n"
    "9. 一物多码/同型号变体：即使品牌和型号一致，如果 sku_name 明确存在颜色、尺寸、功率、电压、容量、配置、包装数量等可选项，且无法确认当前 SKU 选中的就是 query_str 目标规格，输出 MANUAL_REVIEW。如果 sku_name 已明确目标规格，例如白色、500mm、11kW、1TB、2TB、四色套装，并且与 query_str 一致，则不要因为前面有系列营销词或可选范围而直接人工复核。\n"
    "10. 上游字段 model_audit_result、is_match 仅作为输入记录，不得作为同款依据；即使二者都是 1，也必须独立按以上标题规则判断。\n"
    "\n"
    "输出决策规则：\n"
    "- 输出 SAME：品牌一致或明确同义；型号一致或等价；商品本体一致；核心规格、容量、尺寸、颜色、功率、包装数量/套装数量一致；sku_name 多出的信息只是正常补充、营销词、适用机型、标准配置或更具体描述，不与 query_str 冲突。\n"
    "- 输出 NOT_SAME：品牌明确不同；型号明确不同；商品本体明确不同；核心规格/容量/尺寸/功率/颜色/包装数量明确冲突；主商品与配件/耗材/服务明显不一致；包装数量明显不同。\n"
    "- 输出 MANUAL_REVIEW：品牌或型号缺失无法确认；只部分命中目标型号；多规格/多型号/可选但当前 SKU 不明确；存在替代/通用/升级/定制/额外配件/套餐风险但无法仅凭标题确认是否冲突；query_str 或 sku_name 信息不足以稳定判断。\n"
    "\n"
    "字段填写要求：\n"
    "- confidence：SAME 通常 0.85-1.00；MANUAL_REVIEW 通常 0.30-0.85；NOT_SAME 通常 0.70-1.00。\n"
    "- brand_match：只有品牌明确一致或明确同义品牌时为 true，否则为 false。\n"
    "- model_match：完整一致或等价写法为 exact；只命中部分型号、多型号但目标型号存在为 partial；明确冲突为 mismatch；双方无明确型号或无法识别为 unknown。\n"
    "- spec_match：核心规格、容量、尺寸、颜色、功率、包装数量/套装数量一致时为 true；不要因为个/把/件/台/张/片这类单件销售单位差异单独置为 false；核心规格或包装数量冲突时为 false。\n"
    "- risk_points：只写真正影响同款判断的风险点；不要把正常适用机型、正常补充参数、普通销售单位差异写成高风险。没有风险写 []。\n"
    "- reason：用一句中文说明核心判断依据，必须说明品牌、型号、核心规格/包装数量和风险点中影响结论的内容。\n"
)


_PROMPT_TEMPLATE = (
    _PROMPT_RULES
    + "\n输出格式必须严格为：{{\"match_result\":\"SAME|NOT_SAME|MANUAL_REVIEW\",\"confidence\":0.0,\"brand_match\":true,\"model_match\":\"exact|partial|mismatch|unknown\",\"spec_match\":true,\"risk_points\":[],\"reason\":\"\"}}\n"
    "\n"
    "query_str={query_str}\n"
    "sku_name={sku_name}\n"
    "model_audit_result={model_audit_result}\n"
    "is_match={is_match}"
)

_BATCH_OUTPUT_INSTRUCTION = (
    "\n逐条判断待判断数据。输出格式必须严格为："
    "{\"results\":[{\"index\":0,\"match_result\":\"SAME|NOT_SAME|MANUAL_REVIEW\","
    "\"confidence\":0.0,\"brand_match\":true,\"model_match\":\"exact|partial|mismatch|unknown\","
    "\"spec_match\":true,\"risk_points\":[],\"reason\":\"\"}]}\n"
)

# Cache invalidation follows the real prompt and validated result contract,
# rather than a manually maintained label that can drift during deployment.
TITLE_MATCH_PROMPT_VERSION = "sha256:" + hashlib.sha256(
    (_PROMPT_TEMPLATE + _BATCH_OUTPUT_INSTRUCTION).encode("utf-8")
).hexdigest()
TITLE_MATCH_SCHEMA_VERSION = "sha256:" + hashlib.sha256(
    json.dumps(
        TitleMatchModelResult.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def _result_from_binary_value(value: object, raw_response: str = "") -> TitleMatchModelResult:
    is_same = str(value or "").strip()
    if is_same == "是":
        return TitleMatchModelResult(
            match_result="SAME",
            confidence=0.95,
            model_match="unknown",
            reason="模型返回是",
            raw_response=raw_response,
        )
    if is_same == "否":
        return TitleMatchModelResult(
            match_result="NOT_SAME",
            confidence=0.95,
            model_match="unknown",
            reason="模型返回否",
            raw_response=raw_response,
        )
    return TitleMatchModelResult(
        match_result="NOT_SAME",
        confidence=0.0,
        model_match="unknown",
        reason="模型返回非法 is_same，已按否处理",
        raw_response=raw_response,
    )


def _optional_model_bool(value: object) -> bool | None:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str) and value.strip().lower() in {"", "unknown", "null", "none", "n/a"}:
        return None
    return value  # type: ignore[return-value]


def _result_from_model_dict(data: dict, raw_response: str = "") -> TitleMatchModelResult:
    result = TitleMatchModelResult(
        match_result=str(data.get("match_result", "MANUAL_REVIEW")),
        confidence=float(data.get("confidence", 0.0)),
        brand_match=_optional_model_bool(data.get("brand_match")),
        model_match=str(data.get("model_match", "unknown")),
        spec_match=_optional_model_bool(data.get("spec_match")),
        risk_points=list(data.get("risk_points", [])),
        reason=str(data.get("reason", "")),
        raw_response=raw_response,
    )
    if result.match_result not in ALLOWED_RESULTS - {"MODEL_UNAVAILABLE"}:
        result = result.model_copy(update={"match_result": "MANUAL_REVIEW", "reason": "模型返回非法 match_result，已降级人工核查"})
    if result.model_match not in ALLOWED_MODEL_MATCH:
        result = result.model_copy(update={"model_match": "unknown"})
    return result


def _parse_model_response(raw: str, sku_name: str) -> TitleMatchModelResult:
    data = json.loads(raw)
    if "choices" in data:
        content = data["choices"][0]["message"]["content"]
        data = json.loads(content)
    if "is_same" in data:
        return _result_from_binary_value(data.get("is_same"), raw)
    result = _result_from_model_dict(data, raw)
    if any(term in sku_name for term in RISK_TERMS) and result.match_result == "SAME":
        result = result.model_copy(update={
            "match_result": "MANUAL_REVIEW",
            "risk_points": [*result.risk_points, "标题含替代款/通用/适用/升级款风险词"],
            "reason": result.reason or "标题含风险词，需人工确认是否为原型号同款",
        })
    return result


def _model_timeout_seconds(default: float = 30.0) -> float:
    raw = os.environ.get("TITLE_MATCH_MODEL_TIMEOUT", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _model_retry_limit() -> int:
    raw = os.environ.get("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "").strip()
    if not raw:
        # Compatibility alias: historically this setting already represented attempts.
        raw = os.environ.get("TITLE_MATCH_MODEL_MAX_RETRIES", "").strip()
    if not raw:
        return DEFAULT_MODEL_MAX_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MODEL_MAX_ATTEMPTS
    if value <= 0:
        return DEFAULT_MODEL_MAX_ATTEMPTS
    return min(value, DEFAULT_MODEL_MAX_ATTEMPTS)


def _model_retry_elapsed_limit_seconds() -> float:
    raw = os.environ.get("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MODEL_MAX_ELAPSED_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MODEL_MAX_ELAPSED_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_MODEL_MAX_ELAPSED_SECONDS
    return min(value, DEFAULT_MODEL_MAX_ELAPSED_SECONDS)


def _model_retry_sleep_seconds(default: float = 5.0) -> float:
    raw = os.environ.get("TITLE_MATCH_MODEL_RETRY_SLEEP", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _retryable_model_failure(exc: BaseException) -> bool:
    if isinstance(exc, error.HTTPError):
        return exc.code in {408, 429} or 500 <= exc.code <= 599
    return isinstance(
        exc,
        (error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError),
    )


def _retry_model_call(
    operation_name: str,
    call,
    *,
    max_attempts: int | None = None,
    max_elapsed_seconds: float | None = None,
):
    retry_limit = (
        _model_retry_limit()
        if max_attempts is None
        else min(DEFAULT_MODEL_MAX_ATTEMPTS, max(1, int(max_attempts)))
    )
    elapsed_limit = (
        _model_retry_elapsed_limit_seconds()
        if max_elapsed_seconds is None
        else min(DEFAULT_MODEL_MAX_ELAPSED_SECONDS, max(0.001, float(max_elapsed_seconds)))
    )
    if not math.isfinite(elapsed_limit):
        elapsed_limit = DEFAULT_MODEL_MAX_ELAPSED_SECONDS
    retry_sleep = _model_retry_sleep_seconds()
    started_at = time.monotonic()
    deadline = started_at + elapsed_limit
    attempt = 0
    last_cause: BaseException | None = None
    while attempt < retry_limit:
        now = time.monotonic()
        remaining_seconds = deadline - now
        if remaining_seconds <= 0:
            raise ModelCallRetryExhaustedError(
                operation_name,
                attempt,
                max(now - started_at, 0.0),
                last_cause,
            )
        attempt += 1
        try:
            return call(remaining_seconds)
        except error.HTTPError as exc:
            if not _retryable_model_failure(exc):
                raise
            last_cause = exc
        except (error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            last_cause = exc

        now = time.monotonic()
        remaining_seconds = deadline - now
        if attempt >= retry_limit or remaining_seconds <= 0:
            raise ModelCallRetryExhaustedError(
                operation_name,
                attempt,
                max(now - started_at, 0.0),
                last_cause,
            ) from None
        sleep_seconds = min(retry_sleep, remaining_seconds)
        if sleep_seconds > 0:
            print(
                f"[model] {operation_name} failed attempt={attempt}, retry after {sleep_seconds}s: "
                f"{_safe_failure_label(last_cause)}",
                flush=True,
            )
            time.sleep(sleep_seconds)

    raise ModelCallRetryExhaustedError(
        operation_name,
        attempt,
        max(time.monotonic() - started_at, 0.0),
        last_cause,
    )


def _clamp_model_timeout(timeout_seconds: float | None, remaining_seconds: float) -> float:
    configured_timeout = _model_timeout_seconds() if timeout_seconds is None else float(timeout_seconds)
    if configured_timeout <= 0:
        configured_timeout = _model_timeout_seconds()
    return min(configured_timeout, remaining_seconds)


def _build_batch_prompt(requests: list[TitleMatchModelRequest]) -> str:
    items = [
        {
            "index": index,
            "query_str": req.query_str,
            "sku_name": req.sku_name,
            "model_audit_result": req.model_audit_result,
            "is_match": req.is_match,
        }
        for index, req in enumerate(requests)
    ]
    return (
        _PROMPT_RULES
        + _BATCH_OUTPUT_INSTRUCTION
        + "待判断数据：\n"
        f"{json.dumps(items, ensure_ascii=False)}"
    )


def _parse_batch_model_response(raw: str, expected_count: int) -> dict[int, TitleMatchModelResult]:
    data = json.loads(raw)
    if "choices" in data:
        content = data["choices"][0]["message"]["content"]
        data = json.loads(content)
    rows = data.get("results")
    if not isinstance(rows, list):
        raise ValueError("批量模型响应缺少 results 数组")

    parsed: dict[int, TitleMatchModelResult] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= index < expected_count:
            if "is_same" in row:
                parsed[index] = _result_from_binary_value(row.get("is_same"), raw)
            else:
                parsed[index] = _result_from_model_dict(row, raw)
    return parsed


def _model_env() -> tuple[str, str, str] | None:
    api_key = os.environ.get("TITLE_MATCH_MODEL_API_KEY", "").strip()
    base_url = os.environ.get("TITLE_MATCH_MODEL_BASE_URL", "").strip()
    model_name = os.environ.get("TITLE_MATCH_MODEL_NAME", "").strip()
    if not api_key or not base_url:
        return None
    return api_key, base_url, model_name


def _post_model_payload(payload: dict, timeout_seconds: float | None = None) -> str:
    model_env = _model_env()
    if model_env is None:
        raise RuntimeError("TITLE_MATCH_MODEL_API_KEY 或 TITLE_MATCH_MODEL_BASE_URL 未配置")
    api_key, base_url, _model_name = model_env
    http_req = request.Request(
        base_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = _model_timeout_seconds() if timeout_seconds is None else timeout_seconds
    with request.build_opener(request.ProxyHandler({})).open(http_req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def call_title_match_model(
    req: TitleMatchModelRequest,
    timeout_seconds: float | None = None,
    *,
    max_attempts: int | None = None,
    max_elapsed_seconds: float | None = None,
) -> TitleMatchModelResult:
    model_env = _model_env()
    if model_env is None:
        return _unavailable("TITLE_MATCH_MODEL_API_KEY 或 TITLE_MATCH_MODEL_BASE_URL 未配置")
    _api_key, _base_url, model_name = model_env

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": _build_prompt(req)}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    def _call_and_parse(effective_timeout_seconds: float) -> TitleMatchModelResult:
        raw = _post_model_payload(payload, timeout_seconds=effective_timeout_seconds)
        return _parse_model_response(raw, req.sku_name)

    return _retry_model_call(
        "single title match model call",
        lambda remaining_seconds: _call_and_parse(
            _clamp_model_timeout(timeout_seconds, remaining_seconds)
        ),
        max_attempts=max_attempts,
        max_elapsed_seconds=max_elapsed_seconds,
    )


def call_title_match_model_batch(
    requests: list[TitleMatchModelRequest],
    timeout_seconds: float | None = None,
    *,
    max_attempts: int | None = None,
    max_elapsed_seconds: float | None = None,
) -> dict[int, TitleMatchModelResult]:
    model_env = _model_env()
    if model_env is None:
        return {
            index: _unavailable("TITLE_MATCH_MODEL_API_KEY 或 TITLE_MATCH_MODEL_BASE_URL 未配置")
            for index in range(len(requests))
        }
    _api_key, _base_url, model_name = model_env
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": _build_batch_prompt(requests)}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    def _call_and_parse_batch(effective_timeout_seconds: float) -> dict[int, TitleMatchModelResult]:
        raw = _post_model_payload(payload, timeout_seconds=effective_timeout_seconds)
        parsed = _parse_batch_model_response(raw, len(requests))
        missing_indexes = [index for index in range(len(requests)) if index not in parsed]
        if missing_indexes:
            raise ValueError(f"批量模型响应缺少结果 indexes={missing_indexes}")
        return parsed

    return _retry_model_call(
        "batch title match model call",
        lambda remaining_seconds: _call_and_parse_batch(
            _clamp_model_timeout(timeout_seconds, remaining_seconds)
        ),
        max_attempts=max_attempts,
        max_elapsed_seconds=max_elapsed_seconds,
    )
