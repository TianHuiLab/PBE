# 三体01星单机功率模型与出阴影期蓄电池预测

> 模型版本 `1.0`；卫星 `SCS-01-01`；校准窗口 `2025-12-26T00:05:00` 至 `2026-11-05T09:45:00`。

## 1. 模型口径

校准数据来自 `energy_v2_components.csv` 的同分钟单机功率代理。仅有支路电流时，它是“母线电压 × 支路电流”的等价功率；有独立电压/电流对时，它是“分钟平均电压 × 分钟平均电流”。缺失、非有限和负值不按零处理。已知 `max_power_W` 时，超过配置筛选上限的点直接排除；否则公共过滤器只去除极端功率尾部野值。所有剔除数量和原始最大值保存在模型 JSON。

每个单机先检测经验功率层；若 active 层内部还存在按比值明显分离的高功率瞬态，则拆成 `low/active/high` 三层并优先采用分层恒定功率；当前高功率分层要求中位功率至少为稳态层的 14 倍。剩余时间多项式使用运行片段分组留出，并在验证中施加与部署完全相同的 active 层 p05–p95 截断。只有截断后 RMSE 至少改善 10%、支撑区间单调且首末误差不超过恒定基线 10% 时才会选为默认模型。

当配置中有同机状态映射时，优先按状态拟合恒功率；没有确认映射的单机由未来任务计划明确给出 `on/off/active/idle` 或 `power_level`。高功率瞬态使用 `power_level: "high"`；状态量本身不重复计入负载。

## 2. 单机模型结果

| 单机 ID | 名称 | 模型 | 工作功率 W | 高功率层 W | 低功率层 W | 样本 | p95 W | 说明 |
|---|---|---|---:|---:|---:|---:|---:|---|
| `electric_propulsion_a` | 电推进配电A | `constant` | 6.440 | 186.050 | 2.982 | 17390 | 6.578 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；检测到 active/high 双状态；active 层取稳态功率，高功率任务必须显式使用 power_level='high'（阈值约 36.305 W）；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；已过滤 1 个超过配置筛选上限（500.0 W）的遥测野值；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `electric_propulsion_b` | 电推进配电B | `constant` | 13.916 | 208.680 | 6.768 | 2009 | 205.042455 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；检测到 active/high 双状态；active 层取稳态功率，高功率任务必须显式使用 power_level='high'（阈值约 54.925 W）；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `compute_nx1_f` | 智算机NX1-F | `constant` | 21.602 | — | 0.284 | 21157 | 21.45087 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `router_main` | 路由器R1主份 | `constant` | 27.467 | — | 0.284 | 21157 | 27.26094 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `router_backup` | 路由器R1备份 | `constant` | 1.849 | — | 0.568 | 21157 | 0.572 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `compute_nx1_g` | 智算机NX1-G | `constant` | 30.456 | — | 0.284 | 21157 | 29.911708 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `compute_g1` | 智算机G1 | `constant` | 28.905 | — | 0.568 | 21157 | 28.312848 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `compute_g2` | 智算机G2 | `constant` | 29.431 | — | 0.284 | 21157 | 28.8009 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `microwave_1a` | 星间微波处理端机1A | `constant` | 91.448 | — | 57.810 | 1847 | 91.448 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；已过滤 1 个超过配置筛选上限（150.0 W）的遥测野值 |
| `microwave_1b` | 星间微波处理端机1B | `constant` | 5.376 | — | 1.420 | 21155 | 5.322 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；已过滤 2 个超过配置筛选上限（150.0 W）的遥测野值；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `microwave_2a` | 星间微波处理端机2A | `constant` | 59.184 | — | 0.852 | 21157 | 59.147 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `microwave_2b` | 星间微波处理端机2B | `constant` | 55.554 | — | 1.704 | 21157 | 5.322 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `laser_processor_1` | 星间激光集成化处理机1 | `constant` | 42.840 | — | 29.744 | 21157 | 49.170077 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `laser_processor_2` | 星间激光集成化处理机2 | `polynomial` | P(0)=1.386 | — | 0.284 | 21157 | 0.36378 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `reaction_wheel_1` | 反作用轮1 | `constant` | 2.821 | — | 1.704 | 21157 | 2.47932 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `reaction_wheel_2` | 反作用轮2 | `constant` | 5.455 | — | 1.704 | 21157 | 2.481673 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `reaction_wheel_3` | 反作用轮3 | `constant` | 4.949 | — | 1.704 | 21157 | 3.124 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `reaction_wheel_4` | 反作用轮4 | `constant` | 3.058 | — | 1.692 | 21157 | 1.849098 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `transponder_a` | 测控应答机A | `constant` | 14.988 | — | — | 21157 | 15.0944 | 未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `transponder_b` | 测控应答机B | `constant` | 4.525 | — | — | 21157 | 4.5568 | 未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `phased_array` | 测控相控阵 | `constant` | 55.973 | — | — | 1424 | 56.757858 | 未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `uplink` | 测控数传上注 | `constant` | 9.262 | — | — | 1427 | 9.4248 | 未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `cxpd_payload` | CXPD载荷 | `constant` | 24.424 | — | 1.390 | 21157 | 1.43 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率 |
| `fiber_gyro` | 光纤陀螺 | `constant` | 3.108 | — | — | 21157 | 3.108 | 未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |
| `science_payload` | 科学载荷总电源 | `mode_constant` | 2.312 | — | 0.000 | 16868 | 2.96957 | 检测到低功率层与工作功率层；任务计划必须明确 on/mode；至少一个显式状态只有少于 5 个样本；已过滤 1 个超过配置筛选上限（50.0 W）的遥测野值；存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推 |

模型数量：`{"constant": 23, "mode_constant": 1, "polynomial": 1}`。低功率层是观测聚类，不等同于物理下电；只有任务计划明确 `off` 时才按 0 W 计。
高功率层是同一 active 状态内按 log 功率比值识别的瞬态观测层；`active` 默认只使用稳态中位数，不能用它代表全部 active 负载。

### 多项式细节

- `laser_processor_2`：系数（按小时幂次升序）`[1.386193987, 0.789221573, 0.145843723]`，支撑 `[0.0, 0.266667] h`，原始留出 RMSE `0.218487` W，运行时截断 `[0.952, 1.692]` W 后 RMSE `0.218487` W，恒定模型 `0.261736` W；首末 MAE `0.263358` W；单调性 `True`。

## 3. 预测输入

业务入口按“时间窗口 + 单机任务”配置。SOC、当前负载、背景负载、任务单机当前功率和日周期太阳阵功率由模型自动补齐。

```json
{
  "time": "2025-12-29T05:21:00+08:00",
  "prediction": {
    "end": "2025-12-29T05:45:00+08:00",
    "step_seconds": 60,
    "phase_segments": [
      {"start": "05:21", "end": "05:45", "phase": "eclipse"}
    ],
    "tasks": [
      {"start": "05:24", "end": "05:29", "unit": "compute_nx1_f"},
      {"start": "05:30", "end": "05:35", "unit": "microwave_2a",
       "run_mode": "active", "after_mode": "off"}
    ]
  }
}
```

规则：

- `time`必须是整分钟；模型精确查询该分钟的 `TMD016/018/019` 和任务单机功率测点。
- `TMD019`与项目V-SOC曲线自动计算初始SOC；`TMD016×TMD018`自动计算当前总负载。
- 只减去任务单机当前功率得到背景负载；未列入任务的单机和非单机负载均留在背景负载中。
- `run_mode`对`[start,end)`生效，省略时为`active`。
- `after_mode`从`end`持续到该单机下一任务开始或预测结束，省略时为`restore`，即恢复`time`时刻的当前遥测功率。
- `unit`和`units`二选一；同一窗口内同模式的单机放在`units`列表，避免重复配置。
- 任务只写`HH:MM[:SS]`时日期继承`time`；跨日必须写完整日期时间。
- `phase_segments`必须连续、有序、无重叠，并完整覆盖`[time,end)`；`phase`可取`sun/eclipse`或阳照/阴影别名。
- 阴影片段太阳阵功率固定为0 W；阳照片段使用历史多项式模型。
- 01星历史目标采用方案B：`P_load(TMD016×TMD018)+P_charge_est`。

## 4. 预测输出

JSON 输出给出：目标时刻 SOC、剩余 Ah、按额定容量×标称电压折算的剩余 kWh、最低 SOC、到 SOC 下限的可用 Ah、太阳能/负载/充放电能量、各单机任务耗能、由单机模型 p05–p95 半范围或多项式交叉验证 RMSE 近似合成的经验功率散布、触底/触顶时长、假设和证据边界。CSV 时间线给出每个步长的负载、净功率、SOC 和单机功率。

电池记账模型为：

```text
E_next = E + eta_charge × max(P_solar - P_load, 0) × dt
E_next = E - max(P_load - P_solar, 0) / eta_discharge × dt
SOC = E / (capacity_Ah × nominal_voltage_V)
```

能量和 SOC 在 `soc_floor` 与 100% 处截断；超过可用电量时记录未供能量。这是用于任务规划的簿记预测，不是电池容量、温度、滞回或 SOH 的在轨验证。

## 6. 复现命令

```powershell
python unit_power_model.py calibrate `
  --component-csv satellites/SCS-01-01/out/energy_v2_components.csv `
  --components-config satellites/SCS-01-01/config/components.json `
  --out-json satellites/SCS-01-01/power_model/unit_power_models.json `
  --out-report satellites/SCS-01-01/power_model/unit_power_model_report.md `
  --out-summary-csv satellites/SCS-01-01/power_model/unit_power_model_summary.csv

python solar_power_calibration.py `
  --result-csv satellites/SCS-01-01/out/energy_v2_result.csv `
  --satellite-id SCS-01-01 `
  --out satellites/SCS-01-01/power_model/solar_power_model.json `
  --out-report satellites/SCS-01-01/power_model/solar_power_model_report.md

$env:CH_ALLOW_PLAINTEXT='1' # 仅在该项目已授权的本地只读连接环境使用
python unit_power_model.py forecast `
  --models satellites/SCS-01-01/power_model/unit_power_models.json `
  --request satellites/SCS-01-01/power_model/example_prediction_input.json `
  --profile-dir satellites/SCS-01-01/config `
  --transport http `
  --solar-model satellites/SCS-01-01/power_model/solar_power_model.json `
  --out-json satellites/SCS-01-01/power_model/example_prediction_output.json `
  --out-csv satellites/SCS-01-01/power_model/example_prediction_timeline.csv
```

## 7. 证据边界

- 单机功率测点可能与其他支路重叠，当前 25 路负载支路和并未与 `TMD018` 总负载闭合。
- 除科学载荷外，本批配置没有已确认的单机状态到功率支路映射；未来任务必须明确单机工况。
- 背景负载由当前总负载减去任务单机当前功率得到，并在窗口中按恒定值保持；未列入任务的热控等负载变化尚未建模。
- 阳照片段多项式只使用片段内经历小时建模；温度、太阳入射角、姿态、S3R工作点和阵列老化未显式建模。
- 太阳阵目标采用用户确认的方案B，`P_charge_est`继续受TMD017测点边界影响，不是独立阵列或充电功率测量。
- 多项式超出历史运行片段支撑时长时按边界截断，再按 active 层的 p05–p95 做运行截断；离线验证使用同一规则。
- `active/high` 是基于观测功率比值的统计分层，不等同于硬件状态真值；高功率任务必须显式提供 `power_level: "high"`。
- 电池端真实净功率、充放电方向和母线损耗仍需独立测量与拓扑核验。
- 经验功率散布取单机 p05–p95 半范围或多项式留出 RMSE 的平方和近似，不是概率置信区间；不同单机误差相关性和背景负载误差均未量化。
