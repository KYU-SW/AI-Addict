"""7단계: cases/의 수술 전체 데이터에서 '시연 구간'만 잘라 demo/ 폴더에 작은 파일로 저장한다.

   왜 필요한가:
     - 수술 전체(3~4.5시간) 파형을 통째로 읽으면 메모리가 수백 MB → Render 무료 서버(512MB)에서 터짐
     - cases/(약 130~195MB)는 저장소에 못 올림 → 배포 서버에는 실제 데이터가 없었음
     → 시연에 쓰는 구간(selected_cases.csv의 demo_start_s부터 config.DEMO_DURATION초)만 잘라서
       numpy 압축 파일(.npz)로 저장. 4개 방 합쳐 수십 MB 이하라 저장소에 올리고 서버가 pandas 없이 읽음

   구간 기준 (DECISIONS D-07): demo_start_s는 각 방의 이상 시점 몇 분 전으로 잡혀 있어서
   16분 안에 '잡음 걸러냄 → OR-3 노랑/빨강 → OR-2 센서 문제 억제 → OR-4 BIS 이탈 → 1위 교체'가 모두 들어감.
   이 스크립트는 저장 전에 그 사건들이 실제로 구간 안에 있는지 확인해서 출력한다.

   실행 (저장소 최상위에서, cases/가 있는 PC에서 한 번만):
       python data/scripts/7_make_demo_clips.py
   출력: demo/case_<id>_demo.npz  (방마다 1개)
"""
import csv
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.getcwd())
import config  # noqa: E402

SRC = getattr(config, "CASES_DIR", "cases")
OUT = getattr(config, "DEMO_CLIPS_DIR", "demo")
DUR = int(getattr(config, "DEMO_DURATION", 960))
PRE = 10   # 앞쪽 여유(초): 2초 간격 수치의 '마지막 값 유지'가 시연 0초부터 채워지도록
FS = {"num": 1, "eeg": 128, "wave": 500, "vent": 100}


def mmss(s):
    return f"{int(s) // 60}:{int(s) % 60:02d}"


def runs(mask, min_len):
    """연속으로 True인 구간 [(시작, 길이)] (min_len초 이상만)."""
    out, st = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and st is None:
            st = i
        if not v and st is not None:
            if i - st >= min_len:
                out.append((st, i - st))
            st = None
    return out


def check(room, num):
    """구간 안에 시연할 사건이 들어 있는지 간단히 출력 (판정은 에이전트가 함, 여기선 확인용)."""
    m, b, q = num["map"].ffill(), num["bis"], num["sqi"]
    found = {
        "MAP<65 1분 이상": runs(m < 65, 60),
        "MAP<55": runs(m < 55, 1),
        "동맥압 잡음(20 미만/200 초과)": runs((num["map"] < 20) | (num["map"] > 200), 1),
        "BIS 60 초과 1분 이상(SQI 정상)": runs((b > 60) & (q >= 50), 60),
        "BIS 0 또는 SQI<50 (센서)": runs((b < 1) | (q < 50), 5),
    }
    items = [f"{k} @{mmss(a)}({n}초)" for k, v in found.items() for a, n in v[:2]]
    print(f"  {room} 구간 안 사건: " + (", ".join(items) if items else "없음 ← 시작점 확인 필요"))
    return bool(items)


def wave_slice(path, fs, a, b):
    if not os.path.exists(path):
        return {}
    df = pd.read_parquet(path)
    cols = [c for c in df.columns if c != "t"]
    arr = df[cols].to_numpy(np.float32)[a * fs:b * fs]
    return {c: np.ascontiguousarray(arr[:, j]) for j, c in enumerate(cols)}


def main():
    os.makedirs(OUT, exist_ok=True)
    with open(getattr(config, "SELECTED_CASES_CSV", "data/selected_cases.csv"), encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["room"].startswith("OR-")]
    total = 0
    for r in rows:
        cid, start = int(r["caseid"]), int(float(r["demo_start_s"]))
        a, b = max(0, start - PRE), start + DUR
        pre = start - a
        num = pd.read_parquet(f"{SRC}/case_{cid}_num.parquet")
        num_cols = [c for c in num.columns if c != "t"]
        clip_num = num.iloc[a:b]
        check(r["room"], clip_num.iloc[pre:].reset_index(drop=True))

        arrays = {"num": clip_num[num_cols].to_numpy(np.float32)}
        for kind in ("eeg", "wave", "vent"):
            for c, v in wave_slice(f"{SRC}/case_{cid}_{kind}.parquet", FS[kind], a, b).items():
                arrays[f"{kind}__{c}"] = v

        meta_path = f"{SRC}/case_{cid}_meta.json"
        meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}
        meta.update({"clip_start_s": start, "clip_pre_s": pre, "clip_duration_s": DUR, "num_cols": num_cols})
        out = f"{OUT}/case_{cid}_demo.npz"
        np.savez_compressed(out, meta=np.array(json.dumps(meta, ensure_ascii=False)), **arrays)
        size = os.path.getsize(out)
        total += size
        print(f"  → {out}  {size / 1e6:.1f} MB  (케이스 {mmss(start)}~{mmss(b)} 구간, {DUR}초)")
    print(f"합계 {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
