"""AI-Addict 4대 에이전트 및 전체 960초 임상 시나리오 종합 검증 스크립트."""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import config
from core.enums import AlertLevel, DrugType, EventType, SuggestionAction
from core.event_bus import EventBus
from data.replayer import VitalDBReplayer
from agents.room_monitor import RoomMonitorAgent
from agents.medication import MedicationAgent
from agents.supervisor import SupervisorAgent
from agents.recorder import RecorderAgent


async def run_full_verification() -> None:
    print("=" * 65)
    print("AI-Addict 멀티 에이전트 마취 안전감시 플랫폼 최종 점검")
    print("=" * 65)

    # 1. Replayer 로딩 속도 및 데이터 검증
    t0 = time.perf_counter()
    replayer = VitalDBReplayer(case_ids=config.VITALDB_CASE_IDS)
    await replayer.load()
    elapsed_load = time.perf_counter() - t0
    print(f"[1] VitalDBReplayer 로드 완료: {elapsed_load:.3f}초 (총 {replayer.max_ticks}틱)")
    assert elapsed_load < 2.0, f"Replayer 로드가 너무 느립니다: {elapsed_load:.2f}s"

    # 2. 에이전트 초기화 및 시작
    bus = EventBus()
    room_monitors = {r: RoomMonitorAgent(room_id=r, event_bus=bus) for r in range(1, 5)}
    med_agents = {r: MedicationAgent(room_id=r, event_bus=bus) for r in range(1, 5)}
    supervisor = SupervisorAgent(event_bus=bus, room_ids=[1, 2, 3, 4])
    recorder = RecorderAgent(event_bus=bus)

    for r in range(1, 5):
        await room_monitors[r].start()
        await med_agents[r].start()
    await supervisor.start()
    await recorder.start()
    print("[2] 4종 에이전트 (RoomMonitor×4, Medication×4, Supervisor×1, Recorder×1) 시작 완료")

    # 추적용 플래그
    room2_bis_zero_ticks = 0
    room2_bis_zero_red_alerts = 0
    room3_emergency_triggered = False
    room1_rank1_achieved = False
    room3_rank1_achieved = False
    approved_count = 0
    rejected_count = 0

    # 3. 960초(16분) 전 구간 시뮬레이션 실행
    for tick in range(replayer.max_ticks):
        for r in range(1, 5):
            vitals = replayer.get_vitals(r, tick)
            await room_monitors[r].process(vitals)

            # Room 2 BIS=0 센서 탈락 구간(420~479초) 검증
            if r == 2 and 420 <= tick < 480:
                if vitals.bis == 0.0:
                    room2_bis_zero_ticks += 1
                if room_monitors[2].current_alert_level == AlertLevel.RED:
                    room2_bis_zero_red_alerts += 1

            status = supervisor.get_room_status(r)
            await med_agents[r].process(vitals, status)

        await supervisor.process(float(tick))

        # 대기 중인 투약 제안이 발생하면 첫 번째는 승인, 두 번째는 반려 테스트
        for r in range(1, 5):
            st = supervisor.get_room_status(r)
            if st and st.pending_suggestions:
                for sug in list(st.pending_suggestions):
                    if approved_count == 0:
                        await supervisor.handle_approval(sug.id, True)
                        approved_count += 1
                    else:
                        await supervisor.handle_approval(sug.id, False)
                        rejected_count += 1

        # 우선순위 1위 방 및 응급 상태 확인
        all_statuses = supervisor.get_all_statuses()
        ranked = sorted(all_statuses.values(), key=lambda x: x.priority_score, reverse=True)
        if ranked[0].room_id == 1 and ranked[0].priority_score > 0:
            room1_rank1_achieved = True
        if ranked[0].room_id == 3 and ranked[0].priority_score > 0:
            room3_rank1_achieved = True
        if all_statuses[3].is_emergency:
            room3_emergency_triggered = True

    summary = recorder.get_summary()

    print("\n[3] 960초(16분) 시뮬레이션 임상 시나리오 검증 결과:")
    print(f"  - Room 1 (저혈압 시나리오): 경보 {summary['alerts_by_room'].get(1)} / 투약 제안 {summary['suggestions_by_room'].get(1)}건 / 우선순위 1위 달성={room1_rank1_achieved}")
    print(f"  - Room 2 (각성 + BIS=0 센서탈락): 경보 {summary['alerts_by_room'].get(2)} / 투약 제안 {summary['suggestions_by_room'].get(2)}건")
    print(f"    * BIS=0 감지 틱 수: {room2_bis_zero_ticks}틱, EEG δ 정상으로 억제한 오경보(RED): {room2_bis_zero_red_alerts}건 (정상=0건)")
    print(f"  - Room 3 (저산소증 응급 시나리오): 경보 {summary['alerts_by_room'].get(3)} / EMERGENCY 진입={room3_emergency_triggered} / 우선순위 1위 달성={room3_rank1_achieved}")
    print(f"  - Room 4 (정상 대조군): 경보 {summary['alerts_by_room'].get(4)} / 투약 제안 {summary['suggestions_by_room'].get(4)}건")
    print(f"  - Recorder 통계: 총 경보={summary['total_alerts']}건 (주의 {summary['yellow_alerts']}, 위험 {summary['red_alerts']}), "
          f"총 제안={summary['total_suggestions']}건 (승인 {summary['approved_suggestions']}, 반려 {summary['rejected_suggestions']}), "
          f"일치율={summary['overall_match_rate']*100:.1f}%")

    # 핵심 검증 어설션
    assert summary["yellow_alerts"] >= 2, f"YELLOW 경보가 최소 2건 이상 집계되어야 합니다: {summary['yellow_alerts']}"
    assert summary["red_alerts"] == 1, f"RED 경보가 정확히 1건 집계되어야 합니다: {summary['red_alerts']}"
    assert summary["suggestions_by_room"].get(1, 0) >= 1, "Room 1 레미펜타닐 감량 제안이 발생해야 합니다."
    assert summary["suggestions_by_room"].get(2, 0) >= 1, "Room 2 프로포폴 증량 제안이 발생해야 합니다."
    assert room2_bis_zero_ticks == 60, "Room 2 BIS=0 구간이 60초여야 합니다."
    assert room2_bis_zero_red_alerts == 0, "Room 2 BIS=0 센서 탈락 시 RED 경보가 발생하면 안 됩니다."
    assert room3_emergency_triggered, "Room 3 SpO2 급락 시 EMERGENCY 모드가 발동되어야 합니다."
    assert summary["alerts_by_room"].get(4, {"yellow": 0, "red": 0}) == {"yellow": 0, "red": 0}, "Room 4 대조군은 경보가 0건이어야 합니다."
    assert room1_rank1_achieved and room3_rank1_achieved, "Supervisor 동적 우선순위 랭킹이 정상 동작해야 합니다."

    print("\n=== 모든 에이전트 및 임상 시나리오 검증 100% 통과! ===")


def _drive_coro(coro):
    """Windows 샌드박스에서 소켓(ProactorEventLoop) 생성 없이 순수 코루틴을 즉시 실행합니다."""
    try:
        while True:
            coro.send(None)
    except StopIteration as e:
        return e.value


if __name__ == "__main__":
    _drive_coro(run_full_verification())
