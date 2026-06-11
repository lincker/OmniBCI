# OmniBCI Web 控制台（阶段 1：只读 MVP）

Vite + React + TS。只读：引擎/采集状态轮询（2s）+ WS 实时结果 + 断线重连快照。
控制操作、鉴权、控制者锁在阶段 2（见 `docs/frontend-separation.md` §六）。

## 运行

```bash
# 1. 后端（仓库根目录，venv 内）
uvicorn omnibci.presentation.main:app --port 8000

# 2. 前端
cd frontend
npm install
npm run dev          # http://localhost:5173，/api 经 Vite 代理到 127.0.0.1:8000
```

后端不在本机时：`VITE_BACKEND=http://<机架IP>:8000 npm run dev`。

生产构建：`npm run build` → `dist/` 静态文件，可由任意静态服务器或后端托管。

## 类型来源（不手写、不漂移）

- **REST**：`npm run codegen` 由 `docs/openapi.json` 生成 `src/api/rest-types.ts`；
  CI 的契约守卫保证 json 与后端代码同步。
- **WebSocket**：`src/api/ws-types.ts` 手写镜像后端 `schemas/ws.py`
  （4 条消息，受后端契约冻结测试保护；改 ws.py 必须同 commit 改它）。

## 重连快照（已实现，src/hooks/useResultsFeed.ts）

WS 只做增量；socket 每次 (重)连成功后拉快照
`GET /sessions/active` + `GET /sessions/{id}/trials`，再继续消费 WS，
按 `(session_id, trial_number)` 去重（聚合根单调递增，天然幂等键）。
收到未知会话的 TrialCompleted 时自动重新同步。重连退避 1s→×2→封顶 10s，
与后端 NetworkClient 监督循环对称。

## 验收路径（file_reader 回放）

1. `conf/settings.toml` 将 `ACQUISITION.signal_source` 指向 `file_reader`，
   `data_file_path` 指向 `tests/fixture/train.cnt`
2. 起后端 + 前端
3. 依次：POST /engine/start → /sessions → /acquisition/connect → /acquisition/stream/start
   （阶段 1 无控制按钮，可用 /docs Swagger 页触发）
4. 控制台应实时滚动试次结果；杀掉 WS（断网/重启后端）再恢复，
   试次列表应无缺无重。
