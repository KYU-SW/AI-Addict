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
MAP_OUTLIER_LOW: float = 20.0       # 생리학적 불가능 하한 (동맥관 플러싱/영점조절 잡음)
MAP_OUTLIER_HIGH: float = 200.0     # 생리학적 불가능 상한 (동맥관 플러싱 잡음, D-09)
MAP_HYPERTENSION_RATIO: float = 1.20  # 환자 기저치 대비 +20% 상승 시 고혈압 기준

# ── Heart Rate (회/분) ────────────────────────────────────────
HR_WARNING_LOW: float = 50.0
HR_CRITICAL_LOW: float = 40.0
HR_WARNING_HIGH: float = 100.0
HR_CRITICAL_HIGH: float = 120.0

# ── SpO₂ (%) ──────────────────────────────────────────────────
SPO2_WARNING: float = 90.0
SPO2_CRITICAL: float = 85.0

# ── BIS & EEG 신호 품질 ───────────────────────────────────────
BIS_TARGET_LOW: float = 40.0
BIS_TARGET_HIGH: float = 60.0
BIS_DEEP_WARNING: float = 30.0
SQI_THRESHOLD: float = 50.0              # 신호품질지수(SQI) 50% 미만 시 센서 불량 판정
SR_SUPPRESSION_THRESHOLD: float = 40.0   # 돌발억제비율(SR) 40% 이상 시 실제 뇌파 억제 의심
EEG_DELTA_ACTIVITY_MIN: float = 30.0     # δ파 비율(%) 30% 이상이면 뇌파 활동 존재로 판정
EEG_BETA_SURGE_DELTA: float = 10.0       # 기저치 대비 β파 비율 +10%p 이상 상승 시 각성파 동반 판정

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
DRUG_UNIT: float = 0.5             # 기본 0.5 단위 증감 (호환용)
PROPOFOL_STEP: float = 0.5         # 프로포폴 증감 단위 (0.3~0.5 mcg/mL)
REMIFENTANIL_STEP: float = 0.5     # 레미펜타닐 증감 단위 (0.5 ng/mL)
PROPOFOL_CE_MIN: float = 1.0       # 프로포폴 Ce 하한 (mcg/mL)
PROPOFOL_CE_MAX: float = 6.0       # 프로포폴 Ce 상한
REMIFENTANIL_CE_MIN: float = 2.0   # 레미펜타닐 Ce 하한 (ng/mL, 문헌 기준 각성 예방 하한 반영)
REMIFENTANIL_CE_MAX: float = 8.0   # 레미펜타닐 Ce 상한

# ── 우선순위 가중치 (계층 분리 + 분 단위 상한) ─────────────────
PRIORITY_BASE_RED: float = 100.0         # RED 기본 점수 (YELLOW가 절대 역전 불가)
PRIORITY_BASE_YELLOW: float = 20.0       # YELLOW 기본 점수
PRIORITY_W_SEVERITY: float = 3.0         # 하위 호환용 가중치
PRIORITY_W_DURATION: float = 2.0         # 분(minute)당 지속시간 가중치
PRIORITY_DURATION_MAX_BONUS: float = 15.0  # 지속시간 보너스 상한 (최대 15점)
PRIORITY_W_TREND: float = 5.0            # 추세 가중치 (악화 +5, 호전 -5)

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
