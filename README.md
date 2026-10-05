# 多麦克风敲击定位 Web 台

根据房间内 **4～8 个非共线麦克风** 录到的敲击到达时刻，联合选择各麦候选峰并
反演声源位置的交互式定位台。各麦克风使用独立时钟，用两次位置已知、分列目标
事件前后的校准敲击完成时钟偏移与速率（ppm）标定。

- **后端**：Python 3.11 · FastAPI · NumPy
- **前端**：原生 HTML / JavaScript / Canvas（无构建步骤、无前端框架）
- **求解**：双 TDOA 闭式种子 + 覆盖网格 + Gauss–Newton 最小最大残差抛光，
  全矢量化；8 麦 × 每麦 12 候选的典型交互求解约 0.3～0.5 秒
  （要求在 2 核 / 1 GiB 容器内 5 秒内完成）

## 一、Docker Compose 一键启动

```bash
docker compose up --build
```

启动后浏览器访问：

- **定位台页面（房间定位图）**：<http://localhost:8000/>
- 健康检查：<http://localhost:8000/healthz>
- 求解接口：`POST http://localhost:8000/api/locate`

停止：`docker compose down`。容器通过 Compose 的 `cpus: 2` / `mem_limit: 1g`
限定为 2 核 / 1 GiB。

## 二、本地直接运行（可选）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

测试：

```bash
.venv/bin/python -m pytest tests/
```

## 三、页面用法

1. 填写 **房间宽 × 高**、**声速**（m/s）、**时钟漂移上限**（ppm）、
   **时差容限**（最大绝对 TDOA 残差，ms）、**位置分辨率**（m）。
2. 填写 4～8 个麦克风的 `(x, y)`，必须位于矩形房间内且不共线；
   **0 号麦为时钟基准**。
3. 填写两次 **校准敲击** 的已知位置 A、B（A、B 必须不同，且目标事件发生在
   两次校准之间），以及各麦用各自本地时钟记录到的两次到达时刻。
4. 为每个麦克风录入目标敲击的 **1～12 个候选峰**：本地到达时刻（秒）与
   强度。候选峰可乱序录入，系统按时刻排序；可随时增删行。
5. 点击 **定位求解**（或页面右上角的两个一键演示按钮）。

地图上绘制：房间网格、校准点 A/B（紫色三角）、麦克风（蓝色圆点带编号）、
各麦候选峰（灰圈，半径随强度变化）、入选峰（绿色高亮）、定位点（绿色）、
定位点到各麦的几何连线、定位点的分辨率圈。下方表格给出各麦入选候选序号、
本地时刻、强度、时差残差（超差标红）和校准得到的漂移/时钟偏移。

### 裁决规则

1. 以首麦（0 号）为时基，由两次校准敲击估计各麦时钟速率与偏移，
   漂移超过 ppm 上限直接报 `DRIFT_EXCEEDED`。
2. 对每种首麦候选，枚举其余麦两两配对的双 TDOA 闭式解，并辅以房间覆盖网格
   生成候选声源；对每个候选位置按 **几何时差残差** 为各麦独立选峰
   （**峰强不参与选择**——强回声不会因强度压过几何一致的弱直达峰）。
3. 在最大绝对时差残差不超过容限的组合中，先按残差、再按候选序号向量
   字典序（首麦序号为最高位）裁决出定位点。
4. 若不同候选组合在容限内给出相距 **超过位置分辨率** 的位置，报告
   `AMBIGUOUS` 歧义，页面标出全部歧义位置，可点击切换查看各自的入选峰与
   各麦残差。

### 错误状态（页面与接口均返回原因）

| 状态码 | 含义 |
| --- | --- |
| `INVALID_INPUT` | 参数缺失/越界、麦数不在 4～8、候选峰数不在 1～12 等 |
| `MICS_COLLINEAR` | 麦克风共线或重合，无法平面定位 |
| `CALIB_REVERSED` | 校准时刻倒序（B 不晚于 A，或推得的发射时刻未递增） |
| `DRIFT_EXCEEDED` | 某麦时钟漂移超过设定的 ppm 上限 |
| `TARGET_OUTSIDE_CALIBRATION` | 目标候选峰位于两次校准时刻之外 |
| `NO_CONSISTENT_SOLUTION` | 所有候选组合的最大时差残差都超差（附最近似解供排查） |
| `AMBIGUOUS` | 容限内存在多组相距超过分辨率的位置（非错误，结果全部返回） |

## 四、接口示例

```bash
curl -s -X POST http://localhost:8000/api/locate \
  -H 'Content-Type: application/json' \
  -d '{
    "room_width": 8.0, "room_height": 6.0,
    "sound_speed": 343.0, "drift_ppm_limit": 50.0,
    "tdoa_tolerance": 0.00005, "position_resolution": 0.05,
    "microphones": [[1,1],[7,0.8],[7.5,5],[0.5,4.8]],
    "calib_pos_a": [0.6,0.4], "calib_pos_b": [7.4,5.6],
    "calib_a": [100.7211, 100.7302, 100.7188, 100.7120],
    "calib_b": [127.8816, 127.8701, 127.8620, 127.8733],
    "candidates": [
      [{"t":110.01234,"strength":0.15}, {"t":110.028,"strength":0.9}],
      [{"t":110.01610,"strength":0.15}, {"t":110.031,"strength":0.8}],
      [{"t":110.01402,"strength":0.15}, {"t":110.026,"strength":0.7}],
      [{"t":110.01155,"strength":0.15}, {"t":110.040,"strength":0.95}]
    ]
  }'
```

返回 JSON 的关键字段：`status`、`message`、`solution.position`、
`solution.max_residual`、`solution.peak_indices`、`solution.mics[].residual`、
`calibration[].drift_ppm`、`alternatives[]`、`nearest`（无解时）、
`solve_ms`。

## 五、目录结构

```
app/
  locator.py        # 时钟校准、闭式种子、矢量化枚举与抛光、裁决
  main.py           # FastAPI 接口与静态页面
  static/index.html # 原生 JS 页面（表单、Canvas 定位图、结果面板）
tests/              # pytest 功能/歧义/错误分支/性能测试
Dockerfile
docker-compose.yml
requirements.txt
```
