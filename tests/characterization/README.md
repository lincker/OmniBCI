# Characterization Tests — 重构安全网

> 重构第一步：在动任何结构之前，先用特征测试（characterization tests）把
> 四个限界上下文**当前的可观察行为**固化下来。重构（P0 端口化、P1 目录归位…）
> 后只要这套测试仍全绿，就能确信对外行为没有被破坏。

特征测试 ≠ 单元测试断言"应该如何"。它断言的是"现在就是这样"——哪怕某些行为
将来想改，也先锁住，改的时候再有意识地更新断言。

## 覆盖范围（每个上下文一组）

| 文件 | 上下文 | 接缝 | 锁定的行为 |
|---|---|---|---|
| `test_session_characterization.py` | **Session** | `BCISession` 聚合 + `InMemorySessionRepository` + `DomainEventPublisher` (ACL) | 状态机 IDLE→RUNNING→ENDED、试次不变量与编号、领域事件序列、仓储 active/顺序/深拷贝隔离、领域事件→IPC Event 翻译表 |
| `test_neuroengine_characterization.py` | **NeuroEngine** | `EpochManager`（注入 fake 解码器/检测器） | epoch 缓冲切片/拼接/截断、不可变 `Epoch` 构造、`(bool,int)`→`Classification` 包装、`forced` 标志传播 |
| `test_acquisition_characterization.py` | **Acquisition** | 插件注册表/工厂 + `FileReader` 端口生命周期（注入 fake adapter） | `file_reader` 自注册、工厂按配置解析、connect/stream/read_chunk 指针步进/stop/disconnect |
| `test_stimulation_characterization.py` | **Stimulation** | `SSVEPConfig` 范式配置 + 亮度数学契约 | classic40SSVEP 的频率/相位表、5×8 字符布局、目标几何、正弦亮度公式 |

刺激的 OpenGL/GLFW 渲染路径依赖窗口与显卡，**刻意不纳入**——无头环境无法稳定
断言。锁定的是其上游纯逻辑（配置与几何）。

## 运行方式

需在装好依赖的虚拟环境中运行（sandbox 仅有 numpy，无法跑全套）：

```bash
source venv/bin/activate
pip install -r requirements/development.txt   # pytest / fastapi / mne / tomlkit ...
pytest tests/characterization -v
```

只跑某一个上下文：

```bash
pytest tests/characterization/test_session_characterization.py -v
```

### 已就地验证的部分
Session / NeuroEngine / Stimulation 三组共 **59 项**已在纯 numpy 环境下用独立
runner 跑通（不依赖 pytest 插件）。Acquisition 一组需 `tomlkit`（settings）与
`mne`，请在 venv 中执行。

## 这套网在重构时会替你抓到什么

1. **P0 端口化（消除泄漏抽象）**：`test_session_*` 把"领域事件 → IPC Event"的
   翻译完全钉死。当你为引擎引入 `EnginePort`、让 `AcquisitionAppService` 不再
   穿透 `engine_service._engine.event_bus` 时，事件链路一旦错位立刻报红。
2. **P1 目录归位**：把 `neuroengine/`、`acquisition/`、`stimulation/` 迁入
   `infrastructure/` 时，import 路径改动若打断了流水线/注册/配置，对应上下文的
   测试会立即失败。
3. **插件机制回归**：`test_acquisition_*` 锁住 `Registerable` 自注册 + 工厂解析，
   保证"换设备只改 settings"的能力在搬动文件后依旧成立。

## 重构中发现的待修问题（特征测试已规避并记录）

- `omnibci/stimulation/creator/ssvep_target_rect.py` 含裸导入
  `from stimulation.ui.view import View`（缺 `omnibci.` 前缀），多数环境无法直接
  导入该模块。`test_stimulation_*` 因此以独立实现固化亮度公式，待 P1 修复该导入后
  可改为直接比对真实 `StimTargetRect.cal_brightness`。
- `omnibci/stimulation/creator/factory.py` 同样存在 `from ssvep_target_rect import …`
  裸导入。建议在 P1 目录归位时一并改为包内绝对导入。

## 约定

- 新增上下文行为时，**先在这里补特征测试**，再动实现。
- 若有意改变某个被锁定的行为，更新对应断言并在 commit message 说明"为什么这次
  行为变更是预期的"。
