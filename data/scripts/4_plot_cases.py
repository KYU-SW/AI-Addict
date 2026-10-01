"""4단계 (실행 예: python data/scripts/4_plot_cases.py 2421,3763,2680,3680 data/work/final.png): 후보 케이스들의 수술 구간 BIS / 평균동맥압 / 심박 / SpO2 추이를 한 장에 그려 눈으로 검증한다."""
import sys, vitaldb, pandas as pd, numpy as np, matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
c = pd.read_csv('data/work/candidates.csv').set_index('caseid')
ids = [int(x) for x in sys.argv[1].split(',')]
fig, ax = plt.subplots(len(ids), 1, figsize=(14, 2.3*len(ids)))
for a, cid in zip(ax, ids):
    r = c.loc[cid]; v = vitaldb.load_case(cid, ['BIS/BIS','Solar8000/ART_MBP','Solar8000/HR','Solar8000/PLETH_SPO2','BIS/SQI'], 1)
    s, e = int(r.opstart)+600, int(r.opend)-600; v = v[s:e]; t = np.arange(len(v))/60
    for i,(lab,col) in enumerate(zip(['BIS','MBP','HR','SpO2','SQI'],['purple','red','green','blue','gray'])):
        a.plot(t, pd.Series(v[:,i]).ffill(), col, lw=.8, label=lab)
    a.axhline(65, color='red', ls=':'); a.axhline(60, color='purple', ls=':'); a.set_ylim(0,130)
    a.set_title(f'case {cid}', loc='left', fontsize=9); a.legend(loc='upper right', fontsize=7, ncol=4)
ax[-1].set_xlabel('minutes from surgery start+10min'); plt.tight_layout(); plt.savefig(sys.argv[2], dpi=70)
