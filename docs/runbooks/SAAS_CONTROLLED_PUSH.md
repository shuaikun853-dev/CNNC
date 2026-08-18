# SaaS 受控价格补推手册

> 本手册仅用于用户明确提供 SaaS 文档 ID 清单并要求调用 `pushCommodityPrice` 的专项维护批次。它不属于每日三步，不得由第一、二、三步自动触发。

## 固定边界

- 租户固定为 `188605`，商品池固定为 `222`。
- 查询模板固定使用 `data/saas_zhuangtai.curl`，由 `scripts/curl_utils.py` 解析；运行时认证只来自 `data/auth_state.json`。
- 写入口固定为 `scripts/push_saas_commodity_price.py`。禁止手工拼接请求、临时循环 cURL 或直接使用用户消息中的 Token。
- 每批最多 500 个纯数字文档 ID；空值、格式错误、重复 ID 立即暂停。
- 监控 query 必须对每个文档 ID 返回且只返回一个精确 `commodityId`，并取得唯一关系 `id`。缺失、重复或关系 `id` 冲突立即暂停。
- 用户声明已经手工发送但没有可靠结果的关系 `id` 必须通过 `--already-attempted-relation-id` 锁定为 `SKIP_MANUAL_ATTEMPT`，不得重发。
- 每个写请求只发送一次。HTTP 超时、连接中断、非 JSON、认证失败或非 `rspCode=0000` 都立即停止；由于请求可能已经到达服务端，不得自动重试失败项或当前批次。

## 批次与预览

1. 创建全新的 `runtime/saas_push_<时间戳>/`，立即写入 `BATCH_STATE.md`，步骤记为 `CONTROLLED_PUSH`，状态记为 `ACTIVE`。
2. 将用户的准确 ID 清单保存为批次目录内的 `input_ids.txt`；不保存用户消息里的 Cookie、Token 或完整 cURL。
3. 使用当前 `data/auth_state.json` 做只读预览，不刷新认证：

```powershell
python scripts\push_saas_commodity_price.py `
  --ids-file <批次目录>\input_ids.txt `
  --output-dir <批次目录> `
  --already-attempted-relation-id <已手工发送且不得重发的关系ID>
```

4. 核对 `target_count`、`push_candidate_count`、`manual_attempt_skip_count` 和 `execution_plan_sha256`，并检查 `push_manifest.json`。预览只做 query，不调用推送接口。
5. 将 `BATCH_STATE.md` 更新为 `WAITING_USER`，记录准确批次目录、候选数、SHA-256、人工跳过项和下一条执行命令。不得记录认证值。

## 二次授权与执行

只有用户在预览后明确指定批次目录、`push_candidate_count`、`execution_plan_sha256` 并再次授权“执行补推”，才可执行。必须同时说明：执行会在写前原子刷新 `data/auth_state.json`；该次二次授权包含这一次认证刷新许可。

```powershell
python scripts\push_saas_commodity_price.py `
  --ids-file <批次目录>\input_ids.txt `
  --output-dir <批次目录> `
  --already-attempted-relation-id <已手工发送且不得重发的关系ID> `
  --execute `
  --expected-count <预览候选数> `
  --expected-sha256 <预览SHA-256>
```

执行入口在任何写入前重新查询并验证计划、核对已保存的预览 manifest，再刷新 SaaS Token、再次查询并验证同一 SHA-256。计划变化立即暂停。每次成功响应先 fsync 追加到 `push_events.jsonl`，再处理下一项。已有事件或汇总文件时拒绝整批重跑。

## 结果判定

- 只有 `push_summary.json` 为 `COMPLETED`、`confirmedPushCount=pushCandidateCount`，且每条 `push_events.jsonl` 都为 `rspCode=0000`，才能报告本批次补推请求已确认成功。
- `SKIP_MANUAL_ATTEMPT` 只表示本工具没有重发，不代表此前手工请求成功。
- 任一异常将汇总写为 `BLOCKED`。已记录成功的项目不得重发；结果不确定的当前项目也不得重发。需要后续处理时必须建立新批次、明确排除这些项目并重新预览和授权。
- 执行后立即更新 `BATCH_STATE.md`，逐项记录已确认数量、人工跳过数量、阻塞项、证据路径和“不得重复”的关系 `id`，但不写响应中的敏感值。

## 验证

```powershell
python -m pytest tests\test_push_saas_commodity_price.py -q --basetemp=.pytest_tmp_push_controlled
python -m py_compile scripts\push_saas_commodity_price.py
```

自动化测试只能使用替身响应，不得调用真实推送接口。
