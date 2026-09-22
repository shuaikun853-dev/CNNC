# CNNC 安全上线与恢复

## 先分清三件事

代码提交到 GitHub、通过测试、正式生产使用新版本，是三件不同的事。本轮只制作和验证候选，不切换正在运行的生产代码，不注册/停止计划任务，不刷新认证，不调用招商局或 SaaS 写接口。

代码备份不能代替运行数据备份。回退代码也不能撤销已建的远端任务。发现错误时先停止新增生产动作并核对实际状态，不要把旧数据库直接覆盖回去。

## 上线前的检查顺序

1. 读取最新正式 SHA、候选 SHA 和其他 AI 的工作区；有相关新 Bug 修复必须先整合再测。
2. 使用独立 Python 环境运行完整离线测试；失败、跳过和缺少外部验证分别列明。
3. 明确新旧代码是否兼容现有数据库。发生表结构/字段含义变化时，必须另行设计数据迁移与旧版本可读性验证，不能只靠 Git revert。
4. 安排安全切换窗口并确认没有在途写操作；运行数据备份应尽量在同一安静窗口完成。
5. 保存本轮明确版本的代码、环境、外部工具和计划任务定义；备份与候选清单均留在 E 盘私有目录，不上传仓库。
6. 生产切换需要明确授权。先验证候选，保留旧解释器和旧版本，不在活跃目录里逐文件覆盖。

## 独立 Python 环境

`requirements.in` 记录直接能力，`requirements.lock` 固定完整依赖；`requirements.txt` 和 CI constraints 引用同一份锁。`.python-version` 指定验证的 Python 补丁版本。外部招商局脚本真实依赖的 pandas/requests、认证浏览器 Playwright、Windows tzdata 都包含在锁中。

创建**全新、尚不存在**的 E 盘环境，不修改原 Python：

```powershell
$env:TEMP = 'E:\_zcode_tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = 'E:\_zcode_tmp\cnnc-pip-cache'
E:\Python314\python.exe -m venv --copies E:\_zcode_tmp\cnnc-release-env
E:\_zcode_tmp\cnnc-release-env\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
E:\_zcode_tmp\cnnc-release-env\Scripts\python.exe -m pip check
```

虚拟环境只隔离包，不是操作系统安全沙箱。确认 `site.ENABLE_USER_SITE=False`，检查 `PYTHONPATH` 没有注入其他项目。浏览器二进制、外部爬虫环境、用户认证不由 pip 锁代替；不得把“能 import Playwright”写成“真实登录成功”。

## 运行版本清单

`tools/runtime_manifest.py capture` 读取当前候选和明确指定的外部工具，只生成 `approved=false` 的待审核清单。示例中的路径必须换成现场已经核实的路径；不要把它指向未知或无权限目录：

```powershell
python tools/runtime_manifest.py capture --repo . `
  --external-file 'zhaoshang_create=E:\已核实目录\auto_crawl_task.py' `
  --external-file 'zhaoshang_priority=E:\已核实目录\zhaoshang_priority_boost.py' `
  --external-repository 'mall_crawler=E:\已核实目录\cnnc-mall-crawler' `
  --output E:\_zcode_tmp\cnnc-manifest-candidate.json
```

清单需要核对候选 SHA、所有外部脚本、爬虫工作区、Python 隔离、锁文件及计划任务定义。审核人确认这确实是目标发布组合以后，才可将审核结果设为 `approved=true`；不能为通过核验直接把当前错误状态重新采集成“标准”。生产配置文件的安装仍需单独授权。

`python tools/runtime_manifest.py verify --repo . --manifest E:\_zcode_tmp\cnnc-manifest-reviewed.json` 只读核对。没有清单、清单未批准、实时证据缺失都不能显示 `MATCH`。返回的 `MATCH` 也只表示**清单声明的运行项一致**，不是业务正确性或远端系统安全证明。

Dashboard 版本接口新增独立 `runtime_identity`。原 `comparison.version_status` 仍是 Git 关系，`version_scope=repository_git_only` 防止误解。`data/production_manifest.json` 缺失会显示 `UNCONFIGURED`，而不是伪装为全环境一致。

计划任务指纹来自只读 `Export-ScheduledTask` 的 SHA-256，不输出 XML 或可能含敏感值的命令行。无法取得定义时只降级该证据，不修改注册。

## 备份运行数据

`tools/runtime_recovery.py backup` 必须明确业务日、来源根目录和**全新**的目标目录。默认备份三个库：指定业务日的 `state.sqlite3`、跨日 `customer_prewarm.sqlite3`、`shared_crawl_cache.sqlite3`。它使用 SQLite backup API，包括已提交的 WAL 数据，不是简单复制一个 `.sqlite3` 文件。

```powershell
python tools/runtime_recovery.py backup --source-root E:\已核实运行根目录 `
  --business-date 2026-09-22 --destination E:\私有备份目录\本次新目录 --include-artifacts
```

每个库各自是有效快照，**三个库不是同一瞬间的原子快照**。因此清单固定标记 `per_database_snapshot_not_cross_database_atomic`。`--include-artifacts` 额外保存当天允许的交接文档、日志、JSON 和 Excel；逐文件验证稳定副本，不承诺整个目录的同瞬间快照。不断变化的文件会使备份失败，不会发布“完整”标记。

不复制 `.lock`、`.env`、cURL、可执行文件和已知认证状态文件。备份本身可能含敏感业务数据，必须按生产数据保护；校验和只能发现意外损坏，不能替代访问控制或可信来源。代码、外部工具、浏览器、认证、任务注册不在此备份包内，需要对应的独立保护方式。

## 验证与演练

```powershell
python tools/runtime_recovery.py verify --snapshot E:\私有备份目录\本次新目录
python tools/runtime_recovery.py restore --snapshot E:\私有备份目录\本次新目录 `
  --destination E:\_zcode_tmp\本次全新恢复演练目录
```

核验会检查完整标记、固定数据库清单、文件大小、SHA-256 和 SQLite `integrity_check` 和 `foreign_key_check`。恢复只能写新目录，拒绝现有目录、路径逃逸、链接跳转和命名为 `cnnc-prod-runtime` 的生产区域；这个保护不是操作系统级权限沙箱。失败目录保留 `.incomplete`，不自动清理用户文件。

演练要验证实际数据、交接材料和对应版本能否读懂。测试中已经发生的远端动作不随数据库恢复而消失：真实灾后恢复必须对账，尤其 `SUBMIT_UNKNOWN` 不得直接重发。恢复到新目录后先只读核对，不能自动启动计划任务。

## 回退决策

- 仅代码整理且数据库兼容：优先退回已验证的旧代码，保留最新业务状态，再检查任务身份和在途动作。
- 新版本已经产生远端动作：先按真实证据对账；不要还原旧库使系统遗忘已经创建的任务。
- 数据损坏：验证备份，恢复到新目录，用匹配版本只读核验，明确备份之后的任务/结果差额后再决定切换。
- 数据表结构或语义变更：必须有独立迁移/逆向兼容方案；本工具不自动降级 schema。

## 尚需生产负责人确认的运行制度

备份频率、保留周期、离机副本、可接受的数据损失与恢复时长需要结合业务安排确认。当前工具没有自动注册备份计划、删除旧备份或做真实灾难切换；不能把离线恢复测试算成已经完成生产灾备建设。

Windows 虚拟环境还可能通过启动器创建真正的 Python 子进程。停止启动器不一定停止整个任务树；离线测试已按自己创建的进程树完成清理，但这不是所有生产超时/终止入口的验收。切换生产解释器前必须逐入口验证停止后没有遗留 writer、子进程或锁，不能直接照搬测试的 `taskkill` 去结束未知生产任务。

## 本次复审后的精确限制

`--include-artifacts` 只复制登记过的业务证据文件名（例如 BATCH_STATE、daily_status、指定输入/结果 Excel 及其 manifest），不是按扩展名扫描全部 JSON、TXT 或日志。未登记文件不会被自动收入；认证和浏览器会话文件不应放进业务证据目录。业务证据正文仍须由生产者脱敏，文件名白名单不等于内容审计。

`--database-timeout`（兼容旧参数 `--timeout`）是每个 SQLite backup 步骤之间检查的协作式期限，不是硬实时 I/O 超时，也不是端到端文件系统超时；一次底层阻塞可以超过该值。哈希、校验及附件复制的耗时仍取决于数据量和磁盘；执行窗口需要留出空间。跨库、跨文件同一瞬间一致性仍不保证。

输出同时检查普通路径和 Windows 驱动器的真实映射，拒绝 C 盘及其别名、网络盘、设备路径和现有生产目录；大小写冲突的备份成员会被拒绝。外部工具清单保留实际调用的路径别名，别名重定向不能继续核验旧目标冒充一致。

运行身份同时检查依赖版本与导入来源；存在未受控 `PYTHONPATH`、计划任务集合缺项、启动版本未知、查询响应不完整时均不得声称 MATCH。该检查不证明第三方库文件未经恶意篡改，也不代替进程和操作系统隔离。

完整运行指纹将可观察到的受版本控制字节码修改也视为 dirty；依赖导入入口必须属于该发行包的文件清单。缓存返回深复制，调用者不能修改后续身份结果。Dashboard 在模块启动时已捕获身份，不等首次访问才初始化。

新输出路径在查询卷信息或解析路径之前拒绝 NTFS alternate data stream（例如 `existing.txt:manifest`）、Windows 保留设备名和会被系统归一化的尾点/尾空格。它们不能作为“新文件”绕过禁止覆盖既有文件的规则。
