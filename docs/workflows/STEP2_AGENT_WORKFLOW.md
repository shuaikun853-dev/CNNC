# STEP2 Agent Workflow（用户触发）

## 顺序（人工闸门）

STEP1 → 人工审核 → 人工收口 → STOP → 用户明确要求执行 STEP2 → STEP2 三表 → STOP → 用户再次明确要求才进入 STEP3。

- STEP1 的审核/收口/closeout/finalize/调度 tick/Dashboard 一律不得自动触发 STEP2。
- STEP2 产出后立即停止：不自动生成 zhImport、不自动启动 STEP3（`scripts/process_monitor_no_push_zhaoshang.py`）、不自动处理客户 referenceLink、不创建招商局任务、不运行史泰博。

## STEP2 输入

- `data/latest_cnnc_query.curl`（中核待处理列表，`sassCheckStatuss=["0"]`）与 `data/saas_zhuangtai.curl`（租户 188605 / 商品池 222 监控查询），经 `data/auth_state.json` 运行时认证。完整只读分页，一次查询构成唯一 authoritative snapshot。

## STEP2 输出（固定）

`python scripts/export_current_monitor_status.py --business-date 2026-09-08`（缺省用上海时区当前业务日）一次生成：

- `workdays/2026-09-08/03_step2/STEP2_监控结果.xlsx`（权威五 Sheet：全量监控结果/可推送数据/需处理数据/SaaS监控缺失/状态统计）
- `workdays/2026-09-08/03_step2/STEP2_可直接推送.xlsx`（单 Sheet `可直接推送`）
- `workdays/2026-09-08/03_step2/STEP2_不可直接推送.xlsx`（单 Sheet `不可直接推送`，含 SaaS监控缺失行）
- `workdays/2026-09-08/manifest.json`

三份文件来自同一内存 records snapshot；split 两表只是按 `是否可推送` 列的纯投影，恒等式 `monitor_total = pushable + non_pushable`，documentId 交集为空、并集等于全量。同日重跑为原子整代替换（generation+1）。

## Pushability authority

冻结于 `scripts/export_current_monitor_status.py` 的 `_status()` / `_is_valid_new_price()`：判定顺序 采集→同款→`syncPriceStatus=5`→newPrice 有效性；`syncPriceStatus` 为空或 2 且 newPrice 有效即为可推送（客户既有语义）。任何票据不得"顺手修复"这些语义。

## Agent 定位

`workdays/YYYY-MM-DD/manifest.json` 是当天导航权威：`stages.step2.status == COMPLETED` 且 `files.*.sha256` 与磁盘文件一致时才可消费三表；`FAILED` 表示该代不可用。禁止按文件名时间戳猜测目录。

## 禁止

STEP1 不自动 STEP2；STEP2 不自动 STEP3；真实 STEP2 是用户显式业务动作，开发/测试一律 fake 数据。
