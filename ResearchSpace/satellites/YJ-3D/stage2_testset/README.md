# YJ-3D 第二阶段测试集

- 卫星：`YJ-3D`
- 案例数：`10`
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

云尖星主 SOC 为初始 V-SOC 锚点加 TMEA006 梯形电流积分，仅当全部区间通过量程、时间连续性、相位和冻结检查时形成主标签。原始值和有效区间条件值分别保留。BCR 主能量使用所用留出模型训练时的 99.9% 缩尾上限，原始尖峰另列为异常事件诊断。任务中的单机为去重后的物理PL对象；同编号A/B支路电流和功率相加。所有标签质量信息均在 `label_quality` 中显式列出。

## 评测口径

主指标使用 `frozen_snapshot`，背景策略为
`window_start_snapshot`。共享评测按日期聚类bootstrap，
同日多窗口不会被视为完全独立样本。
