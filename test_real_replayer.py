"""실제 케이스 재생기 연동 확인 (가짜 cases 데이터로, 인터넷·cases 폴더 없이 실행).

실행 (저장소 최상위에서): python test_real_replayer.py
확인 항목: 4개 방 로드, 바로가기 시점, VitalSigns 변환(뇌파 %·CT), 2초 간격 수치 유지, 범위 밖 None
"""
import asyncio
import json
import tempfile

import numpy as np
import pandas as pd

import config
from data.real_replayer import RealCaseReplayer

d = tempfile.mkdtemp(prefix="fake_cases_")

# selected_cases.csv의 케이스 번호·시작점 그대로, 시연 시작 후 900초 분량 가짜 데이터
sel = pd.read_csv("data/selected_cases.csv")
for r in sel[sel.room.str.startswith("OR")].itertuples():
    n = int(r.demo_start_s) + 900
    t = np.arange(n)
    num = pd.DataFrame({
        "t": t.astype(float), "bis": 45.0, "sqi": 90.0, "sr": 0.0,
        "map": np.where(t % 2 == 0, 80.0, np.nan),        # 실제처럼 2초마다 기록
        "hr": 70.0, "spo2": 99.0, "etco2": 35.0, "bt": 36.5,
        "ppf_ce": 3.0, "rftn_ce": 3.5, "ppf_ct": 3.2, "rftn_ct": 1.5,
    }).astype("float32")
    num.to_parquet(f"{d}/case_{r.caseid}_num.parquet")
    te = np.arange(n * 128) / 128
    pd.DataFrame({"t": te, "eeg1": np.sin(2 * np.pi * 2 * te), "eeg2": 0.0}).astype("float32") \
        .to_parquet(f"{d}/case_{r.caseid}_eeg.parquet")
    pd.DataFrame({"t": np.arange(n * 500) / 500, "ecg": 0.0, "art": 0.0, "pleth": 0.0}).astype("float32") \
        .to_parquet(f"{d}/case_{r.caseid}_wave.parquet")
    with open(f"{d}/case_{r.caseid}_meta.json", "w") as f:
        json.dump({"caseid": int(r.caseid)}, f)

config.CASES_DIR = d
rp = RealCaseReplayer(case_ids=[1])
asyncio.run(rp.load())

print("max_ticks", rp.max_ticks)
print("jumps", [(j["label"], j["tick"]) for j in rp.jump_points])
v = rp.get_vitals(3, 100)
print(v.model_dump())

assert rp.max_ticks == 900, rp.max_ticks
assert [j["tick"] for j in rp.jump_points] == [170, 290, 410, 530], rp.jump_points
assert v.eeg_delta is not None and v.eeg_delta > 70, v.eeg_delta   # 2Hz 사인파 → δ가 대부분
assert abs(v.ppf_ct - 3.2) < 1e-4 and v.rftn_ct == 1.5 and v.sqi == 90.0
assert rp.get_vitals(1, 1).map == 80.0      # 빈칸(홀수 초) → 직전 값 유지
assert rp.get_vitals(1, 99999) is None      # 케이스 끝 이후
print("=== 연동 확인 통과 ===")
