# 阶段 2 详细设计 —— 控制操作 + 最小安全

> 依据 `frontend-separation.md` §五.4 / §五.5 / §六阶段 2。本文档只定方案,不含实现。
> 原则延续:契约先行;后端先定锁与鉴权契约,前端一次接齐。

> Historical design scope: this document describes the original Stage 2 proposal.
> Session persistence has since been implemented. SQLite is now the default;
> backend restart ends interrupted sessions without automatically resuming them.
> Raw signals are recorded by Acquisition, while the application SessionRuntime
> owns BCISession in the existing processing child. Current persistence, abort,
> recovery, configuration-history, and replay behavior is documented in
> [validation.md](validation.md#session-persistence-and-ownership).

---

## 一、目标与非目标

**目标**

1. 控制台可执行全部控制操作(引擎、采集、会话、刺激、录制)
2. 单 token 鉴权 + CORS 收敛,堵住"任何能访问端口的人都能操控引擎"
3. 控制者锁:同一时刻至多一个客户端可写,其余只读观察
4. 顺带定义 `PlatformAdapter` 接口(§8.2,为 Tauri 壳预留,本阶段仅接口)

**非目标**(留给阶段 3+)

- Historically deferred: SQLite session persistence, WebSocket heartbeat,
  multiple users/roles, privileged lock takeover, and configuration endpoints.
  This list records the original phase boundary, not the current implementation backlog.

---

## 二、控制端点盘点(锁的保护范围,共 16 个)

| 路由 | 端点 |
|---|---|
| engine | POST /engine/start, /engine/stop, /engine/processor/start, /engine/processor/stop |
| acquisition | POST /acquisition/connect, /disconnect, /stream/start, /stream/stop |
| stimulation | POST /stimulation/start, /stop, /trial/start |
| session | POST /sessions, DELETE /sessions/{id} |
| recording | POST /recording/start, /recording/stop |

全部 GET 端点与 WS `/ws/results` 不要求锁(对所有客户端开放)。

---

## 三、后端设计

### 3.1 控制者锁

**语义**:内存态互斥锁,带 TTL 与心跳续期。锁丢失(重启/过期)无副作用——锁只
约束"谁能发写请求",不影响引擎运行状态。

**服务**:`omnibci/application/services/control_lock_service.py`

```
class ControlLockService:
    acquire(client_name: str) -> LockGrant      # 空闲或已过期 → 发新锁; 否则 ControlHeldError
    heartbeat(lock_id: str) -> LockGrant        # 续期; lock_id 不匹配/已过期 → ControlLostError
    release(lock_id: str) -> None               # 幂等; 不匹配则忽略
    status() -> LockStatus                      # holder_name, expires_at, held: bool
```

- `LockGrant = {lock_id: UUID, holder: str, expires_at: datetime, ttl_seconds: int}`
- TTL 默认 30s,settings 可配;到期不自动通知,由下一次 acquire/校验时惰性判定
- 单进程单实例,装配根注入(与现有 service 同模式),无需持久化

**端点**(新路由 `routers/control.py`,prefix `/control`)

| 端点 | 行为 |
|---|---|
| POST /control/acquire `{client_name?}` | 200 `LockGrant` / 409 `CONTROL_HELD`(detail 带 holder 与 expires_at) |
| POST /control/heartbeat(头 X-Control-Token) | 200 新 `LockGrant` / 409 `CONTROL_LOST` |
| POST /control/release(头 X-Control-Token) | 200,幂等 |
| GET /control/status | 公开,前端用于显示"谁在控制" |

**强制方式**:FastAPI 依赖 `require_control`,读 `X-Control-Token` 头,校验通过放行;
缺头返回 409 `CONTROL_REQUIRED`,头无效/过期返回 409 `CONTROL_LOST`。逐个加到
§二的 16 个端点(显式优于路由级隐式,且契约测试能逐端点断言)。

**ErrorCode 新增**:`CONTROL_HELD`、`CONTROL_REQUIRED`、`CONTROL_LOST`
(契约冻结测试 `test_error_code_enum_values` 同 commit 更新)。

**免锁逃生口**:settings `SECURITY.control_lock_enabled = true`;
本机单人调试可关锁,关锁时 `require_control` 直接放行。

### 3.2 Token 鉴权 + CORS

**settings 新增段**

```toml
[SECURITY]
api_token = ""                 # 空字符串 = 关闭鉴权(本机可信模式)
allowed_origins = ["http://localhost:5173"]
control_lock_enabled = true
control_lock_ttl_seconds = 30
```

**REST**:ASGI 中间件校验 `Authorization: Bearer <token>`,覆盖 `/api/*`;
`/healthz`、`/docs`、`/openapi.json` 豁免。失败返回 401 `UNAUTHORIZED` 统一错误体。

**WebSocket**:浏览器 WS 无法带自定义头 → token 经 query 参数 `?token=` 传递,
握手时校验,失败 close(4401)。前端 ws-types.ts 不变(鉴权在连接层,不在消息层)。

**CORS**:`allow_origins` 改读 settings;`allow_credentials` 保持 true 时
origin 不可为 `*`,故默认值即为收敛后的白名单。

**鉴权与锁的关系**:token 是"能不能进门"(所有请求),锁是"进门后谁能动手"
(仅写请求)。两者独立开关,组合出本机调试/单人远程/多人观察三种形态。

### 3.3 契约同步

1. `scripts/export_openapi.py` 重跑 → `docs/openapi.json` 入库
2. `test_openapi_contract.py::test_exact_operation_set` 增补 4 个 control 端点
3. 前端 `npm run codegen` 再生 `rest-types.ts`,CI `codegen:check` 守卫

---

## 四、前端设计

### 4.1 控制状态机(派生,不引状态库)

UI 状态由四个轮询/事件源派生:`engineStatus`、`acquisitionStatus`、`activeSession`、
`lockState`(本地)。按钮可用性矩阵:

| 操作 | 前置条件 |
|---|---|
| engine/start | 持锁 且 engine 未运行 |
| engine/stop | 持锁 且 engine 运行中 |
| acquisition/connect | 持锁 且 engine 运行 且 未连接 |
| stream/start | 持锁 且 已连接 且 未流式 |
| 创建会话 | 持锁 且 engine 运行 且 无活动会话 |
| 结束会话 | 持锁 且 有活动会话 |
| stimulation/trial/start | 持锁 且 有活动会话 且 流式中 |
| recording/start / stop | 持锁 且(同 stream 条件 / 有活动录制) |

不满足前置的按钮置灰并悬浮提示原因;409 响应(竞态下后端兜底)以 toast 显示
`code` 对应文案,不弹错误页。

### 4.2 useControlLock hook

```
状态: idle(观察) | acquiring | held(持锁) | lost(被夺/过期)
acquire(): POST /control/acquire → held; 409 → idle + 显示当前 holder
held 期间: setInterval(TTL/3) 心跳; 心跳 409 CONTROL_LOST → lost → 自动降级只读
release(): 显式释放; 页面卸载时 navigator.sendBeacon 兜底释放
```

锁 token 仅存内存(刻意不进 localStorage:刷新即弃锁,TTL 兜底回收,避免僵尸锁)。

### 4.3 client.ts 扩展

- 新增 `post<T>(path, body?)`,统一注入 `Authorization`(api token,localStorage)
  与 `X-Control-Token`(内存)
- `api` 对象增补 16 个控制方法 + control 4 方法,类型全部来自再生的 rest-types.ts
- WS URL 拼接 `?token=`

### 4.4 UI 布局(单页,扩展现有 App.tsx)

```
┌──────────────────────────────────────────────────┐
│ 顶栏: 引擎● 采集● WS● │ 锁: 空闲/我持有/他人(名字) │ [接管控制] [设置token] │
├────────────┬─────────────────────────────────────┤
│ 控制面板    │  试次结果流(现有组件不动)             │
│ ┌Engine──┐ │                                     │
│ ┌Acquis──┐ │                                     │
│ ┌Session─┐ │                                     │
│ ┌Stim────┐ │                                     │
│ ┌Record──┐ │                                     │
└────────────┴─────────────────────────────────────┘
```

控制面板分组按依赖链排序(上→下即操作顺序),未持锁时整面板半透明只读。

### 4.5 PlatformAdapter(仅接口,Web 实现)

```ts
interface PlatformAdapter {
  backendBaseUrl(): string;          // Web: '' (同源/代理); Tauri: http://127.0.0.1:8000
  loadToken(): string | null;        // Web: localStorage; Tauri: OS keychain
  storeToken(t: string): void;
  onBeforeQuit(cb: () => void): void; // 释放锁的兜底; Web: beforeunload
}
```

---

## 五、测试与验收

**后端**(新增,常规 pytest)

- 锁服务:acquire 互斥、TTL 过期后可重获、heartbeat 续期、release 幂等、关锁开关
- `require_control`:缺头/无效/过期 → 三种 ErrorCode;持锁放行
- 鉴权中间件:无 token 401、错 token 401、豁免路径、token 关闭时放行;WS 4401
- 契约:operation set、ErrorCode 枚举、错误体 envelope 更新

**前端**:`npm run typecheck` + `codegen:check` 过 CI。

**验收(文档 §六原标准)**:两个浏览器标签页 → A 接管控制并执行
engine/start → connect → stream/start → 创建会话 → trial/start,结果流滚动;
B 全程只读、按钮置灰、可见"A 持有控制";A 关页 30s 内 B 可接管。
再以 `api_token` 非空重启后端,无 token 的标签页一切请求 401。

---

## 六、实施顺序与工作量

| # | 工作 | 量 |
|---|---|---|
| 1 | ErrorCode 扩枚举 + ControlLockService + 单测 | 0.5d |
| 2 | control 路由 + require_control 加 16 端点 + 路由测试 | 0.5d |
| 3 | SECURITY settings + 鉴权中间件 + CORS + WS token + 测试 | 0.5d |
| 4 | OpenAPI 导出 + 契约测试更新 + 前端 codegen | 0.25d |
| 5 | client.ts 扩展 + useControlLock + PlatformAdapter | 1d |
| 6 | 控制面板 UI + 状态机 + 锁状态顶栏 | 1~1.5d |
| 7 | 双标签页验收 + 修整 | 0.5d |

合计 ≈ 4.5 人日,与路线图"1 周"吻合。1→4 后端先行,5→7 前端接齐。

## 七、开放问题(默认倾向已标注)

1. **强占锁**:他人持锁时是否允许 `acquire(force=true)`?默认**不做**,等 TTL;
   实验室场景可接受,避免双控竞态。
2. **recording 与 session 路由重叠**(recording/start 内部建会话):锁依赖加在两处
   即可,无需合并;阶段 4 配置面时再审视。
3. **token 进前端 localStorage** 的 XSS 暴露面:单 token 本机/局域网场景可接受;
   若日后公网部署,升级为短时签发 token(超出本阶段范围)。
