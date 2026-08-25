# Monitor Push Price Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让第二步仅把采集成功、同款匹配成功、价格检测状态不是 `5` 且 `newPrice` 为有效正数的数据判定为可推送。

**Architecture:** 在固定入口中新增独立的价格有效性函数，并由现有 `_status()` 按固定顺序组合采集、同款、价格检测和推送价格校验。Excel 显式输出原始 `newPrice`，测试分别覆盖纯判定逻辑和工作簿分类结果。

**Tech Stack:** Python 3.14、pytest、openpyxl、现有 `scripts/curl_utils.py`

## Global Constraints

- 第二步只允许调用中核列表和 SaaS 监控只读接口。
- 可推送必须同时满足 `isSync=1`、`isSyncStandard=1`、`syncPriceStatus!=5`、`newPrice` 是可解析且大于 `0` 的数值。
- `null`、空字符串、非数字、`0` 和负数均视为无效推送价格。
- `syncPriceStatus=5` 优先分类为“价格检测校验失败”，即使 `newPrice` 有值也不能推送。
- 不改变分页、认证、无代理、租户 `188605`、商品池 `222` 和文件归档边界。
- 不调用审批、推送、保存、提交、招商局、爬虫或数据库 SQL。

---

### Task 1: 价格有效性与可推送状态判定

**Files:**
- Create: `tests/test_export_current_monitor_status.py`
- Modify: `scripts/export_current_monitor_status.py`

**Interfaces:**
- Produces: `_is_valid_new_price(value: object) -> bool`
- Produces: `_status(row: dict) -> tuple[str, str, str, str, str, str]`
- `_status()` 保持现有六项返回结构：采集状态、同款状态、价格计算状态、是否可推送、最终分类、问题说明。

- [ ] **Step 1: 写价格有效性的失败测试**

生产变更若错误接受空值、零、负数或非数字，该测试必须失败。

```python
import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location(
    "export_current_monitor_status",
    SCRIPT_DIR / "export_current_monitor_status.py",
)
assert SPEC and SPEC.loader
monitor_export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor_export)


@pytest.mark.parametrize("value", [1, 0.01, "12.50", " 3 "])
def test_valid_new_price_accepts_positive_numbers(value):
    assert monitor_export._is_valid_new_price(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " ",
        "null",
        "None",
        "abc",
        "NaN",
        "Infinity",
        0,
        "0",
        -1,
        "-0.01",
        True,
    ],
)
def test_valid_new_price_rejects_missing_non_numeric_and_non_positive_values(value):
    assert monitor_export._is_valid_new_price(value) is False
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py -q
```

Expected: FAIL，原因是模块尚未定义 `_is_valid_new_price`。

- [ ] **Step 3: 实现最小价格校验**

在 `scripts/export_current_monitor_status.py` 中新增：

```python
from decimal import Decimal, InvalidOperation


def _is_valid_new_price(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"}:
        return False
    try:
        price = Decimal(text)
        return price.is_finite() and price > 0
    except InvalidOperation:
        return False
```

- [ ] **Step 4: 运行价格校验测试并确认 GREEN**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py -q
```

Expected: 所有参数化价格测试 PASS。

- [ ] **Step 5: 写状态判定的失败测试**

生产变更若忽略 `syncPriceStatus=5`、忽略 `newPrice` 或绕过采集/同款状态，该测试必须失败。

```python
@pytest.mark.parametrize(
    ("row", "pushable", "category"),
    [
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": 1,
                "newPrice": "25.80",
            },
            "是",
            "可推送",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": "",
                "newPrice": 8,
            },
            "是",
            "可推送",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": 5,
                "newPrice": 8,
            },
            "否",
            "价格检测校验失败",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": 1,
                "newPrice": "",
            },
            "否",
            "推送价格为空或无效",
        ),
        (
            {
                "isSync": 0,
                "isSyncStandard": 1,
                "syncPriceStatus": 1,
                "newPrice": 8,
            },
            "否",
            "采集未成功",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 2,
                "syncPriceStatus": 1,
                "newPrice": 8,
            },
            "否",
            "同款匹配异常/未成功",
        ),
    ],
)
def test_status_requires_successful_states_and_valid_push_price(
    row, pushable, category
):
    result = monitor_export._status(row)
    assert result[3] == pushable
    assert result[4] == category
```

- [ ] **Step 6: 运行状态测试并确认 RED**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py -q
```

Expected: FAIL；空 `newPrice` 当前仍被判为可推送，且状态 `5` 的分类不符合新规则。

- [ ] **Step 7: 按固定顺序修改 `_status()`**

保持采集和同款文本映射，替换判定分支为：

```python
if sync != "1":
    problem = "采集未成功"
elif standard == "0":
    problem = "同款匹配中"
elif standard != "1":
    problem = "同款匹配异常/未成功"
elif price == "5":
    problem = "价格检测校验失败"
elif not _is_valid_new_price(row.get("newPrice")):
    problem = "推送价格为空或无效"
else:
    return collect, same, price_text, "是", "可推送", ""
return collect, same, price_text, "否", problem, problem
```

- [ ] **Step 8: 运行单元测试并确认 GREEN**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py -q
```

Expected: PASS。

- [ ] **Step 9: 提交状态判定**

```powershell
git add -- scripts/export_current_monitor_status.py tests/test_export_current_monitor_status.py
git commit -m "fix: require valid push price for monitor export"
```

---

### Task 2: Excel 输出 newPrice 与分类对账

**Files:**
- Modify: `scripts/export_current_monitor_status.py`
- Modify: `tests/test_export_current_monitor_status.py`

**Interfaces:**
- Produces: `_build_monitor_values(row: dict) -> list[object]`
- Produces: `_write_workbook(records: list[list[object]], output: Path) -> dict[str, object]`
- 返回顺序固定为：采集状态、同款状态、价格计算状态、`newPrice`、是否可推送、最终分类、问题说明、SaaS 监控状态。

- [ ] **Step 1: 写监控输出字段的失败测试**

生产变更若漏写或错位写入 `newPrice`，该测试必须失败。

```python
def test_build_monitor_values_exposes_new_price_and_push_decision():
    values = monitor_export._build_monitor_values(
        {
            "isSync": 1,
            "isSyncStandard": 1,
            "syncPriceStatus": 1,
            "newPrice": "19.90",
        }
    )

    assert values[3] == "19.90"
    assert values[4] == "是"
    assert values[5] == "可推送"
    assert values[7] == "已匹配"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py::test_build_monitor_values_exposes_new_price_and_push_decision -q
```

Expected: FAIL，原因是 `_build_monitor_values` 尚不存在。

- [ ] **Step 3: 实现监控输出字段构建函数**

```python
def _build_monitor_values(row: dict) -> list[object]:
    collect, same, price, pushable, category, problem = _status(row)
    return [
        collect,
        same,
        price,
        row.get("newPrice"),
        pushable,
        category,
        problem,
        "已匹配",
    ]
```

在表头的“价格计算状态”后加入 `newPrice`，并让主循环复用 `_build_monitor_values()`。

- [ ] **Step 4: 更新缺失监控占位与筛选索引**

新增 `newPrice` 后，记录尾部索引固定为：

```text
13 采集状态
14 同款匹配状态
15 价格计算状态
16 newPrice
17 是否可推送
18 最终分类
19 问题说明
20 SaaS监控状态
```

将筛选和统计修改为：

```python
pushable = [row for row in records if row[17] == "是"]
missing = [row for row in records if row[20] == "缺失"]
needed = [
    row for row in records
    if row[17] == "否" and row[20] != "缺失"
]
categories = Counter(row[18] for row in records)
```

缺失监控占位必须包含八个值：

```python
["", "", "", "", "否", "SaaS监控缺失", "中核存在但SaaS监控未返回", "缺失"]
```

- [ ] **Step 5: 运行单元测试并确认 GREEN**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py -q
```

Expected: PASS。

- [ ] **Step 6: 写离线工作簿分类测试**

将工作簿生成部分提取为 `_write_workbook(records, output)`，然后用三条手工记录断言五个 Sheet、`newPrice` 表头以及可推送/需处理行数。测试不得调用真实接口。

```python
def test_write_workbook_reconciles_new_price_classification(tmp_path):
    records = [
        ["A", *[""] * 12, "采集成功", "同款匹配成功", "价格计算成功",
         10, "是", "可推送", "", "已匹配"],
        ["B", *[""] * 12, "采集成功", "同款匹配成功", "无价格",
         "", "否", "推送价格为空或无效", "推送价格为空或无效", "已匹配"],
        ["C", *[""] * 12, "", "", "", "", "否",
         "SaaS监控缺失", "中核存在但SaaS监控未返回", "缺失"],
    ]
    output = tmp_path / "monitor.xlsx"

    monitor_export._write_workbook(records, output)

    workbook = load_workbook(output, read_only=True, data_only=True)
    assert workbook.sheetnames == [
        "全量监控结果",
        "可推送数据",
        "需处理数据",
        "SaaS监控缺失",
        "状态统计",
    ]
    headers = [cell.value for cell in workbook["全量监控结果"][1]]
    assert headers[16] == "newPrice"
    assert workbook["可推送数据"].max_row - 1 == 1
    assert workbook["需处理数据"].max_row - 1 == 1
    assert workbook["SaaS监控缺失"].max_row - 1 == 1
```

- [ ] **Step 7: 运行工作簿测试并确认 RED**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py::test_write_workbook_reconciles_new_price_classification -q
```

Expected: FAIL，原因是 `_write_workbook` 尚不存在。

- [ ] **Step 8: 提取最小工作簿写入函数**

将表头提取为模块常量 `MONITOR_HEADERS`，再把 `main()` 中创建五个 Sheet、样式和统计的代码提取为以下函数：

```python
def _write_workbook(
    records: list[list[object]],
    output: Path,
) -> dict[str, object]:
    pushable = [row for row in records if row[17] == "是"]
    missing = [row for row in records if row[20] == "缺失"]
    needed = [
        row for row in records
        if row[17] == "否" and row[20] != "缺失"
    ]
    categories = Counter(row[18] for row in records)
    stats = [
        ["指标", "分类", "数量"],
        ["中核待处理总数", "", len(records)],
        ["SaaS匹配数", "", len(records) - len(missing)],
        ["SaaS监控缺失数", "", len(missing)],
        ["可推送数", "", len(pushable)],
        ["需处理数", "", len(needed)],
        *[
            ["最终分类", key, value]
            for key, value in categories.most_common()
        ],
    ]

    workbook = Workbook()
    workbook.remove(workbook.active)
    sheets = (
        ("全量监控结果", [MONITOR_HEADERS, *records]),
        ("可推送数据", [MONITOR_HEADERS, *pushable]),
        ("需处理数据", [MONITOR_HEADERS, *needed]),
        ("SaaS监控缺失", [MONITOR_HEADERS, *missing]),
        ("状态统计", stats),
    )
    for name, data in sheets:
        sheet = workbook.create_sheet(name)
        for values in data:
            sheet.append(values)
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for col in range(1, sheet.max_column + 1):
            width = max(
                len(str(sheet.cell(row, col).value or ""))
                for row in range(1, sheet.max_row + 1)
            )
            sheet.column_dimensions[get_column_letter(col)].width = min(
                max(10, width + 2),
                50,
            )
    workbook.save(output)
    return {
        "cnnc_total": len(records),
        "saas_matched": len(records) - len(missing),
        "saas_missing": len(missing),
        "pushable": len(pushable),
        "needs_processing": len(needed),
        "categories": dict(categories),
    }
```

`main()` 只负责拉取、合并、创建输出路径、调用 `_write_workbook()` 和打印返回的汇总。不得改变现有样式、Sheet 名称和归档路径。

- [ ] **Step 9: 运行工作簿测试和完整新测试文件**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py -q
```

Expected: PASS。

- [ ] **Step 10: 提交 Excel 输出改动**

```powershell
git add -- scripts/export_current_monitor_status.py tests/test_export_current_monitor_status.py
git commit -m "feat: expose push price in monitor workbook"
```

---

### Task 3: 同步第二步规则文档并完成回归验证

**Files:**
- Modify: `AGENTS.md`
- Modify: `TESTING.md`

**Interfaces:**
- 文档中的第二步可推送条件必须与 `_status()` 完全一致。

- [ ] **Step 1: 更新 AGENTS.md**

将第二步原规则：

```text
isSync=1 AND isSyncStandard=1 AND
(syncPriceStatus=1 OR syncPriceStatus="")
```

替换为：

```text
isSync=1 AND isSyncStandard=1 AND
syncPriceStatus!=5 AND newPrice 为可解析且大于 0 的数值
```

补充判定顺序：先拦截 `syncPriceStatus=5`，再检查 `newPrice`；空值、`null`、非数字、`0` 和负数均不可推送。

- [ ] **Step 2: 更新 TESTING.md**

将监控状态验证标准同步为新规则，并加入以下验收用例：

```text
syncPriceStatus=5 + newPrice>0 -> 不能推送
syncPriceStatus!=5 + newPrice为空 -> 不能推送
syncPriceStatus!=5 + newPrice=0 -> 不能推送
syncPriceStatus!=5 + newPrice为正数 -> 可推送
```

- [ ] **Step 3: 运行新功能测试**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py -q
```

Expected: PASS，0 failures。

- [ ] **Step 4: 运行相关既有回归测试**

Run:

```powershell
E:\Python314\python.exe -m pytest tests\test_work_01_saas_approval_json_audit.py -q -k "curl_utils or detail_request_preserves_cnnc_y1_header_case"
```

Expected: PASS，0 failures；确认 cURL 解析和中核请求头大小写保护未受影响。

- [ ] **Step 5: 执行离线语法验证**

Run:

```powershell
E:\Python314\python.exe -m py_compile scripts\export_current_monitor_status.py
```

Expected: exit code 0。

- [ ] **Step 6: 检查改动范围**

Run:

```powershell
git diff --check
git diff -- scripts/export_current_monitor_status.py tests/test_export_current_monitor_status.py AGENTS.md TESTING.md
```

Expected: 无空白错误；改动仅涉及新判定、`newPrice` 输出、测试和第二步文档。

- [ ] **Step 7: 提交文档与最终验证**

```powershell
git add -- AGENTS.md TESTING.md
git commit -m "docs: update monitor pushability rule"
```
