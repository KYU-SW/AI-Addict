"""실제 VitalDB 시연 구간(demo/*.npz) 16분을 에이전트 전체에 통과시켜, 시연 타임라인(DECISIONS D-07)대로 동작하는지 확인.

실행 (저장소 최상위에서, 인터넷·cases/ 불필요): python test_demo_clips.py
출력: 방별 경보 발생 시각, 1위 방이 바뀌는 시각, 걸러낸 가짜 경보 수 → 보고서·발표의 '실험 결과' 표로 그대로 사용 가능
"""
import asyncio
import os
import resource
import sys

import config
from agents.medication import MedicationAgent
from agents.recorder import RecorderAgent
from agents.room_monitor import RoomMonitorAgent
from agents.supervisor import SupervisorAgent
from core.enums import EventType
from core.event_bus import EventBus
from data.real_replayer import RealCaseReplayer

ROOM = {1: "OR-1", 2: "OR-2", 3: "OR-3", 4: "OR-4"}


def mmss(t):
    return f"{int(t) // 60:2d}:{int(t) % 60:02d}"


async def main():
    rp = RealCaseReplayer()
    await rp.load()
    assert all(r.source == "clip" for r in rp.rooms.values()), "demo/ 파일을 못 읽음 (7_make_demo_clips.py 실행 필요)"
    assert "pandas" not in sys.modules, "배포 서버용 경로에서 pandas를 불러오면 안 됨"

    bus = EventBus()
    mons = {r: RoomMonitorAgent(room_id=r, event_bus=bus) for r in ROOM}
    meds = {r: MedicationAgent(room_id=r, event_bus=bus) for r in ROOM}
    sup = SupervisorAgent(event_bus=bus, room_ids=list(ROOM))
    rec = RecorderAgent(event_bus=bus)

    log = []

    async def on_alert(ev):
        d = ev.data
        log.append((ev.timestamp, f"{ROOM.get(d.get('room_id'), '?')} {getattr(d.get('level'), 'value', str(d.get('level'))).upper():6s} {d.get('message', '')}"))

    async def on_sensor(ev):
        d = ev.data
        log.append((ev.timestamp, f"{ROOM.get(d.get('room_id'), '?')} 센서   {d.get('message', '')}"))

    bus.subscribe(EventType.ALERT, on_alert)
    bus.subscribe(EventType.SENSOR_ALERT, on_sensor)
    for a in [*mons.values(), *meds.values(), sup, rec]:
        await a.start()

    first_rank, top, top_changes, max_level = {}, None, [], {r: "green" for r in ROOM}
    or1_wrongly_first = []
    for tick in range(rp.max_ticks):
        for r in ROOM:
            v = rp.get_vitals(r, tick)
            if v is None:
                continue
            await mons[r].process(v)
            await meds[r].process(v, sup.get_room_status(r))
        await sup.process(float(tick))
        st = {r: sup.get_room_status(r) for r in ROOM}
        for r in ROOM:
            lv = getattr(st[r].alert_level, "value", str(st[r].alert_level))
            order = ["green", "yellow", "red"]
            if lv in order and order.index(lv) > order.index(max_level[r]):
                max_level[r] = lv
        ranked = sorted(ROOM, key=lambda r: -st[r].priority_score)
        leader = ranked[0] if st[ranked[0]].priority_score > 0 else None
        # OR-1(체온 노랑만)이, 다른 방에 체온보다 급한 경보(MAP·BIS·HR 등)가 있는데도 1위인 순간 기록
        # (다른 방도 체온 노랑뿐이면 동점이라 방 번호 순서로 OR-1이 앞설 수 있음 → 정상)
        if leader == 1 and any(
            a.parameter != "bt" for r in (2, 3, 4) for a in st[r].active_alerts
        ):
            or1_wrongly_first.append(tick)
        if leader != top:
            top_changes.append((tick, ROOM[leader] if leader else "없음(전부 정상)"))
            top = leader
        await asyncio.sleep(0)

    print(f"=== 실제 VitalDB 시연 구간 {rp.max_ticks}초 재생 결과 ===")
    print("\n[경보·센서 이벤트]")
    for t, msg in sorted(log):
        print(f"  {mmss(t)}  {msg}")
    print("\n['먼저 가야 할 방'(1위) 변화]")
    for t, who in top_changes:
        print(f"  {mmss(t)}  → {who}")
    print("\n[방별 최고 단계 / 걸러낸 가짜 경보]")
    for r in ROOM:
        m = mons[r]
        print(f"  {ROOM[r]}: 최고 {max_level[r]:6s}  순간 잡음 기각 {m.suppressed_artifacts}건, 1분 전 회복 {m.suppressed_transients}건")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"\n최대 메모리 사용량: {rss:.0f} MB (Render 무료 512MB)")

    # 1) 반드시 통과해야 하는 것 (데이터·배포 조건)
    assert rss < 400, f"메모리 {rss:.0f}MB: 512MB 서버에 여유가 부족함"
    assert max_level[3] == "red", "OR-3 저혈압(MAP 55 미만)이 빨강까지 가야 함"
    assert max_level[2] != "red", "OR-2 BIS 0(센서 문제)이 빨강 경보로 가면 안 됨"
    for r in (1, 3):   # 동맥관 플러싱(MAP 140~237, 20~50초)으로 '상승' 빨강이 나면 안 됨
        assert not any(f"{ROOM[r]} RED    MAP 심각 상승" in m for _, m in log), f"{ROOM[r]} 동맥관 플러싱 가짜 빨강"

    # 2) 시연 타임라인(D-07) 기대 동작 대조 → ✗는 에이전트 기준/점수 조정 필요 (실패로 멈추진 않음)
    leaders = [w for _, w in top_changes]
    checks = [
        ("OR-1 동맥압 순간 튐 걸러냄 (빨강 없음)", max_level[1] != "red"),
        # OR-1은 혈역학적으로 안정이지만 실제 체온이 35.3~35.4°C(경도 저체온)라 '체온 노랑'은 정상 판정.
        # 기대 동작: 빨강·혈압·BIS 경보 없이 체온 주의만, 그리고 체온 노랑만으로는 OR-3·OR-4보다 1위가 되지 않음
        ("OR-1 안정 케이스: 체온 주의 외 경보 없음", all(("OR-1" not in m) or ("체온" in m) for _, m in log)),
        ("OR-1 체온 노랑만으로 다른 방의 급한 경보(MAP·BIS·HR)보다 1위가 되지 않음", not or1_wrongly_first),
        ("OR-2 BIS 0 → 센서 문제로 분류", any("OR-2 센서" in m for _, m in log)),
        ("OR-3 MAP<65 1분 → 노랑, 1위", any(t >= 300 and w == "OR-3" for t, w in top_changes)),
        ("OR-3 MAP<55 → 빨강", max_level[3] == "red"),
        ("OR-4 BIS 60 초과 1분 → 노랑", any("OR-4 YELLOW BIS" in m for _, m in log)),
        ("OR-3 회복 후 OR-4가 1위로 교체", "OR-4" in leaders[leaders.index("OR-3"):] if "OR-3" in leaders else False),
    ]
    print("\n[시연 타임라인 D-07 대조]")
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    print("\n=== 실제 데이터 연동·메모리 검증 통과 ===" + ("" if all(ok for _, ok in checks) else
          "  (✗ 항목은 에이전트 판정·우선순위 점수 조정 필요)"))

if __name__ == "__main__":
    asyncio.run(main())
