"""6단계: data/selected_cases.csv의 케이스를 VitalDB에서 '케이스 전체' 길이로 받아
   재생기가 읽을 파일로 저장한다. (결정: 수치 트랙과 파형 트랙 분리 저장)

   실행 (저장소 최상위에서): python data/scripts/6_download_cases.py            → 목록의 모든 케이스
                             python data/scripts/6_download_cases.py 2421 3680  → 지정한 케이스만

   출력 (cases/ 폴더, 케이스마다 5개 파일):
     case_<id>_num.parquet   수치 1Hz   (BIS, SQI, 혈압, 심박, SpO2, 약물농도 등)
     case_<id>_eeg.parquet   뇌파 128Hz (EEG1, EEG2)
     case_<id>_wave.parquet  파형 500Hz (ECG_II, ART, PLETH)
     case_<id>_vent.parquet  호흡 파형 100Hz (CO2, 기도압)
     case_<id>_meta.json     수술 시작/종료, 이상 시점, 방 배정 등
   모든 파일의 t 열 = 케이스 기록 시작부터의 초. 재생기의 start 값과 같은 기준.
"""
import sys, json, os, time
import numpy as np, pandas as pd, vitaldb

OUT = 'cases'
NUM = {  # 1Hz 수치 트랙 (파일 열 이름: VitalDB 트랙 이름)
    'bis': 'BIS/BIS', 'sqi': 'BIS/SQI', 'emg': 'BIS/EMG', 'sef': 'BIS/SEF',
    'map': 'Solar8000/ART_MBP', 'sbp': 'Solar8000/ART_SBP', 'dbp': 'Solar8000/ART_DBP',
    'hr': 'Solar8000/HR', 'spo2': 'Solar8000/PLETH_SPO2', 'etco2': 'Solar8000/ETCO2',
    'ppf_ce': 'Orchestra/PPF20_CE', 'rftn_ce': 'Orchestra/RFTN20_CE',   # 확장(PK/PD)용, 없는 케이스는 빈 열
    'sevo_exp': 'Primus/EXP_SEVO',
    # 실제 수술방 모니터용 추가 항목 (2026-09-30)
    'sr': 'BIS/SR', 'bt': 'Solar8000/BT', 'rr': 'Primus/RR_CO2', 'fio2': 'Primus/FIO2',
    'tv': 'Primus/TV', 'mv': 'Primus/MV', 'peep': 'Primus/PEEP_MBAR', 'pip': 'Primus/PIP_MBAR', 'mac': 'Primus/MAC',
}
EEG = {'eeg1': 'BIS/EEG1_WAV', 'eeg2': 'BIS/EEG2_WAV'}                       # 128Hz
WAVE = {'ecg': 'SNUADC/ECG_II', 'art': 'SNUADC/ART', 'pleth': 'SNUADC/PLETH'}  # 500Hz
VENT = {'co2': 'Primus/CO2', 'awp': 'Primus/AWP'}   # 이산화탄소·기도압 파형, 100Hz로 저장


def fetch(caseid, tracks, fs):
    """트랙 묶음을 fs(Hz) 간격으로 받아 t(초) 열이 붙은 DataFrame으로 반환."""
    v = vitaldb.load_case(caseid, list(tracks.values()), 1 / fs)
    df = pd.DataFrame(v.astype(np.float32), columns=list(tracks.keys()))
    df.insert(0, 't', (np.arange(len(df)) / fs).astype(np.float64))
    return df


def save_case(row):
    cid = int(row.caseid)
    t0 = time.time()
    num = fetch(cid, NUM, 1)
    eeg = fetch(cid, EEG, 128)
    wave = fetch(cid, WAVE, 500)
    vent = fetch(cid, VENT, 100)
    for name, df in [('num', num), ('eeg', eeg), ('wave', wave), ('vent', vent)]:
        df.to_parquet(f'{OUT}/case_{cid}_{name}.parquet', index=False, compression='zstd')

    meta = {k: (None if pd.isna(v) else (int(v) if isinstance(v, (np.integer, float)) and float(v).is_integer() else v))
            for k, v in row._asdict().items() if k != 'Index'}
    meta.update({
        'duration_s': int(len(num)),
        'fs': {'num': 1, 'eeg': 128, 'wave': 500, 'vent': 100},
        'columns': {'num': NUM, 'eeg': EEG, 'wave': WAVE, 'vent': VENT},
        'empty_columns': [c for c in NUM if num[c].isna().all()],   # 이 케이스에 없는 트랙
        'source': 'VitalDB (https://vitaldb.net)',
    })
    with open(f'{OUT}/case_{cid}_meta.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    mb = sum(os.path.getsize(f'{OUT}/case_{cid}_{n}.parquet') for n in ['num', 'eeg', 'wave', 'vent']) / 1e6
    print(f'{row.room:8s} case {cid}: {len(num)/60:.0f}분, {mb:.0f} MB, {time.time()-t0:.0f}초 소요'
          f'{"  (빈 열: " + ", ".join(meta["empty_columns"]) + ")" if meta["empty_columns"] else ""}')


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    sel = pd.read_csv('data/selected_cases.csv')
    if len(sys.argv) > 1:
        sel = sel[sel.caseid.isin([int(x) for x in sys.argv[1:]])]
    for row in sel.itertuples():
        save_case(row)
