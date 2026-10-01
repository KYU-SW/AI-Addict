"""3단계 (실행: 저장소 최상위에서 python data/scripts/3_pick_shortlist.py): 2b 결과(data/work/events.csv)에서 유형별 후보를 뽑아 events_shortlist.csv로 저장한다.
   이 후보들을 4_plot_cases.py 그래프로 눈으로 확인해 최종 4개(selected_cases.csv)를 정했다."""
import pandas as pd
d = pd.read_csv('data/work/events.csv')
ok = (d.spo2_lt94 < 0.005) & (d.art_bad < 0.01)
groups = {
    'hypotension': d[ok & d.hypo_at.notna() & (d.bis_len == 0)],
    'bis_excursion': d[ok & d.bis_at.notna() & (d.hypo_len == 0) & (d.mbp_lt65 < 0.03)],
    'sensor_noise': d[ok & d.noise_at.notna() & (d.hypo_len == 0) & (d.bis_len == 0) & (d.bis_in > 0.7)],
    'stable': d[d.hypo_at.isna() & d.bis_at.isna() & d.noise_at.isna() & (d.bis_in > 0.92) & (d.mbp_lt65 < 0.005)
                & (d.art_bad < 0.003) & (d.sqi_lt50 < 0.01) & (d.spo2_lt94 == 0) & d.dur_min.between(90, 200)],
}
out = pd.concat([g.assign(category=k) for k, g in groups.items()])
out.to_csv('data/events_shortlist.csv', index=False); print(out.category.value_counts())
