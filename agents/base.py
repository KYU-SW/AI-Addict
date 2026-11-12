"""에이전트 기본 추상 클래스."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from core.event_bus import EventBus
from core.enums import EventType
from core.models import Event


class BaseAgent(ABC):
    """
    모든 에이전트의 부모 클래스.

    하위 클래스는 start(), stop(), process()를 구현해야 한다.
    """

    def __init__(self, agent_id: str, event_bus: EventBus) -> None:
        self.agent_id = agent_id
        self.event_bus = event_bus
        self._running = False
        self.logger = logging.getLogger(f"agent.{agent_id}")

    # ── 라이프사이클 ──────────────────────────────────────────

    async def start(self) -> None:
        """에이전트 시작."""
        self._running = True
        self.logger.info("Agent %s started", self.agent_id)

    async def stop(self) -> None:
        """에이전트 정지."""
        self._running = False
        self.logger.info("Agent %s stopped", self.agent_id)

    @abstractmethod
    async def process(self, data: Any) -> None:
        """매 틱마다 호출되는 핵심 처리 메서드."""
        ...

    # ── 헬퍼 ──────────────────────────────────────────────────

    async def emit(self, event_type: EventType, data: dict, timestamp: float) -> None:
        """이벤트 버스에 이벤트 발행 (편의 메서드)."""
        event = Event(
            type=event_type,
            data=data,
            source=self.agent_id,
            timestamp=timestamp,
        )
        await self.event_bus.publish(event)
