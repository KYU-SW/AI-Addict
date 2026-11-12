"""실제 VitalDB 케이스 재생기 → 에이전트 연결용 어댑터.

1번 주자 재생기(replay/replayer.py)가 읽은 cases/ 데이터를
에이전트들이 쓰는 VitalSigns 모델로 바꿔서 넘겨준다.
EEG·NaN 처리 등 실제 변환은 core/models.py의 VitalSigns.from_replayer_frame을 그대로 사용.
main.py가 기대하는 인터페이스(data/replayer.py의 VitalDBReplayer와 동일)를 그대로 따른다:
    rp = RealCaseReplayer(case_ids=...)   # case_ids는 무시 (selected_cases.csv 기준)
    await rp.load()
    rp.max_ticks
    rp.get_vitals(room_id, tick)          # 아무 틱이나 바로 조회 가능 → seek(구간 이동) 그대로 동작

방 매핑: room_id 1~4 = selected_cases.csv의 OR-1~OR-4
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config
from core.models import VitalSigns
from replay.replayer import FS, CaseReplayer

logger = logging.getLogger(__name__)

ROOMS = ("OR-1", "OR-2", "OR-3", "OR-4")

EEG_WINDOW_S = 4   # 뇌파 대역 계산에 쓸 최근 구간 (초)


class RealCaseReplayer:
    """selected_cases.csv의 OR-1~4를 실제 데이터로 재생."""

    def __init__(self, case_ids: Optional[List[int]] = None, speed: int = 1,
                 csv_path: str = None, data_dir: str = None, from_demo_start: bool = None) -> None:
        self.csv_path = csv_path or getattr(config, "SELECTED_CASES_CSV", "data/selected_cases.csv")
        self.data_dir = data_dir or getattr(config, "CASES_DIR", "cases")
        # True: csv의 demo_start_s부터 재생 (이벤트 몇 분 전) / False: 케이스 처음(0초)부터 전체 재생
        self.from_demo_start = (getattr(config, "REPLAY_FROM_DEMO_START", True)
                                if from_demo_start is None else from_demo_start)
        self.speed = speed
        self.rooms: Dict[int, CaseReplayer] = {}
        self._max_ticks = 0
        self.jump_points: List[dict] = []   # 대시보드 '시연 구간 바로가기' 버튼용

    @property
    def max_ticks(self) -> int:
        return self._max_ticks

    async def load(self) -> None:
        # 파켓 읽기가 수십 초 걸릴 수 있어서 서버 이벤트 루프를 막지 않도록 별도 스레드에서
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        sel = pd.read_csv(self.csv_path).set_index("room").loc[list(ROOMS)]
        for room_id, (room, row) in enumerate(sel.iterrows(), start=1):
            start = 0
            if self.from_demo_start and pd.notna(row.demo_start_s):
                start = int(row.demo_start_s)
            self.rooms[room_id] = CaseReplayer(int(row.caseid), start, room, self.data_dir)
            logger.info("[수술실 %d] %s case %d 로드 (시작 %d초, 길이 %d초)",
                        room_id, room, int(row.caseid), start, self.rooms[room_id].length)
            # 이상 시점 10초 전으로 바로가기 (stable 케이스는 잡음 걸러내기 시연 지점)
            if pd.notna(row.event_onset_s):
                tick = int(row.event_onset_s) - start - 10
                if 0 <= tick < self.rooms[room_id].length:
                    self.jump_points.append({"tick": tick, "room": room,
                                             "label": f"{room.replace('-', '')} {row.type}",
                                             "note": str(row.note)})
        self.jump_points.sort(key=lambda j: j["tick"])
        # 가장 긴 방 기준. 먼저 끝난 방은 get_vitals가 None → main.py가 그 방만 건너뜀
        self._max_ticks = max(r.length for r in self.rooms.values())

    def get_vitals(self, room_id: int, tick: int) -> Optional[VitalSigns]:
        cr = self.rooms.get(room_id)
        if cr is None or not (0 <= tick < cr.length):
            return None

        # 수치: 재생기가 이미 '마지막 값 유지 + 10초 넘으면 None' 처리를 해둔 배열을 그대로 사용
        row = cr.num_hold[tick]
        num = {c: (None if np.isnan(row[j]) else float(row[j])) for j, c in enumerate(cr.num_cols)}

        # 뇌파: 1초(128개)는 주파수 해상도가 1Hz라 δ 대역이 3칸뿐 → 최근 EEG_WINDOW_S초를 넘김
        eeg = {}
        if "eeg1" in cr.eeg:
            fs = FS["eeg"]
            lo = max(0, (tick + 1 - EEG_WINDOW_S) * fs)
            eeg["eeg1"] = cr.eeg["eeg1"][lo:(tick + 1) * fs]

        # 변환은 조원이 만든 VitalSigns.from_replayer_frame에 맡김 (EEG % 계산, NaN 정화 포함)
        frame = SimpleNamespace(room=cr.room, caseid=cr.caseid, t=cr.offset + tick, elapsed=tick,
                                num=num, eeg=eeg, vent={})
        return VitalSigns.from_replayer_frame(room_id=room_id, frame=frame)
