"""1단계 (실행: 저장소 최상위에서 python data/scripts/1_build_candidates.py): VitalDB 전체 케이스 중 필수 트랙 10종을 모두 가진 전신마취 1.5~5시간 케이스를 candidates.csv로 저장한다."""
import os
import pandas as pd
c = pd.read_csv('https://api.vitaldb.net/cases', compression='gzip')
t = pd.read_csv('https://api.vitaldb.net/trks', compression='gzip')
NEED = ['BIS/BIS', 'BIS/SQI', 'BIS/EEG1_WAV', 'BIS/EEG2_WAV',            # 뇌파(BIS 모니터, 2채널뿐)
        'SNUADC/ART', 'SNUADC/ECG_II', 'SNUADC/PLETH',                  # 파형 500Hz
        'Solar8000/ART_MBP', 'Solar8000/HR', 'Solar8000/PLETH_SPO2']    # 수치
has = t[t.tname.isin(NEED)].groupby('caseid').tname.nunique()
c = c[c.caseid.isin(has[has == len(NEED)].index)].copy()
c['dur_min'] = (c.aneend - c.anestart) / 60
c = c[(c.ane_type == 'General') & c.dur_min.between(90, 300)]
os.makedirs('data/work', exist_ok=True)

c[['caseid', 'dur_min', 'age', 'sex', 'department', 'optype', 'anestart', 'aneend', 'opstart', 'opend']] \
    .to_csv('data/work/candidates.csv', index=False)
print('candidates:', len(c))   # 2026-09-29 실행 결과 2272
