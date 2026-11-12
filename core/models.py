"""Pydantic 데이터 모델 — 에이전트 간 메시지 구조."""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .enums import AlertLevel, DrugType, EventType, Phase, SuggestionAction


# ── 바이탈 사인 ──────────────────────────────────────────────

class VitalSigns(BaseModel):
    """1초 단위 생체신호 스냅샷."""
    room_id: int
    timestamp: float                     # 시뮬레이션 경과 시간(초)

    # 혈역학
    map: Optional[float] = None          # 평균동맥압 (mmHg)
    sbp: Optional[float] = None          # 수축기혈압
    dbp: Optional[float] = None          # 이완기혈압
    hr: Optional[float] = None           # 심박수

    # 산소화
    spo2: Optional[float] = None         # 산소포화도 (%)
    etco2: Optional[float] = None        # 호기말 CO₂

    # 마취 심도 및 뇌파 품질 지표 (BIS 모듈)
    bis: Optional[float] = None          # BIS 지수
    sqi: Optional[float] = None          # 신호 품질 지수 (Signal Quality Index, %)
    sr: Optional[float] = None           # 돌발 억제 비율 (Suppression Ratio, %)
    emg: Optional[float] = None          # 근전도 파워 (EMG, dB)
    sef: Optional[float] = None          # Spectral Edge Frequency 95 (Hz)

    # 환기
    rr: Optional[float] = None           # 호흡수
    pip: Optional[float] = None          # 최고기도압
    tv: Optional[float] = None           # 일회호흡량
    mac: Optional[float] = None          # MAC 값

    # 체온
    bt: Optional[float] = None           # 체온 (°C)

    # TCI 약물 농도 (Orchestra 펌프 기록: CT=의사 설정 목표치, CE=효과처 농도)
    ppf_ct: Optional[float] = None       # 프로포폴 목표 농도 (mcg/mL)
    ppf_ce: Optional[float] = None       # 프로포폴 효과처 농도 (mcg/mL)
    rftn_ct: Optional[float] = None      # 레미펜타닐 목표 농도 (ng/mL)
    rftn_ce: Optional[float] = None      # 레미펜타닐 효과처 농도 (ng/mL)

    # EEG 주파수 성분 (뇌파 분석용, 0~1 또는 0~100%)
    eeg_delta: Optional[float] = None    # δ 파워 (1~4 Hz)
    eeg_alpha: Optional[float] = None    # α 파워 (8~12 Hz)
    eeg_beta: Optional[float] = None     # β 파워 (13~30 Hz)

    @field_validator(
        "map", "sbp", "dbp", "hr", "spo2", "etco2",
        "bis", "sqi", "sr", "emg", "sef",
        "rr", "pip", "tv", "mac", "bt",
        "ppf_ct", "ppf_ce", "rftn_ct", "rftn_ce",
        "eeg_delta", "eeg_alpha", "eeg_beta",
        mode="before",
    )
    @classmethod
    def _sanitize_float(cls, v: Any) -> Optional[float]:
        """NaN 또는 Inf 값이 유입될 경우 안전하게 None으로 변환하여 JSON 직렬화 및 비교 오류를 방지합니다."""
        if v is None:
            return None
        try:
            fv = float(v)
            if math.isnan(fv) or math.isinf(fv):
                return None
            return fv
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _compute_eeg_bands_from_wave(eeg_dict: Dict[str, Any]) -> Dict[str, Optional[float]]:
        """Frame.eeg에 원시 파형(eeg1, 128Hz)만 있을 때 FFT로 δ(1~4Hz), α(8~12Hz), β(13~30Hz) 비율(%)을 산출합니다."""
        out: Dict[str, Optional[float]] = {
            "delta": eeg_dict.get("delta"),
            "alpha": eeg_dict.get("alpha"),
            "beta": eeg_dict.get("beta"),
        }
        if out["delta"] is not None or "eeg1" not in eeg_dict:
            return out

        try:
            import numpy as np

            raw = np.asarray(eeg_dict["eeg1"], dtype=np.float64)
            valid = raw[~np.isnan(raw)]
            if len(valid) < 32:
                return out
            sig = valid - np.mean(valid)
            freqs = np.fft.rfftfreq(len(sig), d=1.0 / 128.0)
            psd = np.abs(np.fft.rfft(sig)) ** 2
            tot = float(psd[(freqs >= 1.0) & (freqs < 30.0)].sum())
            if tot <= 1e-12:
                return out
            out["delta"] = round(float(100.0 * psd[(freqs >= 1.0) & (freqs < 4.0)].sum() / tot), 2)
            out["alpha"] = round(float(100.0 * psd[(freqs >= 8.0) & (freqs < 12.0)].sum() / tot), 2)
            out["beta"] = round(float(100.0 * psd[(freqs >= 13.0) & (freqs < 30.0)].sum() / tot), 2)
        except Exception:
            pass
        return out

    @classmethod
    def from_replayer_frame(cls, room_id: int, frame: Any) -> "VitalSigns":
        """1번 주자(replay/replayer.py)의 Frame 객체를 VitalSigns로 변환하는 어댑터."""
        num = getattr(frame, "num", {}) or {}
        vent = getattr(frame, "vent", {}) or {}
        eeg = getattr(frame, "eeg", {}) or {}
        bands = cls._compute_eeg_bands_from_wave(eeg)

        # 시뮬레이션 공통 시계는 elapsed를 우선 사용하고, 없을 때만 케이스 시각(t) 사용
        elapsed = getattr(frame, "elapsed", None)
        ts = float(elapsed if elapsed is not None else getattr(frame, "t", 0.0))

        def _scalar(val: Any) -> Optional[float]:
            if val is None or hasattr(val, "__len__"):
                return None
            return val

        return cls(
            room_id=room_id,
            timestamp=ts,
            map=_scalar(num.get("map")),
            sbp=_scalar(num.get("sbp")),
            dbp=_scalar(num.get("dbp")),
            hr=_scalar(num.get("hr")),
            spo2=_scalar(num.get("spo2")),
            etco2=_scalar(num.get("etco2")),
            bis=_scalar(num.get("bis")),
            sqi=_scalar(num.get("sqi")),
            sr=_scalar(num.get("sr")),
            emg=_scalar(num.get("emg")),
            sef=_scalar(num.get("sef")),
            rr=_scalar(num.get("rr")) if _scalar(num.get("rr")) is not None else _scalar(vent.get("rr")),
            pip=_scalar(num.get("pip")) if _scalar(num.get("pip")) is not None else _scalar(vent.get("pip")),
            tv=_scalar(num.get("tv")) if _scalar(num.get("tv")) is not None else _scalar(vent.get("tv")),
            mac=_scalar(num.get("mac")) if _scalar(num.get("mac")) is not None else _scalar(vent.get("mac")),
            bt=_scalar(num.get("bt")),
            ppf_ct=_scalar(num.get("ppf_ct")),
            ppf_ce=_scalar(num.get("ppf_ce")),
            rftn_ct=_scalar(num.get("rftn_ct")),
            rftn_ce=_scalar(num.get("rftn_ce")),
            eeg_delta=_scalar(bands.get("delta")),
            eeg_alpha=_scalar(bands.get("alpha")),
            eeg_beta=_scalar(bands.get("beta")),
        )


# ── 경보 ──────────────────────────────────────────────────────

class Alert(BaseModel):
    """방별 감시 에이전트가 생성하는 경보."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    room_id: int
    level: AlertLevel
    parameter: str                       # 이상 수치 이름
    value: float                         # 현재 값
    threshold: float                     # 초과한 임계값
    message: str
    timestamp: float
    created_at: datetime = Field(default_factory=datetime.now)


# ── 투약 제안 ─────────────────────────────────────────────────

class DrugSuggestion(BaseModel):
    """투약 제안 에이전트가 생성하는 조정 제안."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    room_id: int
    drug_type: DrugType
    action: SuggestionAction
    amount: float                        # 조정량 (±0.5)
    current_value: float                 # 현재 농도/속도
    suggested_value: float               # 제안 농도/속도
    reason: str
    timestamp: float
    approved: Optional[bool] = None      # None=대기, True=승인, False=거부
    created_at: datetime = Field(default_factory=datetime.now)


# ── 방 종합 상태 ──────────────────────────────────────────────

class RoomStatus(BaseModel):
    """중앙 감독 에이전트가 관리하는 방별 종합 상태."""
    room_id: int
    alert_level: AlertLevel = AlertLevel.GREEN
    active_alerts: List[Alert] = Field(default_factory=list)
    pending_suggestions: List[DrugSuggestion] = Field(default_factory=list)
    vitals: Optional[VitalSigns] = None
    priority_score: float = 0.0
    phase: Phase = Phase.MAINTENANCE
    is_emergency: bool = False
    timestamp: float = 0.0

    # 현재 투약 상태 (Ce 목표값)
    propofol_ce: float = 3.0             # 프로포폴 Ce (mcg/mL)
    remifentanil_ce: float = 3.0         # 레미펜타닐 Ce (ng/mL)

    # 센서/신호 품질 이상 여부 ("센서 확인 필요" 배지 표시용)
    sensor_problem: bool = False
    sensor_issues: List[str] = Field(default_factory=list)


# ── 이벤트 (버스 메시지) ──────────────────────────────────────

class Event(BaseModel):
    """이벤트 버스를 통해 전달되는 메시지 래퍼."""
    type: EventType
    data: Dict[str, Any]
    source: str                          # 발신 에이전트 ID
    timestamp: float
    created_at: datetime = Field(default_factory=datetime.now)


# ── 기록 항목 ─────────────────────────────────────────────────

class RecordEntry(BaseModel):
    """기록 에이전트가 저장하는 단일 이벤트 로그."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    room_id: int
    event_type: str
    description: str
    details: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float
    created_at: datetime = Field(default_factory=datetime.now)
