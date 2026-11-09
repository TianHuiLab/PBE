# 云尖3D星太阳阵功率历史拟合

## 目标口径

```text
P_solar_to_bus = Σ(V_BCR_out,j × I_BCR_out,j)
P_solar_to_bus <= total_load + charge_margin  # optional only
P_solar_diag = P_bus - P_battery_signed
```

训练目标采用8路BCR输出侧电压×电流之和。BCR输出是MPPT/DC-DC后的母线侧功率，与母线负载、电池充放电处于同一功能节点；`P_solar_diag`仅作交叉核验。

## 模型

- 类型：`load_aware_linear`
- 阶数：`1`
- 特征：`['elapsed_hours', 'load_W']`；截距/系数 `12.193536799` / `[-0.015911383, 0.945835082]`
- 特征选择：`time_plus_load`；`fixed policy selected by integrated holdout forecast`。
- 时间支撑：`[0.0, 367.133333] h`，超出后截断。
- 阴影片段：固定0 W。
- 母线传输效率：不额外施加；BCR输出目标已经位于母线侧。
- 负荷跟随充电余量：`None` W；MPPT模式默认不启用，仅可作为保守规划上界。

## 验证

- 有效阳照分钟：`95,499`
- 阳照片段：`129`
- 阴影分钟：`1,059`
- 目标p05/p50/p95：`90.596529` / `112.585749` / `152.34` W
- 恒定模型RMSE：`21.050661` W
- 各阶RMSE：`{"1": 20.690508, "2": 20.811328, "3": 22.314911}`
- 选中RMSE：`8.447105` W
- 负载/任务特征候选RMSE：`{"time_load_active_count": 8.409271, "time_load_task_power": 8.252587, "time_load_task_power_active_count": 8.017638, "time_plus_load": 8.447105}`

## 证据边界

- 太阳阵控制逻辑为MPPT；`P_BCR_out`是MPPT/DC-DC后的输出侧功率，不是`V_PV×I_PV`输入功率。
- 8路PV输入电压存在，但8路PV输入电流未在现有遥测中提供。
- BCR输出与母线/电池的功能拓扑已由硬件说明和同源遥测验证；精确针脚网络仍需单线图核准。
- 负荷跟随封顶是任务规划约束，不是电池实际充电接受能力测量；余量为0表示不预测未确认的太阳充电余量。
- `TMEA004`负载覆盖和`TMEA006`方向仍需电路资料最终核准。
- 多项式未显式包含温度、太阳入射角、SAA/姿态、分流工作点和阵列老化。
- 超出历史光照时长范围的预测按训练边界截断。
