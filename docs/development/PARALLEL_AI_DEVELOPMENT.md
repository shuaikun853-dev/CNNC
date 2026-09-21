# 多 AI 并行开发检查

这套规则只解决一件事：每个 AI 在独立 worktree 开始和结束代码任务时，用同一个只读入口核对正式版本与明显冲突。Git、worktree、branch、Issue 和 PR 仍是事实来源；不建立新的任务数据库或本地状态 registry。

## 开始任务

1. 先为任务建立 GitHub Issue，在 Issue 中记录 `BASE_SHA`、branch、worktree 和主要修改区域。
2. 从当时最新的 `origin/prod/closeout-20260818` 建立独立 worktree。独立 worktree 隔开未提交文件和 branch，避免两个 AI 直接写同一目录；禁止在 `cnnc-prod-runtime` 开发。
3. 运行统一检查，`--scope` 可重复，值是计划修改的仓库相对文件或目录：

```powershell
python tools\parallel_ai_check.py `
  --base-sha <任务开始时的完整 PROD_SHA> `
  --scope app\services\prewarm.py `
  --scope tests\test_prewarm.py
```

`BASE_SHA` 的意思是“本任务开始时，正式 prod 指向哪个 commit”。它不能在任务中途悄悄改成新值。工具使用只读 Git 命令读取远端 prod、当前 branch/HEAD/dirty 状态和本机 worktree；安装并可用 `gh` 时，还读取 open PR 的文件列表。`gh` 失败不会丢失本地 Git 结果，但整体状态会降为 `WARNING`。

## 看懂结果

- `SAFE_TO_START`（退出码 0）：`BASE_SHA` 等于当前 prod、当前 worktree clean、已声明 scope，且没有发现同文件重叠。
- `WARNING`（退出码 1）：例如当前 worktree dirty、未声明 scope，或 GitHub 信息不可用。原因会逐条打印。
- `STOP_AND_REVIEW`（退出码 2）：prod 已移动、发现其他 worktree branch 或 open PR 修改同一文件，或必要 Git 事实无法取得。

工具只把“同一个文件”判为 `FILE_OVERLAP`。不同文件但同属 Prewarm 等业务区域时，只能由 AI 标记为 `POSSIBLE_OVERLAP` 并向任务负责人确认。为了让 100 个以上历史 worktree 仍能快速检查，工具不会逐个扫描其他 worktree 的未提交文件；它会列出其 path、branch、HEAD，并比较 branch 已提交文件。当前 worktree 的 dirty 状态始终检查。

## 提交前

用任务开始时同一个 `BASE_SHA` 和 scope 再运行一次。出现 `PRODUCTION_MOVED` 时：

1. 阅读 `NEW_PROD_COMMITS`；若对象尚未在本地，先手工 `git fetch origin` 再运行。
2. 查看新 commit 修改的文件与本任务 scope 是否重叠。
3. 日常 Bug 与生产修复优先；同一区域冲突时，架构任务等待 Bug 合并。
4. 根据实际差异决定是否重新基于最新 prod 整合，并重新运行相关测试。

禁止自动 hard reset、clean、rebase、merge、push、删除 worktree 或覆盖未提交工作。工具不判断两个业务改动一定兼容，也不替用户决定合并、部署或抢占其他任务。
