"""
EventBus 富载荷契约 —— application 层（P2-8 自 infrastructure/outbound 迁入）。

这两个 dataclass 是跨进程 EventBus 事件的标准载荷：由 infrastructure 的
DomainEventPublisher 在领域事件翻译时构造，由 application 的读模型回调
（SessionAppService）和出站契约（infrastructure/outbound.event_to_wire）
消费。放在 application 层的原因：

  - application 服务需要对载荷做 **显式类型检查**（isinstance），若类型
    住在 infrastructure 会制造 application → infrastructure 反向依赖；
  - infrastructure 依赖 application 合法（生产方向内层契约看齐，DIP）。

两者都必须保持可 pickle（跨 mp.Queue）：str + int + frozen 的领域值对象。

载荷契约表（EventType → data 类型）见 shared/events.py。
"""
from dataclasses import dataclass

from omnibci.domain.classification import Classification


@dataclass(frozen=True)
class TrialOutcome:
    """EventType.TrialCompleted 的标准载荷 —— 会话上下文 + 分类结果。

    trial_number 为聚合根内的权威序号（1 起）；投递方若无法提供（例如
    测试构造），置 0 令读模型按本地计数补算。
    """
    session_id: str
    trial_number: int
    classification: Classification


@dataclass(frozen=True)
class SessionSummary:
    """EventType.SessionEnded 的标准载荷 —— 会话标识 + 总 trial 数。"""
    session_id: str
    total_trials: int


@dataclass(frozen=True)
class AcquisitionSnapshot:
    """EventType.AcquisitionState 的标准载荷 —— 设备端权威状态快照。

    由采集 worker 在每次状态转换后发布（快照直接读 SignalSource 端口的
    is_connected / is_streaming，不另设手工布尔）；API 侧
    AcquisitionAppService 以此维护状态投影，/acquisition/status 如实返回。
    """
    connected: bool
    streaming: bool
