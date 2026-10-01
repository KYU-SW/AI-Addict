"""5단계 (실행: python data/scripts/5_check_waves.py): 선정 케이스의 파형 5종(뇌파 2 + 심전도·동맥압·맥파)이 실제로 채워져 있는지와 샘플링 주파수를 확인하고,
   이상 시점 부근 10초 스냅샷을 그린다."""
import vitaldb, numpy as np, pandas as pd, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
c = pd.read_csv('data/work/candidates.csv').set_index('caseid')
W = {'BIS/EEG1_WAV':128, 'BIS/EEG2_WAV':128, 'SNUADC/ECG_II':500, 'SNUADC/ART':500, 'SNUADC/PLETH':500}
# 각 케이스에서 볼 시점(수술시작+10분 기준 분)
PICK = {2421:60, 287:40, 2680:158, 6185:36, 3680:57}
fig, ax = plt.subplots(len(W), len(PICK), figsize=(20, 9))
for j,(cid,m) in enumerate(PICK.items()):
    t0 = int(c.loc[cid].opstart) + 600 + m*60
    for i,(tr,fs) in enumerate(W.items()):
        x = vitaldb.load_case(cid, [tr], 1/fs)[:,0]
        fill = np.mean(~np.isnan(x[int(c.loc[cid].opstart)*fs:int(c.loc[cid].opend)*fs]))
        seg = x[t0*fs:(t0+10)*fs]
        ax[i,j].plot(seg, lw=.5); ax[i,j].set_title(f'{cid} {tr.split("/")[1]} fill={fill:.0%}', fontsize=8)
        if j==0: print()
        print(cid, tr, fs,'Hz', f'수술중 채움률 {fill:.1%}')
plt.tight_layout(); plt.savefig('data/work/waves.png', dpi=60)
