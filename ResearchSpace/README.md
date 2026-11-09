# 三体星卫星整星能源分析项目

## 当前宽表评估（2026-11-05）

01、02 星统一以 `chdb_3body.master_satellite_01/02` 宽表为来源，按各自 `spacecraft_id` 和星上北京时间取数。配置、输入、摘要、图表和分析报告均放在带星号的目录；根目录的 `data_power_*.csv`、`out/` 和 `framework_documentation_current.html` 是此前 01 星长表快照，不作为当前宽表结论。

| 卫星 | 配置与分析 | 离线图表报告 | 观测分钟 | 结论边界 |
|---|---|---|---:|---|
| SCS-01-01 | [01 星宽表分析](satellites/SCS-01-01/能源评估分析_SCS-01-01.md) | [01 星图表](satellites/SCS-01-01/framework_documentation_SCS-01-01.html) | 21,157 | 扩展至2026-11-05；新增SON、地影和5路温度原值覆盖 |
| SCS-01-02 | [02 星宽表分析](satellites/SCS-01-02/能源评估分析_SCS-01-02.md) | [02 星图表](satellites/SCS-01-02/framework_documentation_SCS-01-02.html) | 15,878 | 电池曲线待核；SOC/DoD/评分为条件试算 |
| 云尖3D星（YJ-3D） | [云尖3D宽表分析](satellites/YJ-3D/能源评估分析_YJ-3D.md) | [云尖3D图表](satellites/YJ-3D/framework_documentation_YJ-3D.html) | 103,736 | 独立代号与 UTC 时间源；7.84 A 标定曲线仅支持匹配分钟的条件 SOC/DoD，不给健康评分 |

01星下一步的逐单机功率建模与出阴影期电池预测已落盘：
[日期留出单机模型库](satellites/SCS-01-01/power_model/unit_power_holdout_models.json)、
[逐单机分析与输入输出说明](satellites/SCS-01-01/power_model/unit_power_holdout_report.md)、
[快速使用说明](satellites/SCS-01-01/power_model/README.md)、
[25个单机的训练/留出/排除样本](satellites/SCS-01-01/unit_training/README.md)、
[单机模型适用性评估与优化](19_三体01星单机模型适用性评估与优化.md)。

两星的 `telemetry_params.json`、`components.json`、`design_params.json` 和 `v_soc_curve.json` 分别位于 `satellites/SCS-01-01/config/` 与 `satellites/SCS-01-02/config/`。02 星遥测大表与 01 星的能源代号一致；7 个温度标定系数不同，不参与本轮功率计算。两星报告均完整绘出 `TMH025` 的分钟最小、均值、最大数据库原值，不按 100 A 筛选。02 星原值的工程换算尚待核对，因此其正式电池端功率和独立物理闭合暂不可计算；日照和地影附近另以独立纵轴绘出电池电压乘未筛选原值的条件试算，以便检查量级矛盾。

云尖3D星使用 `chdb_3D.yc_history` 宽表，时间列为 UTC 的 `timestamp`，由 `yj3d_assess.py` 转为北京时间并按唯一时间戳去重。能源测点改为 EPS A 路 `TMEA003–007`，光照标志为 `TMZS003`；[独立配置](satellites/YJ-3D/config/telemetry_params.json)和[可启停的 10 个加热支路](satellites/YJ-3D/config/components.json)均按云尖3D工作簿重配。`yj3d_report.py` 沿用观测窗口、原值曲线和 ≤10 秒短间隔积分的分析方法，并读取[本星 7.84 A 放电标定曲线](../Data/0010-云尖3D星/volt_capacity_3D_v0.csv)计算匹配地影分钟的条件 SOC/DoD；37.69 Ah 为表内容量，不等于在轨实测容量。

云尖3D星未来任务功率预测 Profile 已落盘到
[`satellites/YJ-3D/power_model/`](satellites/YJ-3D/power_model/README.md)：包含54个
原始A/B供电/加热支路模型、合并后的32个物理单机模型、太阳阵BCR输出功率多项式、
BCR输出目标和UTC/BJ快照适配、示例及留出验证。
用户确认太阳阵控制逻辑为MPPT；当前使用`Σ(V_BCR_out×I_BCR_out)`作为
母线/电池节点功率目标，BCR输出与`P_bus−P_battery_signed`的拓扑已核对，
默认模型不启用负荷跟随封顶。最终BCR模型使用时间一阶+未来预测负载。
统一质检后，3/10窗口因电池或母线越量程退出SOC/负载主指标；7个QC通过窗口的
条件SOC MAE为0.149个百分点、剩余容量MAE为0.056 Ah，其中4个饱和窗口误差为零，
3个非饱和窗口SOC MAE为0.348个百分点。10窗口原始积分诊断MAE为0.472个百分点，
不能替代QC主指标。BCR主能量按所用留出模型的99.9%缩尾上限计算，
10窗口MAE为11.35%；7个QC通过窗口的负载能量MAE为3.88%，10窗口原始诊断
MAE为4.16%。公共功率过滤器已按工作簿量程排除54个支路合计5,801个
不可能功率野值（约占单机总样本0.1%），方向和状态模型只使用清洗后样本。
方向V-SOC候选仍未证明优于生产曲线，绝对剩余电量仍需
独立SOC锚点和MPPT侧电压/电流测量。
[云尖32个物理单机的分日期训练/留出数据](satellites/YJ-3D/unit_training/README.md)
已单独落盘；两星不共享训练文件或索引。

## 快速导航

| 序号 | 文件 | 说明 | 类型 | 状态 |
|-----|------|------|------|------|
| 01 | [技术调研报告_卫星整星能源分析](01_技术调研报告_卫星整星能源分析.md) | 卫星能源系统技术调研 | 📄 文档 | ✅ |
| 02 | [评估框架设计_能源平衡定量评估](02_评估框架设计_能源平衡定量评估.md) | 评估框架详细设计 | 📄 文档 | ✅ |
| 03 | [核心评估工具_能源平衡评估器](03_核心评估工具_能源平衡评估器.py) | Python评估核心工具 | 🐍 代码 | ⚠️ v1·已 deprecated |
| 04 | [数据库连接测试脚本](04_数据库连接测试脚本.py) | ClickHouse连接测试 | 🐍 代码 | ⚠️ v1·仅探索用 |
| 05 | [数据库分析工具_ClickHouse能源分析器](05_数据库分析工具_ClickHouse能源分析器.py) | 从数据库读取数据分析 | 🐍 代码 | ⚠️ v1·已 deprecated |
| 06 | [演示报告样例_01星能源评估报告](06_演示报告样例_01星能源评估报告.md) | 评估报告示例 | 📄 文档 | ✅ |
| 07 | [项目总览说明文档](07_项目总览说明文档.md) | 完整使用指南 | 📄 文档 | ✅ |
| 08 | [项目总结报告](08_项目总结报告.md) | 项目成果总结 | 📄 文档 | ✅ |
| 09 | [评估框架设计V2_真实数据实证](09_评估框架设计_v2_真实数据实证.md) | v2.2 方法与数值快照 | 📄 文档 | 历史 |
| 10 | [真实数据评估报告_SCS-01-01](10_真实数据评估报告_SCS-01-01.md) | v2.2 评估快照 | 📄 文档 | 历史 |
| 11 | [调研续篇_真实遥测落地经验](11_调研续篇_真实遥测落地经验.md) | 调研补充与落地经验 | 📄 文档 | ✅ |
| 12 | [代码库深度调研报告](12_代码库深度调研报告_架构_质量与改进优先级.md) | 架构/质量/安全调研与整改路线图 | 📄 文档 | ✅ |
| 13 | [框架全面审查报告](13_框架全面审查报告_设计_原理与代码质量.md) | 设计/原理/代码三方面审查 + TC 待确认清单 | 📄 文档 | ✅ |
| 14 | [整改实施与复算报告](14_整改实施与复算报告_v2.3.md) | 01 星前次工况/评分/能量口径及待验证事项 | 📄 文档 | 历史 |
| 15 | [当前项目框架、原理与计算依据](15_当前项目框架_原理与计算依据.md) | 从取数到报告的模块、物理前提、公式及证据边界 | 📄 文档 | **现行** |
| 16 | [单机能源部件配置与功率平衡](16_单机能源部件配置与功率平衡.md) | 01 星单机目录、可配置部件、工况与分流前物理账本 | 📄 文档 | **现行** |
| 17 | [单机插件化能源平衡技术方案](17_单机插件化能源平衡技术方案_草案.md) | 单机边界、插件契约和物理闭合门控草案 | 📄 文档 | 草案 |
| 18 | [设计功耗对照](18_设计功耗对照_供配电设计表.md) | 设计表、实际供电轨与遥测支路逐项对照 | 📄 文档 | **现行** |
| 19 | [三体01星单机模型适用性评估与优化](19_三体01星单机模型适用性评估与优化.md) | 25个支路模型、异常值、供电轨、误差与优化优先级 | 📄 文档 | **现行** |

> **工具链说明**：03/04/05 为已废弃的 v1 工具。09/10、`framework_documentation.html` 和根目录的 `framework_documentation_current.html` 为历史快照；当前宽表结果见上表中的星号区分报告。

## 现行工具链（v2.3）

```
s1_fetch.py + s1_fetch_raw.py ──▶ 各星专属目录的分钟/原始采样 CSV 与清单 ──▶ s3_run.py ──▶ 各星 out/ ──▶ gen_charts.py ──▶ 带星号文件名的 HTML
(星上时间、有界分批取数)       (分钟极值/原始功率、哈希)            (三类工况、间隔积分与观测样本评分)          (离线内嵌图表)
```

| 模块 | 职责 |
|------|------|
| `ch_http.py` | 旧 HTTP 客户端及连接配置读取；有口令时要求 TLS 或显式明文例外 |
| `power_outlier_filter.py` | 星间公共功率野值过滤：优先使用配置文件中的 `max_power_W` 筛选上限，无该上限时剔除极端功率尾部；返回清洗序列和剔除审计元数据 |
| `branch_aggregation.py` | 按批准拓扑把多条供电支路聚合为一个物理单机；设备去重，并联电流/功率合并，缺支路不填零 |
| `physical_unit_model.py` | 从支路功率与拓扑生成物理单机功率、配置、全量/分组留出模型及报告 |
| `energy_balancer.py` | 评估内核：明确日照/地影/未判定，SOC 缺测保留空值，输出观测样本评分 |
| `components.py`、`config/components.json` | 按卫星装配单机插件并启停功率/状态测点；同分钟接入整星功率账本并保留不可观测项 |
| `s1_fetch.py`、`s1_fetch_raw.py` | 从星别宽表按星上北京时间及半开时间窗分批取分钟与原始采样，逐行写 CSV 和哈希清单；支持 HTTP 或原生端口 |
| `interval_energy.py` | 对相邻原始采样做功率梯形积分，超过 10 秒的缺口不外推 |
| `s3_run.py` | 校验两份来源清单，运行评估，输出逐分钟、日报和摘要 JSON |
| `gen_charts.py` | 由摘要与数据生成当前离线 HTML；旧 HTML 保留历史快照 |
| `design_compare.py`、`config/design_load_table.json` | 表 5-32 供配电设计表 × 图 5-38 供电电压 × 遥测支路电流的逐单机功率对照；生成 HTML 的“设计功耗对照”章节与两张静态图 |
| `s6_fetch_abs_current.py` | 为磁力矩器等双向电流测点聚合 `mean(|I|)` 分钟序列（`data_abs_current_minute.csv`），供对照模块按 `stat="abs_current"` 读取 |
| `tests/`、`test_chart_documentation.py` | 内核、HTTP、合成端到端与真实数据图表回归 |
| `satellites/SCS-01-0*/config/` | 两星各自的设计参数、V-SOC 曲线、遥测和部件配置（JSON） |

其他卫星可直接复用公共过滤器：

```python
from power_outlier_filter import filter_power_values

clean_power, audit = filter_power_values(
    power_series,
    upper_limit_W=component.get("max_power_W"),
)
```

返回值中的 `clean_power` 对缺测、负值和超上限野值均为
`NaN`；`audit` 包含方法、上限、剔除数和原始最大值。不要把过滤后的
`NaN` 自动替换为 0 W。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 ClickHouse 只读连接

```bash
# Windows PowerShell 示例；口令可保存在本地被忽略的 .env 中
$env:CH_HOST='10.200.56.174'
$env:CH_PORT='8123'
$env:CH_SCHEME='http'
$env:CH_PASSWORD='<查询口令>'
$env:CH_ALLOW_PLAINTEXT='1'
```

也可按 `.env.example` 填写被忽略的 `.env`。上述 `CH_ALLOW_PLAINTEXT=1` 是用户此前允许无 TLS 连接时的本次例外；HTTP 明文会暴露口令与数据，运行后应从当前进程移除该变量。未显式放行时，携带口令的明文连接默认拒绝。不要将实际口令提交到仓库。

`chdb_3body.master_satellite_01` 与 `master_satellite_02` 均为 3454 列宽表。当前取数按星别配置中的 `source_table` 只选所需列，宽表字符串数值先转为数值、文字状态保留为 `state_text`。本次复算使用 HTTP 8123 只读查询；旧 04/05 脚本仅供探索。

### 3. 运行评估链

```bash
python s1_fetch.py --transport http --start 2025-12-26 --end 2026-08-12 --source-timezone Asia/Shanghai --profile-dir satellites/SCS-01-02/config --out satellites/SCS-01-02/data_power_minute.csv
python s1_fetch_raw.py --transport http --start 2025-12-26 --end 2026-08-12 --source-timezone Asia/Shanghai --profile-dir satellites/SCS-01-02/config --out satellites/SCS-01-02/data_power_raw.csv
python s3_run.py --data satellites/SCS-01-02/data_power_minute.csv --raw-data satellites/SCS-01-02/data_power_raw.csv --out satellites/SCS-01-02/out --profile-dir satellites/SCS-01-02/config
python gen_charts.py --workspace-dir satellites/SCS-01-02
```

01 星使用相同命令，将目录中的 `SCS-01-02` 全部替换为 `SCS-01-01`；两星不能共用输入、清单、配置或输出目录。宽表状态列已直接纳入分钟文件。`--transport http` 在现有 HTTP 端口可达时流式取数，连接仍按 `.env`/环境变量的明文例外规则校验。

SCS-01-01 现行数据已扩展为北京时间
`[2025-12-26, 2026-11-05T10:00)`，分钟文件包含 `TMD079–TMD083`
五个PCDU温度遥测原始数值；单位与比例换算未确认，因此只用于覆盖和变化审计，
尚未作为功率或SOC模型特征。旧取数文件保存在
`satellites/SCS-01-01/archive/through_20260811/`。

输出各星 `out/energy_v2_components.csv` 和摘要 `components` 段；带星号的 HTML 报告将单机功率曲线和整星遥测图表分别放在一个显示区域，通过选择器切换。根据供配电方案 5.3.3，当前按不调节母线、蓄电池钳位和“负载优先、充电其次、分流最后”解释功率路径；TMD017 相对充电支路的位置未核，因此帆板端与 TMD017 的曲线只称测点差额，不称 S³R 总耗散，TMD017 与负载差额也不称电池充电功率。母线损耗尚不可算。页面样式由 `report_style.css` 内嵌，离线打开无需外部资源；详情及测点证据见 [16 号文档](16_单机能源部件配置与功率平衡.md)。更换卫星时在独立目录放置 `telemetry_params.json`、`design_params.json`、`v_soc_curve.json`、`components.json`，对取数及评估命令同时传 `--profile-dir`。配置项的 `plugin` 选择内置计算器；特殊单机可通过 `plugin_modules` 注册可信本地计算器。新增卫星仍需确认源表和北京时间语义。

### 3.1 01星单机功率与出阴影期预测

`unit_power_model.py` 从 01 星 `out/energy_v2_components.csv` 校准 25 个已启用负载单机：
检测低功率层/工作功率层，并在 active 层内部按 log 功率比值分离可能的高功率瞬态；
只有高功率层中位值至少为稳态层 14 倍时才自动拆层。
本批 2 个电推进配电通道形成 `low/active/high` 三层；A/B 是同一电推进系统的两路配电，
当验证日期使某一路缺少 high 训练样本时，模型从另一路迁移高功率先验。`electric_propulsion_b`
由不稳定的二阶时间模型改为稳态约 13.916 W、高功率层约 215.195 W 的分层恒功率；A/B
高功率任务必须显式指定 `power_level: "high"`。配置同时为25个负载通道设置建模筛选上限，
在状态识别和拟合前剔除数十万瓦级及超设计包络的遥测野值。对连续运行样本提供一至三阶多项式候选，留出验证使用与
部署相同的 active 层 p05–p95 运行截断，并要求截断后 RMSE 至少改善 10%、支撑区间
单调、首末误差不超过恒定基线 10%，才选择多项式。简化预测入口接收
`time`、`prediction.end`、`prediction.step_seconds`、`phase_segments`和`tasks`；模型按`time`精确分钟
自动查询电池电压、总负载、相位和任务单机功率，用V-SOC曲线计算初始SOC，并自动推导背景负载。
任务使用`run_mode`表示窗口内状态、`after_mode`表示结束后状态。阴影片段太阳阵功率为0 W；
01星阳照段使用排除16个验证日期后重新拟合的方案B多项式
`P_load(TMD016×TMD018)+P_charge_est`。当前历史拟合选择3阶多项式，按阳照片段分组验证
RMSE约128.15 W；阴影片段固定0 W。输出目标时刻SOC、
剩余Ah/kWh、最低SOC、各单机耗能和经验功率散布。模型库、JSON Schema、样例输入/输出及证据边界见
[`satellites/SCS-01-01/power_model/`](satellites/SCS-01-01/power_model/README.md)。
该预测是任务规划簿记模型；没有电池温度/滞回/SOH、独立电池净功率或完整负载拓扑时，
不能把结果称为飞行级剩余电量或物理闭合证明。

首段留出验证采用2025-12-29 05:39–06:10连续阳照窗口，并从太阳阵训练集中排除该日。
目标SOC预测91.0309%、观测91.4815%，偏差-0.4506个百分点；太阳阵能量低估15.02%。
闭环误差包含太阳阵低估与恒定背景负载高估的部分抵消，不能据此认定太阳阵模型已准确。

冻结背景主验证保留原16个验证日期，在19个阳照窗口基础上增加6个同日期地影窗口。
25个窗口的SOC MAE为1.9516个百分点；其中阳照1.2778、地影4.0853，说明分相评价必要。
主指标不使用未来背景负载。历史逐分钟背景回放另行保存为“上界/诊断值”，
其19窗口SOC MAE为1.4601，不能与主指标混用。扩展数据新增892个SON分钟和
5路PCDU温度原值，温度换算及进入模型仍未完成。方向V-SOC候选
显示充电/放电中位电压差0.4725 V，但充电方向SOC映射MAE反而由1.8688升至2.9080个百分点，
因此不替换生产V-SOC单曲线。报告见`satellites/SCS-01-01/power_model/`。

### 3.2 第二阶段审计与可复现清单

两星通过 `config/stage2_profile.json` 声明主评测口径，由 `stage2_framework.py`
统一生成 `evaluation_summary.json`、逐案例指标和按日期聚类bootstrap。
`stage2_audit.py` 再把主指标与卫星专属诊断合成同 schema 的 `stage2_audit.json` 和
`stage2_manifest.json`。manifests 记录输入文件与代码的 SHA-256、验证日期、
模型选择候选和最终指标；星别报告生成时会检查清单是否陈旧并自动刷新。
共享评测支持按指标分别应用SOC、太阳能量和负载质量状态；例如地影窗口不参与
太阳能量相对误差，越量程SOC标签也不会污染太阳阵或负载指标。主指标固定为
`frozen_snapshot`，卫星专属的“未来背景回放”只能写入独立诊断字段。

逐单机训练包使用 `unit_training_export.py` 分星导出。每颗卫星各自拥有
`unit_training/`，其中 `train.csv` 只保存有效标签，`excluded_samples.csv` 保留
缺测/负值及原因；两星都把验证日期放入 `eval.csv` 并从 `train.csv` 排除。
三体01星和云尖星的样本外单机模型分别保存，不共享训练文件。

```powershell
python unit_training_export.py --profile-dir satellites/SCS-01-01
python unit_training_export.py --profile-dir satellites/YJ-3D
```

```powershell
python stage2_audit.py --profile-dir satellites/SCS-01-01
python stage2_audit.py --profile-dir satellites/YJ-3D
```

云尖星审计还包括饱和/非饱和窗口分栏、固定快照与前30/60分钟滚动背景负载 A/B、
A/B 供电通道共活跃统计，以及 BCR 输出缩尾异常值数量和能量占比。
滚动背景只使用窗口起点之前的同工况分钟，当前作为候选审计，不自动替换正式模型。
两份星别报告的第二阶段末尾各给出一个完整代表性案例，展示原始输入 JSON、
预测/实际输出指标，并把预测 SOC 时间线与由本星 V-SOC 条件映射得到的实际
SOC 曲线画在同一时间轴上。单窗口只解释链路与误差形态，不替代批量审计。
云尖案例从历史单机功率中按“3点中位平滑、剔除少于3分钟状态短跳变”的规则
提取20条任务，使用留出单机库与负载感知BCR留出模型闭环预测。实际条件曲线采用
初始V-SOC锚点加 TMEA006 梯形电流积分；72.737%的V-SOC端点作为独立交叉参考，
不会再把充电电压逐分钟套入放电曲线。
三体01星案例也与云尖星采用简化输入：只提供时间、`phase_segments` 和
2条历史提取任务，不提供太阳阵功率；阳照功率由留出三次多项式在预测器内部生成。
其逐分钟实际条件曲线仍由 TMD019 和项目 V-SOC 曲线映射。

完整验证集另行导出到各星自己的目录，不使用跨星合并文件：

```powershell
python stage2_testset.py --profile-dir satellites/SCS-01-01
python stage2_testset.py --profile-dir satellites/YJ-3D
```

三体01星输出 25 个案例（19阳照+6地影）至 `satellites/SCS-01-01/stage2_testset/`，
云尖星输出 10 个案例至 `satellites/YJ-3D/stage2_testset/`。每个案例包含
简化 `input.json`、独立观测 `label.json`、基线 `prediction.json`、预测/实际
SOC曲线、任务计划和元数据；两星各自具有 `manifest.json`、`inputs.jsonl`
与`labels.jsonl`。

报告的“设计功耗对照”章节另按《表 5-32 供配电设计表》和图 5-38 的供电电压，把 PCDU 支路电流折算成单机功率与设计功耗逐行对比：一次母线负载用同分钟 `TMD016`，5 V 二次电源负载用 `TMD059`，12 V 用 `TMD062`，不再一律乘母线电压。设计功耗直接用表列单台值，供电路数不作倍乘；智算机-智加 NX1 的 93.5 W 与 49 W 是同台单机的两种工作模式，不叠加；同一行含多台同型单机时取实测最大的一台。21 行中 11 行有可用电流遥测，其余 10 行标注“不可对照”而不估算，并按五类写明原因：配电端口无电流测点（3）、二次电源只有合路电压（2）、仅工作电压（3）、仅开关状态（1）、表内有定义但库中无数据（1，综合电子的 TMU142/143/145/146）。测传一体机改用 `TMC121/TMC122`，磁力矩器改用 `TMK6821–6824` 的 `mean(|I|)` 分钟序列（`s6_fetch_abs_current.py` 生成，双向电流不能用有符号均值）。另列出“反向缺口”——5 个有电流遥测却没有设计行的设备（测控应答机 A/B 的 TMC020/050+TMC021/051、上注通道、CXPD 载荷、科学载荷总电源）。口径、结果与限制见 [18 号文档](18_设计功耗对照_供配电设计表.md)。

`--start` 含起点、`--end` 不含终点；取数时间列为 `onboard_time_bj`（北京时间），不是地面接收时间 `station_time`。只保留分钟 CSV 时，摘要中的分工况能量是名义 60 秒等效值，不能作为实际时长积分；本次宽表复算同时使用原始采样 CSV。旧 CSV 无新版清单时会标记 `unverified_legacy_input`。

云尖3D星的 UTC 宽表结构不同，使用独立命令从数据库读取并生成带哈希的分钟数据、短间隔积分和报告：

```bash
python yj3d_assess.py
python yj3d_components.py
python yj3d_report.py
```

其查询窗口由 `satellites/YJ-3D/config/assessment_params.json` 指定为北京时间半开区间 `[2026-07-24, 2026-10-28)`。数据库连接沿用前述本地 `.env`/环境变量；`yj3d_report.py` 还读取本星曲线和已有分钟数据，不需重新查询数据库即可更新条件 SOC。完整字段、量程、来源哈希、有效区间与缺口见[云尖3D分析](satellites/YJ-3D/能源评估分析_YJ-3D.md)。

### 4. 运行测试

```bash
python -m unittest discover -s tests -v
python test_chart_documentation.py -v  # 无真实 CSV 时跳过数据相关用例；合成端到端用例始终运行
```

## 核心口径（v2.3）

设计方案为 **S3R（顺序开关分流调节）与不调节母线**；TMD017 相对电池充电支路的测点位置仍待连接图核实。因此：

- **TMD017 测点折算功率 = 母线电压 × 母线电流（TMD016 × TMD017）**；旧字段名 `P_solar` 保留兼容，不能把它直接认定为太阳阵实际总发电或独立电池功率；
- **负载功率 = V_bus × I_load（TMD018）**；
- **帆板与母线差额为方向诊断量**：`P_shunt_diag = (I_solar(TMD009) − I_bus(TMD017)) × V_bus`；它不是独立测得的分流功率，求和等式也不是物理闭合验证；
- **分边界物理账本**：母线侧 `P_solar_to_bus + P_bat,dis_at_bus = P_load_at_bus + P_bat,ch_at_bus + P_bus_loss + residual_bus`；阵列/S3R 侧 `P_SA,actual_before_shunt = P_solar_to_bus + P_array_S3R_loss_total + residual_S3R`，其中 `P_array_S3R_loss_total := P_shunt,electrical + P_S3R_other_loss` 为选定边界内的总电耗散。六组帆板 `V×I` 之和减去入母线 `V×I` 已作为逐分钟诊断差额计算；需核实帆板测点在分流前、支路覆盖和同步性才能认定为总耗散。电池电压已有，但当前输入缺少已校准、符号明确的电池电流，母线损耗尚不能定值，物理闭合未验证；
- **工况判定优先用 TMK5315**：阳照区／阴影区标志为主判据，分钟内切换为未判定；标志缺测时才用 TMD009 的分钟极值和 1 A 阈值回退。两者明确冲突时保留 TMK5315 结果并记录冲突；
- **SOC 锚定用三体星设计放电表**（v2.2 切换，用户决策）：10A 恒流放电，22.82–29.31V ↔ 10%–100% SOC，7s13p/65Ah 设计构型（`config/v_soc_curve.json`）；原通用锂电假设曲线已废止（git 历史可溯）。

## 历史长表快照（仅供对照）

以下保留 2026-10-27 前根目录 `telemetry_raw_01` 的 01 星旧结果，当前两星宽表结论以上方星号区分的分析文件与机器摘要为准。

- **数据源**：ClickHouse 集群 `prod_cluster` → `chdb_3body.telemetry_raw_01`（真实在轨遥测）
- **评估对象与时间窗**：SCS-01-01；星上北京时间 `[2025-12-26, 2026-08-12)`；19,727 个观测分钟、138,113 个原始采样时间戳。
- **工况**：日照 13,021、地影 6,475、未判定 231 分钟；TMK5315 直接判定 9,320 分钟，TMD009 回退 10,176 分钟，标志分钟内切换 170 分钟，异常值 1 分钟，两判据明确冲突 54 分钟；日历分钟观测率 6.05%，仅评价观测样本。
- **观测样本评分**：94 分（优秀），整段时期**未评定**；明确日照分钟的 TMD017／负载测点差比 148.3%，DoD p95 21.8%、SOC p5 78.2%。
- **能量**：相邻原始采样 ≤10 秒的梯形积分，按分钟工况分组；日照 TMD017 测点折算 29.019 kWh、负载测点 11.878 kWh。完整现行值见 `out/energy_v2_summary.json`；[14 号报告](14_整改实施与复算报告_v2.3.md)保留旧判据复算记录。旧电池净值字段是两测点差额，不能解释为实测电池电量。
- **电池电流判向**：用户确认 CH 的 `TMH025` 为蓄电池母线电流、单位 A，并指定 `TMD074_b1` 为“通”时赋放电正号。现行分钟输入已包含两项；“断”、同分钟切换或缺测不自动判为充电。电池电流的原生符号、充电路径及 TMD017 相对充电支路的位置仍待核，开关规则的功率试算不用于宣称母线损耗或物理闭合。
- **开关规则全量结果**：19,727 个观测分钟中，`TMD074_b1` 有 19,724 分钟为“通”，3 分钟为同分钟切换；排除 TMH025 原始样本越界等异常后，19,663 分钟可赋放电正电流。另有 13,015 分钟的 `I_负载−I_TMD017` 为负，与开关规则相反；该差额只作冲突诊断，不再给 TMH025 赋号。开关规则几乎把全部观测分钟判为放电，不能据此推断卫星全期的净充放电量或电池容量。
- **核心工具**：[energy_balancer.py](energy_balancer.py)（评估器）＋ [ch_http.py](ch_http.py)（ClickHouse HTTP 客户端）
- 此历史明细见[旧分析页面](framework_documentation_current.html)与根目录 `out/energy_v2_summary.json`；09/10/14 为更早的判据对照。
- ⚠ 引用发电量/裕度时须注明"入母线口径"；帆板级口径数值更大但含被分流部分，仅作诊断
- ⚠ 引用 SOC/DoD 时须注明"三体星设计表锚定（10A 放电，滞回未建模）"

## 核心评估指标

| 指标 | 正常范围 | 预警阈值 | 说明 |
|------|---------|---------|------|
| 功率裕度 | ≥20% | ≤0% | 仅明确日照分钟，入母线功率减负载再除以负载 |
| 放电深度 DoD p95 | ≤30% | ≥50% | 全部有电池电压的观测分钟 |
| 荷电状态 SOC p5 | ≥50% | ≤30% | 设计放电表估算，非独立 SOC 测量 |
| 母线稳定度 | ≤3% | ≥5% | 电压 p95−p5 除以中位数 |

## 工程约定

- **参数单一事实源**：设计参数、V-SOC 曲线、遥测代号映射均在 `config/*.json`，改参数只改配置不改代码；
- **产物目录**：当前各星 `satellites/SCS-01-0*/out/`（评估输出 CSV）、`charts/`（SVG 副本）均为可再生产物，已被 `.gitignore` 排除；
- **数据文件**：各星目录的 `data_power_minute.csv`、`data_power_raw.csv` 与各自 `.manifest.json` 由取数脚本生成，均不入库；旧 CSV 无清单时标为未验证来源；
- **一次性脚本**：`q1–q5`、`s2/s4/s5` 为探索/诊断脚本，结论已沉淀进 09/11 号文档；
- **凭据**：任何口令不得写进代码，一律走环境变量或 `.env`。

## 项目信息

- **适用对象**: 三体星（01星/02星）能源系统分析
- **数据来源**: ClickHouse `chdb_3body.master_satellite_01/02`，本次通过 HTTP 8123 按 `onboard_time_bj` 北京时间只读取数
- **技术栈**: Python 3.12+ + pandas + numpy（图表与 HTTP 客户端仅用标准库）
- **开发环境**: Vibe Research
- **版本**: v2.3（分钟极值工况、缺测显式、观测样本评分和来源清单）
- **日期**: 2026-10-23

## 联系方式

如有问题或建议，请联系项目团队。

---

**技术支持**: Vibe Research
