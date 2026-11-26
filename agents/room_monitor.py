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
from typing import Deque, Dict, Optional, Tuple, Any, List

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

        # 가짜 경보 억제 통계 (사건 단위 누적, 리셋/구간 이동 시 0으로 초기화)
        # - suppressed_artifacts: 순간 잡음/아티팩트가 5초 안에 정상 복귀하여 기각된 사건 수
        # - suppressed_transients: 주의(YELLOW) 범위 이탈이 60초 지속 전에 회복되어 경보를 내지 않은 사건 수
        self.suppressed_artifacts: int = 0
        self.suppressed_transients: int = 0
        self._noise_episode: set = set()          # 잡음으로 기각 중인 파라미터
        # 억제 내역 기록 (대시보드 '경보 억제 내역' 창용, 최근 MAX_SUPPRESSION_LOG건)
        self.suppression_log: List[Dict[str, Any]] = []
        self._noise_info: Dict[str, Dict[str, Any]] = {}     # 잡음 사건별 시작 시각·대표 값
        self._yellow_info: Dict[str, Dict[str, Any]] = {}    # 주의 이탈 사건별 최저/최고값·기준
        self._flush_suspect: bool = False                    # MAP 상승 빨강 지연(동맥관 세척 의심) 발동 여부
        self._sensor_record_ids: Dict[str, str] = {}         # 진행 중인 센서 이상 기록 id
        self._recovering_from_red: set = set()    # RED→YELLOW 호전 중인 파라미터 (일시 이탈 집계 제외)
        self._high_map_since: Optional[float] = None  # MAP 상승(>110) 시작 시각. 동맥관 플러싱(20~50초) 오경보 방지용

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
                    value=vitals.sqi,
                    value_label="SQI",
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
                # 기각한 값이 그대로 통과했다면 경보(YELLOW/RED)가 났을 경우에만 '가짜 경보 억제' 후보로 표시
                noise_level, _, _ = self._evaluate_threshold(param_name, raw_val, vitals, record_baseline=False)
                if noise_level != AlertLevel.GREEN:
                    self._noise_episode.add(param_name)
                    info = self._noise_info.setdefault(
                        param_name, {"start": timestamp, "value": raw_val, "level": noise_level}
                    )
                    # 대표 값: 처음 기각한 값, 이후 더 심각한(RED) 단계를 유발했을 값이 나오면 그 값으로 교체
                    if (noise_level == AlertLevel.RED and info["level"] != AlertLevel.RED):
                        info.update(value=raw_val, level=noise_level)
                continue

            # 잡음 사건이 실제 급변으로 승격되지 않고 정상 복귀했다면 억제 1건으로 집계
            # (실제 급변으로 인정된 경우 _check_noise_spike에서 _noise_episode가 먼저 정리됨)
            if param_name in self._noise_episode:
                self._noise_episode.discard(param_name)
                self.suppressed_artifacts += 1
                info = self._noise_info.pop(param_name, None) or {"start": timestamp, "value": None}
                dur = max(1.0, timestamp - info["start"])
                outlier = (
                    param_name == "map" and info["value"] is not None
                    and (info["value"] < config.MAP_OUTLIER_LOW or info["value"] > config.MAP_OUTLIER_HIGH)
                )
                reason = (
                    f"생리적으로 불가능한 값({config.MAP_OUTLIER_LOW:.0f} 미만/{config.MAP_OUTLIER_HIGH:.0f} 초과) → 측정 아티팩트로 기각"
                    if outlier else
                    f"직전 10초 중앙값 대비 {config.NOISE_SPIKE_RATIO * 100:.0f}% 이상 급변 후 "
                    f"{config.NOISE_RECOVERY_SEC}초 안에 정상 복귀 → 순간 잡음으로 기각"
                )
                await self._record_suppression(
                    category="sensor", kind="artifact", parameter=param_name,
                    value=info["value"], start=info["start"], end=timestamp,
                    reason=f"{reason} (지속 {dur:.0f}초)",
                )

            # 3. 임계값 검사 및 이상 판별 (BIS=0 감별 및 EEG β파 보조 판정 포함)
            level, threshold, msg = self._evaluate_threshold(param_name, raw_val, vitals)

            # BIS==0에서 센서 불량으로 판정된 경우 (threshold < 0 표기): 기존 생리 경보 정리 후 센서 알림 발행
            if param_name == "bis" and threshold < 0.0:
                self._yellow_start_times.pop("bis", None)
                if "bis" in self.active_alerts:
                    await self._handle_alert_state("bis", 0.0, AlertLevel.GREEN, 0.0, "", timestamp)
                await self._set_sensor_issue("bis", msg, timestamp, value=raw_val)
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
            "suppressed_artifacts": self.suppressed_artifacts,
            "suppressed_transients": self.suppressed_transients,
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
                    # (가짜 경보 억제 건수에 포함하지 않음)
                    self._noise_episode.discard(param_name)
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

    async def _set_sensor_issue(
        self, param_name: str, message: str, timestamp: float, value: Optional[float] = None,
        value_label: Optional[str] = None,
    ) -> None:
        """센서 확인 필요 상태를 등록하고 최초 발생 시 SENSOR_ALERT 이벤트를 발행합니다."""
        # 메시지 문구(δ% 등 실시간 수치)는 매 틱 최신값으로 갱신하되,
        # 이벤트는 해당 파라미터의 센서 이상이 "새로 시작될 때" 한 번만 발행합니다.
        # (문구가 바뀔 때마다 발행하면 사건 1건에 이벤트가 수십 번 쌓임)
        is_new_issue = param_name not in self.sensor_issues
        self.sensor_issues[param_name] = message
        if is_new_issue:
            self.logger.info("[Room %d] %s", self.room_id, message)
            # 경보 억제 내역: 의료 RED 대신 '센서 점검'으로 분류한 사건 (복구되면 종료 시각 갱신)
            rec = await self._record_suppression(
                category="sensor", kind="sensor_fault", parameter=param_name, value=value,
                start=timestamp, end=None, value_label=value_label,
                reason=message.replace("센서 확인 필요: ", "") + " → 의료 위험 경보 대신 센서 점검으로 분류",
            )
            self._sensor_record_ids[param_name] = rec["id"]
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
            rec_id = self._sensor_record_ids.pop(param_name, None)
            for rec in self.suppression_log:
                if rec["id"] == rec_id:
                    rec["end"] = timestamp
                    rec["duration"] = round(timestamp - rec["start"], 1)
                    await self.emit(EventType.SUPPRESSION, dict(rec), timestamp)
                    break
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
        record_baseline: bool = True,
    ) -> Tuple[AlertLevel, float, str]:
        """파라미터별 의학적 임계값을 비교하여 상태 수준을 판정합니다.

        record_baseline=False이면 β파 기저치 누적 같은 부수효과 없이 판정만 수행합니다
        (잡음으로 기각된 값의 '경보 유발 여부'만 확인할 때 사용).

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
            if record_baseline and config.BIS_TARGET_LOW <= val <= config.BIS_TARGET_HIGH and beta_pct is not None:
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
        # 0. MAP '상승' 쪽 빨강은 즉시 발령하지 않음 (D-09에는 저혈압 55 미만만 즉시 빨강).
        #    실제 VitalDB 시연 데이터(OR-1, OR-3)에서 동맥관 플러싱으로 MAP 140~237이 20~50초 이어져
        #    즉시 빨강이면 가짜 RED가 나고 1위가 계속 뒤바뀜 → 상승이 MAP_HIGH_RED_DELAY_SEC 이상 지속될 때만 빨강
        if param_name == "map":
            if val > config.MAP_WARNING_HIGH:
                if self._high_map_since is None:
                    self._high_map_since = timestamp
                if (condition_level == AlertLevel.RED
                        and timestamp - self._high_map_since < getattr(config, "MAP_HIGH_RED_DELAY_SEC", 60)):
                    condition_level = AlertLevel.YELLOW
                    self._flush_suspect = True  # 위험 수준 상승이 짧게 끝나면 동맥관 세척으로 분류
            else:
                self._high_map_since = None

        # 1. 정상이 된 경우
        if condition_level == AlertLevel.GREEN:
            yellow_started = self._yellow_start_times.pop(param_name, None)
            recovering = param_name in self._recovering_from_red
            self._recovering_from_red.discard(param_name)
            # 주의 범위 이탈이 60초 지속 전에 회복됨 → 경보 미발령(일시 이탈 억제) 1건 집계
            # 단, 너무 짧은 경계값 흔들림(TRANSIENT_SUPPRESS_MIN_SEC 미만)과 RED 호전 과정은 제외
            y_info = self._yellow_info.pop(param_name, None)
            flush = param_name == "map" and self._flush_suspect
            if param_name == "map":
                self._flush_suspect = False
            if (
                yellow_started is not None
                and not recovering
                and param_name not in self.active_alerts
                and timestamp - yellow_started >= getattr(config, "TRANSIENT_SUPPRESS_MIN_SEC", 5)
            ):
                dur = timestamp - yellow_started
                info = y_info or {"low": val, "high": val, "threshold": threshold, "msg": ""}
                if flush:
                    # 위험 수준 MAP 상승이 60초 안에 끝남 → 동맥관 세척(플러싱) 압력으로 보고 센서 잡음으로 분류
                    self.suppressed_artifacts += 1
                    await self._record_suppression(
                        category="sensor", kind="flush", parameter=param_name, value=info["high"],
                        start=yellow_started, end=timestamp,
                        reason=(f"MAP 최고 {info['high']:.0f}mmHg까지 상승했다가 {dur:.0f}초 만에 정상 복귀 "
                                f"→ 동맥관 세척(플러싱) 압력으로 추정, 위험 경보 보류"),
                    )
                else:
                    self.suppressed_transients += 1
                    th = info["threshold"]
                    is_low = info["low"] < th if th is not None else True
                    extreme = info["low"] if is_low else info["high"]
                    await self._record_suppression(
                        category="transient", kind="transient", parameter=param_name, value=extreme,
                        start=yellow_started, end=timestamp, threshold=th,
                        direction="low" if is_low else "high",
                        reason=(f"기준 {th:g} {'미만으로' if is_low else '초과로'} {dur:.0f}초 벗어났다가 회복 "
                                f"→ {config.ALERT_PERSISTENCE_SEC}초 지속 기준 미달로 경보 보류"),
                    )
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
            self._yellow_info.pop(param_name, None)
            self._recovering_from_red.discard(param_name)
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
            # 이전에 RED 경보였다면, 경보를 끊지 않고 같은 지표의 YELLOW 경보로 바로 낮춤.
            # (예전: RED 해제 후 YELLOW 60초를 처음부터 다시 셈 → 환자는 여전히 이상 범위인데
            #  그 1분 동안 경보가 사라져 우선순위가 급락하는 문제. 실제 OR-3 MAP 54↔55 구간에서 확인)
            if param_name in self.active_alerts and self.active_alerts[param_name].level == AlertLevel.RED:
                prev_red = self.active_alerts[param_name]
                alert = Alert(
                    room_id=self.room_id,
                    level=AlertLevel.YELLOW,
                    parameter=param_name,
                    value=val,
                    threshold=threshold,
                    message=f"{msg} (위험 단계에서 호전, 주의 범위 지속)",
                    timestamp=timestamp,
                )
                self.active_alerts[param_name] = alert
                self._yellow_start_times.pop(param_name, None)
                self.logger.info("[Room %d] RED → YELLOW 하향: %s (RED 발생 시각 %.0f)",
                                 self.room_id, param_name, prev_red.timestamp)
                # 같은 지표 경보는 Supervisor·대시보드에서 '교체'되므로 ALERT_CLEARED 없이 ALERT만 발행
                await self.emit(EventType.ALERT, alert.model_dump(), timestamp)
                return

            # 이미 발령된 YELLOW 경보가 있는 경우 값만 갱신
            if param_name in self.active_alerts and self.active_alerts[param_name].level == AlertLevel.YELLOW:
                self.active_alerts[param_name].value = val
                return

            # 아직 발령되지 않은 YELLOW 상태: 지속 시간 계산
            if param_name not in self._yellow_start_times:
                self._yellow_start_times[param_name] = timestamp
            # 이탈 중 최저/최고값 추적 (60초 전 회복 시 '일시 이탈' 내역에 표시)
            y = self._yellow_info.setdefault(param_name, {"low": val, "high": val, "threshold": threshold})
            y["low"] = min(y["low"], val)
            y["high"] = max(y["high"], val)

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
                self._recovering_from_red.discard(param_name)
                self.logger.info("[Room %d] YELLOW 경보 발령 (60초 지속 충족): %s", self.room_id, msg)
                await self.emit(EventType.ALERT, alert.model_dump(), timestamp)

    async def _record_suppression(
        self,
        category: str,
        kind: str,
        parameter: str,
        value: Optional[float],
        start: float,
        end: Optional[float],
        reason: str,
        threshold: Optional[float] = None,
        direction: Optional[str] = None,
        value_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        """경보를 내지 않고 걸러낸 사건 1건을 근거와 함께 기록하고 SUPPRESSION 이벤트로 발행합니다.

        category: "sensor"(장비·측정 문제: 센서 불량·순간 잡음·동맥관 세척)
                  "transient"(값은 실제지만 기준선 근처를 잠깐 오르내려 60초 지속 기준 미달)
        """
        rec: Dict[str, Any] = {
            "id": f"sup-{self.room_id}-{len(self.suppression_log) + 1}-{int(start)}",
            "room_id": self.room_id,
            "category": category,
            "kind": kind,
            "parameter": parameter,
            "value": round(float(value), 1) if value is not None else None,
            "value_label": value_label or parameter.upper(),
            "threshold": threshold,
            "direction": direction,
            "start": start,
            "end": end,
            "duration": round(end - start, 1) if end is not None else None,
            "reason": reason,
        }
        self.suppression_log.append(rec)
        max_len = getattr(config, "MAX_SUPPRESSION_LOG", 300)
        if len(self.suppression_log) > max_len:
            del self.suppression_log[: len(self.suppression_log) - max_len]
        await self.emit(EventType.SUPPRESSION, dict(rec), end if end is not None else start)
        return rec

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
        self._noise_episode.clear()
        self._recovering_from_red.clear()
        self._high_map_since = None
        self.suppression_log.clear()
        self._noise_info.clear()
        self._yellow_info.clear()
        self._flush_suspect = False
        self._sensor_record_ids.clear()
        self.suppressed_artifacts = 0
        self.suppressed_transients = 0
        self.current_alert_level = AlertLevel.GREEN
