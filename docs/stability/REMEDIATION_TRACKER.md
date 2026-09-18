# CNNC 整改追踪 V1

## 目标

/dashboard/remediation 用一条链回答每个稳定性问题现在到哪一步：

ChatGPT 发现/审计 → Codex 实施 → Branch/PR → ChatGPT 复审 → Merge → Production → 生产验证。

V1 只读，不创建 PR、不合并、不部署、不修改生产 runtime。

## 数据来源

- docs/stability/remediation_tracker.json：F01-F27 的人工关联与审计结论。
- 本地/远端 Git：branch HEAD、生产分支 HEAD、祖先关系。
- GitHub CLI：PR 状态；不可用时显示 PR 状态未知，不猜测。
- ProductionIdentityService：当前 Dashboard 进程实际运行 SHA。

ChatGPT/Codex 的历史映射只记录已确认的对话名与 Session ID；无法可靠确认时保持未关联。

## 状态口径

开发状态：

- NOT_STARTED：未关联 Codex、branch、PR。
- ASSIGNED：已关联 Codex，但尚无 branch。
- BRANCH_ONLY：远端/本地存在开发 branch，尚无已确认 PR。
- PR_OPEN / PR_DRAFT：GitHub 已确认 PR。
- PR_UNKNOWN：知道 PR 或 branch，但 GitHub 查询失败；不得降级成“未创建”。
- MERGED：GitHub 已确认合并。

生产状态：

- NOT_MERGED：修复提交不在生产分支。
- MERGED_NOT_DEPLOYED：生产分支已有修复，但当前运行进程还没有。
- DEPLOYED：当前运行进程包含修复提交。

production_verification.status=VERIFIED 必须来自部署后的实际验证，不能因为 SHA 已部署自动填写。

## 维护规则

每个整改项永久使用 F01…F27。新 Codex 任务、branch、PR、commit message 优先带同一个 Fxx。

新开发开始时只补台账中的 ChatGPT/Codex 关联；Branch/PR/Production 状态由看板自动读取。

ChatGPT 复审后更新 review.status 和 review.note；生产实际验证后才更新 production_verification。

同一个 branch 可同时覆盖多个 finding，V1 会在一次刷新内去重 branch 与 PR 查询。
