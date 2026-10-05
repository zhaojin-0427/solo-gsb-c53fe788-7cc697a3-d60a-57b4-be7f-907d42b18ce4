# 多麦克风敲击定位 Web 台

在矩形房间内，利用 4～8 个**各自使用独立时钟**的麦克风采集到的敲击峰候选，
完成时钟校准、候选峰联合选择与声源定位。后端 Python / FastAPI + NumPy / SciPy，
前端为零依赖原生 JavaScript（SVG 房间定位图），支持 Docker Compose 一键启动。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

启动后打开浏览器访问：

- **房间定位图（主界面）**：<http://localhost:8000/>
- 健康检查：<http://localhost:8000/api/health>
- 接口文档（Swagger）：<http://localhost:8000/docs>

容器按需求限制为 **2 CPU / 1 GiB 内存**（见 `docker-compose.yml` 的 `cpus` /
`mem_limit`）。停止服务：`docker compose down`。

页面右上角提供两个一键示例：

- **载入示例 · 8 麦 × 12 峰**：8 个麦、每麦 12 个候选，直达峰弱、回声强（5～10 倍），
  用于验证「强回声不会压过几何一致的直达峰」。
- **载入示例 · 4 麦**：最小配置。

填入或载入数据后点击 **定位求解**，定位点、入选峰、歧义点和各麦残差直接绘制在房间图上。

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r backend/requirements.txt
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

运行测试：

```bash
python tests/test_locate.py
```

## 录入参数说明

| 参数 | 含义 |
| --- | --- |
| 房间宽 / 高 | 矩形房间尺寸（米），坐标原点在左下角 |
| 麦克风 x / y | 4～8 个麦的坐标，必须全部位于房间内且**非共线**；**1 号麦是时基基准** |
| 声速 | 正数（m/s），如 343 |
| 漂移上限 | 相对首麦允许的时钟速率偏差（ppm），非负 |
| 时差容限 | TDOA 残差容限（毫秒），最大绝对残差不超过该值才算一致解 |
| 位置分辨率 | 两个解相距超过该值（米）即视为不同位置（歧义判据） |
| 校准敲击 A / B | **两次**位置已知的敲击，且在时间上**分列目标事件前后**；录入各麦（独立时钟）到达时刻 |
| 目标候选峰 | 每麦 1～12 个候选，每个候选含 `time`（该麦时钟下的时刻，秒）与 `strength`（峰强度） |

### 峰强度的地位

候选峰的 `strength` **仅用于页面展示，不参与任何几何裁决**。选峰由 TDOA 几何
一致性决定，因此即使回声峰强度远高于直达峰，也不会被误选。

## 算法概要（`backend/app/locate.py`）

1. **输入校验**：房间/声速/容限/分辨率合法性、麦数 4～8、麦克风在房间内、
   非共线（SVD 最小奇异值）、两次校准时刻严格递增且发射时刻不倒序。
2. **时钟校准**：以 1 号麦为时基，每个其它麦的时钟建模为
   `t = o + (1+δ)·t0`。用两次已知位置校准点，按各自反推的**发射时刻间隔之比**
   求速率偏差 `δ`，再由校准点等式求偏移 `o`；`|δ|` 超出 ppm 上限直接报错。
3. **候选峰时基换算**：所有候选时刻换算到 1 号麦时基。
4. **闭式种子**：选与 1 号麦构成最大三角形的两个锚点麦，枚举三麦候选组合
   （≤ 12³ = 1728），两条双曲线（线性化 + R 的二次方程）给出闭式交点；
   非锚点麦先取最近候选并做残差预筛。闭式种子全部退化时，使用**分块房间网格**
   兜底（内存与网格总量无关，峰值约 10 MB 临时量）。
5. **精修**：对限量种子做带解析梯度的 L-BFGS-B（log-sum-exp 光滑化最大残差），
   房间边界约束，非锚点麦在精修后重新选最近候选并二次精修。
6. **裁决**：所有候选组合/位置中，先按**最大绝对 TDOA 残差**升序、残差相同时
   再按**各麦候选序号（1 基）字典序**裁决，序号小者优先。
7. **歧义报告**：容限内的解先按分辨率聚类；若存在多个彼此相距 **超过位置分辨率**
   的位置，主解之外的位置作为 `ambiguous_positions` 返回，状态为 `AMBIGUOUS`。

### 返回状态

| status | 含义 |
| --- | --- |
| `OK` | 容限内唯一定位（按分辨率聚类后） |
| `AMBIGUOUS` | 容限内存在相距超过分辨率的多个位置 |
| `NO_SOLUTION` | 无一致解；响应附带 `best_attempt`（容限外最优组合，页面前端红点展示） |
| 400 `CALIBRATION_REVERSED` | 校准时刻倒序（含几何反推发射时刻倒序、单麦非单调） |
| 400 `MICROPHONES_COLLINEAR` | 麦克风共线 |
| 400 `DRIFT_EXCEEDED` | 某麦相对首麦的漂移超过 ppm 上限 |
| 400 `INVALID_*` / 422 | 其它参数错误 |

成功响应还包含：`selected_peaks`（各麦入选峰、实测/预测 TDOA 与残差）、
`clock_calibration`（各麦漂移与偏移）、`anchor_mics`、`elapsed_ms`、
`used_grid_fallback`、`warnings`（如候选峰落在两次校准时刻之外）。

## 性能

8 麦、每麦 12 候选的交互求解（含闭式搜索 + 精修 + 聚类）在 2 核 / 1 GiB
限制下实测约 **0.2～0.3 秒**（5 秒预算内余量充足；网格兜底最坏场景峰值
驻留内存约 250 MB）。后端有 4.5 秒求解截止时间保护。

## HTTP 接口

`POST /api/locate`，JSON 请求体示例：

```json
{
  "room": {"width": 8.0, "height": 6.0},
  "microphones": [
    {"x": 0.5, "y": 0.5}, {"x": 7.5, "y": 0.5},
    {"x": 7.5, "y": 5.5}, {"x": 0.5, "y": 5.5}
  ],
  "sound_speed": 343.0,
  "drift_ppm": 50.0,
  "tdoa_tolerance_ms": 0.1,
  "resolution": 0.05,
  "calibrations": [
    {"position": {"x": 1.0, "y": 3.0}, "arrivals": [0.108, 0.101, 0.106, 0.104]},
    {"position": {"x": 6.5, "y": 2.5}, "arrivals": [0.909, 0.902, 0.908, 0.915]}
  ],
  "target": {
    "candidates": [
      [{"time": 0.360, "strength": 0.31}, {"time": 0.368, "strength": 9.1}],
      [{"time": 0.351, "strength": 0.39}, {"time": 0.360, "strength": 6.4}],
      [{"time": 0.367, "strength": 0.31}, {"time": 0.372, "strength": 5.3}],
      [{"time": 0.357, "strength": 0.32}, {"time": 0.363, "strength": 7.9}]
    ]
  }
}
```

完整字段约束见 `backend/app/models.py`，交互文档见 `/docs`。

## 目录结构

```
backend/
  app/
    main.py      # FastAPI 入口与静态资源
    models.py    # Pydantic 请求模型
    locate.py    # 时钟校准、闭式/网格搜索、精修与裁决核心
  requirements.txt
frontend/
  index.html     # 房间定位图与录入界面
  styles.css
  app.js         # 原生 JS：表单、SVG 绘图、残差卡、示例数据
tests/
  test_locate.py # 合成场景端到端测试（校准/回声/歧义/错误分支/8×12 性能/内存）
Dockerfile
docker-compose.yml
```
