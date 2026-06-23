import json
import os
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


_PROMPT_TEMPLATE = (
    "你是中核 SaaS 同款审批预审助手。你只做\u201c标准商品描述 query_str 与候选商品标题 sku_name 是否为同款\u201d的标题预审，不判断价格、库存、评论。必须只输出 JSON，禁止输出 Markdown、解释文本、代码块或多余字段。\n"
    "\n"
    "判定总原则：以 query_str（客户标题/标准商品描述）为基准。query_str 中商品名称、品牌、型号、括号内补充描述、斜杠后的规格、件数、销售单位、计量单位都属于客户基准信息。sku_name 必须覆盖 query_str 中所有可识别的关键属性；允许 sku_name 多出非冲突、非变体、非配置差异的描述词，但不允许少于 query_str 的必需属性。sku_name 多出的属性只要会影响具体商品、型号、配置、版本、配件、包装、适用范围或价格口径，就不得输出 SAME，应输出 MANUAL_REVIEW；若与 query_str 明确冲突，应输出 NOT_SAME。\n"
    "\n"
    "归一化规则：忽略空格、大小写、全半角、普通标点、括号样式差异；100*70*3 与 100\u00d770\u00d73 等价；mm=毫米、cm=厘米、m=米、kg=千克、g=克、ml=毫升、L=升；中文品牌与英文品牌别名只有在标题中明确体现为同一品牌时才可视为一致，例如 SATA/世达、KONICA MINOLTA/柯尼卡美能达。不要把 个/把/台/瓶/件 默认等价；销售单位、计量单位、售卖规格必须按客户基准严格核对。\n"
    "\n"
    "必须逐项判断：\n"
    "1. 品牌：如果 query_str 有品牌，sku_name 必须出现相同品牌或明确同义品牌；品牌不同直接 NOT_SAME；sku_name 缺品牌或品牌无法确认时输出 MANUAL_REVIEW，不得输出 SAME。query_str 无品牌而 sku_name 有品牌时，不因品牌多出直接否，继续核对其他属性。\n"
    "2. 型号：如果 query_str 有型号、订货号、物料型号、系列型号或明确编号，sku_name 必须完整包含同一型号。型号明确不同输出 NOT_SAME；sku_name 少了 query_str 的型号输出 MANUAL_REVIEW；sku_name 同时出现多个型号、系列可选、斜杠并列型号，导致目标商品不唯一时输出 MANUAL_REVIEW，不得 SAME。\n"
    "3. 规格参数：sku_name 必须覆盖 query_str 中的规格、尺寸、长度、宽度、厚度、材质、颜色、容量、重量、目数、电压、功率、精度、分辨率、速度、接口、适用介质、等级等关键属性。缺少客户基准关键属性时输出 MANUAL_REVIEW；明确参数不一致输出 NOT_SAME。\n"
    "4. 件数/包装/销售单位/计量单位：1kg/瓶、1个/个、1台/台、销售单位、单位、计量单位、售卖规格等必须一致。单位或件数明确不一致直接 NOT_SAME；候选未写单位但其他完全一致时最多 MANUAL_REVIEW，不得 SAME。若 query_str 自身销售单位与括号计量单位冲突，应加入 risk_points，并输出 MANUAL_REVIEW。\n"
    "5. 商品本体：品类、用途和商品形态必须一致。主商品与配件、耗材、服务、赠品、附件、替换件、组合套装不得直接 SAME。\n"
    "6. 风险词：sku_name 出现以下词或同义表达时，不得输出 SAME，应输出 MANUAL_REVIEW：替代、通用、适用、兼容、升级、升级款、替换、改款、定制、客户定制、可选、随机、套餐、组合、套装、赠、送、含、带、标配+、官方标配+、配、附件、配件、多规格、多型号、系列、店长推荐。若这些内容与 query_str 明确冲突，输出 NOT_SAME。\n"
    "7. 一物多码/同型号变体：即使品牌和型号一致，只要 sku_name 多了可能影响实际商品或价格的配置/变体属性，如黑色十字线、线宽、双面输稿器、盖板、客户定制A、功率档位、电压档位、颜色差异、尺寸可选、长度可选、多型号混售等，应输出 MANUAL_REVIEW。\n"
    "8. 上游字段 model_audit_result、is_match 仅作为输入记录，不得作为同款依据；即使二者都是 1，也必须独立按以上标题规则判断。\n"
    "\n"
    "输出决策规则：\n"
    "- 只有品牌一致、型号一致、query_str 的关键属性全部被 sku_name 覆盖、规格/件数/销售单位/计量单位一致、无风险词、无变体/多型号/替代/通用/适用/升级风险时，才输出 SAME。\n"
    "- 品牌、型号、品类、核心规格、件数、销售单位、计量单位存在明确冲突时输出 NOT_SAME。\n"
    "- 信息缺失、只部分命中、候选多型号/多规格/可选、额外配置/配件/定制、存在替代/通用/适用/升级或同型号变体风险时输出 MANUAL_REVIEW。\n"
    "\n"
    "字段填写要求：\n"
    "- confidence：SAME 通常 0.90-1.00；MANUAL_REVIEW 通常 0.30-0.85；NOT_SAME 通常 0.70-1.00。\n"
    "- brand_match：只有品牌明确一致或明确同义品牌时为 true，否则为 false。\n"
    "- model_match：完整一致为 exact；只命中部分型号、多型号混售或系列可选为 partial；明确冲突为 mismatch；双方无明确型号或无法识别为 unknown。\n"
    "- spec_match：只有规格、件数、销售单位、计量单位等客户基准关键属性全部一致且不缺失时才为 true，否则为 false。\n"
    "- risk_points：写具体风险点数组，没有风险写 []。\n"
    "- reason：用一句中文说明核心判断依据，必须说明品牌、型号、规格/单位和风险点中影响结论的内容。\n"
    "\n"
    "输出格式必须严格为：{{\"match_result\":\"SAME|NOT_SAME|MANUAL_REVIEW\",\"confidence\":0.0,\"brand_match\":true,\"model_match\":\"exact|partial|mismatch|unknown\",\"spec_match\":true,\"risk_points\":[],\"reason\":\"\"}}\n"
    "\n"
    "query_str={query_str}\n"
    "sku_name={sku_name}\n"
    "model_audit_result={model_audit_result}\n"
    "is_match={is_match}"
)


def _parse_model_response(raw: str, sku_name: str) -> TitleMatchModelResult:
    data = json.loads(raw)
    if "choices" in data:
        content = data["choices"][0]["message"]["content"]
        data = json.loads(content)
    result = TitleMatchModelResult(
        match_result=str(data.get("match_result", "MANUAL_REVIEW")),
        confidence=float(data.get("confidence", 0.0)),
        brand_match=data.get("brand_match"),
        model_match=str(data.get("model_match", "unknown")),
        spec_match=data.get("spec_match"),
        risk_points=list(data.get("risk_points", [])),
        reason=str(data.get("reason", "")),
        raw_response=raw,
    )
    if result.match_result not in ALLOWED_RESULTS - {"MODEL_UNAVAILABLE"}:
        result = result.model_copy(update={"match_result": "MANUAL_REVIEW", "reason": "模型返回非法 match_result，已降级人工核查"})
    if result.model_match not in ALLOWED_MODEL_MATCH:
        result = result.model_copy(update={"model_match": "unknown"})
    if any(term in sku_name for term in RISK_TERMS) and result.match_result == "SAME":
        result = result.model_copy(update={
            "match_result": "MANUAL_REVIEW",
            "risk_points": [*result.risk_points, "标题含替代款/通用/适用/升级款风险词"],
            "reason": result.reason or "标题含风险词，需人工确认是否为原型号同款",
        })
    return result


def call_title_match_model(req: TitleMatchModelRequest, timeout_seconds: float = 30.0) -> TitleMatchModelResult:
    api_key = os.environ.get("TITLE_MATCH_MODEL_API_KEY", "").strip()
    base_url = os.environ.get("TITLE_MATCH_MODEL_BASE_URL", "").strip()
    model_name = os.environ.get("TITLE_MATCH_MODEL_NAME", "").strip()
    if not api_key or not base_url:
        return _unavailable("TITLE_MATCH_MODEL_API_KEY 或 TITLE_MATCH_MODEL_BASE_URL 未配置")

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": _build_prompt(req)}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    http_req = request.Request(
        base_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
        return _parse_model_response(raw, req.sku_name)
    except (error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return TitleMatchModelResult(
            match_result="MANUAL_REVIEW",
            confidence=0.0,
            model_match="unknown",
            reason=f"标题模型调用或解析失败，已降级人工核查: {type(exc).__name__}",
        )
