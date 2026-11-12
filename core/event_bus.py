"""asyncio 기반 이벤트 버스 — 에이전트 간 통신 허브."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine, Dict, List, Optional

from .enums import EventType
from .models import Event

logger = logging.getLogger(__name__)

# 콜백 타입: async def handler(event: Event) -> None
Handler = Callable[[Event], Coroutine]


class EventBus:
    """
    Publish-Subscribe 패턴 이벤트 버스.

    - subscribe(event_type, handler): 특정 이벤트 구독
    - subscribe_all(handler): 모든 이벤트 구독 (기록 에이전트용)
    - publish(event): 이벤트 발행 → 구독자에게 비동기 전달
    """

    def __init__(self) -> None:
        self._subscribers: Dict[EventType, List[Handler]] = {}
        self._global_subscribers: List[Handler] = []
        self._history: List[Event] = []
        self._ws_handlers: List[Callable] = []  # WebSocket 브로드캐스트용

    # ── 구독 ──────────────────────────────────────────────────

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        """특정 EventType에 핸들러 등록."""
        self._subscribers.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        """모든 이벤트를 수신하는 핸들러 등록."""
        self._global_subscribers.append(handler)

    def add_ws_handler(self, handler: Callable) -> None:
        """WebSocket 브로드캐스트 핸들러 등록."""
        self._ws_handlers.append(handler)

    def remove_ws_handler(self, handler: Callable) -> None:
        """WebSocket 핸들러 제거."""
        if handler in self._ws_handlers:
            self._ws_handlers.remove(handler)

    # ── 발행 ──────────────────────────────────────────────────

    async def publish(self, event: Event) -> None:
        """이벤트 발행: 히스토리 저장 → 구독자 호출 → WS 전달."""
        self._history.append(event)

        # 타입별 구독자
        for handler in self._subscribers.get(event.type, []):
            try:
                await handler(event)
            except Exception:
                logger.exception("Handler error for %s", event.type)

        # 글로벌 구독자 (기록 에이전트 등)
        for handler in self._global_subscribers:
            try:
                await handler(event)
            except Exception:
                logger.exception("Global handler error")

        # WebSocket 브로드캐스트
        for ws_handler in list(self._ws_handlers):
            try:
                await ws_handler(event)
            except Exception:
                logger.debug("WS handler removed (disconnected)")
                self.remove_ws_handler(ws_handler)

    # ── 히스토리 조회 ─────────────────────────────────────────

    def get_history(
        self,
        event_type: Optional[EventType] = None,
        room_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[Event]:
        """이벤트 히스토리 필터링 조회."""
        result = self._history
        if event_type is not None:
            result = [e for e in result if e.type == event_type]
        if room_id is not None:
            result = [e for e in result if e.data.get("room_id") == room_id]
        return result[-limit:]

    def clear_history(self) -> None:
        """히스토리 초기화."""
        self._history.clear()
