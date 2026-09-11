# GLM-5.3 安全启动指令：CNNC 验收过程结果回溯重建

执行本任务前，必须先读取以下三个文件，且磁盘安全门禁优先级最高：

1. `docs/acceptance/GLM53_LOCAL_DISK_SAFETY_GUARDRAILS_20260911.md`
2. `docs/acceptance/CNNC_ACCEPTANCE_PROCESS_RECONSTRUCTION_20260911.md`
3. `docs/acceptance/GLM53_ACCEPTANCE_RECONSTRUCTION_PROMPT_20260911.md`

## 绝对硬门禁

- FINAL AUTHORITY：`中核全量标准商品价格指数和同款数据详情20260825(2).xlsx`，不可修改。
- 禁止从 `E:\` 根目录递归扫描。
- 历史数据目录只读。
- 只能在专用目录 `E:\.A中核运维\_acceptance_reconstruction_20260911\` 写入。
- 禁止复制历史源文件到工作目录。
- 禁止批量转换所有 Excel 到 CSV/parquet/数据库镜像。
- 禁止批量解压归档。
- 禁止生成无上限 cache/temp/log。
- E 盘剩余空间 <20GB 时不得开始大规模处理；处理中 <15GB 立即停止。
- 本任务新增写盘总量硬上限 5GB。
- 只能删除本任务专用目录里本轮明确创建的临时文件；禁止删除任何历史数据和其他目录。
- 发现任何命令可能无界写盘时必须 fail closed，不能先执行再观察。

## 扫描方式

不要“扫整个E盘”。

第一阶段仅对明确的 CNNC 工作根目录做只读 metadata inventory，优先：

```text
E:\.A中核运维\
```

第一轮只记录：path / size / mtime / extension / 可能用途，不做格式转换，不复制文件。

根据文件名、路径和 120047 标准商品编码再定向打开需要的历史文件。

## 数据处理原则

- 大型 XLSX 使用只读/流式读取；
- CSV 使用 chunksize；
- 只读取需要的列；
- 一次处理一个文件或一个批次；
- 历史候选必须来自真实文件并可追溯；
- 不允许虚构 SKU、URL、价格、评论数；
- 最终验收表中的最终同款、最终价格指数、计算方式不可改变。

## 写盘前置报告

开始处理前先输出：

```text
DISK PRECHECK
E_TOTAL_GB: ...
E_FREE_GB: ...
WORKDIR: E:\.A中核运维\_acceptance_reconstruction_20260911\
WORKDIR_SIZE_GB: ...
EXPECTED_MAX_NEW_WRITE_GB: <= 5
SCAN_ROOTS: ...
WRITE_ROOTS: ...
```

如果不满足安全门禁，停止并报告，不要继续。

之后再按完整验收回溯规范执行，最终生成5个客户Excel和内部审计/血缘报告。
