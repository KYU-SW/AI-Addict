"""가상 수술방 재생기 (DECISIONS D-07, D-08 기준)

사용법 (다음 주자용):
    from replay.replayer import Replayer
    rp = Replayer.from_csv('data/selected_cases.csv', data_dir='cases')   # OR-1~4, 시연 start 자동
    frames = rp.tick()          # 1초 전진, {'OR-1': Frame, 'OR-2': Frame, ...}
    f = frames['OR-3']
    f.num['map'], f.age['map']  # 평균동맥압, 그 값이 몇 초 전에 측정된 값인지
    f.eeg['eeg1']               # 이번 1초의 뇌파 128개 (numpy)
    f.wave['art']               # 이번 1초의 동맥압 파형 500개 (numpy)
    f.vent['co2']               # 이번 1초의 이산화탄소 파형 100개 (numpy)

    rp.run(on_tick, speed=1)    # 실시간 재생: 1초마다 on_tick(frames) 호출 (speed=10이면 10배속)

설계:
- tick() 한 번 = 가상 시간 1초. 배속(speed)은 run()이 tick 사이를 얼마나 쉬는지로만 조절
  → 테스트 때는 run 없이 tick()을 반복 호출하면 30분치를 몇 초 만에 돌릴 수 있음
- 수치(혈압·심박·SpO2)는 원래 약 2초마다 기록됨. 실제 모니터처럼 마지막 값을 유지하고
  age(마지막 측정 후 경과 초)를 함께 넘김. HOLD_S초 넘게 새 값이 없으면 None → '신호 끊김'
- 모든 시간은 케이스 기록 시작부터의 초(t). start도 같은 기준
"""
import json, time
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np, pandas as pd

HOLD_S = 10          # 이 시간(초)보다 오래된 값은 None 처리
FS = {'num': 1, 'eeg': 128, 'wave': 500, 'vent': 100}


@dataclass
class Frame:
    """한 수술방의 1초치 데이터. 에이전트 간 메시지로 그대로 주고받을 수 있음."""
    room: str
    caseid: int
    t: int                      # 케이스 기준 초
    elapsed: int                # 재생 시작 후 경과 초 (시연 타이머)
    num: dict                   # 수치 {'bis': 45.0, 'map': None, ...}
    age: dict                   # 수치별 마지막 측정 후 경과 초 (값 없으면 None)
    eeg: dict = field(repr=False, default_factory=dict)   # {'eeg1': np.ndarray(128), 'eeg2': ...}
    wave: dict = field(repr=False, default_factory=dict)  # {'ecg': np.ndarray(500), 'art': ..., 'pleth': ...}
    vent: dict = field(repr=False, default_factory=dict)  # {'co2': np.ndarray(100), 'awp': ...} 이산화탄소·기도압 파형
    finished: bool = False      # 케이스 데이터 끝

    def summary(self):
        """로그용 한 줄 요약."""
        f = lambda k: '-' if self.num.get(k) is None else f'{self.num[k]:.0f}'
        return (f'{self.room} t={self.t} BIS={f("bis")} SQI={f("sqi")} MAP={f("map")} '
                f'HR={f("hr")} SpO2={f("spo2")}')


class CaseReplayer:
    """케이스 하나를 1초 단위로 재생."""

    def __init__(self, caseid, start=0, room=None, data_dir='cases', duration=None):
        """duration(초)을 주면 start부터 그만큼만 메모리에 올림 (노트북 메모리 절약용)."""
        d = Path(data_dir)
        self.caseid, self.room = int(caseid), room or f'case{caseid}'
        self.meta = json.loads((d / f'case_{caseid}_meta.json').read_text(encoding='utf-8'))
        self.offset = int(start)
        stop = None if duration is None else self.offset + int(duration)

        num = pd.read_parquet(d / f'case_{caseid}_num.parquet')
        self.num_cols = [c for c in num.columns if c != 't']
        num = num.iloc[self.offset:stop]
        vals = num[self.num_cols].to_numpy(np.float32)
        self.num_hold, self.num_age = self._hold(vals)

        self.eeg = self._load_wave(d / f'case_{caseid}_eeg.parquet', FS['eeg'], stop)
        self.wave = self._load_wave(d / f'case_{caseid}_wave.parquet', FS['wave'], stop)
        vp = d / f'case_{caseid}_vent.parquet'   # 없는 옛 데이터도 동작하도록 선택 사항
        self.vent = self._load_wave(vp, FS['vent'], stop) if vp.exists() else {}
        self.length = len(vals)
        self.pos = 0

    @staticmethod
    def _hold(vals):
        """빈칸을 마지막 값으로 채우고, 마지막 측정 후 경과 초를 계산. HOLD_S 초과는 NaN."""
        n, k = vals.shape
        held = np.full_like(vals, np.nan)
        age = np.full((n, k), np.nan, dtype=np.float32)
        last = np.full(k, np.nan, dtype=np.float32)
        last_i = np.full(k, -10**9)
        for i in range(n):
            ok = ~np.isnan(vals[i])
            last[ok], last_i[ok] = vals[i, ok], i
            a = i - last_i
            fresh = a <= HOLD_S
            held[i, fresh] = last[fresh]
            age[i, fresh] = a[fresh]
        return held, age

    def _load_wave(self, path, fs, stop):
        df = pd.read_parquet(path)
        cols = [c for c in df.columns if c != 't']
        a = df[cols].to_numpy(np.float32)[self.offset * fs: None if stop is None else stop * fs]
        return {c: a[:, j] for j, c in enumerate(cols)}

    def tick(self):
        i = self.pos
        if i >= self.length:
            return Frame(self.room, self.caseid, self.offset + i, i, {}, {}, finished=True)
        conv = lambda x: None if np.isnan(x) else float(x)
        num = {c: conv(self.num_hold[i, j]) for j, c in enumerate(self.num_cols)}
        age = {c: (None if np.isnan(self.num_age[i, j]) else int(self.num_age[i, j]))
               for j, c in enumerate(self.num_cols)}
        eeg = {c: v[i * FS['eeg']:(i + 1) * FS['eeg']] for c, v in self.eeg.items()}
        wave = {c: v[i * FS['wave']:(i + 1) * FS['wave']] for c, v in self.wave.items()}
        vent = {c: v[i * FS['vent']:(i + 1) * FS['vent']] for c, v in self.vent.items()}
        self.pos += 1
        return Frame(self.room, self.caseid, self.offset + i, i, num, age, eeg, wave, vent)

    def seek(self, elapsed):
        """재생 시작 후 elapsed초 지점으로 이동 (되감기/건너뛰기)."""
        self.pos = max(0, min(int(elapsed), self.length))


class Replayer:
    """여러 수술방을 같은 시계로 동시에 재생."""

    def __init__(self, rooms):
        """rooms: [CaseReplayer, ...]"""
        self.rooms = {r.room: r for r in rooms}
        self.elapsed = 0

    @classmethod
    def from_csv(cls, csv_path, data_dir='cases', rooms=('OR-1', 'OR-2', 'OR-3', 'OR-4'),
                 duration=None, starts=None):
        """selected_cases.csv에서 방 배정과 demo_start_s를 읽어 생성.
        starts={'OR-3': 13000}처럼 주면 해당 방만 시작 지점 변경."""
        sel = pd.read_csv(csv_path)
        sel = sel[sel.room.isin(rooms)].set_index('room').loc[list(rooms)]
        starts = starts or {}
        return cls([CaseReplayer(int(r.caseid), starts.get(room, r.demo_start_s), room, data_dir, duration)
                    for room, r in sel.iterrows()])

    def tick(self):
        self.elapsed += 1
        return {name: r.tick() for name, r in self.rooms.items()}

    @property
    def finished(self):
        return all(r.pos >= r.length for r in self.rooms.values())

    def seek(self, elapsed):
        self.elapsed = int(elapsed)
        for r in self.rooms.values():
            r.seek(elapsed)

    def run(self, on_tick, speed=1.0, max_seconds=None):
        """실시간 재생. speed=1이면 1초마다, speed=10이면 0.1초마다 on_tick(frames) 호출.
        on_tick이 False를 돌려주면 멈춤."""
        next_t = time.monotonic()
        while not self.finished and (max_seconds is None or self.elapsed < max_seconds):
            if on_tick(self.tick()) is False:
                break
            next_t += 1.0 / speed
            time.sleep(max(0.0, next_t - time.monotonic()))
