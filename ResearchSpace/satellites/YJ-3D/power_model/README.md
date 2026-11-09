# 云尖3D星功率模型与未来任务预测

本目录参考三体01星流程，但采用云尖3D星专属测点、UTC源时间、
上下板A/B双供电支路和7.84 A放电曲线。

## 输入

```json
{
  "time": "2026-07-24T20:53:00+08:00",
  "prediction": {
    "end": "2026-07-24T21:04:00+08:00",
    "step_seconds": 60,
    "phase_segments": [
      {"start": "20:53", "end": "21:04", "phase": "eclipse"}
    ],
    "tasks": []
  }
}
```

- `time/end`使用北京时间；历史快照适配器转换为源表UTC `timestamp`。
- `phase_segments`必须连续、有序、无重叠并完整覆盖窗口。
- 单机任务支持`run_mode`和`after_mode`，与01星一致。
- 原始审计层保留`pl0_a`、`pl0_b`等支路ID。
- 预测任务使用去重后的物理单机ID：`pl0`、`pl1`等。
- `PLA_n/PLB_n`是同一物理单机的上/下板双供电支路；电流和功率相加，
  电压不求和，设备数量只计一次。

## 太阳阵

```text
P_BCR_out
  = Σ(V_BCR_out,j × I_BCR_out,j)

P_solar_to_bus <= total_load + charge_margin  # optional only

P_solar_diag
  = P_bus - P_battery_signed
```

训练目标采用8路BCR输出侧电压×电流之和。BCR输出是MPPT/DC-DC后的母线侧功率，
与母线负载、电池充放电处于同一功能节点。`P_solar_diag`用于独立交叉核验，
不再额外乘经验效率。

实测BCR输出只作为历史训练标签和测试真值，不是未来预测输入。预测时模型根据
`time`、光照相位、历史拟合关系和未来负载/任务计划输出预测功率。

MPPT模式默认不启用负荷跟随封顶。`total_load+charge_margin`仅保留为可选的保守规划上界。

当前最终模型采用负载感知线性结构：

```text
P_BCR_out ≈ intercept
          + beta_time × elapsed_hours
          + beta_load × predicted_total_load_W
```

未来负载先由背景负载、任务计划和单机模型计算。任务单机功率和活动单机数
也做了候选特征测试，但它们在“历史观测状态→预测状态”时分布偏移明显，未采用。

云尖3D太阳阵控制逻辑为**最大功率点跟踪（MPPT）**，不使用PCDU控制。
硬件说明和同源遥测确认：上下板BCR输出共用，电池与母线负载连接在同一功率节点，
满足`P_BCR_out≈P_bus−P_battery_signed`。8路PV输入电流仍未下行，因此BCR输出侧
是当前可用的最佳太阳功率目标；MPPT输入功率模型留待补齐输入电流后实现。

## 单机

- 原始审计层有54个功率通道：22对PL A/B支路和10个加热主路。
- 预测层合并为32个物理对象：22个PL物理单机和10个加热主路。
- PL_25推进供电和PL_29智算机YX 28V2只有电压，没有功率模型。
- 32个物理模型中有13个在留出集识别出`active/idle`阈值；其余用于
  恒定或背景分解，不应等同于可确认的硬件状态。
- A/B双支路严格求和；任一支路缺测时物理单机功率保持缺测，不填0 W。
- 全部模型仍为观测分钟功率代理，不等于独立硬件功率或连续积分单机能量。

物理单机功率口径：

```text
P_plN = P_plN_A + P_plN_B
```

`P_plN_A`、`P_plN_B`先分别经过各自量程过滤；两边都有效才生成本分钟物理
单机标签。任务计划中的单机ID使用`pl0`、`pl16`等物理ID，不再把同机两路
分别计入活动单机数。

## SOC

使用云尖3D星专属7.84 A放电标定表：

- 表内终点容量37.69 Ah。
- 目前只适合放电方向和曲线电压范围内的条件SOC。
- 历史103,736分钟中，绝大多数电压高于28.18 V，不能用单放电曲线给出全时段绝对SOC。
- 初始SOC越界时输出会保留条件/截断警告，不能解释为真实SOC。

## 统一质量控制和主指标

第二阶段标签生成统一读取`config/telemetry_params.json`量程，并检查：

- `range_violation`：电池/母线电压或电流越出工作簿量程；
- `gap`：相邻分钟缺失、乱序或超过配置的最大积分间隔；
- `phase_transition`：区间跨相位或相位未判定；
- `frozen_sensor`：电流、电压连续完全不变的时间超过配置阈值；
- `duplicate`：重复时间戳；
- `missing`：任一端点缺测。

含无效区间的窗口不生成SOC主标签，原始积分值和“仅有效区间”条件值
分别保留在`label.json`。主评测按指标分别过QC，因此SOC质检失败不会
错误删除已经通过检查的BCR能量样本。

BCR主能量与所用留出模型使用同一99.9%缩尾上限，原始尖峰另列
`solar_energy_raw_kWh`和`bcr_above_cap_count`。主指标、原始诊断、
饱和/非饱和、日期聚类bootstrap和逐指标QC样本数写入
`stage2_testset/evaluation_summary.json`。

背景负载候选按“前60分钟中位→前一天同相位时钟窗口中位→固定快照”
分层回退。当前未配置真实轨道周期，前一日回退只使用同相位和同一
时钟附近时刻作为代理，不声称已完成轨道相位验证。

`ab_topology.json`已依据通用平台供配电图3/图4、正文第33页和软件取数建议
第76页批准：同编号A/B通道归入同一物理单机，`aggregation_mode=sum`。
`stage2_audit`现报告22对全部已解析、禁止聚合数为0。BCR结构审计仍明确标记
尚未获得的轨道、入射角、温度、SOC和电池充电接受能力输入。

## 文件

- `forecast_components.json`：A/B通道和加热支路配置。
- `unit_power_minute.csv`：107,737分钟、54个通道的功率代理。
- `ab_topology.json`：22对PL A/B到物理单机的批准聚合契约。
- `ab_physical_aggregation_report.md`：拓扑证据、聚合口径和冻结窗口A/B对比。
- `physical_unit_components.json`：32个物理单机的模型配置。
- `physical_unit_power_minute.csv`：保留54条原始支路并新增32条物理单机功率。
- `physical_unit_models.json` / `physical_unit_holdout_models.json`：
  物理单机全量/日期留出模型。
- `unit_power_models.json`：54个通道的拟合模型。
- `unit_power_model_summary.csv`：逐通道摘要。
- `unit_power_model_report.md`：逐通道分析。
- `solar_power_model.json`：云尖3D太阳阵历史多项式。
- `solar_power_model_report.md`：目标口径、误差和限制。
- `solar_power_model_holdout_20260728.json` / `_report.md`：
  排除10个验证片段涉及日期的BCR输出留出模型。
- `../bcr_output_minute.csv`：8路BCR同源输出功率求和的分钟数据。
- `v_soc_curve.json`：7.84 A放电曲线转换后的反查点。
- `example_prediction_input.json` / `example_prediction_output.json`：示例。
- `validation_sun_20260728_1622_report.md`：留出阳照片段SOC/功率验证。
- `validation_sun_20260728_1622_summary.json`：留出验证机器摘要。
- `validation_sun_20260728_1622_bcr_summary.json`：同窗口8路BCR输出交叉核验。
- `solar_energy_error_analysis_20260728.md`：太阳阵能量误差分解与根因分析。
- `segment_backtest_report.md`：10个多季节/时长/负载片段的分组留出回测。
- `segment_backtest_results.csv` / `segment_backtest_summary.json`：
  逐片段和分方案回测结果。
- `battery_remaining_validation_report.md`：电池剩余SOC/Ah/kWh专项验收。
- `battery_remaining_validation.csv` / `_summary.json`：
  逐片段ΔSOC、ΔAh和额定ΔkWh误差。
- `battery_remaining_metrics_report.md`：按目标SOC、剩余容量、太阳阵能量、
  负载能量四项指标组织的验收报告。
- `battery_remaining_metrics.csv` / `_summary.json`：
  四项指标逐片段结果。
- `model_improvement_roadmap.md`：已有工作、误差来源和降MAE优化优先级。
- `power_outlier_filter_report.md`：公共野值过滤口径、逐通道剔除审计和清洗后复算结果。
- `mppt_telemetry_audit.md`：MPPT输入电压/电流和BCR可用测点核查。
- `bcr_topology_audit.md`：BCR输出、母线、电池的功能拓扑与同源遥测验证。
- `mppt_bcr_comparison_report.md`：旧母线电压代理与BCR输出目标的留出A/B结果。
- `mppt_bcr_comparison_results.csv` / `_summary.json`：逐片段新模型结果。
- `load_aware_comparison_report.md`：时间一阶与未来负载特征模型闭环A/B。
- `load_aware_comparison_results.csv` / `_summary.json`：负载感知逐片段结果。
- `mppt_proxy_validation_20260728.csv` / `_summary.json`：
  `TMEA007×8路PV输入电压均值`的临时MPPT功率近似与BCR输出效率试算。
- `unit_task_validation_report.md`：从历史遥测提取任务状态后的单机模型留出验证。
- `unit_task_validation_results.csv` / `_summary.json`：
  `tasks=[]`与历史任务计划的A/B结果。
- `v_soc_directional_provisional.json` / `v_soc_directional_provisional_report.md`：
  方向V-SOC A/B候选，不用于生产。
- `stage2_audit.json`：饱和/非饱和SOC分栏、bootstrap区间、有效日期数、
  背景负载滚动A/B、A/B共活跃及BCR异常值审计。
- `stage2_manifest.json`：统一第二阶段输入/代码哈希、验证日期、
  模型选择候选与最终指标。
- [`../unit_training/`](../unit_training/README.md)：32个物理单机的
  train/eval/验证重叠/排除样本；9个留出日期只进入 eval，不进入 train。
- `representative_case_yj/`：任务感知代表性案例，包含15条历史提取任务、
  预测输入/输出、预测时间线及由TMEA006积分得到的实际条件SOC曲线。
  该目录中的V-SOC端点72.737%仅作交叉参考，不用于逐分钟实际曲线。
- [完整标准化测试集](../stage2_testset/README.md)：10个验证窗口的简化输入、
  观测标签、基线预测、任务计划和哈希清单。
- [`yj3d_forecast.py`](../../../yj3d_forecast.py)：历史文件或ClickHouse精确分钟驱动的预测入口。

## 复现

```powershell
python yj3d_power_forecast.py fetch-units `
  --profile-dir satellites/YJ-3D/config `
  --out-dir satellites/YJ-3D/power_model

python yj3d_power_forecast.py fetch-bcr `
  --profile-dir satellites/YJ-3D/config `
  --out-dir satellites/YJ-3D

python unit_power_model.py calibrate `
  --component-csv satellites/YJ-3D/power_model/unit_power_minute.csv `
  --components-config satellites/YJ-3D/power_model/forecast_components.json `
  --satellite-id YJ-3D `
  --out-json satellites/YJ-3D/power_model/unit_power_models.json `
  --out-report satellites/YJ-3D/power_model/unit_power_model_report.md `
  --out-summary-csv satellites/YJ-3D/power_model/unit_power_model_summary.csv

python physical_unit_model.py `
  --branch-data satellites/YJ-3D/power_model/unit_power_minute.csv `
  --branch-config satellites/YJ-3D/power_model/forecast_components.json `
  --branch-models satellites/YJ-3D/power_model/unit_task_holdout_models.json `
  --topology satellites/YJ-3D/power_model/ab_topology.json `
  --out-dir satellites/YJ-3D/power_model `
  --satellite-id YJ-3D `
  --exclude-dates-file satellites/YJ-3D/power_model/validation_split.json `
  --model-name physical_unit_holdout_models.json `
  --report-name physical_unit_holdout_report.md

# 生产全量模型：重复以上命令，去掉 --exclude-dates-file，
# 并改用 --model-name physical_unit_models.json
# 和 --report-name physical_unit_model_report.md。

python yj3d_power_forecast.py build-solar `
  --profile-dir satellites/YJ-3D/config `
  --power-minute satellites/YJ-3D/data_power_minute.csv `
  --bcr-minute satellites/YJ-3D/bcr_output_minute.csv `
  --unit-minute satellites/YJ-3D/power_model/physical_unit_power_minute.csv `
  --unit-model satellites/YJ-3D/power_model/physical_unit_models.json `
  --out-dir satellites/YJ-3D/power_model

python yj3d_forecast.py `
  --source-mode clickhouse `
  --profile-dir satellites/YJ-3D/config `
  --request satellites/YJ-3D/power_model/example_prediction_input.json `
  --unit-model satellites/YJ-3D/power_model/physical_unit_models.json `
  --solar-model satellites/YJ-3D/power_model/solar_power_model.json `
  --v-soc-curve satellites/YJ-3D/power_model/v_soc_curve.json `
  --design-params satellites/YJ-3D/power_model/design_params.json `
  --out-json satellites/YJ-3D/power_model/example_prediction_output.json `
  --out-csv satellites/YJ-3D/power_model/example_prediction_timeline.csv

python stage2_audit.py --profile-dir satellites/YJ-3D

python unit_training_export.py --profile-dir satellites/YJ-3D
```

## 当前模型结果

- 54个原始供电/加热支路保留独立审计模型；22对同名PL A/B按已批准拓扑
  合并为22个物理PL模型，再与10个加热主路组成32个预测对象。
- 太阳阵目标采用8路BCR输出功率和`Σ(Vout×Iout)`。最终模型为
  “时间一阶+未来预测负载”，全历史特征组合CV RMSE约14.31 W；
  纯时间一阶约24.07 W。
- 原始10片段历史任务A/B诊断中，负载特征模型的目标SOC MAE由0.856降到
  **0.462个百分点**，剩余容量MAE由0.323降到**0.174 Ah**，太阳阵能量
  MAE由17.74%降到**15.42%**。
- 统一质检后，3/10窗口因电池或母线越量程退出SOC/负载主指标；7个通过
  SOC质检窗口的MAE为**0.149个百分点**、剩余容量MAE为**0.056 Ah**。
  该组含4个饱和窗口（误差为零），3个非饱和窗口SOC MAE为**0.348个百分点**。
  原始积分诊断仍保留，目标SOC MAE为0.472个百分点、剩余容量MAE为0.178 Ah。
- BCR主能量按所用留出模型的99.9%缩尾上限计算，清洗后10窗口MAE为
  **11.35%**；原始尖峰诊断MAE为13.12%。2026-09-11 00:59窗口的原始太阳能量误差约
  −62.85%，缩尾主指标约−45.26%，尾部仍需单列。
- 2026-07-28单片段负载感知模型目标SOC预测76.38%，观测72.74%，
  偏差约**+3.64个百分点**；BCR能量误差约**7.47%**。
- 电池剩余电量专项验收：唯一V-SOC绝对窗口的SOC误差约2.93个百分点、
  1.105 Ah、0.0279 kWh；10片段ΔSOC MAE约3.78个百分点/1.424 Ah，
  但p95约10.76个百分点/4.054 Ah。当前不满足严格的尾部精度验收。
- 原始诊断10片段目标SOC p95为1.531个百分点、剩余Ah p95为0.577 Ah；
  QC通过窗口的目标SOC p95为0.583个百分点。09-11 00:59仍存在显著太阳能量
  尾部误差，不能把总体均值当作所有任务的保证。
- MPPT侧8路PV输入电压约37–43 V，但缺少8路输入电流；当前用BCR输出侧功率替代，
  后续补齐PV输入电流后才能建立真正的MPPT输入功率模型。
- 从历史遥测提取真实`active/idle`状态变化作为任务计划后，在保持太阳阵模型不变的
  A/B中，旧代理目标SOC MAE由3.418降到0.875个百分点，剩余容量MAE由1.288降到0.330 Ah；
  新BCR目标进一步为0.856个百分点、0.323 Ah，
  负载能量MAE由22.34%降到8.12%。单机任务模型具有明确降MAE收益。
- 2026-11-05 起，单机建模统一使用公共功率过滤器：已知工作簿量程时按
  `max_power_W`硬限制，未知量程时只剔除以p99为基准的极端尾部。当前54个
  支路合计排除5,801个野值，原始最大值和剔除数量逐通道记入模型JSON。
  22对A/B随后按物理单机求和；冻结测试集负载能量MAE为3.88%（7个QC窗口）、
  诊断口径4.16%（10窗口），太阳阵能量MAE分别为11.35%和13.12%。
- 充电/放电方向V-SOC中位电压差约1.07 V，但方向曲线使总体SOC映射MAE由
  3.21个百分点恶化到10.34个百分点，因此不替换7.84 A放电单曲线。

文件中的地影示例为2026-07-24 20:53–21:04：初始SOC 99.69%，预测目标SOC
96.27%，按终点`TMEA005`映射的观测SOC为93.92%，偏差约+2.35个百分点。
该示例同样只用于验证输入/输出闭环。
