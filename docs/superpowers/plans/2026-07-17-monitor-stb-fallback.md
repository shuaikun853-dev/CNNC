# 监控表史泰博兜底 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持第三步固定入口从本地受保护 env 文件加载招商局优先级配置，并在显式授权时自动补采史泰博缺价链接。

**Architecture:** `process_monitor_no_push_zhaoshang.py` 保持唯一入口。它在运行时解析指定 env 文件，招商局阶段后从无价格行筛选史泰博 URL，调用既有专用脚本并按 URL 精确回填，另存补价后的模板和剩余缺价清单。

**Tech Stack:** Python 3、openpyxl、subprocess、pytest。

## Global Constraints

- 不记录或输出认证、账号、密码、Cookie、Token。
- 招商局仍优先于本地爬虫；史泰博兜底仅由 `--auto-stbchina-fallback` 显式启用。
- 只允许现有史泰博专用脚本；403、405、验证码、登录限制时停止，不规避。
- 非史泰博缺价 URL 必须保留在待确认清单。

---

### Task 1: 提取可测辅助函数

**Files:**
- Modify: `scripts/process_monitor_no_push_zhaoshang.py`
- Create: `tests/test_process_monitor_no_push_zhaoshang.py`

**Interfaces:**
- Produces: `load_zhaoshang_db_env(path: Path) -> dict[str, str]`
- Produces: `is_stbchina_url(url: str) -> bool`

- [ ] **Step 1: Write failing tests**

```python
def test_load_zhaoshang_db_env_requires_all_variables(tmp_path):
    path = tmp_path / "db.env"
    path.write_text("ZHAOSHANG_DB_HOST=x\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="缺少"):
        module.load_zhaoshang_db_env(path)

def test_is_stbchina_url_matches_host_only():
    assert module.is_stbchina_url("https://www.stbchina.cn/p?itemId=1")
    assert not module.is_stbchina_url("https://example.com/?next=stbchina.cn")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_process_monitor_no_push_zhaoshang.py -v`

- [ ] **Step 3: Implement helpers and arguments**

```python
parser.add_argument("--zhaoshang-db-env")
parser.add_argument("--auto-stbchina-fallback", action="store_true")

def load_zhaoshang_db_env(path: Path) -> dict[str, str]:
    values = dotenv_values_without_logging(path)
    required = ("ZHAOSHANG_DB_HOST", "ZHAOSHANG_DB_PORT", "ZHAOSHANG_DB_USER", "ZHAOSHANG_DB_PASSWORD")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RuntimeError("招商局数据库配置缺少: " + ", ".join(missing))
    return {key: values[key] for key in required}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_process_monitor_no_push_zhaoshang.py -v`

### Task 2: 史泰博调用与精确回填

**Files:**
- Modify: `scripts/process_monitor_no_push_zhaoshang.py`
- Modify: `tests/test_process_monitor_no_push_zhaoshang.py`

**Interfaces:**
- Produces: `run_stbchina_fallback(urls: list[str], output_dir: Path) -> dict[str, dict[str, str]]`
- Consumes: `missing_rows` 中仍缺价的导入行。

- [ ] **Step 1: Write failing URL 精确回填 test**

```python
def test_apply_stb_results_only_updates_matching_price_rows():
    rows = [FakeRow(URL="https://www.stbchina.cn/p?itemId=1")]
    count = module.apply_stbchina_results(rows, {rows[0].URL: {"price": 12.5, "title": "商城标题", "uid": "1", "comment": "2"}})
    assert count == 1
    assert rows[0].价格 == 12.5
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_process_monitor_no_push_zhaoshang.py::test_apply_stb_results_only_updates_matching_price_rows -v`

- [ ] **Step 3: Implement external invocation and workbook reader**

```python
input_path.write_text("\n".join(urls), encoding="utf-8")
proc = subprocess.run([PYTHON, str(STB_SCRIPT), "--input", str(input_path), "--output", str(output_path)], cwd=STB_ROOT, capture_output=True, text=True, encoding="utf-8")
if proc.returncode != 0:
    raise RuntimeError("史泰博专用脚本失败")
```

Read only successful workbook rows and map by `商品链接`; never set title without a numeric price.

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_process_monitor_no_push_zhaoshang.py -v`

### Task 3: 输出、文档和实际批次

**Files:**
- Modify: `scripts/process_monitor_no_push_zhaoshang.py`
- Modify: `AGENTS.md`
- Modify: `TESTING.md`

- [ ] **Step 1: Write output behavior test**

```python
assert output_path.name == "导入zhimport模版_<timestamp>_招商局_STB补价后.xlsx"
assert remaining_path.name == "STB补价后仍无价格清单_<timestamp>.xlsx"
```

- [ ] **Step 2: Implement separate final outputs**

Copy the finalized import template only when the flag ran; write the remaining missing rows with reasons; preserve the original three files.

- [ ] **Step 3: Document the protected env path and flag**

Document only `data/ZHAOSHANG_DB.env`, required variable names, and the two command-line switches. State that values are never committed or printed and that the fallback is opt-in.

- [ ] **Step 4: Validate and run the requested batch**

Run: `python -m py_compile scripts\process_monitor_no_push_zhaoshang.py`

Run the fixed entrypoint with the specified monitor workbook, three cURL files, `--zhaoshang-db-env data\ZHAOSHANG_DB.env`, and `--auto-stbchina-fallback`.

- [ ] **Step 5: Verify generated workbooks**

Open each workbook with `openpyxl`, check row counts and required sheet names, and report the final unavailable prices without exposing any credential.
