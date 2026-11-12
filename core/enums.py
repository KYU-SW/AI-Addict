"""열거형 정의 — 에이전트 간 공통 상수."""

from enum import Enum


class AlertLevel(str, Enum):
    """경보 수준 (3단계 색상)."""
    GREEN = "green"    # 정상
    YELLOW = "yellow"  # 주의
    RED = "red"        # 위험


class DrugType(str, Enum):
    """투약 약물 종류."""
    PROPOFOL = "propofol"
    REMIFENTANIL = "remifentanil"


class EventType(str, Enum):
    """이벤트 버스 메시지 유형."""
    VITAL_UPDATE = "vital_update"
    ALERT = "alert"
    ALERT_CLEARED = "alert_cleared"
    SENSOR_ALERT = "sensor_alert"
    SENSOR_CLEARED = "sensor_cleared"
    DRUG_SUGGESTION = "drug_suggestion"
    SUGGESTION_CANCELLED = "suggestion_cancelled"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESPONSE = "approval_response"
    EMERGENCY = "emergency"
    EMERGENCY_CLEARED = "emergency_cleared"
    ROOM_STATUS = "room_status"
    PRIORITY_UPDATE = "priority_update"
    RECORD = "record"
    SYSTEM = "system"


class Phase(str, Enum):
    """마취 단계."""
    INDUCTION = "induction"      # 유도기
    MAINTENANCE = "maintenance"  # 유지기
    EMERGENCE = "emergence"      # 각성기


class SuggestionAction(str, Enum):
    """투약 제안 방향."""
    INCREASE = "increase"   # 증량
    DECREASE = "decrease"   # 감량
    MAINTAIN = "maintain"   # 유지
