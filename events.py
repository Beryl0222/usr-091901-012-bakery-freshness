"""事件存储：追加式、按 event_id 幂等。所有库存变化都来自事件重放。

事件结构：
  event_id    全局唯一标识；重复提交同一 event_id 视为重放，无副作用
  type        事件类型（见 domain_contract.json event_types）
  occurred_at 业务发生时间（决定投影顺序，晚到事件按此归位）
  recorded_at 系统登记时间（只用于审计，不影响投影顺序）
  store_id / sku / batch_id / qty / payload  业务字段
"""

from timeutil import now_str


class DuplicateEvent(Exception):
    """同一 event_id 重复提交。"""

    def __init__(self, event_id):
        super().__init__(f"事件 {event_id} 已存在，按幂等重放忽略")
        self.event_id = event_id


class EventStore:
    def __init__(self):
        self._events = []
        self._ids = set()

    def append(self, event):
        """追加事件；event_id 重复时抛 DuplicateEvent，不产生任何效果。"""
        eid = event.get("event_id")
        if not eid:
            raise ValueError("事件缺少 event_id")
        if eid in self._ids:
            raise DuplicateEvent(eid)
        if "occurred_at" not in event:
            raise ValueError("事件缺少 occurred_at")
        event.setdefault("recorded_at", now_str())
        event.setdefault("payload", {})
        self._events.append(dict(event))
        self._ids.add(eid)
        return event

    def try_append(self, event):
        """幂等提交：重复时返回 (None, True)。"""
        try:
            return self.append(event), False
        except DuplicateEvent:
            return None, True

    def discard(self, event_id):
        """撤回最后追加的事件（用于提交校验失败时回滚）。"""
        for i in range(len(self._events) - 1, -1, -1):
            if self._events[i]["event_id"] == event_id:
                self._events.pop(i)
                self._ids.discard(event_id)
                return True
        return False

    def all(self):
        """按发生时间排序的全部事件（稳定排序，同时刻按登记顺序）。"""
        return sorted(self._events, key=lambda e: (e["occurred_at"], e["recorded_at"]))

    def by_id(self, event_id):
        for e in self._events:
            if e["event_id"] == event_id:
                return e
        return None

    def count(self):
        return len(self._events)

    def __len__(self):
        return len(self._events)
