# PBE：卫星整星能源评估与功率预测

PBE 面向卫星能源系统，提供从 ClickHouse 遥测取数、能源平衡评估、单机功率建模，到未来任务功率与电池状态预测、离线图表报告和自动化测试的完整链路。

项目当前覆盖三体 01 星、三体 02 星和云尖 3D 星（YJ-3D）。不同卫星使用独立的遥测映射、设计参数、SOC 曲线、单机配置与分析目录，避免跨星混用。

> **数据说明**：Git 仓库只保留代码、配置、模型参数、报告、测试和可公开的说明文档。原始遥测、训练集、留出集、验证样本及派生分钟数据均不进入 Git。

## 主要能力

- 从 ClickHouse 宽表按卫星、时间和星上时间窗口批量提取遥测，并生成带哈希清单的数据快照。
- 判定阳照、地影和未判定工况，计算功率、能量、SOC、DoD、电压稳定性及观测样本评分。
- 按卫星供配电拓扑过滤异常功率，聚合 A/B 供电支路，并校准逐单机功率模型。
- 使用冻结快照、整日留出和标准化测试集进行可追溯验证。
- 根据未来任务计划预测负载、太阳阵功率、目标 SOC、剩余 Ah/kWh 和任务耗能。
- 生成可离线打开的 HTML 图表报告，并通过合成数据和真实数据回归测试检查完整性。

## 当前对象

| 对象 | 数据来源或重点 | 主要文档 |
|---|---|---|
| SCS-01-01（三体 01 星） | `chdb_3body.master_satellite_01`，宽表遥测、25 个负载通道建模、出阴影期预测 | [能源评估分析](ResearchSpace/satellites/SCS-01-01/能源评估分析_SCS-01-01.md) · [功率模型](ResearchSpace/satellites/SCS-01-01/power_model/README.md) |
| SCS-01-02（三体 02 星） | `chdb_3body.master_satellite_02`，与 01 星共用主要能源代号，电池曲线与温度标定待核 | [能源评估分析](ResearchSpace/satellites/SCS-01-02/能源评估分析_SCS-01-02.md) |
| YJ-3D（云尖 3D 星） | `chdb_3D.yc_history`，UTC 源时间、A/B 双供电支路、BCR/MPPT 功率与未来任务预测 | [能源评估分析](ResearchSpace/satellites/YJ-3D/能源评估分析_YJ-3D.md) · [功率模型](ResearchSpace/satellites/YJ-3D/power_model/README.md) |

## 目录结构

```text
.
├── .github/workflows/            # GitHub Actions 持续集成
├── Data/                         # 本地卫星资料，已忽略，不入库
└── ResearchSpace/
    ├── README.md                 # 详细方法、口径、命令与结果
    ├── config/                   # 公共参数和图表配置
    ├── satellites/
    │   ├── SCS-01-01/            # 01 星配置、模型、报告与标准化测试
    │   ├── SCS-01-02/            # 02 星配置、输出与报告
    │   └── YJ-3D/                # 云尖 3D 星配置、模型、报告与标准化测试
    ├── tests/                    # 单元测试与合成端到端测试
    ├── s1_fetch.py               # 分钟遥测取数
    ├── s1_fetch_raw.py           # 原始采样取数
    ├── s3_run.py                 # 能源平衡评估
    ├── gen_charts.py             # 离线图表与 HTML 报告
    ├── unit_power_model.py       # 单机功率建模与预测
    └── requirements.txt
```

本地运行后还会生成 `out/`、`out_*/`、CSV 数据集和实验快照；这些可再生产物均被 `.gitignore` 排除。

## 快速开始

### 1. 获取代码

```bash
git clone https://github.com/TianHuiLab/PBE.git
cd PBE/ResearchSpace
```

### 2. 安装依赖

建议使用 Python 3.12 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell 激活虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

### 3. 配置 ClickHouse

复制配置模板，并在本地 `.env` 中填写当前环境提供的只读账号信息：

```bash
cp .env.example .env
```

`.env` 已被忽略，禁止提交真实口令。生产查询应优先使用 TLS 或受控内网通道；只有明确接受明文传输风险时，才按模板说明设置 `CH_ALLOW_PLAINTEXT=1`。

### 4. 运行测试

```bash
python -m unittest discover -s tests -v
python test_chart_documentation.py -v
```

不连接数据库时，合成端到端测试仍会运行；依赖真实 CSV 的图表回归用例会跳过。

### 5. 运行分析

在 `ResearchSpace` 目录执行。下面是 01 星的流程示例，其他卫星应将配置和输出目录替换为各自独立目录：

```bash
python s1_fetch.py \
  --start 2025-12-26 \
  --end 2026-11-05T10:00 \
  --source-timezone Asia/Shanghai \
  --profile-dir satellites/SCS-01-01/config \
  --out satellites/SCS-01-01/data_power_minute.csv

python s1_fetch_raw.py \
  --start 2025-12-26 \
  --end 2026-11-05T10:00 \
  --source-timezone Asia/Shanghai \
  --profile-dir satellites/SCS-01-01/config \
  --out satellites/SCS-01-01/data_power_raw.csv

python s3_run.py \
  --data satellites/SCS-01-01/data_power_minute.csv \
  --raw-data satellites/SCS-01-01/data_power_raw.csv \
  --out satellites/SCS-01-01/out \
  --profile-dir satellites/SCS-01-01/config

python gen_charts.py --workspace-dir satellites/SCS-01-01
```

具体参数、卫星差异、评估口径和当前结果见 [ResearchSpace/README.md](ResearchSpace/README.md) 与 [当前项目框架、原理与计算依据](ResearchSpace/15_当前项目框架_原理与计算依据.md)。

## 145 服务器训练数据

训练数据不随仓库分发，统一存放在 145 服务器：

```text
/data/wangcong/PBE_data
```

在 145 服务器开发时，训练数据的项目内软链接路径统一为：

```text
<仓库根目录>/ResearchSpace/training_data
  -> /data/wangcong/PBE_data
```

若仓库目录为 `~/PBE`，创建软链接：

```bash
cd ~/PBE
ln -s /data/wangcong/PBE_data ResearchSpace/training_data
readlink -f ResearchSpace/training_data
```

最后一条命令应输出 `/data/wangcong/PBE_data`。`ResearchSpace/training_data` 已写入 `.gitignore`，链接本身和链接指向的数据都不会被提交。

推荐的服务器数据组织方式：

```text
/data/wangcong/PBE_data/
├── SCS-01-01/unit_training/
├── YJ-3D/unit_training/
└── ...
```

从项目重新导出训练数据时，直接写入该目录：

```bash
cd ResearchSpace
python unit_training_export.py \
  --profile-dir satellites/SCS-01-01 \
  --output-dir training_data/SCS-01-01/unit_training

python unit_training_export.py \
  --profile-dir satellites/YJ-3D \
  --output-dir training_data/YJ-3D/unit_training
```

训练数据包内的 `train.csv`、`eval.csv`、`validation_overlap.csv` 和 `excluded_samples.csv` 不得复制回 Git 跟踪目录。代码与数据通过软链接解耦，拉取仓库的新版本不会覆盖服务器上的训练数据。

## 文档入口

- [项目详细说明与现行工具链](ResearchSpace/README.md)
- [评估框架、物理前提与计算公式](ResearchSpace/15_当前项目框架_原理与计算依据.md)
- [单机能源部件配置与功率平衡](ResearchSpace/16_单机能源部件配置与功率平衡.md)
- [设计功耗对照](ResearchSpace/18_设计功耗对照_供配电设计表.md)
- [01 星单机模型适用性评估](ResearchSpace/19_三体01星单机模型适用性评估与优化.md)
- [云尖 3D 星功率模型与预测](ResearchSpace/satellites/YJ-3D/power_model/README.md)

## 测试与持续集成

GitHub Actions 在 Python 3.12 环境中执行：

1. 全量语法编译检查；
2. `tests/` 下的单元测试和合成端到端测试；
3. 图表与 HTML 回归测试；没有真实数据时，相关用例自动跳过。

真实遥测验证必须在内网环境单独运行。CI 通过只说明代码和合成链路正常，不等同于真实卫星数据的物理闭合、模型精度或飞行级适用性已经得到证明。

## 数据与结论边界

- 功率测点、单机功率和电池净功率可能是分钟代理量，不能自动视为独立硬件真值。
- 只有通过量程、时间连续性、相位和冻结检查的区间才形成主标签；缺测不能按零值处理。
- SOC 结果依赖指定 V-SOC 曲线、容量假设和温度/滞回/老化等未建模项。
- 留出验证、标准化测试集和任务规划样例分别代表不同证据层级，不能混用为飞行级结论。
- 引用具体数值时，应同时注明卫星、时间窗、数据口径、样本数和证据边界。
