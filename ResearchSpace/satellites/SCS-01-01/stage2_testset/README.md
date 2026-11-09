# SCS-01-01 第二阶段测试集

- 卫星：`SCS-01-01`
- 案例数：`25`
- 输入：`inputs.jsonl`；每行含 `case_id` 和简化预测请求。
- 标签：`labels.jsonl`；只含观测得到的标签，不含模型预测。
- 模型支持：`model_support.json`。
- 主评测：`evaluation_summary.json`；逐案例指标：`evaluation_cases.csv`。
- 推荐用途：批量推理读取 `inputs.jsonl`，逐案例对照 `label.json`。

## 输入边界

每个 `input.json` 只提供时间、`prediction.end`、步长、连续相位分段和任务计划。
太阳阵功率、背景负载和实际标签不在输入中。
`tasks` 中普通工作使用 `run_mode: "active"`；若模型检测到分离高功率层，
对应任务额外提供 `power_level: "high"`，稳态任务不携带该字段。

## 标签边界

三体01星 SOC 为 TMD019 逐分钟 V-SOC 映射，属于条件参考。所有标签质量信息均在 `label_quality` 中显式列出。

## 评测口径

主指标使用 `frozen_snapshot`，背景策略为
`window_start_snapshot`。共享评测按日期聚类bootstrap，
同日多窗口不会被视为完全独立样本。
