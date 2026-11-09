# SCS-01-01 单机功率模型与出阴影期电池预测

本目录保存三体01星下一步分析的模型产物。所有单机模型都来自
`out/energy_v2_components.csv` 的逐分钟功率代理。未来预测的业务入口只需
`time/end/step_seconds/phase_segments/tasks`；SOC、当前负载、背景负载、任务单机当前功率
和太阳阵功率由模型自动补齐。

## 文件

| 文件 | 用途 |
|---|---|
| [unit_power_holdout_models.json](unit_power_holdout_models.json) | 排除16个验证日期后训练的25个单机模型；主评测使用 |
| [unit_power_holdout_summary.csv](unit_power_holdout_summary.csv) | 留出单机的一行摘要 |
| [unit_power_holdout_report.md](unit_power_holdout_report.md) | 留出单机逐项分析、输入输出说明和证据边界 |
| [validation_split.json](validation_split.json) | 16个整日留出日期及来源 |
| [extended_data_coverage.json](extended_data_coverage.json) | 扩展时间窗的季节、相位和5路温度原值覆盖 |
| [prediction_input.schema.json](prediction_input.schema.json) | 预测输入 JSON Schema |
| [prediction_output.schema.json](prediction_output.schema.json) | 预测输出 JSON Schema |
| [unit_power_models.schema.json](unit_power_models.schema.json) | 模型库 JSON Schema |
| [19 单机模型适用性评估](../../../19_三体01星单机模型适用性评估与优化.md) | 25个支路的模型选择、过滤、供电轨修正、误差影响和后续优先级 |
| [solar_power_model.json](solar_power_model.json) | 01星历史太阳阵功率代理多项式参数和验证指标 |
| [solar_power_model.schema.json](solar_power_model.schema.json) | 太阳阵模型JSON Schema |
| [solar_power_model_report.md](solar_power_model_report.md) | 方案B目标口径、拟合系数、误差与证据边界 |
| [example_prediction_input.json](example_prediction_input.json) | 以 2025-12-29 05:21 当前遥测构造的出阴影期任务计划样例 |
| [example_prediction_output.json](example_prediction_output.json) | 样例的目标 SOC、剩余 Ah/kWh、能量、经验散布和警告 |
| [example_prediction_timeline.csv](example_prediction_timeline.csv) | 样例逐分钟负载、净功率、SOC 和单机功率 |
| [example_sun_eclipse_input.json](example_sun_eclipse_input.json) | 含阳照、阴影两个连续相位窗口的样例输入 |
| [example_sun_eclipse_output.json](example_sun_eclipse_output.json) | 阳照多项式充电后进入阴影的样例结果 |
| [example_sun_eclipse_timeline.csv](example_sun_eclipse_timeline.csv) | 阳照—阴影样例逐分钟结果 |
| [solar_validation_20251229_0539_report.md](solar_validation_20251229_0539_report.md) | 05:39–06:10阳照留出验证及SOC误差分解 |
| [solar_validation_20251229_0539_summary.json](solar_validation_20251229_0539_summary.json) | 验证指标机器摘要 |
| [solar_power_model_holdout_20251229.json](solar_power_model_holdout_20251229.json) | 排除2025-12-29全天后的太阳阵留出模型 |
| [stage2_audit.json](stage2_audit.json) | bootstrap、有效日期、窗口/时长/能量加权指标及审计边界 |
| [stage2_manifest.json](stage2_manifest.json) | 输入/代码哈希、验证日期、模型选择与最终指标 |
| [单机训练数据](../unit_training/README.md) | 25个单机的 train/eval/验证重叠/排除样本、字段定义和 SHA-256 清单 |
| [representative_case_scs/](representative_case_scs/) | 云尖同构的简化输入：时间、相位、2条历史提取任务；太阳阵由留出模型内部计算 |
| [完整标准化测试集](../stage2_testset/README.md) | 25个冻结快照窗口（19阳照+6地影）的简化输入、标签、预测和哈希 |
| [stage2_testset/evaluation_summary.json](../stage2_testset/evaluation_summary.json) | 主评测、按相位分栏和按日期聚类bootstrap |
| [batch_validation_report.md](batch_validation_report.md) | 19个历史逐分钟背景回放窗口；仅作上界/诊断 |
| [batch_validation_summary.json](batch_validation_summary.json) | 历史背景回放总体及分层摘要 |
| [batch_validation_windows.csv](batch_validation_windows.csv) | 历史背景回放窗口的SOC、太阳能量和负载误差 |
| [batch_validation_holdout_solar_model.json](batch_validation_holdout_solar_model.json) | 扩展数据训练且排除16个验证日期的太阳阵模型 |
| [v_soc_directional_provisional_report.md](v_soc_directional_provisional_report.md) | 充电/放电方向V-SOC条件校准与A/B结论 |
| [v_soc_directional_provisional.json](v_soc_directional_provisional.json) | 方向V-SOC候选曲线，不替换生产单曲线 |
| [unit_power_model.py](../../../unit_power_model.py) | 校准、简化入口、任务语义展开和电池预测 |
| [future_forecast_runtime.py](../../../future_forecast_runtime.py) | ClickHouse精确分钟遥测快照适配器 |
| [solar_power_provider.py](../../../solar_power_provider.py) | 历史多项式、eclipse-only和外部太阳阵功率模块接口 |
| [solar_power_calibration.py](../../../solar_power_calibration.py) | 从历史结果校准太阳阵多项式 |

## 模型选择

- 先在每个单机上检测低功率层和工作功率层，低层不自动解释为物理下电。
  若 active 层内部还存在按 log 功率比值至少 14 倍分离的高功率瞬态，再拆出 `high` 层。
- 恒定模型默认使用稳态工作层中位数；任务明确 `off` 时才按 0 W。
  高功率任务必须显式写 `power_level: "high"`，不能把稳态 13.916 W 当成全部
  电推进 B active 负载。
- 电推进 A/B 是同一系统的两路配电。若某一路在训练日期中没有 high 样本、
  另一路有，则只在缺失侧借用同组的高功率先验；本机一旦有 high 观测，
  始终优先使用本机证据。
- 多项式候选使用每次连续运行开始后的小时数。只有以运行片段为分组的留出集
  在**与运行时相同的 active 层 p05–p95 截断后** RMSE 仍至少改善 10%，
  且支撑区间单调、首末误差不超过恒定基线 10%，才选择多项式。
- 科学载荷采用已配置的同机状态表。其余单机缺少经确认的状态到功率支路映射，
  因此必须由未来任务中的 `run_mode` 或 `power_level` 指定运行层。

本批 25 个单机中：23 个恒定/分层功率模型、1 个稳定多项式模型、1 个状态表模型。
电推进 A/B 都有 `low/active/high` 三层；B 的本地训练高功率中位为 215.195 W、
阈值为 55.787 W，A 在该留出切分中借用这一同机先验。光纤陀螺改按5 V轨
`TMD059×TMD067` 计算，数传上注改按12 V轨 `TMD062×TMD056` 计算。
25个负载通道均配置筛选上限，在拟合前过滤超限野值；上限是建模包络，不是铭牌功率。
拟合数据已扩展至 `2026-11-05T09:45:00`（北京时间），并整日排除
`validation_split.json` 中的16个验证日期。

## 预测输入

```json
{
  "time": "2025-12-29T05:21:00+08:00",
  "prediction": {
    "end": "2025-12-29T05:45:00+08:00",
    "step_seconds": 60,
    "phase_segments": [
      {
        "start": "05:21",
        "end": "05:45",
        "phase": "eclipse"
      }
    ],
    "tasks": [
      {
        "start": "05:24",
        "end": "05:29",
        "unit": "compute_nx1_f"
      },
      {
        "start": "05:30",
        "end": "05:35",
        "unit": "microwave_2a",
        "run_mode": "active",
        "after_mode": "off"
      },
      {
        "start": "05:36",
        "end": "05:40",
        "unit": "electric_propulsion_b",
        "run_mode": "active",
        "power_level": "high"
      }
    ]
  }
}
```

任务语义：

- `run_mode`对`[start,end)`生效，省略时默认为`active`。
- 高功率瞬态使用`power_level: "high"`；省略`power_level`表示稳态`active`。
- `after_mode`从`end`持续到该单机下一任务开始或预测结束；省略时默认为
  `restore`，即恢复`time`时刻的当前功率。
- `unit`和`units`二选一。同一时间窗口和模式的设备放入`units`数组。
- 只写`HH:MM[:SS]`时日期继承`time`；跨日任务写完整时间。
- 同一单机任务不能重叠。
- `phase_segments`必须连续、有序、不重叠并覆盖`[time,end)`；`phase`取`sun`或`eclipse`。

模型在`time`精确分钟查询`TMD016/TMD018/TMD019`和任务单机功率测点：

- SOC由`TMD019`与项目V-SOC曲线计算；
- 当前总负载由`TMD016 × TMD018`计算；
- 背景负载由当前总负载减去任务单机当前功率得到；
- 未列入任务的单机和非单机负载都留在背景负载中；
- 阴影片段太阳阵功率固定为0 W；
- 阳照片段用`solar_power_model.json`中的多项式按阳照片段内经历小时估算；
- 01星历史目标采用方案B：`P_load(TMD016×TMD018)+P_charge_est`。

若`time`精确分钟不存在，或任务单机的当前功率缺测，程序会报错并列出缺少的测点，
不会用零值或最近样本静默替代。

## 预测输出

JSON 输出包含：

- `target_end_state.soc`：出阴影目标时刻 SOC；
- `target_end_state.remaining_Ah`：剩余安时；
- `target_end_state.remaining_energy_nominal_kWh`：按额定容量×28 V 标称电压折算的剩余能量；
- `target_end_state.available_to_soc_floor_Ah`：到 5% SOC 下限的可用安时；
- `target_end_state.soc_half_spread_from_power_models`：单机模型经验散布折算的 SOC 半范围；
- `energy.unit_energy_kWh`：各单机任务耗能；
- `energy.battery_discharge_stored_kWh`：按放电效率折算的电池储存侧减少量；
- `limits`、`warnings`、`assumptions`、`evidence`：截断、假设和未建模项。
- `derived_state`：自动查询的电压、总负载、相位、任务单机当前功率和背景负载；
- `snapshot`：ClickHouse来源、查询分钟和行数等追踪信息。

CSV 时间线包含 `solar_power_W`、`background_load_W`、`unit_load_W`、
`total_load_W`、`soc_start`、`soc_end` 及每个单机的 `P_<unit_id>_W`。

## 复现

在 `ResearchSpace` 目录执行：

```powershell
python unit_power_model.py calibrate `
  --component-csv satellites/SCS-01-01/out/energy_v2_components.csv `
  --components-config satellites/SCS-01-01/config/components.json `
  --exclude-dates-file satellites/SCS-01-01/power_model/validation_split.json `
  --out-json satellites/SCS-01-01/power_model/unit_power_holdout_models.json `
  --out-report satellites/SCS-01-01/power_model/unit_power_holdout_report.md `
  --out-summary-csv satellites/SCS-01-01/power_model/unit_power_holdout_summary.csv

python solar_power_calibration.py `
  --result-csv satellites/SCS-01-01/out/energy_v2_result.csv `
  --satellite-id SCS-01-01 `
  --exclude-dates-file satellites/SCS-01-01/power_model/validation_split.json `
  --out satellites/SCS-01-01/power_model/batch_validation_holdout_solar_model.json `
  --out-report satellites/SCS-01-01/power_model/batch_validation_holdout_solar_report.md

$env:CH_ALLOW_PLAINTEXT='1' # 仅在该项目已授权的本地只读连接环境使用
python unit_power_model.py forecast `
  --models satellites/SCS-01-01/power_model/unit_power_holdout_models.json `
  --request satellites/SCS-01-01/power_model/example_prediction_input.json `
  --profile-dir satellites/SCS-01-01/config `
  --transport http `
  --solar-model satellites/SCS-01-01/power_model/batch_validation_holdout_solar_model.json `
  --out-json satellites/SCS-01-01/power_model/example_prediction_output.json `
  --out-csv satellites/SCS-01-01/power_model/example_prediction_timeline.csv

python batch_validate_forecast.py `
  --result-csv satellites/SCS-01-01/out/energy_v2_result.csv `
  --components-csv satellites/SCS-01-01/out/energy_v2_components.csv `
  --unit-model satellites/SCS-01-01/power_model/unit_power_holdout_models.json `
  --v-soc-curve satellites/SCS-01-01/config/v_soc_curve.json `
  --design-params satellites/SCS-01-01/config/design_params.json `
  --out-dir satellites/SCS-01-01/power_model

python battery_soc_calibration.py `
  --result-csv satellites/SCS-01-01/out/energy_v2_result.csv `
  --v-soc-curve satellites/SCS-01-01/config/v_soc_curve.json `
  --design-params satellites/SCS-01-01/config/design_params.json `
  --out satellites/SCS-01-01/power_model/v_soc_directional_provisional.json `
  --out-report satellites/SCS-01-01/power_model/v_soc_directional_provisional_report.md

python stage2_audit.py --profile-dir satellites/SCS-01-01

python stage2_testset.py --profile-dir satellites/SCS-01-01

python stage2_coverage.py `
  --result-csv satellites/SCS-01-01/out/energy_v2_result.csv `
  --minute-csv satellites/SCS-01-01/data_power_minute.csv `
  --profile-dir satellites/SCS-01-01/config `
  --out satellites/SCS-01-01/power_model/extended_data_coverage.json

python unit_training_export.py --profile-dir satellites/SCS-01-01
```

定轨/太阳阵功率算法作为可信本地模块提供，导出：

```python
def build_profile(start, end, step_seconds, snapshot=None):
    return [
        {"start": start, "end": end, "power_W": 0.0},
    ]
```

通过`--solar-module path/to/orbit_provider.py`接入。只靠定轨得到阳照/阴影还不等于
得到太阳阵母线功率；阳照段还需要阵列指向、太阳入射角、温度和工作点模型。

## 当前样例结果

样例窗口为 `2025-12-29 05:21–05:45`，`phase_segments`标明全程阴影。模型自动查询到电池电压27.99 V、
当前总负载219.9814 W和阴影区状态；初始SOC为0.8619。历史太阳阵模型在阴影片段生成
0 W功率，示例任务下目标时刻SOC为0.8065，剩余52.426 Ah / 1.468 kWh。
这只是任务规划样例。

阳照—阴影演示样例从05:00开始，05:00—05:21使用多项式太阳阵功率，05:21—05:45为阴影；
该相位安排仅用于演示跨相位闭环，不是从遥测中抽取的实际边界。
自动快照初始SOC为0.9148，期间太阳阵入母线能量0.1387 kWh，目标时刻SOC为0.9514，
剩余61.841 Ah。该结果说明闭环能够处理跨光暗相位，但不构成真实轨道或飞行功率验证。

留出验证选择2025-12-29 05:39–06:10连续阳照窗口，并从训练集排除该日。预测目标SOC为
91.0309%，观测为91.4815%，偏差**-0.4506个百分点**；预测太阳阵能量0.1890 kWh，
观测代理能量0.2224 kWh，低15.02%。误差分解显示闭环结果存在负载误差的部分抵消，
详见[验证报告](solar_validation_20251229_0539_report.md)。

冻结快照主评测使用同一16个验证日期下的19个阳照窗口，并增加6个地影窗口。
25窗口SOC MAE为1.9516个百分点，其中阳照1.2778、地影4.0853；
太阳能量MAE（阳照适用）22.928%，负载能量MAE 15.5210%。
历史逐分钟背景回放仍保留19个阳照窗口，其SOC MAE为1.4601个百分点，
但该口径提前使用未来背景，只作为上界/诊断值，不与主指标混用。
方向V-SOC候选中充电/放电中位电压差0.4725 V，但充电SOC映射MAE由1.8688恶化到
2.9080个百分点，因此**不替换生产曲线**。

## 不能据此声称

- 25 路支路只是分钟功率代理，彼此可能重叠，并不等于 `TMD018` 总负载的完整分解；
- 除科学载荷外，当前未从遥测大表证实单机状态到功率支路的映射；
- 未建模温度、倍率、充电滞回、老化、太阳入射角、S3R 工作点和未列负载；
- 电池端独立净功率、充放电方向、母线损耗和物理闭合仍未验证。
- 新增TMD079–083温度原值尚未确认单位和比例换算，也未进入功率/SOC模型。
