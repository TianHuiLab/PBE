# YJ-3D 单机功率模型训练数据

## 1. 目录与用途

- `units/<unit_id>/train.csv`：用于单机模型拟合的有效样本。
- `units/<unit_id>/eval.csv`：独立留出日期的有效样本；无独立留出时为空表。
- `units/<unit_id>/validation_overlap.csv`：与第二阶段验证窗口重叠的有效样本，
  用 `used_in_model_fit` 标明是否已参与拟合。
- `units/<unit_id>/excluded_samples.csv`：缺测、非有限值、负功率或超筛选上限样本。
- `units/<unit_id>/metadata.json`：单机来源、模型、阈值、样本统计和证据边界。
- `model_support.json`：所有单机的任务/状态可建模性摘要。
- `manifest.json`：输入、代码、单机文件和 SHA-256 校验值。

本数据包只包含 `YJ-3D`。另一颗卫星的数据在其自身的
`unit_training/` 目录中，不共享训练文件或索引。

## 2. 数据字段

| 字段 | 定义 |
|---|---|
| `timestamp` | 北京时间，ISO-8601，显式带 `+08:00` |
| `observed_power_W` | 分钟功率代理，单位 W |
| `target_power_W` | 监督学习标签，等于可用观测功率；缺测不填 0 |
| `valid_power` | 有效功率标记；三个训练/验证 CSV 中均为 `true` |
| `active_label` | 有阈值时，稳态 active 和高功率瞬态均为 `true`；无阈值时为空 |
| `power_band` | 观测功率层：`low`、`active`、`high` 或 `unclassified_single_band` |
| `episode_id` | 连续 active 片段标识；无阈值或非 active 时为空 |
| `elapsed_hours` | active 片段起点后的小时数 |
| `explicit_mode` | 配置存在同机状态列时保留的显式状态 |
| `source_codes` | 构造该单机功率代理的原始遥测点代码 |
| `excluded_reason` | `missing_or_non_numeric`、`negative_power` 或 `above_power_limit` |
| `fit_role` | `train`、`holdout_eval` 或 `excluded` |
| `used_in_model_fit` | 该行是否已参与当前单机模型拟合 |

`low` 是观测到的低功率聚类，不是物理下电真值。没有确认状态映射或阈值的通道
不会伪造 `active/idle`。真正的 `off` 只能由任务计划显式给出。

## 3. 切分规则

有独立整日期留出。留出日期为：

- `2026-07-24`
- `2026-07-28`
- `2026-08-01`
- `2026-08-02`
- `2026-08-05`
- `2026-08-10`
- `2026-09-10`
- `2026-09-11`
- `2026-09-12`

## 4. 数量与完整性

- 单机数：`32`
- 有 active 阈值：`13`
- 无 active 阈值：`19`
- 含高功率瞬态层：`0`
- 源分钟行数：`3447584`
- 有效样本合计（跨单机计数）：`3347358`
- 排除样本合计（跨单机计数）：`100226`

所有计数按“单机 × 分钟”计算，因此不同单机对同一分钟分别计数。缺测、负值和
超筛选上限样本保留在 `excluded_samples.csv`，不会用零填充后混入训练。

## 5. 证据边界

- 单机功率可能是支路电压乘电流的分钟代理，尚不等同于独立硬件功率真值。
- 某些支路可能重叠，单机功率之和未必与整星总负载闭合。
- 本数据包的`unit_id`是物理单机；`plN`的同机A/B支路已按`ab_topology.json`先过滤后求和，设备数量不重复计算。
- 配置中的 `max_power_W` 优先作为筛选上限；未配置时公共过滤器只剔除以
  p99 为基准的极端尾部野值。配置值是建模包络，不等同于铭牌额定功率；
  两类剔除均保留在 `excluded_samples.csv`。
- 功率层与阈值来自本项目既有校准模型，不在导出过程中重新发明阈值。
- `validation_overlap.csv` 只用于诊断训练窗口与第二阶段测试窗口的重叠程度。
- 整日留出日期未进入 `train.csv`，`eval.csv` 中的样本未参与当前模型拟合；这支持单机模型的日期组样本外验证。
