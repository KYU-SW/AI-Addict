"""2b단계 (2차 스크리닝, 채택): '직전 15분은 정상 → 이후 이탈이 일정 시간 지속'인 사건을 찾는다.
   실행 (저장소 최상위에서): python data/scripts/2b_screen_events.py 1000  → data/work/events.csv
   저혈압: 직전 MBP 중앙값 ≥80 → MBP<60이 5분 이상
   BIS 이탈: 직전 BIS 중앙값 ≤55 → BIS≥65가 3분 이상 (수술 종료 20분 전 이후는 각성기라 제외)
   잡음: SQI<50이 2분 이상 이어지는 구간 (뇌파 전극 불량/전기소작 간섭)"""
import numpy as np, pandas as pd, vitaldb, sys
from concurrent.futures import ThreadPoolExecutor

TRK = ['BIS/BIS', 'BIS/SQI', 'Solar8000/ART_MBP', 'Solar8000/HR', 'Solar8000/PLETH_SPO2']

def runs(mask, min_len, gap=30):
    """조건 만족 구간을 (시작, 길이) 리스트로 반환."""
    idx = np.flatnonzero(mask); out = []
    if len(idx) == 0: return out
    s = p = idx[0]
    for i in list(idx[1:]) + [None]:
        if i is None or i - p > gap:
            if p - s + 1 >= min_len: out.append((s, p - s + 1))
            if i is not None: s = i
        if i is not None: p = i
    return out

def first_event(sig, bad, base_ok, min_len, end_cut):
    for s, L in runs(bad, min_len):
        if s < 900 or s > len(sig) - end_cut: continue
        pre = sig[s - 900:s]
        if base_ok(np.nanmedian(pre)):
            return s, L
    return None, 0

def screen(row):
    try:
        v = vitaldb.load_case(int(row.caseid), TRK, 1)
        a, b = int(row.opstart) + 600, int(row.opend) - 600
        if b - a < 3600: return None
        v = v[a:b]
        bis, sqi, mbp, hr, spo2 = [pd.Series(v[:, i]).ffill().values for i in range(5)]
        art_bad = (mbp < 20) | (mbp > 200) | np.isnan(mbp)
        m = np.where(art_bad, np.nan, mbp)
        h_at, h_len = first_event(m, m < 60, lambda x: x >= 80, 300, 900)
        b_at, b_len = first_event(bis, bis >= 65, lambda x: x <= 55, 180, 1200)
        n = runs((sqi < 50), 120)
        n = [x for x in n if 900 < x[0] < len(sqi) - 900]
        return dict(caseid=int(row.caseid), dur_min=round((b - a) / 60),
                    hypo_at=None if h_at is None else round(h_at / 60), hypo_len=h_len,
                    hypo_min=None if h_at is None else round(np.nanmin(m[h_at:h_at + h_len])),
                    bis_at=None if b_at is None else round(b_at / 60), bis_len=b_len,
                    bis_max=None if b_at is None else round(np.nanmax(bis[b_at:b_at + b_len])),
                    noise_at=round(n[0][0] / 60) if n else None, noise_len=n[0][1] if n else 0,
                    bis_in=round(np.nanmean((bis >= 40) & (bis <= 60)), 3),
                    mbp_lt65=round(np.nanmean(m < 65), 3), spo2_lt94=round(np.nanmean(spo2 < 94), 3),
                    art_bad=round(np.mean(art_bad), 3), sqi_lt50=round(np.nanmean(sqi < 50), 3))
    except Exception:
        return None

if __name__ == '__main__':
    c = pd.read_csv('data/work/candidates.csv').sample(int(sys.argv[1]), random_state=7)
    with ThreadPoolExecutor(12) as ex:
        res = [r for r in ex.map(screen, c.itertuples()) if r]
    pd.DataFrame(res).to_csv('data/work/events.csv', index=False); print('screened', len(res))
