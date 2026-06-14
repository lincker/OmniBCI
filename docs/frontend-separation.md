# OmniBCI 前后端分离方案

> 范围：后端架构重构已完成（domain / shared / application / infrastructure / presentation + bootstrap）。本文给出在此基础上接入 Web 控制台的分离方案，并把现有 `presentation/` 的 REST + WebSocket 契约**逐端点盘出**作为前端对接的事实来源。
> 本文为设计与契约文档，不改动运行代码。

---

## 一、总体拓扑

运行时是三个进程，前端有两类，各司其职：

```
┌──────────────────┐      REST / WebSocket      ┌──────────────────────┐
│  Web 控制台 (新)  │ ◀───────────────────────▶ │  API 后端 (uvicorn)   │
│  React / Vue      │   配置·监控·会话·结果展示    │  presentation/ 层      │
└──────────────────┘                             └──────────┬───────────┘
                                                            │ EventBus (mp.Queue)
                                                            │ / socket
                          ┌─────────────────────────────────┼──────────────────┐
                          ▼                                 ▼                  ▼
                  ┌───────────────┐                 ┌───────────────┐  ┌──────────────────┐
                  │ NeuroEngine    │                 │ Acquisition    │  │ Stimulation (GL) │
                  │ 进程 (mp)       │                 │ worker (mp)    │  │ OpenGL/GLFW 进程  │
                  └───────────────┘                 └───────────────┘  └──────────────────┘
```

**谁渲染什么（关键决策）**

- **刺激呈现端**保留原生 OpenGL/GLFW 独立进程，**不进浏览器**。SSVEP 亮度按 `0.5 + 0.5·sin(2π·f/fs·frame + φ)` 逐帧驱动，60 Hz 下不能丢帧；浏览器的 requestAnimationFrame + 合成器抖动达不到时序精度。它本质上已是一个"前端"，经 socket 受控。
- **Web 控制台**是新增前端，只做低频、对时序不敏感的交互：设备/范式配置、引擎与采集监控、会话管理、分类结果展示。走 REST + WebSocket 正合适。

---

## 二、三层契约（按落地优先级）

| 层 | 边界 | 现状 | 待办 |
|---|---|---|---|
| ① REST/WS DTO | Web ↔ API | `presentation/schemas/` 完整 pydantic 模型 + 统一错误体 + 契约冻结测试（见第三节） | 运行导出脚本产出 `docs/openapi.json` 并接入 CI |
| ② WS 事件契约 | API → Web | `schemas/ws.py` union（含 `StimulationAck`）+ `infrastructure/outbound.py` 统一映射；`session_id` 为真实可查 id | 定义重连/快照语义（方案见 §五.3） |
| ③ IPC 契约 | API ↔ Engine ↔ GL | `Event` 信封 + `Frame`（4 字节版本头，`contracts.py` 收发校验）；会话生命周期 `StartSession`/`EndSession` 已入信封 | 逐事件 payload schema 化（风险高，放最后） |

前端的**单一事实来源**是 FastAPI 自动生成的 OpenAPI（`/openapi.json`、`/docs`）。前端用 `openapi-typescript` 之类工具生成类型，杜绝手写漂移。

---

## OpenAPI 导出与契约冻结（已落地）

OpenAPI 是 REST 契约的**单一事实来源**，前端据此生成 TS 类型，不手写、不漂移。

导出（在 venv 中）：

```bash
python scripts/export_openapi.py            # → docs/openapi.json
```

> ⚠️ 截至本次盘点，`docs/openapi.json` **尚未实际生成**——"已落地"的是导出脚本与冻结测试，不是导出物。前端开工前先跑一次，并建议把导出+diff 检查挂进 CI，保证 json 与代码同步。

前端生成类型（示例）：

```bash
npx openapi-typescript docs/openapi.json -o src/api/types.ts
```

**契约冻结**由 `tests/presentation/test_openapi_contract.py` 保证：钉死了
(1) 全部 22 个端点的 (path, method) 集合、(2) 关键响应 DTO 的字段集合
（`SessionResponse`/`TrialResponse`/`EngineStatusResponse`/`AcquisitionStatusResponse`）、
(3) 统一错误体 `ErrorResponse` 及 `ErrorCode` 枚举值。任何无意改动 DTO/端点都会让该测试失败——这就是"冻结"。

错误体已通过 `main.py` 的 `ERROR_RESPONSES` 声明在所有路由上，因此 OpenAPI 里能看到 `ErrorResponse`，前端类型也会包含错误形状。

---

## 三、REST 接口契约表（现状盘点）

公共前缀 `/api/v1`。所有成功响应统一包络 `ApiResponse`（见 §3.1）。所有请求/响应均为 `application/json`（健康检查除外）。

### 3.1 公共响应包络

`ApiResponse<T>`（`schemas/common.py`）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `success` | bool | `true` | 是否成功 |
| `message` | string | `"ok"` | 人类可读信息 |
| `data` | `T \| null` | `null` | 业务负载，按端点而定 |

> ✅ 已修：错误体已统一为 `ErrorResponse{success:false, code, message, detail?}`（全局异常处理器 + 路由 `responses` 声明），不再是裸 `{"detail": ...}`。`code` 是 `ErrorCode` 枚举，前端按 `code` 分支。

### 3.2 Engine —『/api/v1/engine』

| 方法 | 路径 | 请求体 | 成功响应 data | 成功码 | 错误码 |
|---|---|---|---|---|---|
| POST | `/engine/start` | — | （无 data） | 200 | 409 引擎已运行/未就绪 |
| POST | `/engine/stop` | — | （无 data） | 200 | — |
| POST | `/engine/processor/start` | — | （无 data） | 200 | 409 引擎未初始化 |
| POST | `/engine/processor/stop` | — | （无 data） | 200 | 409 |
| GET | `/engine/status` | — | `EngineStatusResponse` | 200 | — |

`EngineStatusResponse`：`running: bool`、`acquisition_alive: bool`、`processor_alive: bool`。

### 3.3 Acquisition —『/api/v1/acquisition』

| 方法 | 路径 | 请求体 | 成功响应 data | 成功码 | 错误码 |
|---|---|---|---|---|---|
| POST | `/acquisition/connect` | — | （无 data） | 200 | 503 引擎未启动 |
| POST | `/acquisition/disconnect` | — | （无 data） | 200 | 503 |
| POST | `/acquisition/stream/start` | — | （无 data） | 200 | 503 |
| POST | `/acquisition/stream/stop` | — | （无 data） | 200 | 503 |
| GET | `/acquisition/status` | — | `AcquisitionStatusResponse` | 200 | — |

`AcquisitionStatusResponse`：`connected: bool`、`streaming: bool`、`source?: string`、`sample_rate?: int`、`channel_count?: int`。

> ✅ 已修：`handle_get_status` 现已从 settings 填充 `source/sample_rate/channel_count`（best-effort，缺配置时为 `null`）。注意当前值来自**配置**而非设备实测元数据，设备热插拔场景下可能与实际不符——真实来源待接 `get_metadata` 链路。

### 3.4 Stimulation —『/api/v1/stimulation』

| 方法 | 路径 | 请求体 | 成功响应 data | 成功码 | 错误码 |
|---|---|---|---|---|---|
| GET | `/stimulation/paradigms` | — | `ParadigmsResponse` | 200 | — |
| POST | `/stimulation/start` | `StartStimulationRequest` | （无 data） | 200 | 503 |
| POST | `/stimulation/stop` | — | （无 data） | 200 | 503 |
| POST | `/stimulation/trial/start` | `StartTrialRequest` | （无 data） | 200 | 503 |

- `StartStimulationRequest`：`paradigm?: string`（缺省取配置值）
- `StartTrialRequest`：`n_trials: int = 1`（跑几个试次，范式无关的基础参数）、`trial_params?: int[]`（每个试次的范式专属参数，如目标/事件 id；提供时 `len == n_trials`，缺省 `null` 表示每个试次用范式默认）
- `ParadigmsResponse`：`paradigms: ParadigmInfo[]`；`ParadigmInfo{plugin_type, display_name, description?, version?, source, params?}`（`params` 为 manifest `[params]` 的只读键值，选中范式后在 Web 上展示）
- 现状 `paradigms` 为硬编码 `[classic40SSVEP]`，未来应从插件注册表动态注入。

### 3.5 Session —『/api/v1/sessions』

| 方法 | 路径 | 请求体 | 成功响应 data | 成功码 | 错误码 |
|---|---|---|---|---|---|
| POST | `/sessions` | `CreateSessionRequest` | `SessionResponse` | 201 | — |
| GET | `/sessions` | — | `SessionListResponse` | 200 | — |
| GET | `/sessions/active` | — | `SessionResponse` | 200 | 404 无活动会话 |
| GET | `/sessions/{id}` | — | `SessionResponse` | 200 | 404 会话不存在 |
| DELETE | `/sessions/{id}` | — | （无 data，结束会话） | 200 | 404 |
| GET | `/sessions/{id}/trials` | — | `TrialResponse[]` | 200 | 404 |
| GET | `/sessions/{id}/trials/last` | — | `TrialResponse` | 200 | 404 会话不存在 / 无试次 |

DTO 字段：

- `CreateSessionRequest`：`paradigm?: string`
- `SessionResponse`：`session_id: string`、`paradigm?: string`、`state: SessionStateDTO("running"|"ended")`、`trial_count: int`、`started_at?: datetime`、`ended_at?: datetime`
- `SessionDetailResponse`：继承 `SessionResponse` + `trials: TrialResponse[]`（注：当前路由未使用该 detail 模型，`/trials` 直接返回 `TrialResponse[]`）
- `SessionListResponse`：`sessions: SessionResponse[]`、`total: int`
- `TrialResponse`：`trial_number: int`、`target_id: int`、`event_id: int`、`confidence?: float[0..1]`、`forced: bool`、`produced_at: datetime`

### 3.6 Health

| 方法 | 路径 | 响应 | 说明 |
|---|---|---|---|
| GET | `/healthz` | `{"status":"ok"}` | **不**走 `ApiResponse` 包络（裸 JSON） |

### 3.7 通用错误

- 所有失败（`ApiError` / `HTTPException` / 422 校验 / 未捕获异常）经全局异常处理器统一为 `ErrorResponse{success:false, code, message, detail?}`，`code` 为 `ErrorCode` 枚举；前端按 `code` 分支，无需解析 message 文本。

---

## 四、WebSocket 契约 —『/api/v1/ws/results』

服务端单向推送（客户端发来的文本被读取循环丢弃，仅用于保活/探测断开）。每条消息含顶层 `event` 判别字段。

| `event` | 负载字段 | 触发源 |
|---|---|---|
| `TrialCompleted` | `session_id, trial_number, target_id, event_id, confidence?, forced, produced_at` | 引擎 `TrialCompleted` 领域事件 |
| `SessionStarted` | `session_id` | `SessionStarted` |
| `SessionEnded` | `session_id, total_trials` | `SessionEnded` |
| `StimulationAck` | `command, target_index?, status, message?` | 刺激进程经 socket 回推的命令回执 |
| `Ping` | （无负载） | 服务端心跳（每 ~15s；客户端静默 45s 即重连+快照,见 §五.3） |

类型定义见 `schemas/ws.py`（`WsMessage = Union[...]`），可直接用于前端代码生成。重 EEG 数据（Epoch）刻意不下发——前端只拿结果。

> ✅ 已修：`TrialCompleted` 携带 `TrialOutcome`、`SessionEnded` 携带 `SessionSummary`，WS 消息均为真实值。两条出站通道（WS、socket）共用 `infrastructure/outbound.py` 的 `event_to_wire`。
>
> ✅ **会话 ID 已全链路统一**（docs/session-id-unification.md）：WS 推送的 `session_id` 就是 REST 创建的 id，前端可直接拿它调 `GET /sessions/{id}` / `GET /sessions/{id}/trials`——这是重连快照方案（§五.3）的基础。

---

## 五、接入 Web 前需要补的契约缺口

盘点过程中发现的、在接前端前应处理的点（带 ✅ 的已随重构/修复完成）：

1. ✅ **统一错误体**。全局异常处理器 + `ErrorCode` 枚举，前端按 `code` 分支，已声明在 OpenAPI 上。
2. ✅ **WS 消息补全 `session_id`/`trial_number`**，且会话 id 已全链路统一（可直接回查 REST）。
3. **WS 重连/快照语义（方案已定，待实现）**。会话 id 统一后方案变得简单：**WS 只做增量，REST 负责快照**——前端重连成功后调 `GET /sessions/active` + `GET /sessions/{id}/trials` 拉全量补齐断线期间错过的试次，再继续消费 WS 增量；以 `trial_number` 去重（聚合根单调递增，天然幂等键）。服务端无需消息缓冲/重放机制。另需 WS 心跳（服务端定期 ping 或空消息），否则死连接只能在下次广播失败时才被清理。
4. **CORS 收敛 + 鉴权**。现状 `allow_origins=["*"]` 且零鉴权——任何能访问端口的人都能 start/stop 引擎。最小方案：settings 配置单 token，`Authorization: Bearer` 校验中间件，CORS 收敛到控制台域名；本机单用户场景可绑定 `127.0.0.1` 暂缓。
5. **控制权与并发**。引擎单例、会话默认内存仓储（重启即丢）。需定义「单控制者锁 vs 只读观察者」：建议控制类端点（engine/acquisition/stimulation 的 POST + sessions 写操作）要求持有控制锁，锁经 `POST /control/acquire` 获取、带 TTL 心跳续期，未持锁返回 409 `CONTROL_HELD`；查询与 WS 对所有客户端开放。持久化：把 `PERSISTENCE.session_repository`（装配根已预留开关）补一个 SQLite 适配器。
6. ✅ **刺激命令回执**。`StimulationAck` 链路已完整：stim 进程 → socket（入站白名单）→ 引擎 `StimulationFeedbackHandler` → WS `StimulationAck` 消息推给前端。
7. **范式列表动态化**。`/stimulation/paradigms` 仍硬编码 `[classic40SSVEP]`；配置型前端需要它来自插件注册表（`Paradigm._target_registry`），否则新增范式插件前端不可见。
8. **配置读写端点缺失**。控制台要做"设备/范式配置"，但后端没有暴露 settings 的读写 API；需要新增 `GET/PATCH /config`（白名单键、改后需重启引擎的键要标注）——否则前端只能展示、不能配置。

---

## 六、落地顺序（修订版：每步可独立交付、可演示，风险递增）

> 原则：契约先行；先只读后控制；先单机后多客户端；IPC 重构永远放最后。

**阶段 0 —— 契约固化（半天）**
跑 `scripts/export_openapi.py` 产出 `docs/openapi.json` 入库；CI 加一步"导出 + git diff 为空"校验，杜绝契约与代码漂移。前端仓库用 `openapi-typescript` 生成类型，`schemas/ws.py` 对应的 WS 消息类型手写一份（仅 4 条，变更受契约冻结测试保护）。

**阶段 1 —— 只读控制台 MVP（1~2 周，最大风险敞口在前端工程而非后端）**
Vite + React/Vue + TS；数据层 = REST 轮询（engine/acquisition status，2s 间隔足够）+ WS 增量（results）。实现 §五.3 的重连快照逻辑。验收：用 file_reader 回放 `tests/fixture/train.cnt`，控制台实时显示试次结果，断 WS 重连后试次列表无缺无重。此阶段后端**零改动**。

**阶段 2 —— 控制操作 + 最小安全（1 周）**
控制台接入 start/stop 引擎、connect/stream、创建/结束会话、触发刺激试次。同步落地 §五.4 token 鉴权 + CORS 收敛、§五.5 控制者锁。验收：两个浏览器标签页，一个持锁可操作、一个只读观察，互不干扰。

**阶段 3 —— 状态健壮性（1 周）**
SQLite 会话仓储（装配根开关切换，应用层零改动）；WS 心跳 ✅（服务端 15s `Ping` + 前端 45s 静默看门狗，见 §四）。

> 已划掉「`/engine/status` 增补当前会话 id 与处理器待命/运行态」：动机（前端区分"引擎活着但无会话"）已被 `GET /sessions/active`（`NO_ACTIVE_SESSION` 为正常态）+ WS `SessionStarted/SessionEnded` + 重连快照覆盖；处理器"待命/运行"可由 `processor_alive && 有活动会话` 推导，进程内部真实状态机的回传链路成本不抵收益。剩余价值（单请求原子状态快照）归入阶段 4 调试面板按需做。

**阶段 4 —— 配置面（1~2 周，按需）**
范式列表接插件注册表（§五.7）；`GET/PATCH /config` 配置端点（§五.8）；acquisition 元数据从设备 `get_metadata` 实测回填；（按需）调试面板用的原子状态快照端点。

**阶段 5 —— IPC payload schema 化（机会性）**
版本头与收发校验已具备（`contracts.py`），剩余工作是逐事件 payload 的显式 schema。仅在跨进程契约再次出 bug 或要支持第三方刺激端实现时才值得做——不要为做而做。

---

## 七、与已完成重构的衔接

这套方案完全踩在已完成的五层结构上：Web 前端只对接 `presentation/`，命令落到 `application/` 的 CQRS handler，事件经已修复的钩子链路（`register_engine_listeners`）从 EventBus 推到 WS。换言之，P0–P4 + 命名收口 + 钩子修复，正是为「干净地接一个外部前端」打的地基——现在加前端不需要再动 `domain/` 或 `application/`。

此后的 bug 修复轮（docs/bug-report.md B1–B6）与会话 ID 统一进一步扫清了前端接入的隐性障碍：解码链路契约修复（B6）让结果流真实可用；`NetworkClient` 监督循环重连（B3）让刺激端链路可生产；会话 ID 统一让 WS 与 REST 首次构成自洽闭环。约 450 项测试在纯 Linux 环境全绿，阶段 1 的只读控制台可以直接开工。

---

## 八、桌面 + Web 双形态方案（控制台跨平台）

### 8.1 部署前提：控制台可分离，刺激端不可远程

系统物理上分两块。**机架**（EEG 设备 + 采集进程 + 刺激 OpenGL 进程 + uvicorn 后端）必须同机——刺激端直接占显卡与显示器逐帧驱动，不可能远程。**控制台**低频、对时序不敏感，可以在任意位置。

因此"桌面 vs Web"不是二选一，而是控制台的三种部署位置，**一套前端代码全覆盖**：

| 部署位置 | 形态 | 后端来源 |
|---|---|---|
| 机架本机 | 桌面 app | 随包 sidecar 拉起，或连本机已运行实例 |
| 同 LAN 另一台机器 | 桌面 app（纯客户端） | 远程连机架后端 |
| 任意浏览器 | Web SPA | 远程连机架后端（`dist/` 静态分发） |

### 8.2 核心方案：一份 SPA，桌面壳包一层

`frontend/` SPA 是唯一代码源。Web 部署 = `dist/` 静态文件；桌面部署 = 同一个 `dist/` 塞进桌面壳。UI、生成的 REST 类型（`rest-types.ts`）、WS 客户端、重连快照逻辑全部平台无关，原封复用。

平台差异收敛到一个 `PlatformAdapter` 接口（阶段 2 顺带定义）：

```ts
interface PlatformAdapter {
  apiBaseUrl(): string;            // web: 同源/配置；desktop: http://127.0.0.1:<port>
  backend: {                       // 后端生命周期（仅机架本机桌面形态非 no-op）
    ensureRunning(): Promise<void>;   // spawn sidecar → 轮询 /healthz
    stop(): Promise<void>;
  };
  pickFile?(filter: string): Promise<string | null>;  // 原生文件对话框（选 .cnt 等）
}
```

其余代码不感知运行环境。

### 8.3 桌面壳选型：Tauri（推荐）

| | Tauri | Electron |
|---|---|---|
| 包体 | ~10MB（系统 webview） | ~150MB（自带 Chromium） |
| 后端托管 | sidecar 机制专为"随包附带二进制"设计 | Node spawn，也能做 |
| 渲染一致性 | 依赖各平台 webview（控制台够用） | 全平台 Chromium 一致 |
| 团队成本 | 需碰少量 Rust（壳层很薄） | 纯 JS/Node |

控制面板（非刺激渲染器）场景下 Tauri 明显合适：轻、sidecar 正好托管后端。仅当团队完全不碰 Rust 或需要 Chromium 级一致渲染时选 Electron。

### 8.4 真正的成本：后端打包（不是前端）

"双击即用、自带后端"意味着把 Python + numpy/scipy/mne + 设备/OpenGL 依赖用 PyInstaller 冻成每 OS 一份的 sidecar 二进制——整个方案最重、最平台相关的工作。两条路线：

- **A（推荐）**：仅"机架本机桌面 app"打包后端（PyInstaller sidecar；启动拉起，`/healthz` 就绪后再载入 UI）；远程桌面 app 与浏览器一律纯客户端。
- **B（早期省事）**：全部形态都当纯客户端，后端由机架单独安装运行。UX 略差，但完全跳过跨平台冻结工作。

### 8.5 对路线图的影响

- 双形态使阶段 2 的**鉴权从可选变硬性**：只要控制台可远程（LAN 浏览器/远程桌面 app），token + CORS 收敛必须先行——否则同网段任何人可操控引擎与设备。本机 `127.0.0.1` 形态可信任免鉴权。
- 增量节奏：阶段 1 纯 Web SPA 跑通只读闭环（零桌面复杂度）→ 阶段 2 做鉴权时定义 `PlatformAdapter` → 阶段 3 之后再加 Tauri 壳 + sidecar 打包（路线 A）。桌面壳是"加包装"，不阻塞任何核心功能。
