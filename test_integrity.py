"""에이전트 버그 및 무결성 정밀 검증 스크립트 (test_integrity.py).

검증 항목:
1. 우선순위 역전 방지 (10분 지속 YELLOW vs 방금 발생한 RED)
2. 잡음 필터: 단발성 아티팩트(MAP>200, 3초 스파이크) 차단 vs 실제 급락(MAP 90->54) 6초 만에 즉시 경보
3. BIS=0 및 SQI/SR/EEG 3단계 판정 (침묵 방지 '센서 확인 필요' 알림 vs 실제 뇌파 억제 RED, β파 상승 감지)
4. 투약 제안 에이전트: 초기 CT/CE 농도 동기화, MAP<65 통일 및 복합 처치 문구, 하한 2.0 준수, SQI<50 보류, RED 시 취소/승인차단
5. 1번 주자 Frame 어댑터(from_replayer_frame) 및 NaN/Inf 방어 무결성
"""

from __future__ import annotations

import math
import sys
from types import SimpleNamespace

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import config
from agents.medication import MedicationAgent
from agents.recorder import RecorderAgent
from agents.room_monitor import RoomMonitorAgent
from agents.supervisor import SupervisorAgent
from core.enums import AlertLevel, DrugType, EventType, SuggestionAction
from core.event_bus import EventBus
from core.models import VitalSigns


def drive(coro):
    """Windows 샌드박스에서 소켓 생성 없이 코루틴을 동기 실행합니다."""
    try:
        while True:
            coro.send(None)
    except StopIteration as e:
        return e.value


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        raise AssertionError(msg)


async def test_1_priority_non_inversion() -> None:
    print("\n[검증 1] 우선순위 점수 공식 역전 방지 (장기 YELLOW vs 신규 RED)")
    bus = EventBus()
    rm1 = RoomMonitorAgent(1, bus)
    rm2 = RoomMonitorAgent(2, bus)
    rm3 = RoomMonitorAgent(3, bus)
    sup = SupervisorAgent(bus, room_ids=[1, 2, 3, 4])
    await rm1.start()
    await rm2.start()
    await rm3.start()
    await sup.start()

    # Room 1: YELLOW 상태(MAP=60)를 600초(10분) 동안 지속시킴
    for t in range(0, 600):
        await rm1.process(VitalSigns(room_id=1, timestamp=float(t), map=60.0))
        await sup.process(float(t))

    # 600초 시점에 Room 2에서 RED(SpO2=82%) 방금 발생, Room 3에서는 센서 이상(BIS=0, SQI=0) 발생
    await rm1.process(VitalSigns(room_id=1, timestamp=600.0, map=60.0))
    await rm2.process(VitalSigns(room_id=2, timestamp=600.0, spo2=82.0))
    await rm3.process(VitalSigns(room_id=3, timestamp=600.0, bis=0.0, sqi=0.0))
    await sup.process(600.0)

    s1 = sup.get_room_status(1)
    s2 = sup.get_room_status(2)
    s3 = sup.get_room_status(3)
    s4 = sup.get_room_status(4)

    last_prio_evt = [e for e in bus._history if e.type == EventType.PRIORITY_UPDATE][-1]
    ranking = last_prio_evt.data["ranking"]

    check(s1.alert_level == AlertLevel.YELLOW, f"Room 1은 10분 지속 YELLOW (점수: {s1.priority_score})")
    check(s2.alert_level == AlertLevel.RED, f"Room 2는 방금 발생한 RED (점수: {s2.priority_score})")
    check(s2.priority_score > s1.priority_score, f"신규 RED({s2.priority_score}) > 10분 YELLOW({s1.priority_score}) 점수 우위 보장")
    check(ranking[0] == 2 and ranking[1] == 1, f"랭킹 순서 RED(2번방) -> YELLOW(1번방) 보장: {ranking}")
    check(ranking[2] == 3 and ranking[3] == 4, f"센서 확인 필요(3번방, 점수 {s3.priority_score}) > 일반 GREEN(4번방, 점수 {s4.priority_score}): {ranking}")


async def test_2_noise_filter_and_rapid_drop() -> None:
    print("\n[검증 2] 잡음 필터: 아티팩트/단발 튐 무시 vs 실제 급락(MAP 90 -> 54) 즉시 수용")
    bus = EventBus()
    rm = RoomMonitorAgent(3, bus)
    await rm.start()

    # 0~9초: 정상 MAP 90 유지 (기준선 형성)
    for t in range(10):
        await rm.process(VitalSigns(room_id=3, timestamp=float(t), map=90.0))

    # 10~11초: 동맥관 플러싱 아티팩트(MAP=240 > 200) -> 즉시 무시되어야 함
    for t in range(10, 12):
        await rm.process(VitalSigns(room_id=3, timestamp=float(t), map=240.0))
    check(rm.current_alert_level == AlertLevel.GREEN, "MAP=240 생리학적 아티팩트 즉시 기각 (경보 없음)")

    # 12~14초(3초간): 일시적 튐(MAP=50) 후 15초에 90 복귀 -> 5초 이하이므로 잡음 무시
    for t in range(12, 15):
        await rm.process(VitalSigns(room_id=3, timestamp=float(t), map=50.0))
    check(rm.current_alert_level == AlertLevel.GREEN, "3초간 일시적 MAP=50 스파이크는 잡음으로 무시")
    await rm.process(VitalSigns(room_id=3, timestamp=15.0, map=90.0))

    # 20초부터 실제 급락: MAP 90 -> 54 연속 발생
    # 20, 21, 22, 23, 24초 (5초간 보류) -> 25초(6번째 초)에 즉시 수용되어 RED 경보 발령되어야 함!
    red_tick = None
    for t in range(20, 35):
        await rm.process(VitalSigns(room_id=3, timestamp=float(t), map=54.0))
        if rm.current_alert_level == AlertLevel.RED and red_tick is None:
            red_tick = t

    check(red_tick == 25, f"MAP 90->54 실제 급락 시 30초 지연 없이 정확히 6초째(t={red_tick})에 RED 경보 즉시 발령")
    # 26초 이후에도 중앙값이 동기화되어 계속 수용되는지 확인
    check(rm._spike_counts["map"] == 0 and len(rm._history["map"]) >= 6, "급변 수용 후 기준선 히스토리 즉시 동기화 완료")


async def test_3_bis_sqi_sr_eeg_judgment() -> None:
    print("\n[검증 3] BIS=0 및 SQI/SR/EEG 3단계 판정 & 침묵 방지")
    bus = EventBus()
    rm = RoomMonitorAgent(2, bus)
    sup = SupervisorAgent(bus, room_ids=[1, 2, 3, 4])
    await rm.start()
    await sup.start()

    # Case A: OR-2 시나리오 (BIS=0, SQI=0) -> 의료 RED 없이 SENSOR_ALERT('센서 확인 필요') 발생
    await rm.process(VitalSigns(room_id=2, timestamp=10.0, bis=0.0, sqi=0.0))
    await sup.process(10.0)
    st = sup.get_room_status(2)
    check(rm.current_alert_level == AlertLevel.GREEN, "BIS=0 + SQI=0 시 가짜 의료 RED 경보 미발생")
    check(st.sensor_problem is True and len(st.sensor_issues) == 1, f"침묵하지 않고 '센서 확인 필요' 상태 등록: {st.sensor_issues}")

    # Case B: BIS=0, SQI=80(양호), SR=0(억제 없음), delta=42% -> 센서/아티팩트 알림 유지
    await rm.process(VitalSigns(room_id=2, timestamp=11.0, bis=0.0, sqi=80.0, sr=0.0, eeg_delta=42.0))
    check(rm.current_alert_level == AlertLevel.GREEN and bool(rm.sensor_issues), "BIS=0 + SR=0 + δ파 활성 시 센서 확인 필요 유지")

    # Case C: 정상 신호 복구 (BIS=48, SQI=92, beta=12%) -> 센서 이슈 자동 해제
    for t in range(12, 20):
        await rm.process(VitalSigns(room_id=2, timestamp=float(t), bis=48.0, sqi=92.0, eeg_beta=12.0))
    await sup.process(19.0)
    check(not rm.sensor_issues and sup.get_room_status(2).sensor_problem is False, "정상 BIS/SQI 복귀 시 센서 확인 필요 자동 해제")

    # Case D: 실제 뇌파 억제 (BIS=0, SQI=90, SR=85%, delta=5%) -> 진짜 의료 RED 경보 발생!
    await rm.process(VitalSigns(room_id=2, timestamp=20.0, bis=0.0, sqi=90.0, sr=85.0, eeg_delta=5.0))
    check(rm.current_alert_level == AlertLevel.RED, "BIS=0 + SQI=90 + SR=85% 실제 뇌파 억제 시 즉시 RED 경보 발생")

    # Case E: BIS > 60 + EEG β파 상승 동반 (기저 12% -> 28%)
    rm.reset()
    for t in range(0, 10):
        await rm.process(VitalSigns(room_id=2, timestamp=float(t), bis=48.0, sqi=95.0, eeg_beta=12.0))
    for t in range(10, 76):
        await rm.process(VitalSigns(room_id=2, timestamp=float(t), bis=68.0, sqi=95.0, eeg_beta=28.0))
    bis_alert = rm.active_alerts.get("bis")
    check(bis_alert is not None and "EEG β파 상승 동반" in bis_alert.message, f"BIS>60 경보에 EEG β파 상승 근거 포함: {bis_alert.message if bis_alert else None}")


async def test_4_medication_literature_and_safety() -> None:
    print("\n[검증 4] 투약 제안 에이전트: 초기 CT/CE 동기화, MAP<65 통일, 하한 2.0, SQI<50 보류, 응급 시 취소")
    bus = EventBus()
    rm = RoomMonitorAgent(1, bus)
    med = MedicationAgent(1, bus)
    sup = SupervisorAgent(bus, room_ids=[1, 2, 3, 4])
    await rm.start()
    await med.start()
    await sup.start()

    # 1. 초기 CT/CE 농도 동기화 (ppf_ct=3.8, rftn_ct=2.5)
    v0 = VitalSigns(room_id=1, timestamp=0.0, map=80.0, bis=48.0, sqi=90.0, ppf_ct=3.8, rftn_ct=2.5)
    await rm.process(v0)
    await med.process(v0, sup.get_room_status(1))
    check(med.propofol_ce == 3.8 and med.remifentanil_ce == 2.5, f"MedicationAgent 초기 농도 동기화: PPF={med.propofol_ce}, RFTN={med.remifentanil_ce}")
    check(sup.get_room_status(1).propofol_ce == 3.8 and sup.get_room_status(1).remifentanil_ce == 2.5, "Supervisor RoomStatus 초기 농도도 일치")

    # 2. SQI < 50 불량 상태에서 BIS=72가 40초 지속되어도 프로포폴 제안이 나가면 안 됨
    for t in range(1, 45):
        v = VitalSigns(room_id=1, timestamp=float(t), map=80.0, bis=72.0, sqi=30.0)
        await rm.process(v)
        await med.process(v, sup.get_room_status(1))
    check(len(med._pending_suggestions) == 0, "SQI=30(<50) 센서 불량 시 BIS=72여도 프로포폴 증량 제안 보류")

    # 3. MAP=62 (< 65.0) 저혈압 30초 지속 시 레미펜타닐 감량 제안 (2.5 -> 2.0, 하한 2.0 준수 및 복합 처치 문구 확인)
    for t in range(50, 82):
        v = VitalSigns(room_id=1, timestamp=float(t), map=62.0, bis=48.0, sqi=90.0)
        await rm.process(v)
        await med.process(v, sup.get_room_status(1))
        await sup.process(float(t))

    suggs = list(sup.get_room_status(1).pending_suggestions)
    check(len(suggs) == 1, "MAP=62 (<65) 30초 지속 시 레미펜타닐 감량 제안 발생")
    check(suggs[0].suggested_value == 2.0 and "수액·승압제" in suggs[0].reason, f"제안 농도={suggs[0].suggested_value} (하한 2.0), 사유: {suggs[0].reason}")

    # 4. 미결 제안이 있는 상태에서 RED(SpO2=80) 응급 발생 -> 제안 자동 취소 및 승인 거부
    sugg_id = suggs[0].id
    v_red = VitalSigns(room_id=1, timestamp=85.0, map=62.0, spo2=80.0, bis=48.0, sqi=90.0)
    await rm.process(v_red)
    await med.process(v_red, sup.get_room_status(1))
    await sup.process(85.0)

    cancelled_evts = [e for e in bus._history if e.type == EventType.SUGGESTION_CANCELLED]
    check(len(cancelled_evts) == 1 and cancelled_evts[0].data["suggestion_id"] == sugg_id, "응급(RED) 진입 시 대기 제안 자동 취소 이벤트(SUGGESTION_CANCELLED) 발행")
    res = await sup.handle_approval(sugg_id, True)
    check(res is None, "응급(RED)으로 취소된 제안에 대한 뒤늦은 승인 요청 완벽 차단")


async def test_5_replayer_frame_adapter_and_nan_safety() -> None:
    print("\n[검증 5] 1번 주자 Frame 어댑터(EEG FFT 대역 계산) 및 NaN/Inf 무결성")
    # 128Hz 1초 사인파 생성: 2Hz(delta) 진폭 3.0 + 20Hz(beta) 진폭 1.0
    t_axis = np.linspace(0, 1.0, 128, endpoint=False)
    eeg_wave = 3.0 * np.sin(2 * np.pi * 2.0 * t_axis) + 1.0 * np.sin(2 * np.pi * 20.0 * t_axis)

    fake_frame = SimpleNamespace(
        room="OR-3",
        caseid=2680,
        t=13045,
        elapsed=45,
        num={
            "map": 68.5,
            "hr": float("nan"),       # NaN 유입 시뮬레이션
            "spo2": float("inf"),     # Inf 유입 시뮬레이션
            "bis": 46.0,
            "sqi": 94.0,
            "sr": 0.0,
            "ppf_ce": 3.2,
            "rftn_ce": 4.1,
        },
        age={"map": 1},
        eeg={"eeg1": eeg_wave},
        wave={},
        vent={"rr": 12.0},
    )

    vitals = VitalSigns.from_replayer_frame(room_id=3, frame=fake_frame)
    dumped = vitals.model_dump()

    check(vitals.timestamp == 45.0, f"케이스 시각(13045)이 아닌 시연 경과 시각(elapsed=45.0) 사용: {vitals.timestamp}")
    check(vitals.hr is None and vitals.spo2 is None, "NaN 및 Inf 입력이 안전하게 None으로 정화됨")
    check(vitals.eeg_delta is not None and vitals.eeg_delta > 70.0, f"원시 128Hz 파형(eeg1)에서 FFT로 δ파 비율 자동 산출: δ={vitals.eeg_delta}%, β={vitals.eeg_beta}%")
    check(
        all(not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) for v in dumped.values()),
        "직렬화 딕셔너리 내 NaN/Inf 0건 보장",
    )


async def run_all() -> None:
    print("=" * 65)
    print("AI-Addict 에이전트 정밀 버그 및 무결성 검사 (Opus급 검증)")
    print("=" * 65)
    await test_1_priority_non_inversion()
    await test_2_noise_filter_and_rapid_drop()
    await test_3_bis_sqi_sr_eeg_judgment()
    await test_4_medication_literature_and_safety()
    await test_5_replayer_frame_adapter_and_nan_safety()
    print("\n=== 정밀 버그 및 무결성 검사 전 항목(5개 스위트) 100% 통과! ===")


if __name__ == "__main__":
    drive(run_all())
