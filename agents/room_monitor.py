"""방별 감시 에이전트 (RoomMonitorAgent).

각 수술실의 생체신호를 1초 단위로 감시하며,
- 신호 잡음(Spike) 필터링
- 문헌 기반 임계값 검사 (MAP, SpO2, HR, BIS, EtCO2, BT)
- BIS 0 센서 탈락 vs 뇌파 억제 감별 (EEG δ 파워 분석)
- 1분 지속성 규칙 (Yellow 경보는 60초 지속 시 발생, Red는 즉시 발생)
- 경보(Alert) 발행 및 정상 회복 시 경보 해제(Alert Cleared) 이벤트 발행
- 매 틱 생체신호 갱신(Vital Update) 발행
"""

from __future__ import annotations

import collections
import logging
import statistics
from typing import Deque, Dict, Optional, Tuple

import config
from agents.base import BaseAgent
from core.enums import AlertLevel, EventType
from core.event_bus import EventBus
from core.models import Alert, VitalSigns

logger = logging.getLogger(__name__)


class RoomMonitorAgent(BaseAgent):
    """단일 수술실의 생체신호를 실시간 감시하는 에이전트."""

    def __init__(self, room_id: int, event_bus: EventBus) -> None:
        """방별 감시 에이전트 초기화.

        Args:
            room_id: 감시 대상 수술실 번호
            event_bus: 중앙 이벤트 버스
        """
        super().__init__(agent_id=f"room_monitor_{room_id}", event_bus=event_bus)
        self.room_id: int = room_id

        # 최근 10개 유효 측정값 슬라이딩 윈도우 (파라미터별)
        self._history: Dict[str, Deque[float]] = collections.defaultdict(lambda: collections.deque(maxlen=10))

        # 잡음 연속 틱 카운터 (파라미터별)
        self._spike_counts: Dict[str, int] = collections.defaultdict(int)

        # 주의(YELLOW) 상태 지속 시작 시각 기록 (파라미터별)
        self._yellow_start_times: Dict[str, float] = {}

        # 현재 활성화된 경보 목록 (파라미터명 -> Alert 객체)
        self.active_alerts: Dict[str, Alert] = {}

        # 현재 방의 종합 경보 수준
        self.current_alert_level: AlertLevel = AlertLevel.GREEN

    async def process(self, vitals: VitalSigns) -> None:
        """매 틱마다 생체신호를 전달받아 분석 및 이벤트를 발행합니다.

        Args:
            vitals: 현재 틱의 생체신호 데이터
        """
        timestamp = vitals.timestamp

        # 1. 대상 파라미터 리스트 검사 (None 값은 안전하게 건너뜀)
        params_to_check = [
            ("map", vitals.map),
            ("spo2", vitals.spo2),
            ("hr", vitals.hr),
            ("bis", vitals.bis),
            ("etco2", vitals.etco2),
            ("bt", vitals.bt),
        ]

        for param_name, raw_val in params_to_check:
            if raw_val is None:
                continue

            # 2. 잡음(Spike) 필터링
            # 단, BIS == 0은 센서 탈락/뇌파 억제 감별 로직에서 처리하므로 일반 잡음 필터에서 건너뜀
            if param_name == "bis" and raw_val == 0.0:
                is_noise = False
            else:
                is_noise = self._check_noise_spike(param_name, raw_val)

            if is_noise:
                continue

            # 3. 임계값 검사 및 이상 판별
            level, threshold, msg = self._evaluate_threshold(param_name, raw_val, vitals)

            # 4. 1분 지속성 규칙 및 경보 발령/해제 처리
            await self._handle_alert_state(param_name, raw_val, level, threshold, msg, timestamp)

        # 5. 방 전체 종합 경보 수준 산출
        self._update_overall_alert_level()

        # 6. 매 틱마다 현재 바이탈 신호 발행 (백엔드 top-level 필드 및 프론트엔드 vitals/alert_level 동시 지원)
        vitals_dict = vitals.model_dump()
        payload = {
            **vitals_dict,
            "vitals": vitals_dict,
            "alert_level": self.current_alert_level.value,
        }
        await self.emit(EventType.VITAL_UPDATE, payload, timestamp)

    def _check_noise_spike(self, param_name: str, val: float) -> bool:
        """최근 10초 중앙값 대비 30% 급변(스파이크 잡음) 여부를 판정합니다.

        지속 시간이 config.NOISE_RECOVERY_SEC(5초) 이하이면 잡음으로 무시하고,
        5초를 초과하여 지속되면 실제 생체신호 변화로 간주하여 수용합니다.
        """
        history = self._history[param_name]

        if len(history) >= 3:
            median_val = statistics.median(history)
            if median_val > 0 and (abs(val - median_val) / median_val) > config.NOISE_SPIKE_RATIO:
                self._spike_counts[param_name] += 1
                if self._spike_counts[param_name] <= config.NOISE_RECOVERY_SEC:
                    self.logger.debug(
                        "[Room %d] 잡음 스파이크 무시: %s=%.1f (중앙값=%.1f, %d/%d초)",
                        self.room_id,
                        param_name,
                        val,
                        median_val,
                        self._spike_counts[param_name],
                        config.NOISE_RECOVERY_SEC,
                    )
                    return True
                else:
                    # 5초 이상 지속된 변화는 실제 상태 변화로 인정
                    self._spike_counts[param_name] = 0
                    history.append(val)
                    return False

        # 정상치 또는 윈도우 채우는 중
        self._spike_counts[param_name] = 0
        history.append(val)
        return False

    def _evaluate_threshold(
        self,
        param_name: str,
        val: float,
        vitals: VitalSigns,
    ) -> Tuple[AlertLevel, float, str]:
        """파라미터별 의학적 임계값을 비교하여 상태 수준을 판정합니다.

        Returns:
            Tuple[AlertLevel, float, str]: (경보 수준, 초과 임계값, 경보 메시지)
        """
        # ── MAP (평균동맥압) ──
        if param_name == "map":
            if val < config.MAP_CRITICAL_LOW:
                return AlertLevel.RED, config.MAP_CRITICAL_LOW, f"MAP 심각 저하: {val:.1f} mmHg (< {config.MAP_CRITICAL_LOW})"
            if val < config.MAP_WARNING_LOW:
                return AlertLevel.YELLOW, config.MAP_WARNING_LOW, f"MAP 주의 저하: {val:.1f} mmHg (< {config.MAP_WARNING_LOW})"
            if val > config.MAP_CRITICAL_HIGH:
                return AlertLevel.RED, config.MAP_CRITICAL_HIGH, f"MAP 심각 상승: {val:.1f} mmHg (> {config.MAP_CRITICAL_HIGH})"
            if val > config.MAP_WARNING_HIGH:
                return AlertLevel.YELLOW, config.MAP_WARNING_HIGH, f"MAP 주의 상승: {val:.1f} mmHg (> {config.MAP_WARNING_HIGH})"

        # ── SpO2 (산소포화도) ──
        elif param_name == "spo2":
            if val < config.SPO2_CRITICAL:
                return AlertLevel.RED, config.SPO2_CRITICAL, f"SpO₂ 위험 저하 (저산소증): {val:.1f}% (< {config.SPO2_CRITICAL}%)"
            if val < config.SPO2_WARNING:
                return AlertLevel.YELLOW, config.SPO2_WARNING, f"SpO₂ 주의 저하: {val:.1f}% (< {config.SPO2_WARNING}%)"

        # ── HR (심박수) ──
        elif param_name == "hr":
            if val < config.HR_CRITICAL_LOW:
                return AlertLevel.RED, config.HR_CRITICAL_LOW, f"HR 심각 서맥: {val:.1f} bpm (< {config.HR_CRITICAL_LOW})"
            if val < config.HR_WARNING_LOW:
                return AlertLevel.YELLOW, config.HR_WARNING_LOW, f"HR 주의 서맥: {val:.1f} bpm (< {config.HR_WARNING_LOW})"
            if val > config.HR_CRITICAL_HIGH:
                return AlertLevel.RED, config.HR_CRITICAL_HIGH, f"HR 심각 빈맥: {val:.1f} bpm (> {config.HR_CRITICAL_HIGH})"
            if val > config.HR_WARNING_HIGH:
                return AlertLevel.YELLOW, config.HR_WARNING_HIGH, f"HR 주의 빈맥: {val:.1f} bpm (> {config.HR_WARNING_HIGH})"

        # ── BIS (마취 심도 지수) ──
        elif param_name == "bis":
            if val == 0.0:
                # BIS 0 검출: EEG delta 파워 검사로 센서 탈락 여부 감별
                delta_pwr = vitals.eeg_delta if vitals.eeg_delta is not None else 0.0
                if delta_pwr >= 0.3:  # 뇌파 서파 활동이 감지되면 센서 접촉 불량(Artifact)
                    self.logger.info(
                        "[Room %d] BIS=0 감지되었으나 EEG δ 파워(%.2f >= 0.3) 정상 유지 → 센서 탈락 판정(경보 무시)",
                        self.room_id,
                        delta_pwr,
                    )
                    return AlertLevel.GREEN, 0.0, ""
                else:
                    return AlertLevel.RED, config.BIS_DEEP_WARNING, f"BIS 위험 극저하 (뇌파 억제): {val:.1f} (δ={delta_pwr:.2f})"

            if val < config.BIS_DEEP_WARNING:
                return AlertLevel.RED, config.BIS_DEEP_WARNING, f"BIS 위험 과진정: {val:.1f} (< {config.BIS_DEEP_WARNING})"
            if val < config.BIS_TARGET_LOW:
                return AlertLevel.YELLOW, config.BIS_TARGET_LOW, f"BIS 주의 과진정: {val:.1f} (< {config.BIS_TARGET_LOW})"
            if val > config.BIS_TARGET_HIGH:
                return AlertLevel.YELLOW, config.BIS_TARGET_HIGH, f"BIS 주의 각성 위험: {val:.1f} (> {config.BIS_TARGET_HIGH})"

        # ── EtCO2 (호기말 이산화탄소 분압) ──
        elif param_name == "etco2":
            if val > config.ETCO2_CRITICAL_HIGH:
                return AlertLevel.RED, config.ETCO2_CRITICAL_HIGH, f"EtCO₂ 심각 고탄산혈증: {val:.1f} mmHg (> {config.ETCO2_CRITICAL_HIGH})"
            if val > config.ETCO2_WARNING_HIGH:
                return AlertLevel.YELLOW, config.ETCO2_WARNING_HIGH, f"EtCO₂ 주의 상승: {val:.1f} mmHg (> {config.ETCO2_WARNING_HIGH})"
            if val < config.ETCO2_WARNING_LOW:
                return AlertLevel.YELLOW, config.ETCO2_WARNING_LOW, f"EtCO₂ 주의 저하: {val:.1f} mmHg (< {config.ETCO2_WARNING_LOW})"

        # ── BT (체온) ──
        elif param_name == "bt":
            if val < config.BT_WARNING_LOW:
                return AlertLevel.YELLOW, config.BT_WARNING_LOW, f"체온 주의 저하: {val:.1f} °C (< {config.BT_WARNING_LOW})"
            if val > config.BT_WARNING_HIGH:
                return AlertLevel.YELLOW, config.BT_WARNING_HIGH, f"체온 주의 상승: {val:.1f} °C (> {config.BT_WARNING_HIGH})"

        return AlertLevel.GREEN, 0.0, ""

    async def _handle_alert_state(
        self,
        param_name: str,
        val: float,
        condition_level: AlertLevel,
        threshold: float,
        msg: str,
        timestamp: float,
    ) -> None:
        """경보 상태 변화 및 1분 지속성 규칙을 처리합니다.

        - GREEN: 이전 경보 해제 (ALERT_CLEARED)
        - YELLOW: 60초 이상 연속 지속 시에만 ALERT 발행
        - RED: 즉시 ALERT 발행
        """
        # 1. 정상이 된 경우
        if condition_level == AlertLevel.GREEN:
            self._yellow_start_times.pop(param_name, None)
            if param_name in self.active_alerts:
                cleared_alert = self.active_alerts.pop(param_name)
                self.logger.info("[Room %d] 경보 해제: %s (값: %.1f)", self.room_id, param_name, val)
                await self.emit(
                    EventType.ALERT_CLEARED,
                    {
                        "id": cleared_alert.id,
                        "alert_id": cleared_alert.id,
                        "room_id": self.room_id,
                        "parameter": param_name,
                        "message": f"{param_name.upper()} 정상 회복 (현재값: {val:.1f})",
                        "value": val,
                        "timestamp": timestamp,
                    },
                    timestamp,
                )
            return

        # 2. 위험(RED) 상태인 경우 (즉시 경보 발령)
        if condition_level == AlertLevel.RED:
            self._yellow_start_times.pop(param_name, None)
            existing = self.active_alerts.get(param_name)
            if existing is None or existing.level != AlertLevel.RED:
                alert = Alert(
                    room_id=self.room_id,
                    level=AlertLevel.RED,
                    parameter=param_name,
                    value=val,
                    threshold=threshold,
                    message=msg,
                    timestamp=timestamp,
                )
                self.active_alerts[param_name] = alert
                self.logger.warning("[Room %d] RED 경보 발생! %s", self.room_id, msg)
                await self.emit(EventType.ALERT, alert.model_dump(), timestamp)
            else:
                # 이미 RED 경보 발령 중이면 값 최신화
                existing.value = val
            return

        # 3. 주의(YELLOW) 상태인 경우 (1분 지속성 규칙 적용)
        if condition_level == AlertLevel.YELLOW:
            # 이전에 RED 경보였다면 RED는 해제 처리하고 YELLOW 지속성 검사 시작
            if param_name in self.active_alerts and self.active_alerts[param_name].level == AlertLevel.RED:
                cleared_alert = self.active_alerts.pop(param_name)
                await self.emit(
                    EventType.ALERT_CLEARED,
                    {
                        "id": cleared_alert.id,
                        "alert_id": cleared_alert.id,
                        "room_id": self.room_id,
                        "parameter": param_name,
                        "message": f"{param_name.upper()} RED 상태 해제 (YELLOW로 호전)",
                        "value": val,
                        "timestamp": timestamp,
                    },
                    timestamp,
                )
                self._yellow_start_times[param_name] = timestamp
                return

            # 이미 발령된 YELLOW 경보가 있는 경우 값만 갱신
            if param_name in self.active_alerts and self.active_alerts[param_name].level == AlertLevel.YELLOW:
                self.active_alerts[param_name].value = val
                return

            # 아직 발령되지 않은 YELLOW 상태: 지속 시간 계산
            if param_name not in self._yellow_start_times:
                self._yellow_start_times[param_name] = timestamp

            elapsed = timestamp - self._yellow_start_times[param_name]
            if elapsed >= config.ALERT_PERSISTENCE_SEC:
                # 60초 이상 지속됨 -> 경보 발령
                alert = Alert(
                    room_id=self.room_id,
                    level=AlertLevel.YELLOW,
                    parameter=param_name,
                    value=val,
                    threshold=threshold,
                    message=f"{msg} ({int(elapsed)}초 지속)",
                    timestamp=timestamp,
                )
                self.active_alerts[param_name] = alert
                self.logger.info("[Room %d] YELLOW 경보 발령 (60초 지속 충족): %s", self.room_id, msg)
                await self.emit(EventType.ALERT, alert.model_dump(), timestamp)

    def _update_overall_alert_level(self) -> None:
        """활성 경보 목록 중 가장 심각한 수준을 방의 종합 경보 수준으로 설정합니다."""
        if any(a.level == AlertLevel.RED for a in self.active_alerts.values()):
            self.current_alert_level = AlertLevel.RED
        elif any(a.level == AlertLevel.YELLOW for a in self.active_alerts.values()):
            self.current_alert_level = AlertLevel.YELLOW
        else:
            self.current_alert_level = AlertLevel.GREEN
