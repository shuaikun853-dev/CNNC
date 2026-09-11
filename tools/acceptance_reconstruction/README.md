# acceptance_reconstruction 工具集

CNNC 验收过程结果数据回溯重建管线（对应 docs/acceptance/CNNC_ACCEPTANCE_PROCESS_RECONSTRUCTION_20260911.md）。

- 规则唯一来源: 仓库 `docs/acceptance/` 4 份文档 + `cnnc_price_algorithms_review_v1`（算法语义）。
- 本目录只包含通用脚本与合成夹具测试；**不含任何客户真实数据**。真实路径通过 `config.local.json`（不入库，模板见 `config.example.json`）注入。

## 结构

| 文件 | 作用 |
|---|---|
| p1_common.py | 归一化（norm_key/norm_price/norm_cell）、duckdb 连接（支持本地 libs 目录）、sha256、parquet 写出 |
| p1_extract.py | F1 抽取层: FINAL 权威表/候选池/阶段标志/剔除清单/模型再校验/评论五层合并 → parquet + p1_stats.json |
| p2_rules.py | F2 规则: 候选合并优先级、问题表判定映射、去极值 R-a..R-e、一物多码 winner（委托 ALGO 包） |
| p2_build_steps.py | F2 重建层: Step1-5 + lineage + stage_stats（25 项运行内断言） |
| p3_format.py | F3 格式: 客户中文列规格、xlsx 流式写出（冻结/筛选/文本格式/分 sheet）、敏感词扫描、Decimal 复算 |
| p3_export_validate.py | F3 导出+验证层: 5 客户 xlsx + 内部产物 + G1-G9 门禁 |

## 用法

```
python -X utf8 p1_extract.py --config config.local.json --only all
python -X utf8 p2_build_steps.py --config config.local.json --stage all
python -X utf8 p3_export_validate.py --config config.local.json --part all
python -X utf8 -m pytest tests -q          # 合成夹具单测（57 项）
```

## 安全边界

- 全部输出只写 config.workdir；历史源文件零写入；FINAL 权威表只读（SHA256 前后校验）。
- 不重跑方差公式全量去极值（规范 §8.3/§23）；去极值只使用真实历史剔除证据。
- 客户文件禁含基准价/内部标签（G6 全量扫描 0 命中门禁）。
