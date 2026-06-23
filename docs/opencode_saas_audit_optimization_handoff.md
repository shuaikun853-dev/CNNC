# SaaS 审批全流程优化交接给 opencode

本文档用于让 opencode 直接集成当前已验证的优化能力，目标是缩短 `scripts/audit_saas_excel_file.py` 全流程耗时，并让日志能清楚显示每个阶段在哪里耗时。

## 目标

需要集成 4 个能力：

1. 两个 JSON 对应完成后，立即启动招商局任务。
2. 招商局多个商城并发执行，而不是串行跑。
3. 标题模型改成批量调用，并支持批次并发。
4. 主流程和招商局每一步打印时间戳和耗时。

当前实测在 76 条数据上：

- 优化前串行全流程约 `455s`
- 优化后全流程约 `209s`
- 招商局 6 个商城并发后实际任务耗时约 `36-38s`

## 涉及文件

主要文件：

- `scripts/audit_saas_excel_file.py`
- `app/workflows/work_01_saas_approval_json_audit.py`
- `app/services/title_match_model_adapter.py`
- `tests/test_work_01_saas_approval_json_audit.py`

验证输出目录示例：

- `runtime/full_current_optimized_20260623_2015/`

## 关键实现一：批量模型调用

已在 `app/services/title_match_model_adapter.py` 中提供：

- `call_title_match_model_batch(requests)`
- `_parse_batch_model_response(...)`

工作流在 `app/workflows/work_01_saas_approval_json_audit.py` 中接入：

- `_title_model_batch_size(default=30)`
- `_title_model_batch_concurrency(default=1)`
- `_precompute_title_model_results(records)`

环境变量：

```powershell
$env:TITLE_MATCH_MODEL_BATCH_SIZE='30'
$env:TITLE_MATCH_MODEL_BATCH_CONCURRENCY='12'
$env:TITLE_MATCH_MODEL_TIMEOUT='180'
```

逻辑要求：

- 先按 `(queryStr, skuName, modelAuditResult, isMatch)` 去重。
- 按 `TITLE_MATCH_MODEL_BATCH_SIZE` 分批。
- 多批之间用 `ThreadPoolExecutor` 并发。
- 模型异常时不要中断全流程，降级为 `MANUAL_REVIEW`。

测试覆盖：

- 二值返回 `{ "is_same": "是|否" }`
- 富 JSON 返回 `match_result/confidence/brand_match/model_match/spec_match/risk_points/reason`
- 批量调用缓存去重

## 关键实现二：主流程提前异步启动招商局

在 `scripts/audit_saas_excel_file.py` 的 `run_main(...)` 中，招商局任务不要放在 crawler 后面串行启动。

正确顺序：

1. 读取输入模式。
2. 导出招商局模板 `export_excluded_templates(...)`。
3. 如果启用 `--zhaoshang-task`，立即异步提交招商局任务。
4. 主线程继续执行：
   - 读取 SaaS JSON 和客户 JSON
   - join 两个 JSON
   - 详情接口补客户价
   - 批量模型调用
   - multi_mall 爬非招商局链接
5. 在写最终 Excel 前等待招商局 future。
6. 回填招商局页面价。
7. 写最终 Excel。

参考实现形态：

```python
zhaoshang_summary = {"enabled": False}
zhaoshang_executor = None
zhaoshang_future = None

if zhaoshang_task_enabled and mode == "input_saas_json":
    zhaoshang_start = time.perf_counter()
    _log_step("zhaoshang", f"submit async tasks concurrency={max(1, zhaoshang_concurrency)}")
    zhaoshang_executor = ThreadPoolExecutor(max_workers=1)
    zhaoshang_future = zhaoshang_executor.submit(
        run_zhaoshang_tasks,
        excluded_export_summary=excluded_export_summary,
        task_name_prefix=zhaoshang_task_prefix,
        tool_path=zhaoshang_tool_path,
        wait_timeout_seconds=zhaoshang_wait_timeout,
        progress_interval_seconds=zhaoshang_progress_interval,
        skip_if_exists=True,
        max_workers=zhaoshang_concurrency,
    )
```

等待位置：

```python
if zhaoshang_future is not None:
    _log_step("zhaoshang", "waiting async tasks")
    try:
        zhaoshang_results = zhaoshang_future.result()
    finally:
        if zhaoshang_executor is not None:
            zhaoshang_executor.shutdown(wait=True)

    zhaoshang_meta = zhaoshang_results.get("_meta", {}) if isinstance(zhaoshang_results, dict) else {}
    task_elapsed = zhaoshang_meta.get("elapsed_seconds")
    task_elapsed_text = f"{task_elapsed:.2f}s" if isinstance(task_elapsed, (int, float)) else "unknown"
    _log_step(
        "zhaoshang",
        f"async tasks joined task_elapsed={task_elapsed_text} overlapped_since_submit={_elapsed_seconds(zhaoshang_start)}",
    )

    backfilled = apply_zhaoshang_results_to_rows(rows, enriched, audit_response, zhaoshang_results)
```

注意：`task_elapsed` 是招商局任务实际耗时，`overlapped_since_submit` 是从异步提交到 join 的总跨度。后者包含主流程详情/模型/crawler 时间，不代表招商局实际卡住。

## 关键实现三：招商局商城并发

新增 CLI 参数：

```powershell
--zhaoshang-concurrency 6
```

`run_main(...)` 增加参数：

```python
zhaoshang_concurrency: int = 6
```

`main()` 调 `run_main(...)` 时传入：

```python
zhaoshang_concurrency=args.zhaoshang_concurrency
```

`run_zhaoshang_tasks(...)` 增加参数：

```python
max_workers=6
```

并发逻辑：

- 遍历 `excluded_export_summary`
- 跳过 `_meta`、`enabled` 和 `deduped == 0`
- 为每个商城组装一个 job
- 使用 `ThreadPoolExecutor(max_workers=min(max_workers, len(jobs)))`
- 每个 job 调一次招商局 `run_crawl_task(...)`
- 返回结果中保留：
  - `ok`
  - `task_name`
  - `mall_key`
  - `rows`
  - `template_path`
  - `result_path`
- 返回 `_meta`：
  - `elapsed_seconds`
  - `concurrency`
  - `task_count`

重要：当前文件里为了快速验证，末尾有一个同名新版 `run_zhaoshang_tasks(...)` 覆盖前面的旧版定义。opencode 正式集成时不要保留两个同名函数，应该把旧版串行函数替换成并发版。

## 关键实现四：招商局回填

招商局返回文件通常没有 URL，只有：

- `商品编码`
- `商品价格`

所以不能只按 URL 回填。需要通过导出的招商局模板建立映射：

```text
商品编码 -> url
```

然后读取招商局结果：

```text
商品编码 -> 商品价格 -> url -> page_price
```

已实现的辅助函数：

- `_read_zhaoshang_template_code_url_map(template_path)`
- `read_zhaoshang_result_xlsx(path, template_path=None)`
- `apply_zhaoshang_results_to_rows(...)`

`run_zhaoshang_tasks(...)` 返回每个商城结果时必须带上：

```python
"template_path": str(excel_path)
```

否则无法按商品编码反查 URL。

## 关键实现五：统一日志

在 `scripts/audit_saas_excel_file.py` 顶部增加：

```python
def _elapsed_seconds(start: float) -> str:
    return f"{time.perf_counter() - start:.2f}s"


def _log_step(component: str, message: str, *, start: float | None = None) -> None:
    elapsed = f" elapsed={_elapsed_seconds(start)}" if start is not None else ""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{component}] {message}{elapsed}", flush=True)
```

建议打点位置：

- `detail start/completed`
- `model start/completed`
- `crawler pending_urls`
- `crawler dry-run start/completed`
- `crawler run start/completed`
- `crawler loaded results`
- `crawler stage completed`
- `zhaoshang submit async tasks`
- `zhaoshang run mall tasks`
- `zhaoshang per mall submit/completed/failed`
- `zhaoshang waiting async tasks`
- `zhaoshang joined`
- `zhaoshang backfilled`
- `output workbook written`

## 推荐运行命令

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:TITLE_MATCH_MODEL_BATCH_SIZE='30'
$env:TITLE_MATCH_MODEL_BATCH_CONCURRENCY='12'
$env:TITLE_MATCH_MODEL_TIMEOUT='180'

E:\Python314\python.exe scripts\audit_saas_excel_file.py `
  --input-saas-json .\data\saas_json.json `
  --input-customer-json .\data\customer_json.json `
  --curl-file .\data\detail_request.curl `
  --output-xlsx .\runtime\full_current_optimized\saas_full_audit.xlsx `
  --use-model `
  --request-interval 0.5 `
  --enable-crawler `
  --skip-dry-run `
  --crawler-headless `
  --crawler-concurrency 3 `
  --crawler-per-domain-delay 2 `
  --crawler-timeout-ms 18000 `
  --excluded-output-dir .\runtime\full_current_optimized\zhaoshang_templates `
  --zhaoshang-task `
  --zhaoshang-concurrency 6 `
  --zhaoshang-task-prefix 'cnnc_current_opt' `
  --zhaoshang-wait-timeout 7200 `
  --zhaoshang-progress-interval 30
```

## 验证命令

```powershell
E:\Python314\python.exe -m py_compile `
  scripts\audit_saas_excel_file.py `
  app\workflows\work_01_saas_approval_json_audit.py `
  app\services\title_match_model_adapter.py

E:\Python314\python.exe -m pytest tests\test_work_01_saas_approval_json_audit.py -q
```

当前验证结果：

```text
182 passed, 1 warning
```

## 风险和注意事项

1. 不要打印 cookie/token/API key。
2. 招商局 wrapper 的 `RequestsDependencyWarning` 不影响结果。
3. `openpyxl` 的 `Workbook contains no default style` 是格式 warning，不代表业务失败。
4. multi_mall 的领先未来旧商城可能随机触发页面超时/域名停止；这和招商局并发优化无关。
5. 正式集成时清理重复函数定义，保留最后的并发版 `run_zhaoshang_tasks(...)`。
6. 如果 opencode 已经实现了“两个 JSON join 后马上启动招商局和非招商局爬虫”，需要确认 join 后仍保留 `excluded_export_summary` 的模板路径，否则招商局按商品编码无法回填 URL。

