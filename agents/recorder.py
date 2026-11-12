"""기록 에이전트 (RecorderAgent).

이벤트 버스에서 발생하는 모든 이벤트를 청취하여 영구 감사 로그(RecordEntry)로 저장하고,
- 투약 제안에 대한 의료진 승인율(Match Rate) 분석
- 수술실별 경보 통계 (수준별 경보 횟수)
- 시연 및 분석용 요약 통계(Summary) 생성
- 전체 기록 JSON 내보내기(Export) 기능을 제공합니다.
"""

from __future__ import annotations

import collections
import json
import logging
from typing import Any, Dict, List, Optional

from agents.base import BaseAgent
from core.enums import EventType
from core.event_bus import EventBus
from core.models import Event, RecordEntry

logger = logging.getLogger(__name__)


class RecorderAgent(BaseAgent):
    """전체 시스템 이벤트를 수집, 저장 및 통계 분석하는 기록 에이전트."""

    def __init__(self, event_bus: EventBus) -> None:
        """기록 에이전트 초기화.

        Args:
            event_bus: 중앙 이벤트 버스
        """
        super().__init__(agent_id="recorder", event_bus=event_bus)

        # 전체 이벤트 기록 목록
        self.records: List[RecordEntry] = []

        # 방별 투약 제안 결과 추적 (room_id -> {"approved": 0, "rejected": 0})
        self._outcomes_per_room: Dict[int, Dict[str, int]] = collections.defaultdict(
            lambda: {"approved": 0, "rejected": 0}
        )

        # 방별 투약 제안 총 횟수 (room_id -> int)
        self._suggestions_per_room: Dict[int, int] = collections.defaultdict(int)

        # 방별 수준별 경보 횟수 (room_id -> {"yellow": 0, "red": 0})
        self._alerts_per_room: Dict[int, Dict[str, int]] = collections.defaultdict(
            lambda: {"yellow": 0, "red": 0}
        )

        # 비상 상황 발생 횟수 (room_id -> int)
        self._emergencies_per_room: Dict[int, int] = collections.defaultdict(int)

    async def start(self) -> None:
        """에이전트 시작 및 이벤트 버스의 모든 이벤트 구독 등록."""
        await super().start()
        self.event_bus.subscribe_all(self.process)

    async def process(self, event: Event) -> None:
        """모든 수신 이벤트를 처리하여 RecordEntry로 저장하고 통계를 갱신합니다.

        Args:
            event: 이벤트 버스로부터 전달된 이벤트
        """
        # 1. 방 번호 추출 (없을 경우 0으로 기록)
        raw_room_id = event.data.get("room_id", 0)
        try:
            room_id = int(raw_room_id)
        except (ValueError, TypeError):
            room_id = 0

        # 2. 사람이 읽기 쉬운 한국어 설명 생성
        description = self._generate_description(event, room_id)

        # 3. RecordEntry 객체 생성 및 저장
        event_type_str = event.type.value if hasattr(event.type, "value") else str(event.type)
        entry = RecordEntry(
            room_id=room_id,
            event_type=event_type_str,
            description=description,
            details=event.data,
            timestamp=event.timestamp,
        )
        self.records.append(entry)

        # 4. 통계 항목 갱신
        if event.type == EventType.ALERT:
            raw_lvl = event.data.get("level", "yellow")
            lvl = (
                raw_lvl.value
                if hasattr(raw_lvl, "value")
                else str(raw_lvl).lower().replace("alertlevel.", "")
            )
            if lvl in ("yellow", "red"):
                self._alerts_per_room[room_id][lvl] += 1

        elif event.type == EventType.DRUG_SUGGESTION:
            self._suggestions_per_room[room_id] += 1

        elif event.type == EventType.APPROVAL_RESPONSE:
            approved = event.data.get("approved")
            if approved is True:
                self._outcomes_per_room[room_id]["approved"] += 1
            elif approved is False:
                self._outcomes_per_room[room_id]["rejected"] += 1

        elif event.type == EventType.EMERGENCY:
            self._emergencies_per_room[room_id] += 1

    def _generate_description(self, event: Event, room_id: int) -> str:
        """이벤트 유형별 가독성 높은 설명 문자열을 생성합니다."""
        data = event.data

        if event.type == EventType.VITAL_UPDATE:
            return (
                f"수술실 {room_id} 바이탈 갱신 "
                f"(MAP: {data.get('map')}, HR: {data.get('hr')}, "
                f"SpO2: {data.get('spo2')}, BIS: {data.get('bis')})"
            )

        if event.type == EventType.ALERT:
            raw_lvl = data.get("level", "")
            level = (raw_lvl.value if hasattr(raw_lvl, "value") else str(raw_lvl)).upper()
            msg = data.get("message", "경보")
            return f"수술실 {room_id} [{level} 경보] {msg}"

        if event.type == EventType.ALERT_CLEARED:
            msg = data.get("message", f"{data.get('parameter', '')} 해제")
            return f"수술실 {room_id} [경보 해제] {msg}"

        if event.type == EventType.SENSOR_ALERT:
            msg = data.get("message", "센서 확인 필요")
            return f"수술실 {room_id} [센서 점검] {msg}"

        if event.type == EventType.SENSOR_CLEARED:
            msg = data.get("message", "센서 신호 복구")
            return f"수술실 {room_id} [센서 복구] {msg}"

        if event.type == EventType.DRUG_SUGGESTION:
            raw_drug = data.get("drug_type", "")
            drug = raw_drug.value if hasattr(raw_drug, "value") else str(raw_drug)
            raw_action = data.get("action", "")
            action = raw_action.value if hasattr(raw_action, "value") else str(raw_action)
            curr_v = data.get("current_value", "")
            sugg_v = data.get("suggested_value", "")
            reason = data.get("reason", "")
            return f"수술실 {room_id} 투약 제안: {drug} {action} ({curr_v} -> {sugg_v}) [{reason}]"

        if event.type == EventType.APPROVAL_RESPONSE:
            approved = data.get("approved")
            status_text = "승인 (반영 완료)" if approved else "거부 (미반영)"
            drug = data.get("drug_type", "")
            sugg_v = data.get("suggested_value", "")
            return f"수술실 {room_id} 의료진 결정: {drug} -> {sugg_v} {status_text}"

        if event.type == EventType.EMERGENCY:
            return f"수술실 {room_id} [비상 상황] 위험(RED) 경보에 따른 긴급 모드 진입"

        if event.type == EventType.EMERGENCY_CLEARED:
            return f"수술실 {room_id} [비상 해제] 정상 모드 복귀"

        if event.type == EventType.PRIORITY_UPDATE:
            ranking = data.get("ranking", [])
            return f"수술실 집중도 우선순위 갱신 (1위: {ranking[0] if ranking else '-'}번 방)"

        return f"이벤트 {event.type.value if hasattr(event.type, 'value') else event.type} 발생"

    def get_match_rate(self, room_id: Optional[int] = None) -> float:
        """투약 제안 승인율(Match Rate)을 계산합니다.

        Args:
            room_id: 특정 수술실 번호 (None인 경우 전체 수술실 대상)

        Returns:
            float: 승인율 (0.0 ~ 1.0)
        """
        if room_id is not None:
            stats = self._outcomes_per_room.get(room_id, {"approved": 0, "rejected": 0})
            total = stats["approved"] + stats["rejected"]
            return round(stats["approved"] / total, 3) if total > 0 else 0.0

        total_approved = sum(s["approved"] for s in self._outcomes_per_room.values())
        total_rejected = sum(s["rejected"] for s in self._outcomes_per_room.values())
        grand_total = total_approved + total_rejected
        return round(total_approved / grand_total, 3) if grand_total > 0 else 0.0

    def get_records(
        self,
        room_id: Optional[int] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[RecordEntry]:
        """필터링된 이벤트 기록 목록을 반환합니다.

        Args:
            room_id: 특정 수술실 번호 (None인 경우 전체)
            limit: 반환할 최대 최근 레코드 수 (0 이하인 경우 전체 반환)

        Returns:
            List[RecordEntry]: 조건에 맞는 기록 리스트
        """
        filtered = [
            r for r in self.records
            if (room_id is None or r.room_id == room_id)
            and (event_type is None or r.event_type == event_type)
        ]
        if limit > 0:
            return filtered[-limit:]
        return filtered

    def get_summary(self) -> Dict[str, Any]:
        """종합 모니터링 통계 요약 정보를 생성합니다.

        Returns:
            Dict[str, Any]: 경보 통계, 제안 수, 승인율, 중요 타임라인 등 요약
        """
        # 중요 임상 이벤트만 필터링한 타임라인 (매 틱 반복되는 VITAL/PRIORITY/SYSTEM 제외)
        excluded_types = {
            EventType.VITAL_UPDATE.value,
            EventType.PRIORITY_UPDATE.value,
            EventType.SYSTEM.value,
        }
        significant_events = [
            r.model_dump(mode="json")
            for r in self.records
            if r.event_type not in excluded_types
        ]

        yellow_alerts = sum(lvl_dict.get("yellow", 0) for lvl_dict in self._alerts_per_room.values())
        red_alerts = sum(lvl_dict.get("red", 0) for lvl_dict in self._alerts_per_room.values())
        total_alerts = yellow_alerts + red_alerts
        total_suggestions = sum(self._suggestions_per_room.values())
        approved_suggestions = sum(s.get("approved", 0) for s in self._outcomes_per_room.values())
        rejected_suggestions = sum(s.get("rejected", 0) for s in self._outcomes_per_room.values())
        total_emergencies = sum(self._emergencies_per_room.values())
        elapsed_seconds = max((r.timestamp for r in self.records), default=0.0)

        room_ids = sorted(
            list(
                set(
                    list(self._alerts_per_room.keys())
                    + list(self._suggestions_per_room.keys())
                    + [1, 2, 3, 4]
                )
            )
        )

        approval_rate_by_room = {
            rid: self.get_match_rate(rid) for rid in room_ids if rid > 0
        }

        return {
            "total_records": len(self.records),
            "total_alerts": total_alerts,
            "yellow_alerts": yellow_alerts,
            "red_alerts": red_alerts,
            "total_suggestions": total_suggestions,
            "approved_suggestions": approved_suggestions,
            "rejected_suggestions": rejected_suggestions,
            "total_emergencies": total_emergencies,
            "elapsed_seconds": elapsed_seconds,
            "overall_match_rate": self.get_match_rate(),
            "alerts_by_room": {
                rid: dict(self._alerts_per_room[rid])
                for rid in room_ids if rid > 0
            },
            "suggestions_by_room": {
                rid: self._suggestions_per_room.get(rid, 0)
                for rid in room_ids if rid > 0
            },
            "approval_rate_by_room": approval_rate_by_room,
            "emergencies_by_room": {
                rid: self._emergencies_per_room.get(rid, 0)
                for rid in room_ids if rid > 0
            },
            "timeline": significant_events,
        }

    def export_json(self) -> str:
        """모든 기록 데이터를 JSON 문자열로 내보냅니다."""
        records_data = [r.model_dump(mode="json") for r in self.records]
        return json.dumps(records_data, ensure_ascii=False, indent=2)
