"""2a단계 (1차 스크리닝, 폐기된 방식): 후보 케이스의 수술 중 전체 비율로 지표를 계산한다.
   원래 혈압이 낮은 환자까지 저혈압으로 잡혀서 2b로 개선함. 안정 케이스 2421은 여기서 채택.
   실행 (저장소 최상위에서): python data/scripts/2a_screen_cases.py 400  → data/work/screen_result.csv"""
import numpy as np, pandas as pd, vitaldb, sys
from concurrent.futures import ThreadPoolExecutor

TRK = ['BIS/BIS', 'BIS/SQI', 'Solar8000/ART_MBP', 'Solar8000/HR', 'Solar8000/PLETH_SPO2']

def longest_run(mask, gap=30):
    """True가 이어지는 가장 긴 구간(초)과 시작 인덱스. gap초 이하 끊김은 이어진 것으로 봄."""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return 0, None
    best, s, prev = (0, None), idx[0], idx[0]
    for i in list(idx[1:]) + [None]:
        if i is None or i - prev > gap:
            if prev - s + 1 > best[0]:
                best = (prev - s + 1, s)
            if i is not None:
                s = i
        if i is not None:
            prev = i
    return best

def screen(row):
    try:
        v = vitaldb.load_case(int(row.caseid), TRK, 1)
        # 수술 구간만 사용 (시작/끝 10분은 유도·각성이라 제외)
        a, b = int(row.opstart) + 600, int(row.opend) - 600
        if b - a < 3600:
            return None
        v = v[a:b]
        bis, sqi, mbp, hr, spo2 = [pd.Series(v[:, i]).ffill().values for i in range(5)]
        art_bad = (mbp < 20) | (mbp > 200) | np.isnan(mbp)          # 동맥압 라인 잡음/공백
        mbp_ok = np.where(art_bad, np.nan, mbp)
        hypo_len, hypo_at = longest_run(mbp_ok < 65)
        bis_len, bis_at = longest_run(bis > 60)
        noise_len, noise_at = longest_run((sqi < 50) | art_bad)
        return dict(caseid=int(row.caseid), dur_min=round((b - a) / 60),
                    hypo_run_s=hypo_len, hypo_at_min=None if hypo_at is None else round(hypo_at / 60),
                    bis_hi_run_s=bis_len, bis_at_min=None if bis_at is None else round(bis_at / 60),
                    noise_run_s=noise_len, noise_at_min=None if noise_at is None else round(noise_at / 60),
                    bis_in_40_60=round(np.nanmean((bis >= 40) & (bis <= 60)), 3),
                    mbp_lt65=round(np.nanmean(mbp_ok < 65), 3),
                    spo2_lt94=round(np.nanmean(spo2 < 94), 3),
                    hr_out=round(np.nanmean((hr < 45) | (hr > 110)), 3),
                    art_bad=round(np.mean(art_bad), 3), sqi_lt50=round(np.nanmean(sqi < 50), 3))
    except Exception as e:
        return None

if __name__ == '__main__':
    n = int(sys.argv[1])
    c = pd.read_csv('data/work/candidates.csv').sample(n, random_state=42)
    with ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(screen, c.itertuples()) if r]
    pd.DataFrame(res).to_csv('data/work/screen_result.csv', index=False)
    print('screened', len(res))
