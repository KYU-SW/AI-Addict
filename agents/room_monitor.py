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

        # 잡음 연속 틱 카운터 및 보류 값 버퍼 (지속 급변 시 기준선 즉시 동기화용)
        self._spike_counts: Dict[str, int] = collections.defaultdict(int)
        self._pending_spikes: Dict[str, Deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=10)
        )

        # 주의(YELLOW) 상태 지속 시작 시각 기록 (파라미터별)
        self._yellow_start_times: Dict[str, float] = {}

        # 현재 활성화된 경보 목록 (파라미터명 -> Alert 객체)
        self.active_alerts: Dict[str, Alert] = {}

        # 현재 활성화된 센서 확인 필요 이슈 (파라미터명 -> 설명 메시지)
        self.sensor_issues: Dict[str, str] = {}

        # 환자 초기 정상 상태의 EEG β파 기저치 기록 (%)
        self._beta_baseline_samples: Deque[float] = collections.deque(maxlen=30)

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
            # 2-A. BIS 전용 SQI(신호품질지수) 사전 게이트 (Step 1: bis가 None이어도 SQI 불량이면 즉시 감지)
            if param_name == "bis" and vitals.sqi is not None and vitals.sqi < config.SQI_THRESHOLD:
                self._yellow_start_times.pop("bis", None)
                if "bis" in self.active_alerts:
                    await self._handle_alert_state("bis", 0.0, AlertLevel.GREEN, 0.0, "", timestamp)
                await self._set_sensor_issue(
                    "bis",
                    f"센서 확인 필요: BIS 신호품질 저하 (SQI={vitals.sqi:.0f}% < {config.SQI_THRESHOLD:.0f}%)",
                    timestamp,
                )
                continue

            if raw_val is None:
                continue

            # 2-B. 잡음(Spike) 및 생리학적 이상치 필터링
            # 단, BIS == 0은 센서 탈락/뇌파 억제 감별 로직에서 처리하므로 일반 잡음 필터에서 건너뜀
            if param_name == "bis" and raw_val == 0.0:
                is_noise = False
            else:
                is_noise = self._check_noise_spike(param_name, raw_val)

            if is_noise:
                continue

            # 3. 임계값 검사 및 이상 판별 (BIS=0 감별 및 EEG β파 보조 판정 포함)
            level, threshold, msg = self._evaluate_threshold(param_name, raw_val, vitals)

            # BIS==0에서 센서 불량으로 판정된 경우 (threshold < 0 표기): 기존 생리 경보 정리 후 센서 알림 발행
            if param_name == "bis" and threshold < 0.0:
                self._yellow_start_times.pop("bis", None)
                if "bis" in self.active_alerts:
                    await self._handle_alert_state("bis", 0.0, AlertLevel.GREEN, 0.0, "", timestamp)
                await self._set_sensor_issue("bis", msg, timestamp)
                continue
            elif param_name == "bis":
                await self._clear_sensor_issue("bis", timestamp)

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
            "sensor_problem": bool(self.sensor_issues),
            "sensor_issues": list(self.sensor_issues.values()),
        }
        await self.emit(EventType.VITAL_UPDATE, payload, timestamp)

    def _check_noise_spike(self, param_name: str, val: float) -> bool:
        """생리학적 불가능 범위 및 최근 10초 중앙값 대비 30% 급변(스파이크 잡음) 여부를 판정합니다.

        - MAP < 20 또는 > 200 mmHg 등 아티팩트는 즉시 잡음으로 기각합니다.
        - 30% 이상 급변이 config.NOISE_RECOVERY_SEC(5초) 이하이면 잡음으로 무시하되,
          임시 버퍼(_pending_spikes)에 보관합니다.
        - 5초를 초과하여 지속되면 실제 급변(예: MAP 90 -> 54)으로 인정하고
          기존 히스토리를 버퍼 값들로 즉시 교체하여 30초 지연을 방지합니다.
        """
        # 1. 생리학적 불가능 아티팩트 즉시 기각 (동맥관 플러싱/영점조절 잡음, D-09)
        if param_name == "map" and (val < config.MAP_OUTLIER_LOW or val > config.MAP_OUTLIER_HIGH):
            self.logger.debug("[Room %d] MAP 아티팩트 수치 무시: %.1f mmHg", self.room_id, val)
            return True

        history = self._history[param_name]
        pending = self._pending_spikes[param_name]

        if len(history) >= 3:
            median_val = statistics.median(history)
            if median_val > 0 and (abs(val - median_val) / median_val) > config.NOISE_SPIKE_RATIO:
                self._spike_counts[param_name] += 1
                pending.append(val)
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
                    # 5초 이상 지속된 변화는 실제 상태 변화로 즉시 인정하고 기준선 동기화
                    history.clear()
                    history.extend(pending)
                    pending.clear()
                    self._spike_counts[param_name] = 0
                    return False

        # 정상치 복귀 또는 초기 윈도우 채우는 중
        self._spike_counts[param_name] = 0
        pending.clear()
        history.append(val)
        return False

    @staticmethod
    def _to_pct(val: Optional[float]) -> Optional[float]:
        """0~1 비율 또는 0~100% 스케일의 뇌파 대역 값을 0~100(%) 단위로 정규화합니다."""
        if val is None:
            return None
        return val * 100.0 if 0.0 < val <= 1.0 else float(val)

    async def _set_sensor_issue(self, param_name: str, message: str, timestamp: float) -> None:
        """센서 확인 필요 상태를 등록하고 최초 발생 시 SENSOR_ALERT 이벤트를 발행합니다."""
        prev = self.sensor_issues.get(param_name)
        self.sensor_issues[param_name] = message
        if prev != message:
            self.logger.info("[Room %d] %s", self.room_id, message)
            await self.emit(
                EventType.SENSOR_ALERT,
                {
                    "room_id": self.room_id,
                    "parameter": param_name,
                    "message": message,
                    "timestamp": timestamp,
                },
                timestamp,
            )

    async def _clear_sensor_issue(self, param_name: str, timestamp: float) -> None:
        """센서 신호가 정상 복구되면 이슈를 해제하고 SENSOR_CLEARED 이벤트를 발행합니다."""
        if param_name in self.sensor_issues:
            self.sensor_issues.pop(param_name, None)
            await self.emit(
                EventType.SENSOR_CLEARED,
                {
                    "room_id": self.room_id,
                    "parameter": param_name,
                    "message": f"{param_name.upper()} 센서 신호 정상 복구",
                    "timestamp": timestamp,
                },
                timestamp,
            )

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

        # ── BIS (마취 심도 지수) + SQI/SR/EEG 3단계 감별 ──
        elif param_name == "bis":
            delta_pct = self._to_pct(vitals.eeg_delta)
            beta_pct = self._to_pct(vitals.eeg_beta)

            if val == 0.0:
                # Step 2: BIS == 0 감별
                # - SQI < 50 이거나
                # - SR(돌발억제비율)이 명시적으로 < 40% (실제 등전위 뇌파 억제가 아님) 이거나
                # - SR이 없더라도 δ파가 30% 이상 살아있는 경우 -> 센서 이상("센서 확인 필요")
                sqi_bad = vitals.sqi is not None and vitals.sqi < config.SQI_THRESHOLD
                sr_explicitly_normal = vitals.sr is not None and vitals.sr < config.SR_SUPPRESSION_THRESHOLD
                sr_not_suppressed = vitals.sr is None or vitals.sr < config.SR_SUPPRESSION_THRESHOLD
                delta_alive = delta_pct is not None and delta_pct >= config.EEG_DELTA_ACTIVITY_MIN

                if sqi_bad or sr_explicitly_normal or (sr_not_suppressed and delta_alive):
                    if vitals.sqi is not None:
                        detail = f"SQI={vitals.sqi:.0f}%"
                    elif delta_pct is not None:
                        detail = f"δ={delta_pct:.1f}%"
                    else:
                        detail = f"SR={vitals.sr:.0f}%"
                    # threshold=-1.0을 반환하여 호출부에서 침묵하지 않고 SENSOR_ALERT("센서 확인 필요")를 띄우도록 처리
                    return AlertLevel.GREEN, -1.0, f"센서 확인 필요: BIS=0 감지 ({detail}, 센서 탈락/신호 불량 의심)"
                else:
                    sr_info = f", SR={vitals.sr:.0f}%" if vitals.sr is not None else ""
                    delta_info = f", δ={delta_pct:.1f}%" if delta_pct is not None else ""
                    return (
                        AlertLevel.RED,
                        config.BIS_DEEP_WARNING,
                        f"BIS 위험 극저하 (뇌파 억제): {val:.1f} ({sr_info.lstrip(', ')}{delta_info})",
                    )

            # 정상 마취 유지 범위(40~60)일 때 환자 β파 기저치 누적
            if config.BIS_TARGET_LOW <= val <= config.BIS_TARGET_HIGH and beta_pct is not None:
                self._beta_baseline_samples.append(beta_pct)

            if val < config.BIS_DEEP_WARNING:
                return AlertLevel.RED, config.BIS_DEEP_WARNING, f"BIS 위험 과진정: {val:.1f} (< {config.BIS_DEEP_WARNING})"
            if val < config.BIS_TARGET_LOW:
                return AlertLevel.YELLOW, config.BIS_TARGET_LOW, f"BIS 주의 과진정: {val:.1f} (< {config.BIS_TARGET_LOW})"
            if val > config.BIS_TARGET_HIGH:
                # Step 3: BIS > 60 각성 경향 시 EEG β파(13~30Hz) 상승 동반 여부 보조 판정
                beta_note = ""
                if beta_pct is not None and len(self._beta_baseline_samples) >= 5:
                    base_beta = statistics.median(self._beta_baseline_samples)
                    if beta_pct >= base_beta + config.EEG_BETA_SURGE_DELTA:
                        beta_note = f", EEG β파 상승 동반({base_beta:.1f}%→{beta_pct:.1f}%)"
                return (
                    AlertLevel.YELLOW,
                    config.BIS_TARGET_HIGH,
                    f"BIS 주의 각성 위험: {val:.1f} (> {config.BIS_TARGET_HIGH}{beta_note})",
                )

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

    def reset(self) -> None:
        """타임라인 점프(seek) 또는 리셋 시 모든 일시적 감시 상태, 버퍼, 센서 이슈를 초기화합니다."""
        self.active_alerts.clear()
        self.sensor_issues.clear()
        self._history.clear()
        self._spike_counts.clear()
        self._pending_spikes.clear()
        self._yellow_start_times.clear()
        self._beta_baseline_samples.clear()
        self.current_alert_level = AlertLevel.GREEN
