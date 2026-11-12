"""투약 제안 에이전트 (MedicationAgent).

환자의 생체신호(BIS, MAP) 동태를 실시간으로 추적하여,
정맥 마취제(프로포폴) 및 진통제(레미펜타닐)의 목표 혈중/효과처 농도(Ce) 조절안을 제안합니다.

규칙 (문헌 근거 및 VitalDB TCI 데이터 기반):
1. 유지기(MAINTENANCE)에만 동작하며, 응급(Emergency) 상태인 경우 제안을 중단합니다.
2. 케이스 초기 실제 TCI 농도(ppf_ct/ppf_ce, rftn_ct/rftn_ce)가 존재하면 그 값으로 시작 농도를 동기화합니다.
3. BIS > 60이 30초 이상 지속되면 프로포폴 +0.5 mcg/mL 증량 제안 (SQI < 50 센서 불량 시 보류).
4. BIS < 40이 30초 이상 지속되면 프로포폴 -0.5 mcg/mL 감량 제안 (BIS=0 및 SQI < 50 센서 이상 제외).
5. MAP가 환자 기저치 대비 +20% 이상(또는 > 100~110 mmHg) 30초 이상 지속되면 레미펜타닐 +0.5 ng/mL 증량 제안.
6. MAP < 65 mmHg(문헌 표준 저혈압 기준)가 30초 이상 지속되면 레미펜타닐 -0.5 ng/mL 감량 고려 제안 (하한 2.0 ng/mL, 수액·승압제 병행 의사 판단 안내).
7. 제안 후 60초간 동일 약물에 대한 쿨다운(Cooldown)을 적용합니다.
8. 의료진 승인(Approval) 시 내부 목표 농도를 갱신합니다.
"""

from __future__ import annotations

import collections
import logging
import statistics
from typing import Deque, Dict, Optional

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

        # 현재 약물 목표 농도 (Ce/Ct, 케이스 데이터 수신 시 실제 초기값으로 동기화)
        self.propofol_ce: float = 3.0       # mcg/mL
        self.remifentanil_ce: float = 3.0   # ng/mL
        self._ppf_initialized: bool = False
        self._rftn_initialized: bool = False

        # 환자 초기 정상 혈압(MAP) 기저치 샘플 (상대 고혈압 +20% 판정용)
        self._baseline_map_samples: Deque[float] = collections.deque(maxlen=60)

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

        # 0. 케이스 실제 TCI 시작 농도(CT 우선, 없으면 CE) 1회 동기화
        self._sync_initial_drug_concentrations(vitals)

        # 1. 응급 상황 검사: RED 경보 또는 긴급 상태 시 투약 제안 중단 및 미결 제안 정리
        if room_status is not None:
            if room_status.is_emergency or room_status.alert_level == AlertLevel.RED:
                self.logger.debug("[Room %d] 응급 상태 감지 — 투약 제안 일시 중단", self.room_id)
                self._reset_persistence_timers()
                self._pending_suggestions.clear()
                return

            if room_status.phase != Phase.MAINTENANCE:
                # 유지기 이외 단계에서는 규칙 기반 제안 건너뜀
                return

        # 2. BIS 기반 프로포폴 조절 평가
        await self._evaluate_propofol(vitals, timestamp)

        # 3. MAP 기반 레미펜타닐 조절 평가
        await self._evaluate_remifentanil(vitals, timestamp)

    def _sync_initial_drug_concentrations(self, vitals: VitalSigns) -> None:
        """VitalDB 실제 케이스의 초기 TCI 농도(CT 또는 CE)가 있으면 3.0 고정값 대신 동기화합니다."""
        if not self._ppf_initialized:
            init_ppf = vitals.ppf_ct if vitals.ppf_ct is not None else vitals.ppf_ce
            if init_ppf is not None and init_ppf > 0:
                self.propofol_ce = round(
                    max(config.PROPOFOL_CE_MIN, min(config.PROPOFOL_CE_MAX, float(init_ppf))), 1
                )
                self._ppf_initialized = True
        if not self._rftn_initialized:
            init_rftn = vitals.rftn_ct if vitals.rftn_ct is not None else vitals.rftn_ce
            if init_rftn is not None and init_rftn > 0:
                self.remifentanil_ce = round(
                    max(config.REMIFENTANIL_CE_MIN, min(config.REMIFENTANIL_CE_MAX, float(init_rftn))), 1
                )
                self._rftn_initialized = True

    def reset(self) -> None:
        """시뮬레이션 리셋 시 투약 상태, 지속 타이머, 쿨다운, 대기 제안을 모두 초기화합니다."""
        self.propofol_ce = 3.0
        self.remifentanil_ce = 3.0
        self._ppf_initialized = False
        self._rftn_initialized = False
        self._baseline_map_samples.clear()
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
        """BIS 기반 프로포폴 Ce 조정안 검토 (SQI < 50 센서 불량 시 보류)."""
        if vitals.bis is None or vitals.bis == 0.0:
            # BIS가 없거나 0(센서 탈락 의심)인 경우 타이머 리셋 후 건너뜀
            self._bis_high_start = None
            self._bis_low_start = None
            return

        if vitals.sqi is not None and vitals.sqi < config.SQI_THRESHOLD:
            # 신호품질(SQI) 불량 시 잘못된 BIS 수치에 의한 프로포폴 오투약 제안 방지
            self._bis_high_start = None
            self._bis_low_start = None
            return

        step = getattr(config, "PROPOFOL_STEP", config.DRUG_UNIT)

        # BIS > 60: 각성 위험 -> 프로포폴 증량 (+0.5)
        if vitals.bis > config.BIS_TARGET_HIGH:
            self._bis_low_start = None
            if self._bis_high_start is None:
                self._bis_high_start = timestamp

            elapsed = timestamp - self._bis_high_start
            if elapsed >= 30.0 and self._can_suggest_propofol(timestamp):
                target_val = round(min(config.PROPOFOL_CE_MAX, self.propofol_ce + step), 1)
                if target_val > self.propofol_ce:
                    await self._create_and_emit_suggestion(
                        drug_type=DrugType.PROPOFOL,
                        action=SuggestionAction.INCREASE,
                        amount=step,
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
            if elapsed >= 30.0 and self._can_suggest_propofol(timestamp):
                target_val = round(max(config.PROPOFOL_CE_MIN, self.propofol_ce - step), 1)
                if target_val < self.propofol_ce:
                    await self._create_and_emit_suggestion(
                        drug_type=DrugType.PROPOFOL,
                        action=SuggestionAction.DECREASE,
                        amount=step,
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
        """MAP 기반 레미펜타닐 Ce 조정안 검토 (저혈압 65 통일, 기저치 +20% 고혈압 기준 적용)."""
        if vitals.map is None:
            self._map_high_start = None
            self._map_low_start = None
            return

        # 생리학적 아티팩트(플러싱 등) 제외 후 초기 정상 범위(65~105)에서 환자 기저 혈압 누적
        if config.MAP_WARNING_LOW <= vitals.map <= 105.0 and len(self._baseline_map_samples) < 60:
            self._baseline_map_samples.append(vitals.map)

        baseline_map = (
            statistics.median(self._baseline_map_samples)
            if len(self._baseline_map_samples) >= 5
            else 85.0
        )
        # 기저치 대비 +20% 상승 또는 절대 기준(100~110 mmHg) 중 환자 맞춤 고혈압 임계값 산출
        high_threshold = round(
            min(config.MAP_WARNING_HIGH, max(100.0, baseline_map * config.MAP_HYPERTENSION_RATIO)),
            1,
        )
        low_threshold = config.MAP_WARNING_LOW  # 65.0 mmHg (감시 에이전트 및 문헌 기준과 통일)
        step = getattr(config, "REMIFENTANIL_STEP", config.DRUG_UNIT)

        # MAP > high_threshold: 수술 자극/혈압 상승 -> 레미펜타닐 증량 (+0.5)
        if vitals.map > high_threshold:
            self._map_low_start = None
            if self._map_high_start is None:
                self._map_high_start = timestamp

            elapsed = timestamp - self._map_high_start
            if elapsed >= 30.0 and self._can_suggest_remifentanil(timestamp):
                target_val = round(min(config.REMIFENTANIL_CE_MAX, self.remifentanil_ce + step), 1)
                if target_val > self.remifentanil_ce:
                    await self._create_and_emit_suggestion(
                        drug_type=DrugType.REMIFENTANIL,
                        action=SuggestionAction.INCREASE,
                        amount=step,
                        current_val=self.remifentanil_ce,
                        suggested_val=target_val,
                        reason=(
                            f"MAP 고혈압 지속 ({vitals.map:.1f} > {high_threshold:.0f} mmHg [기저치 대비 상승], "
                            f"{int(elapsed)}초 경과) → 진통 강화 권고"
                        ),
                        timestamp=timestamp,
                    )
                    self._last_remifentanil_sugg_time = timestamp
                    self._map_high_start = None

        # MAP < 65.0 (MAP_WARNING_LOW): 저혈압 -> 레미펜타닐 감량 고려 (-0.5, 하한 2.0)
        elif vitals.map < low_threshold:
            self._map_high_start = None
            if self._map_low_start is None:
                self._map_low_start = timestamp

            elapsed = timestamp - self._map_low_start
            if elapsed >= 30.0 and self._can_suggest_remifentanil(timestamp):
                target_val = round(max(config.REMIFENTANIL_CE_MIN, self.remifentanil_ce - step), 1)
                if target_val < self.remifentanil_ce:
                    await self._create_and_emit_suggestion(
                        drug_type=DrugType.REMIFENTANIL,
                        action=SuggestionAction.DECREASE,
                        amount=step,
                        current_val=self.remifentanil_ce,
                        suggested_val=target_val,
                        reason=(
                            f"MAP 저혈압 지속 ({vitals.map:.1f} < {low_threshold:.0f} mmHg, {int(elapsed)}초 경과) "
                            f"→ 레미펜타닐 감량 고려 (수액·승압제 등 혈역학 처치 병행 여부 의사 판단 필요)"
                        ),
                        timestamp=timestamp,
                    )
                    self._last_remifentanil_sugg_time = timestamp
                    self._map_low_start = None

        else:
            # 정상 범위(65 <= MAP <= high_threshold)
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
