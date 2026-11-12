"""투약 제안 에이전트 (MedicationAgent).

환자의 생체신호(BIS, MAP) 동태를 실시간으로 추적하여,
정맥 마취제(프로포폴) 및 진통제(레미펜타닐)의 목표 혈중/효과처 농도(Ce) 조절안을 제안합니다.

규칙:
1. 유지기(MAINTENANCE)에만 동작하며, 응급(Emergency) 상태인 경우 제안을 중단합니다.
2. BIS > 60이 30초 이상 지속되면 프로포폴 +0.5 mcg/mL 증량 제안.
3. BIS < 40이 30초 이상 지속되면 프로포폴 -0.5 mcg/mL 감량 제안 (BIS=0 센서 이상 제외).
4. MAP > 100이 30초 이상 지속되면 레미펜타닐 +0.5 ng/mL 증량 제안.
5. MAP < 60이 30초 이상 지속되면 레미펜타닐 -0.5 ng/mL 감량 제안.
6. 제안 후 60초간 동일 약물에 대한 쿨다운(Cooldown)을 적용합니다.
7. 의료진 승인(Approval) 시 내부 목표 농도를 갱신합니다.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import config
from agents.base import BaseAgent
from core.enums import AlertLevel, DrugType, EventType, Phase, SuggestionAction
from core.event_bus import EventBus
from core.models import DrugSuggestion, Event, RoomStatus, VitalSigns

logger = logging.getLogger(__name__)


class MedicationAgent(BaseAgent):
    """단일 수술실의 마취 약물 목표 농도(Ce) 조절을 제안하는 에이전트."""

    def __init__(self, room_id: int, event_bus: EventBus) -> None:
        """투약 제안 에이전트 초기화.

        Args:
            room_id: 대상 수술실 번호
            event_bus: 중앙 이벤트 버스
        """
        super().__init__(agent_id=f"medication_{room_id}", event_bus=event_bus)
        self.room_id: int = room_id

        # 현재 약물 목표 농도 (Ce)
        self.propofol_ce: float = 3.0       # mcg/mL
        self.remifentanil_ce: float = 3.0   # ng/mL

        # 이상 상태 지속 시작 시각 (초)
        self._bis_high_start: Optional[float] = None
        self._bis_low_start: Optional[float] = None
        self._map_high_start: Optional[float] = None
        self._map_low_start: Optional[float] = None

        # 약물별 최근 제안 시각 (쿨다운 60초 계산용)
        self._last_propofol_sugg_time: Optional[float] = None
        self._last_remifentanil_sugg_time: Optional[float] = None

        # 승인 대기 중인 제안 맵 (suggestion_id -> DrugSuggestion)
        self._pending_suggestions: Dict[str, DrugSuggestion] = {}

    async def start(self) -> None:
        """에이전트 시작 및 승인 응답 이벤트 구독."""
        await super().start()
        self.event_bus.subscribe(EventType.APPROVAL_RESPONSE, self._on_approval_response)

    async def _on_approval_response(self, event: Event) -> None:
        """이벤트 버스로부터 전달받은 승인/거부 응답 처리."""
        target_room = event.data.get("room_id")
        if target_room is not None and target_room != self.room_id:
            return

        suggestion_id = event.data.get("suggestion_id")
        approved = event.data.get("approved")
        if suggestion_id and approved is not None:
            self.apply_approval(suggestion_id, bool(approved))

    async def process(
        self,
        vitals: VitalSigns,
        room_status: Optional[RoomStatus] = None,
    ) -> None:
        """생체신호 및 방 상태를 평가하여 투약 조절을 제안합니다.

        Args:
            vitals: 현재 생체신호 스냅샷
            room_status: 중앙 감독 에이전트가 관리하는 방 종합 상태 (선택)
        """
        timestamp = vitals.timestamp

        # 1. 응급 상황 검사: RED 경보 또는 긴급 상태 시 투약 제안 중단
        if room_status is not None:
            if room_status.is_emergency or room_status.alert_level == AlertLevel.RED:
                self.logger.debug("[Room %d] 응급 상태 감지 — 투약 제안 일시 중단", self.room_id)
                self._reset_persistence_timers()
                return

            if room_status.phase != Phase.MAINTENANCE:
                # 유지기 이외 단계에서는 규칙 기반 제안 건너뜀
                return

        # 2. BIS 기반 프로포폴 조절 평가
        await self._evaluate_propofol(vitals, timestamp)

        # 3. MAP 기반 레미펜타닐 조절 평가
        await self._evaluate_remifentanil(vitals, timestamp)

    def reset(self) -> None:
        """시뮬레이션 리셋 시 투약 상태, 지속 타이머, 쿨다운, 대기 제안을 모두 초기화합니다."""
        self.propofol_ce = 3.0
        self.remifentanil_ce = 3.0
        self._reset_persistence_timers()
        self._last_propofol_sugg_time = None
        self._last_remifentanil_sugg_time = None
        self._pending_suggestions.clear()

    def _reset_persistence_timers(self) -> None:
        """이상 지속 타이머 초기화."""
        self._bis_high_start = None
        self._bis_low_start = None
        self._map_high_start = None
        self._map_low_start = None

    async def _evaluate_propofol(self, vitals: VitalSigns, timestamp: float) -> None:
        """BIS 기반 프로포폴 Ce 조정안 검토."""
        if vitals.bis is None or vitals.bis == 0.0:
            # BIS가 없거나 0(센서 탈락 의심)인 경우 타이머 리셋 후 건너뜀
            self._bis_high_start = None
            self._bis_low_start = None
            return

        # BIS > 60: 각성 위험 -> 프로포폴 증량 (+0.5)
        if vitals.bis > config.BIS_TARGET_HIGH:
            self._bis_low_start = None
            if self._bis_high_start is None:
                self._bis_high_start = timestamp

            elapsed = timestamp - self._bis_high_start
            if elapsed > 30.0 and self._can_suggest_propofol(timestamp):
                target_val = round(min(config.PROPOFOL_CE_MAX, self.propofol_ce + config.DRUG_UNIT), 1)
                if target_val > self.propofol_ce:
                    await self._create_and_emit_suggestion(
                        drug_type=DrugType.PROPOFOL,
                        action=SuggestionAction.INCREASE,
                        amount=config.DRUG_UNIT,
                        current_val=self.propofol_ce,
                        suggested_val=target_val,
                        reason=f"BIS 각성 경향 지속 ({vitals.bis:.1f} > 60, {int(elapsed)}초 경과) → 마취 심도 심화 권고",
                        timestamp=timestamp,
                    )
                    self._last_propofol_sugg_time = timestamp
                    self._bis_high_start = None

        # BIS < 40: 과진정 -> 프로포폴 감량 (-0.5)
        elif vitals.bis < config.BIS_TARGET_LOW:
            self._bis_high_start = None
            if self._bis_low_start is None:
                self._bis_low_start = timestamp

            elapsed = timestamp - self._bis_low_start
            if elapsed > 30.0 and self._can_suggest_propofol(timestamp):
                target_val = round(max(config.PROPOFOL_CE_MIN, self.propofol_ce - config.DRUG_UNIT), 1)
                if target_val < self.propofol_ce:
                    await self._create_and_emit_suggestion(
                        drug_type=DrugType.PROPOFOL,
                        action=SuggestionAction.DECREASE,
                        amount=config.DRUG_UNIT,
                        current_val=self.propofol_ce,
                        suggested_val=target_val,
                        reason=f"BIS 과진정 상태 지속 ({vitals.bis:.1f} < 40, {int(elapsed)}초 경과) → 혈역학 안정 권고",
                        timestamp=timestamp,
                    )
                    self._last_propofol_sugg_time = timestamp
                    self._bis_low_start = None

        else:
            # 정상 범위(40 <= BIS <= 60)
            self._bis_high_start = None
            self._bis_low_start = None

    async def _evaluate_remifentanil(self, vitals: VitalSigns, timestamp: float) -> None:
        """MAP 기반 레미펜타닐 Ce 조정안 검토."""
        if vitals.map is None:
            self._map_high_start = None
            self._map_low_start = None
            return

        # MAP > 100: 수술 자극/혈압 상승 -> 레미펜타닐 증량 (+0.5)
        if vitals.map > 100.0:
            self._map_low_start = None
            if self._map_high_start is None:
                self._map_high_start = timestamp

            elapsed = timestamp - self._map_high_start
            if elapsed > 30.0 and self._can_suggest_remifentanil(timestamp):
                target_val = round(min(config.REMIFENTANIL_CE_MAX, self.remifentanil_ce + config.DRUG_UNIT), 1)
                if target_val > self.remifentanil_ce:
                    await self._create_and_emit_suggestion(
                        drug_type=DrugType.REMIFENTANIL,
                        action=SuggestionAction.INCREASE,
                        amount=config.DRUG_UNIT,
                        current_val=self.remifentanil_ce,
                        suggested_val=target_val,
                        reason=f"MAP 고혈압 지속 ({vitals.map:.1f} > 100, {int(elapsed)}초 경과) → 진통 및 혈압 강하 권고",
                        timestamp=timestamp,
                    )
                    self._last_remifentanil_sugg_time = timestamp
                    self._map_high_start = None

        # MAP < 60: 저혈압 -> 레미펜타닐 감량 (-0.5)
        elif vitals.map < 60.0:
            self._map_high_start = None
            if self._map_low_start is None:
                self._map_low_start = timestamp

            elapsed = timestamp - self._map_low_start
            if elapsed > 30.0 and self._can_suggest_remifentanil(timestamp):
                target_val = round(max(config.REMIFENTANIL_CE_MIN, self.remifentanil_ce - config.DRUG_UNIT), 1)
                if target_val < self.remifentanil_ce:
                    await self._create_and_emit_suggestion(
                        drug_type=DrugType.REMIFENTANIL,
                        action=SuggestionAction.DECREASE,
                        amount=config.DRUG_UNIT,
                        current_val=self.remifentanil_ce,
                        suggested_val=target_val,
                        reason=f"MAP 저혈압 지속 ({vitals.map:.1f} < 60, {int(elapsed)}초 경과) → 혈압 회복 권고",
                        timestamp=timestamp,
                    )
                    self._last_remifentanil_sugg_time = timestamp
                    self._map_low_start = None

        else:
            # 정상 범위(60 <= MAP <= 100)
            self._map_high_start = None
            self._map_low_start = None

    def _can_suggest_propofol(self, timestamp: float) -> bool:
        """프로포폴 쿨다운(60초) 검사."""
        if self._last_propofol_sugg_time is None:
            return True
        return (timestamp - self._last_propofol_sugg_time) >= 60.0

    def _can_suggest_remifentanil(self, timestamp: float) -> bool:
        """레미펜타닐 쿨다운(60초) 검사."""
        if self._last_remifentanil_sugg_time is None:
            return True
        return (timestamp - self._last_remifentanil_sugg_time) >= 60.0

    async def _create_and_emit_suggestion(
        self,
        drug_type: DrugType,
        action: SuggestionAction,
        amount: float,
        current_val: float,
        suggested_val: float,
        reason: str,
        timestamp: float,
    ) -> DrugSuggestion:
        """투약 제안 객체를 생성하고 이벤트를 발행합니다."""
        suggestion = DrugSuggestion(
            room_id=self.room_id,
            drug_type=drug_type,
            action=action,
            amount=amount,
            current_value=current_val,
            suggested_value=suggested_val,
            reason=reason,
            timestamp=timestamp,
        )
        self._pending_suggestions[suggestion.id] = suggestion
        self.logger.info(
            "[Room %d] 투약 제안 발생: %s %s -> %.1f (%s)",
            self.room_id,
            drug_type.value,
            action.value,
            suggested_val,
            reason,
        )
        await self.emit(EventType.DRUG_SUGGESTION, suggestion.model_dump(), timestamp)
        return suggestion

    def apply_approval(self, suggestion_id: str, approved: bool) -> bool:
        """전문의의 승인 결과를 내부 투약 상태에 반영합니다.

        Args:
            suggestion_id: 제안 고유 ID
            approved: True(승인 및 목표 농도 변경), False(거절)

        Returns:
            bool: 처리 성공 여부
        """
        if suggestion_id not in self._pending_suggestions:
            return False

        suggestion = self._pending_suggestions.pop(suggestion_id)
        suggestion.approved = approved

        if approved:
            if suggestion.drug_type == DrugType.PROPOFOL:
                self.propofol_ce = suggestion.suggested_value
                self.logger.info(
                    "[Room %d] 승인 반영: 프로포폴 Ce = %.1f mcg/mL",
                    self.room_id,
                    self.propofol_ce,
                )
            elif suggestion.drug_type == DrugType.REMIFENTANIL:
                self.remifentanil_ce = suggestion.suggested_value
                self.logger.info(
                    "[Room %d] 승인 반영: 레미펜타닐 Ce = %.1f ng/mL",
                    self.room_id,
                    self.remifentanil_ce,
                )
        else:
            self.logger.info(
                "[Room %d] 투약 제안 거부됨: %s %s",
                self.room_id,
                suggestion.drug_type.value,
                suggestion.action.value,
            )

        return True
