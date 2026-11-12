"""중앙 감독 에이전트 (SupervisorAgent).

4개 수술실의 종합 상태(RoomStatus)를 총괄 관리하며,
- 다중 수술실 우선순위 점수(Priority Score) 동적 산출 및 랭킹 관리
- 이상 발생 시 응급(EMERGENCY) 상태 전환 및 투약 제안 제어
- 응급 해제(EMERGENCY_CLEARED) 시 정상 투약 제안 복귀
- 전문의 승인(Approval) 요청 큐 관리 및 의사결정 처리
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import config
from agents.base import BaseAgent
from core.enums import AlertLevel, DrugType, EventType
from core.event_bus import EventBus
from core.models import Alert, DrugSuggestion, Event, RoomStatus, VitalSigns

logger = logging.getLogger(__name__)


class SupervisorAgent(BaseAgent):
    """전체 수술실의 종합 상태와 우선순위를 관리하는 중앙 감독관 에이전트."""

    def __init__(
        self,
        event_bus: EventBus,
        room_ids: Optional[List[int]] = None,
    ) -> None:
        """중앙 감독 에이전트 초기화.

        Args:
            event_bus: 중앙 이벤트 버스
            room_ids: 관리할 수술실 ID 목록 (기본값: [1, 2, 3, 4])
        """
        super().__init__(agent_id="supervisor", event_bus=event_bus)
        self.room_ids: List[int] = room_ids or [1, 2, 3, 4]

        # 4개 방의 종합 상태 객체 (room_id -> RoomStatus)
        self.room_statuses: Dict[int, RoomStatus] = {
            rid: RoomStatus(room_id=rid) for rid in self.room_ids
        }

        # 각 방별 경보 시작 시각 기록 (room_id -> timestamp)
        self._alert_start_times: Dict[int, float] = {}

        # 각 방별 직전 틱 심각도 값 기록 (추세 계산용: room_id -> severity_value)
        self._prev_severity: Dict[int, float] = {rid: 0.0 for rid in self.room_ids}

        # 전체 대기 중인 투약 제안 맵 (suggestion_id -> DrugSuggestion)
        self._pending_suggestions: Dict[str, DrugSuggestion] = {}

    async def start(self) -> None:
        """에이전트 시작 및 필요 이벤트 구독."""
        await super().start()
        self.event_bus.subscribe(EventType.VITAL_UPDATE, self._on_vital_update)
        self.event_bus.subscribe(EventType.ALERT, self._on_alert)
        self.event_bus.subscribe(EventType.ALERT_CLEARED, self._on_alert_cleared)
        self.event_bus.subscribe(EventType.DRUG_SUGGESTION, self._on_drug_suggestion)
        self.event_bus.subscribe(EventType.APPROVAL_RESPONSE, self._on_approval_response)

    # ── 이벤트 핸들러 ─────────────────────────────────────────

    async def _on_vital_update(self, event: Event) -> None:
        """생체신호 갱신 반영."""
        room_id = event.data.get("room_id")
        if room_id in self.room_statuses:
            vital_data = event.data.get("vitals") or {
                k: v for k, v in event.data.items() if k not in ("vitals", "alert_level")
            }
            self.room_statuses[room_id].vitals = VitalSigns(**vital_data)
            self.room_statuses[room_id].timestamp = event.timestamp

    async def _on_alert(self, event: Event) -> None:
        """경보 수신 시 활성 경보 목록 갱신 및 경보 시작 시각 기록."""
        alert = Alert(**event.data)
        room_id = alert.room_id
        if room_id in self.room_statuses:
            status = self.room_statuses[room_id]
            # 동일 파라미터 기존 경보 대체
            status.active_alerts = [a for a in status.active_alerts if a.parameter != alert.parameter]
            status.active_alerts.append(alert)

            self._recalculate_room_alert_level(room_id)

            if room_id not in self._alert_start_times:
                self._alert_start_times[room_id] = event.timestamp

    async def _on_alert_cleared(self, event: Event) -> None:
        """경보 해제 수신 시 활성 목록에서 제거."""
        room_id = event.data.get("room_id")
        param = event.data.get("parameter")
        alert_id = event.data.get("alert_id")

        if room_id in self.room_statuses:
            status = self.room_statuses[room_id]
            status.active_alerts = [
                a for a in status.active_alerts
                if a.id != alert_id and a.parameter != param
            ]
            self._recalculate_room_alert_level(room_id)

    async def _on_drug_suggestion(self, event: Event) -> None:
        """새 투약 제안 접수 시 승인 대기 큐 등록."""
        suggestion = DrugSuggestion(**event.data)
        room_id = suggestion.room_id
        if room_id in self.room_statuses:
            status = self.room_statuses[room_id]
            # 긴급(RED) 상태인 방의 제안은 접수 거절
            if status.is_emergency or status.alert_level == AlertLevel.RED:
                self.logger.warning(
                    "[Supervisor] Room %d 응급 상태로 인해 투약 제안 %s 접수 거부",
                    room_id,
                    suggestion.id,
                )
                return

            status.pending_suggestions.append(suggestion)
            self._pending_suggestions[suggestion.id] = suggestion

    async def _on_approval_response(self, event: Event) -> None:
        """승인 결과가 버스로 전달될 경우 큐 상태 동기화."""
        suggestion_id = event.data.get("suggestion_id")
        approved = event.data.get("approved")
        if suggestion_id:
            for status in self.room_statuses.values():
                status.pending_suggestions = [
                    s for s in status.pending_suggestions if s.id != suggestion_id
                ]
            if suggestion_id in self._pending_suggestions:
                self._pending_suggestions[suggestion_id].approved = approved

    def _recalculate_room_alert_level(self, room_id: int) -> None:
        """방의 활성 경보들을 바탕으로 종합 AlertLevel을 재계산합니다."""
        status = self.room_statuses[room_id]
        if any(a.level == AlertLevel.RED for a in status.active_alerts):
            status.alert_level = AlertLevel.RED
        elif any(a.level == AlertLevel.YELLOW for a in status.active_alerts):
            status.alert_level = AlertLevel.YELLOW
        else:
            status.alert_level = AlertLevel.GREEN
            self._alert_start_times.pop(room_id, None)

    # ── 핵심 프로세스 ──────────────────────────────────────────

    async def process(self, tick: float) -> None:
        """매 틱마다 전체 방의 우선순위를 계산하고 랭킹 및 비상 상황을 발행합니다.

        우선순위 점수 공식:
        score = (severity_value * PRIORITY_W_SEVERITY)
              + (duration_sec * PRIORITY_W_DURATION)
              + (trend * PRIORITY_W_TREND)

        - severity_value: GREEN=0, YELLOW=1, RED=3
        - duration_sec: 경보 지속 시간 (초)
        - trend: 악화=1, 유지=0, 호전=-1
        """
        for room_id, status in self.room_statuses.items():
            # 1. 심각도 값
            if status.alert_level == AlertLevel.RED:
                severity_value = 3.0
            elif status.alert_level == AlertLevel.YELLOW:
                severity_value = 1.0
            else:
                severity_value = 0.0

            # 2. 경보 지속 시간
            if status.alert_level == AlertLevel.GREEN:
                duration_sec = 0.0
            else:
                start_time = self._alert_start_times.get(room_id, tick)
                duration_sec = max(0.0, tick - start_time)

            # 3. 추세 판별
            prev_sev = self._prev_severity.get(room_id, 0.0)
            if severity_value > prev_sev:
                trend = 1.0  # 악화
            elif severity_value < prev_sev:
                trend = -1.0  # 호전
            else:
                trend = 0.0  # 유지
            self._prev_severity[room_id] = severity_value

            # 4. 우선순위 점수 계산
            score = (
                (severity_value * config.PRIORITY_W_SEVERITY)
                + (duration_sec * config.PRIORITY_W_DURATION)
                + (trend * config.PRIORITY_W_TREND)
            )
            status.priority_score = max(0.0, round(score, 2))
            status.timestamp = tick

            # 5. 응급 상태(EMERGENCY) 처리
            was_emergency = status.is_emergency
            is_emergency = (status.alert_level == AlertLevel.RED)
            status.is_emergency = is_emergency

            if is_emergency and not was_emergency:
                # 응급 진입: 대기 중인 투약 제안 취소 및 알림
                status.pending_suggestions.clear()
                self.logger.warning("[Supervisor] Room %d EMERGENCY 비상 상태 발생!", room_id)
                await self.emit(
                    EventType.EMERGENCY,
                    {
                        "room_id": room_id,
                        "timestamp": tick,
                        "message": f"수술실 {room_id} 위험 경보 (RED) 발생으로 긴급 모드 전환",
                        "active_alerts": [a.model_dump() for a in status.active_alerts],
                    },
                    tick,
                )
            elif not is_emergency and was_emergency:
                # 응급 해제
                self.logger.info("[Supervisor] Room %d EMERGENCY 비상 상황 해제", room_id)
                await self.emit(
                    EventType.EMERGENCY_CLEARED,
                    {
                        "room_id": room_id,
                        "timestamp": tick,
                        "message": f"수술실 {room_id} 위험 경보 해제되어 정상 모드로 복귀",
                    },
                    tick,
                )

        # 6. 우선순위 점수 내림차순 정렬
        ranked_rooms = sorted(
            self.room_statuses.values(),
            key=lambda r: r.priority_score,
            reverse=True,
        )

        # 7. PRIORITY_UPDATE 이벤트 발행
        await self.emit(
            EventType.PRIORITY_UPDATE,
            {
                "ranking": [r.room_id for r in ranked_rooms],
                "rankings": [
                    {
                        "room_id": r.room_id,
                        "score": r.priority_score,
                        "level": r.alert_level.value,
                    }
                    for r in ranked_rooms
                ],
                "rooms": [r.model_dump() for r in ranked_rooms],
                "timestamp": tick,
            },
            tick,
        )

    # ── 공용 조회 및 제어 인터페이스 ──────────────────────────

    def get_room_status(self, room_id: int) -> Optional[RoomStatus]:
        """특정 방의 종합 상태 반환."""
        return self.room_statuses.get(room_id)

    def get_all_statuses(self) -> Dict[int, RoomStatus]:
        """모든 방의 종합 상태 맵 반환."""
        return dict(self.room_statuses)

    async def handle_approval(
        self,
        suggestion_id: str,
        approved: bool,
    ) -> Optional[DrugSuggestion]:
        """전문의의 승인/거부 결정을 처리하고 이벤트를 브로드캐스트합니다.

        Args:
            suggestion_id: 투약 제안 ID
            approved: True(승인), False(거절)

        Returns:
            Optional[DrugSuggestion]: 처리된 제안 객체 또는 None
        """
        suggestion = self._pending_suggestions.get(suggestion_id)
        if not suggestion:
            # 방 목록 내부의 제안 검색
            for status in self.room_statuses.values():
                for s in status.pending_suggestions:
                    if s.id == suggestion_id:
                        suggestion = s
                        break
                if suggestion:
                    break

        if not suggestion:
            self.logger.warning("[Supervisor] 제안 ID %s를 찾을 수 없습니다.", suggestion_id)
            return None

        suggestion.approved = approved
        room_id = suggestion.room_id
        status = self.room_statuses.get(room_id)

        if status:
            # 대기 목록에서 제거
            status.pending_suggestions = [
                s for s in status.pending_suggestions if s.id != suggestion_id
            ]
            # 승인된 경우 목표 농도 반영
            if approved:
                if suggestion.drug_type == DrugType.PROPOFOL:
                    status.propofol_ce = suggestion.suggested_value
                elif suggestion.drug_type == DrugType.REMIFENTANIL:
                    status.remifentanil_ce = suggestion.suggested_value

        self._pending_suggestions.pop(suggestion_id, None)

        # APPROVAL_RESPONSE 이벤트 발행 (MedicationAgent 및 RecorderAgent가 수신)
        ts = status.timestamp if status else 0.0
        await self.emit(
            EventType.APPROVAL_RESPONSE,
            {
                "suggestion_id": suggestion_id,
                "room_id": room_id,
                "approved": approved,
                "drug_type": suggestion.drug_type.value,
                "suggested_value": suggestion.suggested_value,
                "timestamp": ts,
            },
            ts,
        )

        self.logger.info(
            "[Supervisor] 투약 제안 %s 결정 완료: approved=%s (Room %d, %s -> %.1f)",
            suggestion_id,
            approved,
            room_id,
            suggestion.drug_type.value,
            suggestion.suggested_value,
        )
        return suggestion
