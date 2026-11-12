"""AI-Addict 다중 수술실 마취 안전감시 플랫폼 메인 서버.

전문의 1명이 최대 4개 수술실을 감독할 수 있도록 지원하는 FastAPI 서버입니다.
- 4개 수술실 바이탈 실시간 재생 (VitalDBReplayer)
- 다중 에이전트 협업 오케스트레이션 (RoomMonitor, Medication, Supervisor, Recorder)
- 웹소켓(WebSocket) 기반 전 수술실 실시간 생체신호, 경보 및 제안 브로드캐스트
- RESTful API (투약 승인/반려, 재생 배속 조절, 감사 로그 및 종합 통계 보고서 조회, 리셋)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from agents.base import BaseAgent
from agents.medication import MedicationAgent
from agents.recorder import RecorderAgent
from agents.room_monitor import RoomMonitorAgent
from agents.supervisor import SupervisorAgent
from core.enums import EventType
from core.event_bus import EventBus
from core.models import Event, VitalSigns
if getattr(config, "USE_REAL_CASES", False):
    # 실제 VitalDB 케이스 (1번 주자 재생기 + 어댑터)
    from data.real_replayer import RealCaseReplayer as VitalDBReplayer
else:
    # 가상 시나리오 데이터 (cases/ 폴더 없을 때 대비용)
    from data.replayer import VitalDBReplayer

# ── 로깅 설정 ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ai_addict.main")

# ── 정적 파일 디렉터리 경로 ────────────────────────────────────
BASE_DIR: Path = Path(__file__).resolve().parent
STATIC_DIR: Path = BASE_DIR / "static"

# ── 전역 상태 (Global State) ──────────────────────────────────
event_bus: EventBus = EventBus()
replayer: Optional[VitalDBReplayer] = None
room_monitors: Dict[int, RoomMonitorAgent] = {}
medication_agents: Dict[int, MedicationAgent] = {}
supervisor: Optional[SupervisorAgent] = None
recorder: Optional[RecorderAgent] = None

current_tick: int = 0
current_speed: int = config.DEFAULT_SPEED
simulation_running: bool = False
simulation_paused: bool = False
simulation_task: Optional[asyncio.Task] = None

# 틱 처리 상호배제 잠금: 메인 루프 / 구간 이동(seek) / 리셋이 동시에 에이전트 상태를 건드리지 않도록 보호
tick_lock: asyncio.Lock = asyncio.Lock()


def _system_data(**extra: Any) -> Dict[str, Any]:
    """SYSTEM 이벤트 공통 페이로드 (tick, speed, max_ticks, running, paused + 추가 플래그)."""
    data: Dict[str, Any] = {
        "tick": current_tick,
        "speed": current_speed,
        "max_ticks": replayer.max_ticks if replayer else config.DEMO_DURATION,
        "running": simulation_running,
        "paused": simulation_paused,
    }
    data.update(extra)
    return data


# ── 데이터 직렬화 헬퍼 ─────────────────────────────────────────

def _to_json_compatible(obj: Any) -> Any:
    """임의 객체를 JSON 직렬화 가능한 표준 파이썬 자료형으로 재귀 변환합니다."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        # Pydantic v2
        return _to_json_compatible(obj.model_dump(mode="json"))
    if hasattr(obj, "dict"):
        # Pydantic v1 fallback
        return _to_json_compatible(obj.dict())
    if isinstance(obj, dict):
        return {str(k): _to_json_compatible(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_json_compatible(item) for item in obj]
    # numpy 스칼라 및 배열 대응
    if hasattr(obj, "item"):
        return obj.item()
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


def serialize_event(event: Event) -> Dict[str, Any]:
    """Event 모델을 프론트엔드가 요구하는 JSON 규격 딕셔너리로 변환합니다.

    Format:
        {
            "type": "vital_update",
            "data": {...},
            "source": "room_monitor_1",
            "timestamp": 12.0,
            "created_at": "2026-10-29T09:45:00"
        }
    """
    event_type_str = event.type.value if hasattr(event.type, "value") else str(event.type)
    return {
        "type": event_type_str,
        "data": _to_json_compatible(event.data),
        "source": str(event.source),
        "timestamp": float(event.timestamp),
        "created_at": event.created_at.isoformat() if hasattr(event, "created_at") else "",
    }


# ── 시뮬레이션 루프 ───────────────────────────────────────────

async def _process_single_tick(tick: int) -> None:
    """단일 틱(1초)에 대해 4개 수술실 바이탈 재생 및 에이전트 파이프라인을 수행합니다."""
    if not replayer:
        return

    # 1. 1~4번 수술실 데이터 처리
    for room_id in range(1, 5):
        try:
            vitals = replayer.get_vitals(room_id, tick)
        except Exception as e:
            logger.error("[수술실 %d] 바이탈 획득 실패 (틱: %d): %s", room_id, tick, e)
            vitals = None

        if vitals is not None and room_id in room_monitors:
            try:
                await room_monitors[room_id].process(vitals)
            except Exception as e:
                logger.error("[수술실 %d] RoomMonitor 처리 중 오류 (틱: %d): %s", room_id, tick, e)

        room_status = None
        if supervisor is not None:
            try:
                room_status = supervisor.get_room_status(room_id)
            except Exception as e:
                logger.error("[수술실 %d] 방 종합 상태 조회 실패: %s", room_id, e)

        if vitals is not None and room_id in medication_agents:
            try:
                await medication_agents[room_id].process(vitals, room_status)
            except Exception as e:
                logger.error("[수술실 %d] MedicationAgent 처리 중 오류 (틱: %d): %s", room_id, tick, e)

    # 2. 중앙 감독 에이전트 틱 처리 (우선순위 랭킹 재산출, 응급 상태 감시)
    if supervisor is not None:
        try:
            await supervisor.process(tick)
        except Exception as e:
            logger.error("SupervisorAgent 틱 처리 중 오류 (틱: %d): %s", tick, e)


async def simulation_loop() -> None:
    """1초 단위로 4개 수술실의 생체신호를 재생하고 에이전트들을 순차 구동하는 메인 루프."""
    global current_tick, simulation_running

    max_t = replayer.max_ticks if replayer else config.DEMO_DURATION
    logger.info("시뮬레이션 루프 가동 시작 (총 틱: %d, 초기 배속: %dx)", max_t, current_speed)

    try:
        while simulation_running and replayer and current_tick < replayer.max_ticks:
            # 일시정지 상태인 경우 틱을 진행하지 않고 대기
            if simulation_paused:
                await asyncio.sleep(0.15)
                continue

            async with tick_lock:
                # 잠금 대기 중 일시정지/구간 이동/종료가 일어났을 수 있으므로 재확인
                if simulation_paused or not simulation_running or current_tick >= replayer.max_ticks:
                    continue

                tick = current_tick
                await _process_single_tick(tick)

                # 시스템 진행 상태 이벤트 브로드캐스트
                try:
                    await event_bus.publish(
                        Event(
                            type=EventType.SYSTEM,
                            data=_system_data(tick=tick),
                            source="system",
                            timestamp=float(tick),
                        )
                    )
                except Exception as e:
                    logger.error("시스템 이벤트 발행 실패 (틱: %d): %s", tick, e)

                # 틱 증가
                current_tick = tick + 1

            # 설정된 배속에 따른 지연 대기
            speed_mult = max(1, current_speed)
            sleep_sec = config.TICK_INTERVAL / speed_mult
            await asyncio.sleep(sleep_sec)

    except asyncio.CancelledError:
        logger.info("시뮬레이션 백그라운드 태스크가 정상 취소되었습니다.")
    except Exception as e:
        logger.exception("시뮬레이션 루프 중 예기치 않은 오류 발생: %s", e)
    finally:
        simulation_running = False
        logger.info("시뮬레이션 루프가 종료되었습니다. (종료 틱: %d)", current_tick)


# ── 수명 주기 관리 (Lifespan) ──────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 시작 및 종료 시 전체 시스템 라이프사이클을 조율합니다."""
    global replayer, supervisor, recorder, simulation_running, simulation_task

    logger.info("==================================================")
    logger.info("AI-Addict 마취 안전감시 플랫폼 서버 가동을 시작합니다.")
    logger.info("==================================================")

    # 1. VitalDBReplayer 생성 및 데이터 사전 로드
    try:
        replayer = VitalDBReplayer(case_ids=config.VITALDB_CASE_IDS)
    except TypeError:
        try:
            replayer = VitalDBReplayer(config.VITALDB_CASE_IDS)
        except TypeError:
            replayer = VitalDBReplayer()

    await replayer.load()
    logger.info("VitalDBReplayer 데이터 로드 완료 (총 %d 틱 / %d개 케이스)", replayer.max_ticks, len(config.VITALDB_CASE_IDS))

    # 2. 1~4번 수술실 에이전트 인스턴스 생성 및 시작
    for r_id in range(1, 5):
        try:
            room_monitors[r_id] = RoomMonitorAgent(room_id=r_id, event_bus=event_bus)
        except TypeError:
            room_monitors[r_id] = RoomMonitorAgent(r_id, event_bus)
        await room_monitors[r_id].start()

        try:
            medication_agents[r_id] = MedicationAgent(room_id=r_id, event_bus=event_bus)
        except TypeError:
            medication_agents[r_id] = MedicationAgent(r_id, event_bus)
        await medication_agents[r_id].start()

    logger.info("수술실 1~4번 RoomMonitorAgent 및 MedicationAgent 시작 완료")

    # 3. 중앙 감독 에이전트 생성 및 시작 (이벤트 버스 구독)
    try:
        supervisor = SupervisorAgent(event_bus=event_bus, room_ids=[1, 2, 3, 4])
    except TypeError:
        try:
            supervisor = SupervisorAgent(event_bus=event_bus)
        except TypeError:
            supervisor = SupervisorAgent(event_bus)
    await supervisor.start()
    logger.info("SupervisorAgent 중앙 감독 시작 완료")

    # 4. 기록 에이전트 생성 및 시작 (전체 이벤트 구독)
    try:
        recorder = RecorderAgent(event_bus=event_bus)
    except TypeError:
        recorder = RecorderAgent(event_bus)
    await recorder.start()
    logger.info("RecorderAgent 감사 로깅 및 통계 집계 시작 완료")

    # 5. 백그라운드 시뮬레이션 루프 태스크 시작
    simulation_running = True
    simulation_task = asyncio.create_task(simulation_loop(), name="ai_addict_sim_loop")
    logger.info("백그라운드 시뮬레이션 태스크 실행 완료")

    yield

    # 시스템 종료 절차
    logger.info("AI-Addict 서버 종료 절차를 수행합니다...")

    # 1. 시뮬레이션 정지
    simulation_running = False
    if simulation_task and not simulation_task.done():
        simulation_task.cancel()
        try:
            await simulation_task
        except asyncio.CancelledError:
            pass
    logger.info("시뮬레이션 태스크 정지 완료")

    # 2. 모든 에이전트 정지
    for r_id, monitor in list(room_monitors.items()):
        try:
            await monitor.stop()
        except Exception as e:
            logger.warning("[수술실 %d] RoomMonitor 정지 중 오류: %s", r_id, e)

    for r_id, med_agent in list(medication_agents.items()):
        try:
            await med_agent.stop()
        except Exception as e:
            logger.warning("[수술실 %d] MedicationAgent 정지 중 오류: %s", r_id, e)

    if supervisor is not None:
        try:
            await supervisor.stop()
        except Exception as e:
            logger.warning("SupervisorAgent 정지 중 오류: %s", e)

    if recorder is not None:
        try:
            await recorder.stop()
        except Exception as e:
            logger.warning("RecorderAgent 정지 중 오류: %s", e)

    logger.info("AI-Addict 모든 리소스 해제 및 정상 종료 완료")


# ── FastAPI 앱 인스턴스 ────────────────────────────────────────

app = FastAPI(
    title="AI-Addict Multi-Agent Anesthesia Monitoring System",
    description="다중 수술실 마취 안전감시 및 지능형 자율보조 플랫폼 API",
    version="1.0.0",
    lifespan=lifespan,
)


# ── 웹소켓 엔드포인트 ──────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """실시간 웹소켓 엔드포인트.

    클라이언트 연결 시 이벤트 버스의 브로드캐스트 핸들러를 등록하여
    생체신호(VITAL_UPDATE), 경보(ALERT), 투약 제안(DRUG_SUGGESTION),
    응급(EMERGENCY), 종합 상태(ROOM_STATUS) 이벤트를 JSON으로 실시간 전달합니다.
    """
    await websocket.accept()

    async def ws_send_handler(event: Event) -> None:
        """이벤트 버스에서 발행된 이벤트를 직렬화하여 웹소켓 클라이언트로 전송."""
        payload = serialize_event(event)
        await websocket.send_json(payload)

    event_bus.add_ws_handler(ws_send_handler)
    logger.info("WebSocket 클라이언트 연결 승인 (현재 활성 세션: %d개)", len(event_bus._ws_handlers))

    # 연결 즉시 시스템 현재 상태 및 각 방 종합 정보 동기화 전송
    try:
        init_system_event = Event(
            type=EventType.SYSTEM,
            data={
                "tick": current_tick,
                "speed": current_speed,
                "max_ticks": replayer.max_ticks if replayer else config.DEMO_DURATION,
                "running": simulation_running,
                "paused": simulation_paused,
            },
            source="system",
            timestamp=float(current_tick),
        )
        await websocket.send_json(serialize_event(init_system_event))

        if supervisor is not None:
            for r_id in range(1, 5):
                r_status = supervisor.get_room_status(r_id)
                if r_status is not None:
                    await websocket.send_json({
                        "type": EventType.ROOM_STATUS.value,
                        "data": _to_json_compatible(r_status),
                        "source": "supervisor",
                        "timestamp": float(current_tick),
                    })
    except Exception as e:
        logger.warning("웹소켓 초기 동기화 패킷 전송 중 오류: %s", e)

    try:
        while True:
            # 클라이언트로부터 메시지 수신 대기 (향후 확장 또는 웹소켓 제어용)
            client_text = await websocket.receive_text()
            logger.debug("웹소켓 클라이언트 메시지 수신: %s", client_text)
            try:
                msg = json.loads(client_text)
                action = msg.get("action")
                if action == "approve" and "suggestion_id" in msg and supervisor:
                    await supervisor.handle_approval(str(msg["suggestion_id"]), True)
                elif action == "reject" and "suggestion_id" in msg and supervisor:
                    await supervisor.handle_approval(str(msg["suggestion_id"]), False)
                elif action == "speed" and "multiplier" in msg:
                    spd = int(msg["multiplier"])
                    if spd in [1, 2, 4, 8]:
                        await set_speed(spd)
                elif action == "pause":
                    await pause_simulation()
                elif action == "seek" and "tick" in msg:
                    await seek_simulation(int(msg["tick"]))
                elif action == "reset":
                    await reset_simulation()
            except json.JSONDecodeError:
                pass
            except Exception as ex:
                logger.warning("웹소켓 제어 메시지 처리 실패: %s", ex)
    except WebSocketDisconnect:
        logger.info("WebSocket 클라이언트와의 연결이 종료되었습니다.")
    except Exception as e:
        logger.warning("WebSocket 연결 에러: %s", e)
    finally:
        event_bus.remove_ws_handler(ws_send_handler)
        logger.info("WebSocket 브로드캐스트 핸들러 등록 해제 (남은 세션: %d개)", len(event_bus._ws_handlers))


# ── RESTful API 엔드포인트 ─────────────────────────────────────

@app.post("/api/approve/{suggestion_id}")
async def approve_suggestion(suggestion_id: str) -> JSONResponse:
    """전문의가 특정 AI 투약 조절 제안을 승인합니다."""
    if supervisor is None:
        return JSONResponse(
            status_code=503,
            content={"success": False, "error": "SupervisorAgent가 초기화되지 않았습니다."},
        )

    try:
        res = supervisor.handle_approval(suggestion_id, True)
        if asyncio.iscoroutine(res):
            res = await res

        if res is None:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": f"제안 ID '{suggestion_id}'를 찾을 수 없습니다."},
            )

        return JSONResponse(
            content={
                "success": True,
                "suggestion_id": suggestion_id,
                "approved": True,
                "result": _to_json_compatible(res),
            }
        )
    except Exception as e:
        logger.exception("투약 제안 승인 처리 중 에러: %s", e)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@app.post("/api/reject/{suggestion_id}")
async def reject_suggestion(suggestion_id: str) -> JSONResponse:
    """전문의가 특정 AI 투약 조절 제안을 거절(반려)합니다."""
    if supervisor is None:
        return JSONResponse(
            status_code=503,
            content={"success": False, "error": "SupervisorAgent가 초기화되지 않았습니다."},
        )

    try:
        res = supervisor.handle_approval(suggestion_id, False)
        if asyncio.iscoroutine(res):
            res = await res

        if res is None:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": f"제안 ID '{suggestion_id}'를 찾을 수 없습니다."},
            )

        return JSONResponse(
            content={
                "success": True,
                "suggestion_id": suggestion_id,
                "approved": False,
                "result": _to_json_compatible(res),
            }
        )
    except Exception as e:
        logger.exception("투약 제안 반려 처리 중 에러: %s", e)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@app.post("/api/speed/{multiplier}")
async def set_speed(multiplier: int) -> JSONResponse:
    """시뮬레이션 재생 배속을 변경합니다 (1x, 2x, 4x, 8x 지원)."""
    global current_speed
    allowed = [1, 2, 4, 8]
    if multiplier not in allowed:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": f"유효하지 않은 배속입니다: {multiplier}. 허용 배속은 {allowed} 입니다.",
            },
        )

    current_speed = multiplier
    logger.info("시뮬레이션 배속이 %dx 로 설정되었습니다.", current_speed)

    # 배속 변경 알림을 시스템 이벤트로 브로드캐스트
    try:
        await event_bus.publish(
            Event(
                type=EventType.SYSTEM,
                data={
                    "tick": current_tick,
                    "speed": current_speed,
                    "max_ticks": replayer.max_ticks if replayer else config.DEMO_DURATION,
                    "running": simulation_running,
                    "paused": simulation_paused,
                },
                source="system",
                timestamp=float(current_tick),
            )
        )
    except Exception as e:
        logger.error("배속 갱신 이벤트 발행 실패: %s", e)

    return JSONResponse(content={"success": True, "speed": current_speed})


@app.post("/api/pause")
async def pause_simulation(paused: Optional[bool] = None) -> JSONResponse:
    """시뮬레이션 일시정지/재개 상태를 토글하거나 지정합니다."""
    global simulation_paused

    if paused is None:
        simulation_paused = not simulation_paused
    else:
        simulation_paused = bool(paused)

    logger.info("시뮬레이션 일시정지 상태 변경: paused=%s (현재 틱: %d)", simulation_paused, current_tick)

    await event_bus.publish(
        Event(
            type=EventType.SYSTEM,
            data={
                "tick": current_tick,
                "speed": current_speed,
                "max_ticks": replayer.max_ticks if replayer else config.DEMO_DURATION,
                "running": simulation_running,
                "paused": simulation_paused,
            },
            source="system",
            timestamp=float(current_tick),
        )
    )

    return JSONResponse(
        content={
            "success": True,
            "paused": simulation_paused,
            "tick": current_tick,
        }
    )


async def _clear_transient_agent_states(reset_drugs: bool = False) -> None:
    """타임라인 점프 또는 리셋 시 에이전트들의 일시적 경보/지속 타이머 상태를 초기화합니다."""
    from core.enums import AlertLevel
    from core.models import RoomStatus

    if supervisor is not None:
        prev_ce = {
            rid: (st.propofol_ce, st.remifentanil_ce)
            for rid, st in supervisor.room_statuses.items()
        }
        supervisor.room_statuses = {
            rid: RoomStatus(
                room_id=rid,
                propofol_ce=3.0 if reset_drugs else prev_ce.get(rid, (3.0, 3.0))[0],
                remifentanil_ce=3.0 if reset_drugs else prev_ce.get(rid, (3.0, 3.0))[1],
            )
            for rid in supervisor.room_ids
        }
        if hasattr(supervisor, "_alert_start_times"):
            supervisor._alert_start_times.clear()
        if hasattr(supervisor, "_prev_severity"):
            supervisor._prev_severity = {rid: 0.0 for rid in supervisor.room_ids}
        if hasattr(supervisor, "_pending_suggestions"):
            supervisor._pending_suggestions.clear()
        if reset_drugs:
            if hasattr(supervisor, "_ppf_initialized"):
                supervisor._ppf_initialized = {rid: False for rid in supervisor.room_ids}
            if hasattr(supervisor, "_rftn_initialized"):
                supervisor._rftn_initialized = {rid: False for rid in supervisor.room_ids}

    for rm in room_monitors.values():
        if hasattr(rm, "reset"):
            rm.reset()
        else:
            if hasattr(rm, "active_alerts"):
                rm.active_alerts.clear()
            if hasattr(rm, "sensor_issues"):
                rm.sensor_issues.clear()
            if hasattr(rm, "_history"):
                rm._history.clear()
            if hasattr(rm, "_spike_counts"):
                rm._spike_counts.clear()
            if hasattr(rm, "_pending_spikes"):
                rm._pending_spikes.clear()
            if hasattr(rm, "_yellow_start_times"):
                rm._yellow_start_times.clear()
            if hasattr(rm, "_beta_baseline_samples"):
                rm._beta_baseline_samples.clear()
            if hasattr(rm, "current_alert_level"):
                rm.current_alert_level = AlertLevel.GREEN

    for ma in medication_agents.values():
        if reset_drugs and hasattr(ma, "reset"):
            r = ma.reset()
            if asyncio.iscoroutine(r):
                await r
        else:
            if reset_drugs:
                ma.propofol_ce = 3.0
                ma.remifentanil_ce = 3.0
            if hasattr(ma, "_reset_persistence_timers"):
                ma._reset_persistence_timers()
            if hasattr(ma, "_last_propofol_sugg_time"):
                ma._last_propofol_sugg_time = None
            if hasattr(ma, "_last_remifentanil_sugg_time"):
                ma._last_remifentanil_sugg_time = None
            if hasattr(ma, "_pending_suggestions"):
                ma._pending_suggestions.clear()


@app.post("/api/seek/{tick}")
async def seek_simulation(tick: int) -> JSONResponse:
    """시뮬레이션 타임라인을 특정 틱(초)으로 즉시 이동합니다 (시연용 구간 점프)."""
    global current_tick, simulation_running, simulation_task

    max_t = replayer.max_ticks if replayer else config.DEMO_DURATION
    target_tick = max(0, min(int(tick), max_t - 1))

    logger.info("시뮬레이션 타임라인 점프 요청: %d -> %d 틱", current_tick, target_tick)

    # 메인 루프가 틱을 처리하는 도중에 끼어들지 않도록 잠금 획득
    async with tick_lock:
        # 1. 일시적 경보 및 지속성 타이머 초기화 (점프 구간에서 깨끗하게 재판정)
        await _clear_transient_agent_states(reset_drugs=False)
        current_tick = target_tick

        # 2. 화면 초기화 신호(seek)를 먼저 보낸 뒤 → 대상 틱 데이터를 보내야 화면에서 지워지지 않음
        await event_bus.publish(
            Event(
                type=EventType.SYSTEM,
                data=_system_data(tick=target_tick, seek=True),
                source="system",
                timestamp=float(target_tick),
            )
        )

        # 3. 대상 틱을 즉시 1회 처리 (일시정지 중에도 화면 갱신) 후 다음 틱부터 루프가 이어받음
        await _process_single_tick(target_tick)
        current_tick = target_tick + 1

    # 4. 시뮬레이션이 이미 끝까지 재생되어 종료되었다면 재가동
    if not simulation_running or simulation_task is None or simulation_task.done():
        simulation_running = True
        simulation_task = asyncio.create_task(simulation_loop(), name="ai_addict_sim_loop")

    return JSONResponse(
        content={
            "success": True,
            "tick": target_tick,
            "paused": simulation_paused,
            "speed": current_speed,
        }
    )


@app.get("/api/records")
async def get_records(
    room_id: Optional[int] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
) -> JSONResponse:
    """기록 에이전트에 저장된 감사 로그(RecordEntry) 목록을 JSON으로 반환합니다."""
    if recorder is None:
        return JSONResponse(
            status_code=503,
            content={"success": False, "error": "RecorderAgent가 초기화되지 않았습니다.", "records": []},
        )

    try:
        # get_records(room_id, event_type, limit) 지원
        records = recorder.get_records(room_id=room_id, event_type=event_type, limit=limit)
        if asyncio.iscoroutine(records):
            records = await records

        serialized = _to_json_compatible(records)
        return JSONResponse(content={"success": True, "records": serialized})
    except Exception as e:
        logger.exception("기록 조회 처리 중 에러: %s", e)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e), "records": []})


@app.get("/api/summary")
async def get_summary() -> JSONResponse:
    """기록 에이전트가 집계한 마취 모니터링 종합 통계 요약 보고서를 반환합니다."""
    if recorder is None:
        return JSONResponse(
            status_code=503,
            content={"success": False, "error": "RecorderAgent가 초기화되지 않았습니다.", "summary": {}},
        )

    try:
        summary_data = recorder.get_summary()
        if asyncio.iscoroutine(summary_data):
            summary_data = await summary_data

        serialized = _to_json_compatible(summary_data)
        return JSONResponse(content={"success": True, "summary": serialized})
    except Exception as e:
        logger.exception("통계 요약 조회 처리 중 에러: %s", e)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e), "summary": {}})


@app.post("/api/reset")
async def reset_simulation() -> JSONResponse:
    """시뮬레이션을 틱 0으로 리셋하고 전체 에이전트 상태 및 히스토리를 초기화합니다."""
    global current_tick, simulation_running, simulation_paused, simulation_task

    logger.info("시뮬레이션 리셋 절차를 시작합니다...")

    # 1. 기존 실행 중인 백그라운드 태스크 중지
    simulation_running = False
    simulation_paused = False
    if simulation_task and not simulation_task.done():
        simulation_task.cancel()
        try:
            await simulation_task
        except asyncio.CancelledError:
            pass

    async with tick_lock:
        # 2. 틱 및 이벤트 버스 초기화
        current_tick = 0
        event_bus.clear_history()

        # 3. 기록 에이전트 초기화
        if recorder is not None:
            if hasattr(recorder, "clear"):
                r = recorder.clear()
                if asyncio.iscoroutine(r):
                    await r
            elif hasattr(recorder, "records"):
                recorder.records.clear()
                if hasattr(recorder, "_outcomes_per_room"):
                    recorder._outcomes_per_room.clear()
                if hasattr(recorder, "_suggestions_per_room"):
                    recorder._suggestions_per_room.clear()
                if hasattr(recorder, "_alerts_per_room"):
                    recorder._alerts_per_room.clear()
                if hasattr(recorder, "_emergencies_per_room"):
                    recorder._emergencies_per_room.clear()

        # 4. 감독, 방 모니터, 투약 에이전트 상태 초기화
        await _clear_transient_agent_states(reset_drugs=True)

        # 5. 리셋 이벤트를 루프 재시작 "전에" 브로드캐스트 (0초 데이터가 리셋 신호보다 먼저 도착하지 않도록)
        simulation_running = True
        await event_bus.publish(
            Event(
                type=EventType.SYSTEM,
                data=_system_data(tick=0, reset=True),
                source="system",
                timestamp=0.0,
            )
        )

    # 6. 시뮬레이션 태스크 재시작
    simulation_task = asyncio.create_task(simulation_loop(), name="ai_addict_sim_loop")

    logger.info("시뮬레이션이 틱 0으로 성공적으로 리셋되었습니다.")
    return JSONResponse(
        content={
            "success": True,
            "current_tick": current_tick,
            "speed": current_speed,
            "running": simulation_running,
            "paused": simulation_paused,
        }
    )


# ── 정적 파일 및 UI 엔드포인트 ─────────────────────────────────

# static 디렉터리 존재 보장 및 /static 마운트
if not STATIC_DIR.exists():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/jump_points")
async def get_jump_points() -> JSONResponse:
    """시연 구간 바로가기 목록. 실제 케이스 재생기면 selected_cases.csv의 이상 시점, 가상 데이터면 빈 목록(HTML 기본 버튼 유지)."""
    points = getattr(replayer, "jump_points", None) or []
    return JSONResponse(content={"success": True, "jump_points": points})


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """메인 모니터링 대시보드 웹페이지(static/index.html)를 서빙합니다."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse(
            "<!DOCTYPE html><html><body>"
            "<h1>AI-Addict Anesthesia Safety Monitoring</h1>"
            "<p>static/index.html 파일을 찾을 수 없습니다.</p>"
            "</body></html>",
            status_code=404,
        )
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


@app.get("/{file_name:path}")
async def serve_static_root_asset(file_name: str) -> Any:
    """루트 경로 기준 정적 리소스 서빙 (예: /style.css, /app.js, favicon 등)."""
    # 경로 조작(../) 공격 방지: static 디렉터리 내부 파일만 서빙
    target = (STATIC_DIR / file_name).resolve()
    if target.is_file() and STATIC_DIR in target.parents:
        return FileResponse(target)
    raise HTTPException(status_code=404, detail=f"File '{file_name}' not found")


# ── 메인 엔트리포인트 ──────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
