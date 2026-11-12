"""재생기 검증 스크립트: 시연 16분을 빠르게 돌려 각 방의 사건이 예정 시각에 나오는지 확인.
   실행 (저장소 최상위에서): python -m replay.test_replay
   필요: cases/ 폴더(6_download_cases.py 결과), data/selected_cases.csv"""
import time, numpy as np, pandas as pd
from replay.replayer import Replayer

CSV, DATA = 'data/selected_cases.csv', 'cases'


def mmss(s):
    return '없음' if s is None or np.isnan(s) else f'{int(s)//60}분 {int(s)%60:02d}초'


def first_sustained(x, cond, sec):
    """cond가 sec초 연속 성립하는 첫 시점(시작 시점 반환)."""
    run = 0
    for i, v in enumerate(x):
        run = run + 1 if (v is not None and cond(v)) else 0
        if run == sec:
            return i - sec + 1
    return None


t0 = time.time()
rp = Replayer.from_csv(CSV, data_dir=DATA, duration=1000)
print(f'로딩 {time.time()-t0:.1f}초')

log = {r: {k: [] for k in ['bis', 'sqi', 'map']} for r in rp.rooms}
t0 = time.time()
for _ in range(960):
    for room, f in rp.tick().items():
        assert len(f.eeg['eeg1']) == 128 and len(f.wave['art']) == 500
        for k in log[room]:
            log[room][k].append(f.num.get(k))
print(f'960초 재생 {time.time()-t0:.2f}초 / 프레임 크기 정상 (뇌파 128, 파형 500)\n')

L = log
print('[시연 타임라인 — 재생 시작 기준]')
print('OR-1 동맥압 순간 튐      :', mmss(next((i for i, v in enumerate(L['OR-1']['map']) if v and v > 200), None)))
print('OR-3 MAP<65 1분 지속(노랑):', mmss(first_sustained(L['OR-3']['map'], lambda v: v < 65, 60)))
print('OR-3 MAP<55 첫 발생(빨강) :', mmss(next((i for i, v in enumerate(L['OR-3']['map']) if v and v < 55), None)))
print('OR-2 BIS<20 (SQI 동반)    :', mmss(next((i for i, v in enumerate(L['OR-2']['bis']) if v is not None and v < 20), None)))
print('OR-4 BIS>60 1분 지속(노랑):', mmss(first_sustained(L['OR-4']['bis'], lambda v: v > 60, 60)))
hypo = first_sustained(L['OR-3']['map'], lambda v: v < 65, 60)
rec = first_sustained(L['OR-3']['map'][hypo:], lambda v: v >= 65, 60)
print('OR-3 회복(MAP≥65 1분)     :', mmss(None if rec is None else hypo + rec))

ts = []
Replayer.from_csv(CSV, data_dir=DATA, duration=30).run(lambda fr: ts.append(time.monotonic()), speed=5, max_seconds=10)
print(f'\nspeed=5 실시간 재생 tick 간격: {np.mean(np.diff(ts)):.3f}초 (기대값 0.200)')
