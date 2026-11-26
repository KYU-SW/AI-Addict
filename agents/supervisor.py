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

        # 각 방별 경보 시작 시각 기록 (room_id -> timestamp) — 하위 호환용(seek 리셋 코드가 참조)
        self._alert_start_times: Dict[int, float] = {}

        # 지표별 이상 시작 시각 ((room_id, parameter) -> timestamp)
        # 같은 지표의 RED↔YELLOW 교체 중에는 유지되고, 그 지표 경보가 해제될 때만 지워짐
        self._param_start_times: Dict[tuple, float] = {}

        # 각 방별 직전 틱 심각도 값 기록 (추세 계산용: room_id -> severity_value)
        self._prev_severity: Dict[int, float] = {rid: 0.0 for rid in self.room_ids}

        # 전체 대기 중인 투약 제안 맵 (suggestion_id -> DrugSuggestion)
        self._pending_suggestions: Dict[str, DrugSuggestion] = {}

        # 방별 초기 실제 약물 농도(CT/CE) 1회 동기화 여부
        self._ppf_initialized: Dict[int, bool] = {rid: False for rid in self.room_ids}
        self._rftn_initialized: Dict[int, bool] = {rid: False for rid in self.room_ids}

    async def start(self) -> None:
        """에이전트 시작 및 필요 이벤트 구독."""
        await super().start()
        self.event_bus.subscribe(EventType.VITAL_UPDATE, self._on_vital_update)
        self.event_bus.subscribe(EventType.ALERT, self._on_alert)
        self.event_bus.subscribe(EventType.ALERT_CLEARED, self._on_alert_cleared)
        self.event_bus.subscribe(EventType.SENSOR_ALERT, self._on_sensor_alert)
        self.event_bus.subscribe(EventType.SENSOR_CLEARED, self._on_sensor_cleared)
        self.event_bus.subscribe(EventType.DRUG_SUGGESTION, self._on_drug_suggestion)
        self.event_bus.subscribe(EventType.APPROVAL_RESPONSE, self._on_approval_response)

    # ── 이벤트 핸들러 ─────────────────────────────────────────

    async def _on_vital_update(self, event: Event) -> None:
        """생체신호 갱신 반영."""
        room_id = event.data.get("room_id")
        if room_id in self.room_statuses:
            vital_data = event.data.get("vitals") or {
                k: v
                for k, v in event.data.items()
                if k not in ("vitals", "alert_level", "sensor_problem", "sensor_issues")
            }
            status = self.room_statuses[room_id]
            vitals_obj = VitalSigns(**vital_data)
            status.vitals = vitals_obj
            status.timestamp = event.timestamp

            # 케이스 초기 실제 TCI 농도(CT 우선, 없으면 CE) 1회 동기화 (MedicationAgent와 일치 보장)
            if not self._ppf_initialized.get(room_id, False):
                init_ppf = vitals_obj.ppf_ct if vitals_obj.ppf_ct is not None else vitals_obj.ppf_ce
                if init_ppf is not None and init_ppf > 0:
                    status.propofol_ce = round(
                        max(config.PROPOFOL_CE_MIN, min(config.PROPOFOL_CE_MAX, float(init_ppf))), 1
                    )
                    self._ppf_initialized[room_id] = True
            if not self._rftn_initialized.get(room_id, False):
                init_rftn = vitals_obj.rftn_ct if vitals_obj.rftn_ct is not None else vitals_obj.rftn_ce
                if init_rftn is not None and init_rftn > 0:
                    status.remifentanil_ce = round(
                        max(config.REMIFENTANIL_CE_MIN, min(config.REMIFENTANIL_CE_MAX, float(init_rftn))), 1
                    )
                    self._rftn_initialized[room_id] = True

            if "sensor_problem" in event.data:
                status.sensor_problem = bool(event.data["sensor_problem"])
            if "sensor_issues" in event.data and isinstance(event.data["sensor_issues"], list):
                status.sensor_issues = list(event.data["sensor_issues"])

    async def _on_sensor_alert(self, event: Event) -> None:
        """센서/신호 품질 이상('센서 확인 필요') 수신 반영."""
        room_id = event.data.get("room_id")
        msg = event.data.get("message", "센서 확인 필요")
        if room_id in self.room_statuses:
            status = self.room_statuses[room_id]
            status.sensor_problem = True
            if msg not in status.sensor_issues:
                status.sensor_issues.append(msg)

    async def _on_sensor_cleared(self, event: Event) -> None:
        """센서/신호 품질 정상 복구 수신 반영."""
        room_id = event.data.get("room_id")
        if room_id in self.room_statuses:
            status = self.room_statuses[room_id]
            status.sensor_issues.clear()
            status.sensor_problem = False

    async def _on_alert(self, event: Event) -> None:
        """경보 수신 시 활성 경보 목록 갱신 및 경보 시작 시각 기록."""
        alert = Alert(**event.data)
        room_id = alert.room_id
        if room_id in self.room_statuses:
            status = self.room_statuses[room_id]
            # 동일 파라미터 기존 경보 대체 (RED↔YELLOW 교체 시 지표 이상 시작 시각은 유지)
            already_active = any(a.parameter == alert.parameter for a in status.active_alerts)
            status.active_alerts = [a for a in status.active_alerts if a.parameter != alert.parameter]
            status.active_alerts.append(alert)
            if not already_active or (room_id, alert.parameter) not in self._param_start_times:
                self._param_start_times[(room_id, alert.parameter)] = event.timestamp

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
            removed = [a for a in status.active_alerts if a.id == alert_id or a.parameter == param]
            status.active_alerts = [
                a for a in status.active_alerts
                if a.id != alert_id and a.parameter != param
            ]
            for a in removed:
                self._param_start_times.pop((room_id, a.parameter), None)
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

    @staticmethod
    def _param_weight(parameter: str) -> float:
        """지표별 생명 위급도 가중치 (config.PRIORITY_PARAM_WEIGHTS, 없으면 기본값)."""
        weights = getattr(config, "PRIORITY_PARAM_WEIGHTS", None) or {
            "spo2": 8.0, "map": 8.0, "etco2": 5.0, "bis": 5.0, "hr": 4.0, "bt": 1.0,
        }
        return float(weights.get(parameter, 2.0))

    def _alert_score_bonus(self, room_id: int, active_alerts: List[Alert], tick: float) -> float:
        """방 안 경보들 중 '가장 급한 지표 하나'의 점수 = 위급도 + 위급도 비례 지속시간 보너스.

        지속시간 보너스를 지표 위급도에 비례시켜(최대 가중치 대비 비율), 체온처럼 덜 급한 지표가
        오래 지속됐다는 이유만으로 MAP·BIS 같은 급한 지표의 신규 경보를 앞지르지 못하게 함.
        예) 16분 지속 저체온(BT) = 1 + 15×(1/8) ≈ 2.9점 < 방금 시작된 BIS 주의 = 5점
        """
        if not active_alerts:
            return 0.0
        top_level = AlertLevel.RED if any(a.level == AlertLevel.RED for a in active_alerts) else AlertLevel.YELLOW
        max_bonus = getattr(config, "PRIORITY_DURATION_MAX_BONUS", 15.0)
        max_weight = max(self._param_weight(p) for p in ("spo2", "map", "etco2", "bis", "hr", "bt"))
        best = 0.0
        for a in active_alerts:
            if a.level != top_level:
                continue  # 방의 최고 단계 경보만 비교 (RED 방이면 RED 지표끼리)
            w = self._param_weight(a.parameter)
            started = self._param_start_times.get((room_id, a.parameter), a.timestamp)
            duration_min = max(0.0, tick - started) / 60.0
            duration_bonus = min(max_bonus, duration_min * config.PRIORITY_W_DURATION) * (w / max_weight)
            best = max(best, w + duration_bonus)
        return best

    async def process(self, tick: float) -> None:
        """매 틱마다 전체 방의 우선순위를 계산하고 랭킹 및 비상 상황을 발행합니다.

        우선순위 산출 규칙 (계층 분리 + 분 단위 지속시간 상한):
        1. 계층(Tier) 절대 보장: RED(기본 100점) > YELLOW(기본 20점) > 센서점검(5점) > GREEN(0점)
           → YELLOW가 오래 지속되어도 방금 발생한 RED를 절대 역전하지 못함.
        2. 지표 위급도 + 지속시간 보너스: 방에서 가장 급한 지표 하나 기준.
           위급도 SpO2·MAP(8) > EtCO2·BIS(5) > HR(4) > BT(1) (config.PRIORITY_PARAM_WEIGHTS),
           지속시간은 분 단위 × 2점(상한 15점)에 '위급도/8' 비율을 곱함 → 덜 급한 지표가
           오래 지속돼도 급한 지표의 신규 경보를 역전하지 못함. 같은 지표의 RED↔YELLOW 교체 중에는
           지속시간이 이어짐.
        4. 추세 보너스: 악화(+5), 유지(0), 호전(-5).
        """
        for room_id, status in self.room_statuses.items():
            # 1. 심각도 기본 점수 (계층 분리)
            if status.alert_level == AlertLevel.RED:
                severity_value = 3.0
                base_score = getattr(config, "PRIORITY_BASE_RED", 100.0)
            elif status.alert_level == AlertLevel.YELLOW:
                severity_value = 1.0
                base_score = getattr(config, "PRIORITY_BASE_YELLOW", 20.0)
            else:
                severity_value = 0.0
                base_score = 0.0

            # 3. 추세 판별
            prev_sev = self._prev_severity.get(room_id, 0.0)
            if severity_value > prev_sev:
                trend = 1.0  # 악화
            elif severity_value < prev_sev:
                trend = -1.0  # 호전
            else:
                trend = 0.0  # 유지
            self._prev_severity[room_id] = severity_value

            # 4. 지표별 위급도 가중치 및 센서 점검 보너스
            # 지표 위급도 + (위급도 비례) 지속시간 보너스
            param_bonus = self._alert_score_bonus(room_id, status.active_alerts, tick)
            sensor_bonus = 5.0 if status.sensor_problem else 0.0

            # 5. 최종 우선순위 점수 계산
            if status.alert_level == AlertLevel.GREEN and not status.sensor_problem:
                score = 0.0
            else:
                score = (
                    base_score
                    + param_bonus
                    + (trend * config.PRIORITY_W_TREND)
                    + sensor_bonus
                )
            status.priority_score = max(0.0, round(score, 2))
            status.timestamp = tick

            # 6. 응급 상태(EMERGENCY) 처리
            was_emergency = status.is_emergency
            is_emergency = (status.alert_level == AlertLevel.RED)
            status.is_emergency = is_emergency

            if is_emergency and not was_emergency:
                # 응급 진입: 대기 중인 투약 제안 취소(_pending_suggestions 포함) 및 알림
                cancelled = list(status.pending_suggestions)
                status.pending_suggestions.clear()
                for c_sugg in cancelled:
                    self._pending_suggestions.pop(c_sugg.id, None)
                    await self.emit(
                        EventType.SUGGESTION_CANCELLED,
                        {
                            "suggestion_id": c_sugg.id,
                            "room_id": room_id,
                            "reason": "응급(RED) 경보 진입으로 인한 자동 취소",
                            "timestamp": tick,
                        },
                        tick,
                    )
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

        # 7. 계층(RED > YELLOW > 센서점검 > GREEN) 우선, 동일 계층 내에서는 점수 내림차순 정렬
        tier_order = {AlertLevel.RED: 3, AlertLevel.YELLOW: 2, AlertLevel.GREEN: 1}
        ranked_rooms = sorted(
            self.room_statuses.values(),
            key=lambda r: (
                tier_order.get(r.alert_level, 0),
                1 if r.sensor_problem else 0,
                r.priority_score,
            ),
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

        room_id = suggestion.room_id
        status = self.room_statuses.get(room_id)

        # 제안 대상 방이 현재 응급(RED) 상태이면 승인 처리 차단
        if status and (status.is_emergency or status.alert_level == AlertLevel.RED) and approved:
            self.logger.warning(
                "[Supervisor] Room %d 응급(RED) 상태이므로 투약 제안 %s 승인이 거부됩니다.",
                room_id,
                suggestion_id,
            )
            return None

        suggestion.approved = approved

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
