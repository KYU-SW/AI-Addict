"""시연용 타임라인 컨트롤러(일시정지 / 구간 이동 / 리셋) 검증 스크립트.

Windows 샌드박스에서는 asyncio 이벤트 루프가 소켓을 만들지 못하므로,
코루틴을 직접 구동하고 백그라운드 태스크 생성(create_task)은 가짜로 대체합니다.
"""
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def drive(coro):
    """소켓 없이 코루틴을 끝까지 실행합니다."""
    try:
        while True:
            coro.send(None)
    except StopIteration as e:
        return e.value


class _FakeTask:
    def done(self):
        return True

    def cancel(self):
        pass


created_tasks = []


def _fake_create_task(coro, name=None):
    created_tasks.append(name)
    coro.close()  # 실제 루프는 돌리지 않음 (틱은 테스트에서 직접 처리)
    return _FakeTask()


asyncio.create_task = _fake_create_task

import config  # noqa: E402
import main  # noqa: E402
from core.enums import AlertLevel, EventType  # noqa: E402
from data.replayer import VitalDBReplayer  # noqa: E402
from agents.room_monitor import RoomMonitorAgent  # noqa: E402
from agents.medication import MedicationAgent  # noqa: E402
from agents.supervisor import SupervisorAgent  # noqa: E402
from agents.recorder import RecorderAgent  # noqa: E402


def body(resp):
    return json.loads(resp.body)


def setup():
    """lifespan과 동일하게 에이전트를 구성하되 백그라운드 루프는 띄우지 않습니다."""
    main.replayer = VitalDBReplayer(case_ids=config.VITALDB_CASE_IDS)
    drive(main.replayer.load())
    for r in range(1, 5):
        main.room_monitors[r] = RoomMonitorAgent(room_id=r, event_bus=main.event_bus)
        drive(main.room_monitors[r].start())
        main.medication_agents[r] = MedicationAgent(room_id=r, event_bus=main.event_bus)
        drive(main.medication_agents[r].start())
    main.supervisor = SupervisorAgent(event_bus=main.event_bus, room_ids=[1, 2, 3, 4])
    drive(main.supervisor.start())
    main.recorder = RecorderAgent(event_bus=main.event_bus)
    drive(main.recorder.start())
    main.simulation_running = True
    main.simulation_task = _FakeTask()


def run_ticks(start, end):
    """메인 루프 1회분(틱 처리 + 틱 증가)을 흉내냅니다."""
    for t in range(start, end):
        drive(main._process_single_tick(t))
        main.current_tick = t + 1


def check(cond, msg):
    print(("  [통과] " if cond else "  [실패] ") + msg)
    if not cond:
        raise AssertionError(msg)


def main_test():
    setup()
    print("=" * 60)
    print("타임라인 컨트롤러 검증")
    print("=" * 60)

    # ── 1. 구간 이동: OR1 저혈압 (285초) ──
    print("\n[1] 구간 이동 → 285초 (OR1 저혈압)")
    main.event_bus.clear_history()
    res = body(drive(main.seek_simulation(285)))
    check(res["tick"] == 285, f"응답 tick=285 (실제 {res['tick']})")
    check(main.current_tick == 286, f"대상 틱 처리 후 루프는 286부터 이어받음 (실제 {main.current_tick})")

    hist = main.event_bus._history
    seek_idx = next(i for i, e in enumerate(hist) if e.type == EventType.SYSTEM and e.data.get("seek"))
    vital_idx = next(i for i, e in enumerate(hist) if e.type == EventType.VITAL_UPDATE)
    check(seek_idx < vital_idx, "화면 초기화 신호(seek)가 285초 데이터보다 먼저 전송됨")
    vitals_285 = [e for e in hist if e.type == EventType.VITAL_UPDATE]
    check(len(vitals_285) == 4, f"285초 데이터는 4개 방 각 1회씩만 처리 (실제 {len(vitals_285)}건, 중복 없음)")

    # 점프 후 시나리오가 제때 재현되는지 (버튼 안내문: 약 50초 뒤 제안, 약 70초 뒤 경보)
    sugg_tick = alert_tick = None
    for t in range(286, 420):
        before_s = main.recorder._suggestions_per_room.get(1, 0)
        before_a = main.recorder._alerts_per_room[1]["yellow"]
        run_ticks(t, t + 1)
        if sugg_tick is None and main.recorder._suggestions_per_room.get(1, 0) > before_s:
            sugg_tick = t
        if alert_tick is None and main.recorder._alerts_per_room[1]["yellow"] > before_a:
            alert_tick = t
    check(sugg_tick is not None, f"OR1 레미펜타닐 감량 제안 발생 (점프 후 {sugg_tick - 285}초)")
    check(alert_tick is not None, f"OR1 주의 경보 발생 (점프 후 {alert_tick - 285}초)")

    # 제안 하나를 승인해 약물 농도를 바꿔 둠 (구간 이동 시 유지되어야 함)
    pending = main.supervisor.get_room_status(1).pending_suggestions
    approved_ce = None
    if pending:
        drive(main.supervisor.handle_approval(pending[0].id, True))
        approved_ce = main.medication_agents[1].remifentanil_ce

    # ── 2. 뒤로 구간 이동: 경보는 지워지고 약물 농도는 유지 ──
    print("\n[2] 뒤로 구간 이동 → 115초 (OR2 각성)")
    check(bool(main.room_monitors[1].active_alerts), "이동 전 OR1 경보가 활성 상태")
    drive(main.seek_simulation(115))
    check(not main.room_monitors[1].active_alerts, "이동 후 OR1 이전 경보가 정리됨")
    check(main.supervisor.get_room_status(1).alert_level == AlertLevel.GREEN, "감독 에이전트의 OR1 상태도 정상으로 초기화")
    check(not main.medication_agents[1]._pending_suggestions, "대기 중이던 투약 제안 정리됨")
    if approved_ce is not None:
        check(main.medication_agents[1].remifentanil_ce == approved_ce,
              f"승인된 약물 농도는 유지됨 (레미펜타닐 {approved_ce})")
        check(main.supervisor.get_room_status(1).remifentanil_ce == approved_ce,
              "감독 에이전트 쪽 약물 농도도 유지됨")

    # ── 3. 일시정지 ──
    print("\n[3] 일시정지 / 재개")
    check(body(drive(main.pause_simulation()))["paused"] is True, "토글 1회 → 일시정지")
    check(body(drive(main.pause_simulation()))["paused"] is False, "토글 2회 → 재개")
    check(body(drive(main.pause_simulation(paused=True)))["paused"] is True, "명시적 지정(paused=True)")

    # 일시정지 중에도 구간 이동 시 화면이 갱신되어야 함
    main.event_bus.clear_history()
    drive(main.seek_simulation(415))
    v = [e for e in main.event_bus._history if e.type == EventType.VITAL_UPDATE]
    check(len(v) == 4 and main.simulation_paused, "일시정지 상태에서 구간 이동해도 대상 틱 화면 갱신, 일시정지는 유지")

    # ── 4. 끝까지 재생 후 구간 이동 → 루프 재가동 ──
    print("\n[4] 재생 종료 후 구간 이동")
    main.simulation_running = False
    created_tasks.clear()
    drive(main.seek_simulation(650))
    check(main.simulation_running and created_tasks == ["ai_addict_sim_loop"], "종료된 시뮬레이션이 재가동됨")

    # ── 5. 리셋 ──
    print("\n[5] 리셋")
    main.medication_agents[2].propofol_ce = 4.5
    created_tasks.clear()
    res = body(drive(main.reset_simulation()))
    check(res["current_tick"] == 0 and main.current_tick == 0, "틱 0으로 복귀")
    check(main.simulation_paused is False, "일시정지 해제됨")
    check(main.medication_agents[2].propofol_ce == 3.0, "약물 농도 3.0으로 초기화")
    check(all(m._last_propofol_sugg_time is None for m in main.medication_agents.values()), "투약 제안 대기시간(쿨다운) 초기화")
    hist = main.event_bus._history
    check(len(hist) == 1 and hist[0].data.get("reset"), "리셋 신호가 루프 재시작 전 첫 이벤트로 전송됨")
    check(len(main.recorder.records) == 1, f"기록이 비워짐 (리셋 이벤트 1건만 남음, 실제 {len(main.recorder.records)}건)")
    check(created_tasks == ["ai_addict_sim_loop"], "리셋 후 루프 재가동")

    print("\n=== 타임라인 컨트롤러 검증 모두 통과 ===")


if __name__ == "__main__":
    main_test()
