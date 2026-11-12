"""Pydantic 데이터 모델 — 에이전트 간 메시지 구조."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

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

    # 마취 심도
    bis: Optional[float] = None          # BIS 지수

    # 환기
    rr: Optional[float] = None           # 호흡수
    pip: Optional[float] = None          # 최고기도압
    tv: Optional[float] = None           # 일회호흡량
    mac: Optional[float] = None          # MAC 값

    # 체온
    bt: Optional[float] = None           # 체온 (°C)

    # EEG 주파수 성분 (뇌파 분석용)
    eeg_delta: Optional[float] = None    # δ 파워
    eeg_alpha: Optional[float] = None    # α 파워
    eeg_beta: Optional[float] = None     # β 파워


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
