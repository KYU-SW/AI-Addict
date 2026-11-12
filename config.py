"""
AI-Addict 마취 안전감시 플랫폼 설정.

임계값은 PPT 문헌 근거 기반:
- MAP 65/55 (주의/위험)
- BIS 40~60 (목표 범위)
- SpO₂ 90/85 (주의/위험)
"""

# ── VitalDB 케이스 ──────────────────────────────────────────
# 시연용 4개 방 (ASA 2~3, 일반 전신마취, 1.5~5시간)
# 실제 VitalDB 케이스 ID로 교체 가능
VITALDB_CASE_IDS: list[int] = [2, 19, 36, 81]
USE_VITALDB_REMOTE: bool = False    # True 설정 시 외부 VitalDB API 다운로드 시도 (오프라인 시연 시 False 권장)

# ── 재생 설정 ────────────────────────────────────────────────
TICK_INTERVAL: float = 1.0          # 초 단위 갱신 주기
DEFAULT_SPEED: int = 1              # 기본 재생 배속
DEMO_DURATION: int = 960            # 시연 길이 (초), 16분

# ── MAP (평균동맥압, mmHg) ────────────────────────────────────
MAP_WARNING_LOW: float = 65.0
MAP_CRITICAL_LOW: float = 55.0
MAP_WARNING_HIGH: float = 110.0
MAP_CRITICAL_HIGH: float = 130.0

# ── Heart Rate (회/분) ────────────────────────────────────────
HR_WARNING_LOW: float = 50.0
HR_CRITICAL_LOW: float = 40.0
HR_WARNING_HIGH: float = 100.0
HR_CRITICAL_HIGH: float = 120.0

# ── SpO₂ (%) ──────────────────────────────────────────────────
SPO2_WARNING: float = 90.0
SPO2_CRITICAL: float = 85.0

# ── BIS (Bispectral Index) ────────────────────────────────────
BIS_TARGET_LOW: float = 40.0
BIS_TARGET_HIGH: float = 60.0
BIS_DEEP_WARNING: float = 30.0

# ── EtCO₂ (mmHg) ──────────────────────────────────────────────
ETCO2_WARNING_LOW: float = 30.0
ETCO2_WARNING_HIGH: float = 45.0
ETCO2_CRITICAL_HIGH: float = 50.0

# ── 체온 (°C) ──────────────────────────────────────────────────
BT_WARNING_LOW: float = 35.5
BT_WARNING_HIGH: float = 38.0

# ── 경보 타이밍 ────────────────────────────────────────────────
ALERT_PERSISTENCE_SEC: int = 60     # 주의 상태 지속 → 경보 (초)
NOISE_SPIKE_RATIO: float = 0.3     # 30 % 급변 = 잡음 의심
NOISE_RECOVERY_SEC: int = 5        # 잡음 복귀 판정 시간 (초)

# ── 투약 제안 ──────────────────────────────────────────────────
DRUG_UNIT: float = 0.5             # 0.5 단위 증감
PROPOFOL_CE_MIN: float = 1.0      # 프로포폴 Ce 하한 (mcg/mL)
PROPOFOL_CE_MAX: float = 6.0      # 프로포폴 Ce 상한
REMIFENTANIL_CE_MIN: float = 0.5  # 레미펜타닐 Ce 하한 (ng/mL)
REMIFENTANIL_CE_MAX: float = 8.0  # 레미펜타닐 Ce 상한

# ── 우선순위 가중치 ──────────────────────────────────────────
PRIORITY_W_SEVERITY: float = 3.0   # 심각도 가중치
PRIORITY_W_DURATION: float = 1.0   # 지속 시간 가중치
PRIORITY_W_TREND: float = 2.0     # 추세 가중치

# ── VitalDB 트랙 이름 ─────────────────────────────────────────
VITAL_TRACKS: dict[str, str] = {
    "map":   "Solar8000/ART_MBP",
    "hr":    "Solar8000/HR",
    "spo2":  "Solar8000/PLETH_SPO2",
    "bis":   "BIS/BIS",
    "etco2": "Solar8000/ETCO2",
    "bt":    "Solar8000/BT",
    "sbp":   "Solar8000/ART_SBP",
    "dbp":   "Solar8000/ART_DBP",
    "rr":    "Primus/RR_CO2",
    "pip":   "Primus/PEAK_PRESSURE",
    "tv":    "Primus/TV",
    "mac":   "Primus/MAC",
}

WAVEFORM_TRACKS: dict[str, str] = {
    "ecg":    "SNUADC/ECG_II",
    "abp":    "SNUADC/ART",
    "pleth":  "SNUADC/PLETH",
    "co2":    "SNUADC/CO2",
    "airway": "SNUADC/AWP",
    "eeg":    "BIS/EEG1_WAV",
}
