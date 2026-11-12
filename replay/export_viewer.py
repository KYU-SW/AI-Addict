"""재생기 확인 화면 생성기: Replayer가 실제로 내보내는 프레임을 그대로 받아
   설치 없이 브라우저로 여는 단일 HTML 파일(viewer.html)을 만든다.

   실행 (저장소 최상위): python -m replay.export_viewer            → replay/viewer.html
                         python -m replay.export_viewer --sec 600  → 앞 10분만

   용량을 줄이려고 화면 표시용으로만 파형을 솎아냄 (ECG·ART·PLETH 500→100Hz, CO2·기도압 100→50Hz),
   16비트 정수로 압축. 원본 데이터(cases/)는 건드리지 않음.
   뇌파 주파수 성분(델타 1~4Hz, 알파 8~12Hz, 베타 13~30Hz)은 EEG1 원파형에서 여기서 계산:
     - 성분 파형: 4차 버터워스 대역 통과 필터(앞뒤 양방향, 위상 지연 없음)
     - 성분 비율: 매초 직전 4초 구간의 주파수 분석(Welch)으로 1~30Hz 전체 대비 %
"""
import argparse, base64, json
from pathlib import Path
import numpy as np
from scipy.signal import butter, sosfiltfilt, welch
from replay.replayer import Replayer

BANDS = {'delta': (1, 4), 'alpha': (8, 12), 'beta': (13, 30)}
BAND_FS = {'delta': 32, 'alpha': 64, 'beta': 64}   # 화면 표시용 샘플링


def band_waves(eeg, fs=128):
    x = np.nan_to_num(eeg.astype(np.float64))
    out = {}
    for k, (lo, hi) in BANDS.items():
        y = sosfiltfilt(butter(4, [lo, hi], btype='band', fs=fs, output='sos'), x)
        y[np.isnan(eeg)] = np.nan
        out['band_' + k] = y[::fs // BAND_FS[k]]
    return out


def band_ratio(eeg, fs=128, win=4):
    n = len(eeg) // fs
    r = {k: np.full(n, np.nan) for k in BANDS}
    for i in range(n):
        seg = eeg[max(0, (i + 1 - win) * fs):(i + 1) * fs]
        if np.isnan(seg).mean() > 0.2 or len(seg) < fs * 2:
            continue
        f, p = welch(np.nan_to_num(seg), fs=fs, nperseg=min(256, len(seg)))
        tot = p[(f >= 1) & (f < 30)].sum()
        if tot <= 0:
            continue
        for k, (lo, hi) in BANDS.items():
            r[k][i] = 100 * p[(f >= lo) & (f < hi)].sum() / tot
    return r

NUM_KEYS = ['bis', 'sqi', 'emg', 'sr', 'sef', 'map', 'sbp', 'dbp', 'hr', 'spo2', 'etco2', 'rr', 'bt',
            'fio2', 'tv', 'mv', 'peep', 'pip', 'mac', 'sevo_exp', 'ppf_ce', 'rftn_ce']
WAVES = {'eeg1': ('eeg', 1), 'ecg': ('wave', 5), 'art': ('wave', 5), 'pleth': ('wave', 5), 'co2': ('vent', 2), 'awp': ('vent', 2)}
WAVE_FS = {'eeg1': 128, 'ecg': 100, 'art': 100, 'pleth': 100, 'co2': 50, 'awp': 50,
           'band_delta': BAND_FS['delta'], 'band_alpha': BAND_FS['alpha'], 'band_beta': BAND_FS['beta']}
NAN16 = -32768


def pack(x):
    """float 배열 → (base64 int16, offset, scale). NaN은 -32768."""
    x = np.asarray(x, np.float32)
    ok = ~np.isnan(x)
    lo, hi = (float(np.nanmin(x)), float(np.nanmax(x))) if ok.any() else (0.0, 1.0)
    scale = max(hi - lo, 1e-6) / 65000.0
    q = np.full(x.shape, NAN16, np.int16)
    q[ok] = np.clip(np.round((x[ok] - lo) / scale) - 32500, -32500, 32500).astype(np.int16)
    return {'b64': base64.b64encode(q.tobytes()).decode(), 'off': lo, 'scale': scale}


def main(sec, out):
    rp = Replayer.from_csv('data/selected_cases.csv', data_dir='cases', duration=sec + 5)
    rooms = list(rp.rooms)
    num = {r: {k: [] for k in NUM_KEYS} for r in rooms}
    wav = {r: {k: [] for k in WAVES} for r in rooms}
    meta = {r: {'caseid': rp.rooms[r].caseid, 'start': rp.rooms[r].offset,
                'type': rp.rooms[r].meta.get('type'), 'note': rp.rooms[r].meta.get('note'),
                'optype': rp.rooms[r].meta.get('optype'), 'age': rp.rooms[r].meta.get('age'),
                'sex': rp.rooms[r].meta.get('sex')} for r in rooms}
    for _ in range(sec):
        for r, f in rp.tick().items():
            for k in NUM_KEYS:
                v = f.num.get(k)
                num[r][k].append(np.nan if v is None else v)
            for k, (src, step) in WAVES.items():
                wav[r][k].append(getattr(f, src)[k][::step] if k in getattr(f, src) else np.full(128 // step if src == 'eeg' else (500 if src == 'wave' else 100) // step, np.nan))
    wave_out, num_out = {}, {}
    for r in rooms:
        w = {k: np.concatenate(wav[r][k]) for k in WAVES}
        eeg = w['eeg1']
        w.update(band_waves(eeg))
        wave_out[r] = {k: pack(v) for k, v in w.items()}
        ratio = band_ratio(eeg)
        num_out[r] = {k: pack(num[r][k]) for k in NUM_KEYS}
        num_out[r].update({'ratio_' + k: pack(v) for k, v in ratio.items()})
    data = {'sec': sec, 'rooms': rooms, 'meta': meta, 'wave_fs': WAVE_FS, 'num': num_out, 'wave': wave_out}
    tpl = (Path(__file__).parent / 'viewer_template.html').read_text(encoding='utf-8')
    html = tpl.replace('/*__DATA__*/null', json.dumps(data, separators=(',', ':')))
    Path(out).write_text(html, encoding='utf-8')
    print(f'{out} 생성: {len(html)/1e6:.1f} MB, {sec}초, 방 {len(rooms)}개')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--sec', type=int, default=960)
    ap.add_argument('--out', default='replay/viewer.html')
    a = ap.parse_args()
    main(a.sec, a.out)
