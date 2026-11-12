"""VitalDB 데이터 재생기 및 시뮬레이션 데이터 생성 모듈.

4개 수술실에 대한 생체신호 데이터를 실시간 틱 단위로 재생합니다.
VitalDB 라이브러리를 통해 실제 임상 데이터를 로드하거나,
네트워크/환경 제약 시 4가지 시연 시나리오에 맞춘 시뮬레이션 데이터를 생성합니다.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np

import config
from core.models import VitalSigns

logger = logging.getLogger(__name__)


class VitalDBReplayer:
    """VitalDB 데이터 재생 및 시뮬레이션 클래스.

    4개 수술실의 환자 상태:
    - Room 1: 정상 기준선, t=300초(5분) 부근에서 MAP가 75에서 58로 서서히 하강 후 2분간 유지, 이후 점진적 회복.
    - Room 2: 정상 기준선, t=420초(7분) 부근에서 BIS가 0으로 급락(센서 탈락 의심)하지만 EEG δ 파워는 정상 유지, t=480초에 회복.
    - Room 3: 정상 기준선, t=600초(10분) 부근에서 SpO2가 98%에서 87%로 서서히 하강(저산소혈증 이벤트), 이후 점진적 회복.
    - Room 4: 전 구간 안정적 유지 (대조군).
    """

    def __init__(
        self,
        case_ids: Optional[List[int]] = None,
        speed: int = config.DEFAULT_SPEED,
    ) -> None:
        """재생기 초기화.

        Args:
            case_ids: 4개 방에 매핑할 VitalDB 케이스 ID 목록 (기본값: config.VITALDB_CASE_IDS)
            speed: 재생 배속 (기본값: 1)
        """
        self.case_ids: List[int] = case_ids or list(config.VITALDB_CASE_IDS)
        self.speed: int = speed
        self._max_ticks: int = config.DEMO_DURATION
        # room_id (1~4) -> 틱 인덱스별 VitalSigns 리스트
        self._data: Dict[int, List[VitalSigns]] = {i: [] for i in range(1, 5)}
        self._loaded: bool = False

    @property
    def max_ticks(self) -> int:
        """총 시뮬레이션 틱 길이 (초)."""
        return self._max_ticks

    async def load(self) -> None:
        """VitalDB에서 데이터를 로드하거나 시뮬레이션 데이터를 생성합니다.

        vitaldb 라이브러리를 통해 로드를 시도하며,
        실패하거나 데이터가 불충분할 경우 현실적인 시뮬레이션 데이터를 생성합니다.
        """
        success = False
        if getattr(config, "USE_VITALDB_REMOTE", False):
            import concurrent.futures
            import socket

            def _try_vitaldb_load() -> Dict[int, List[VitalSigns]]:
                """별도 스레드에서 VitalDB 로드를 시도합니다 (타임아웃 적용)."""
                socket.setdefaulttimeout(3.0)
                import vitaldb  # type: ignore

                loaded_rooms: Dict[int, List[VitalSigns]] = {}
                track_names = list(config.VITAL_TRACKS.values())

                for idx, case_id in enumerate(self.case_ids[:4], start=1):
                    vf = vitaldb.VitalFile(case_id, track_names)
                    df = vf.to_pandas(track_names, interval=config.TICK_INTERVAL)
                    if df is not None and not df.empty and len(df) >= self._max_ticks:
                        room_vitals = []
                        for t in range(self._max_ticks):
                            row = df.iloc[t]
                            vitals = VitalSigns(
                                room_id=idx,
                                timestamp=float(t),
                                map=float(row.get(config.VITAL_TRACKS["map"], 75.0)),
                                hr=float(row.get(config.VITAL_TRACKS["hr"], 72.0)),
                                spo2=float(row.get(config.VITAL_TRACKS["spo2"], 98.0)),
                                bis=float(row.get(config.VITAL_TRACKS["bis"], 45.0)),
                                etco2=float(row.get(config.VITAL_TRACKS["etco2"], 35.0)),
                                bt=float(row.get(config.VITAL_TRACKS["bt"], 36.5)),
                                sbp=float(row.get(config.VITAL_TRACKS["sbp"], 115.0)),
                                dbp=float(row.get(config.VITAL_TRACKS["dbp"], 65.0)),
                                rr=float(row.get(config.VITAL_TRACKS["rr"], 14.0)),
                                pip=float(row.get(config.VITAL_TRACKS["pip"], 18.0)),
                                tv=float(row.get(config.VITAL_TRACKS["tv"], 450.0)),
                                mac=float(row.get(config.VITAL_TRACKS["mac"], 1.0)),
                                eeg_delta=0.70,
                                eeg_alpha=0.20,
                                eeg_beta=0.10,
                            )
                            room_vitals.append(vitals)
                        loaded_rooms[idx] = room_vitals
                    else:
                        break
                return loaded_rooms

            logger.info("VitalDB 데이터 로드 시도 중 (5초 타임아웃)...")
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(_try_vitaldb_load)
                loaded_rooms = future.result(timeout=5)
                if len(loaded_rooms) == 4:
                    self._data = loaded_rooms
                    self._loaded = True
                    success = True
                    logger.info("VitalDB 실제 케이스 4건 로드 완료")
            except concurrent.futures.TimeoutError:
                logger.warning("VitalDB 로드 5초 타임아웃, 시뮬레이션 데이터를 생성합니다.")
            except Exception as exc:
                logger.warning("VitalDB 로드 실패 또는 건너뜀 (%s), 시뮬레이션 데이터를 생성합니다.", exc)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        if not success:
            self._generate_simulated_data()
            self._loaded = True
            logger.info("시나리오 기반 시뮬레이션 데이터 %d초 생성 완료", self._max_ticks)

    def _generate_simulated_data(self) -> None:
        """4개 수술실의 960초(16분) 시뮬레이션 데이터를 생성합니다.

        각 수술실별 고유 시나리오 및 미세한 생체신호 잡음(±1~2%)을 합성합니다.
        """
        np.random.seed(42)  # 재현 가능한 시연 데이터 생성
        total_ticks = self._max_ticks
        t = np.arange(total_ticks, dtype=float)

        for room_id in range(1, 5):
            # 기본 생체신호 기준선
            map_base = np.full(total_ticks, 75.0)
            hr_base = np.full(total_ticks, 72.0)
            spo2_base = np.full(total_ticks, 98.0)
            bis_base = np.full(total_ticks, 45.0)
            etco2_base = np.full(total_ticks, 35.0)
            bt_base = np.full(total_ticks, 36.5)
            rr_base = np.full(total_ticks, 14.0)
            sbp_base = np.full(total_ticks, 115.0)
            dbp_base = np.full(total_ticks, 65.0)
            pip_base = np.full(total_ticks, 18.0)
            tv_base = np.full(total_ticks, 450.0)
            mac_base = np.full(total_ticks, 1.0)

            # EEG 주파수 파워 성분 (정상 마취 시 고 델타, 저 알파/베타)
            delta_base = np.full(total_ticks, 0.70)
            alpha_base = np.full(total_ticks, 0.20)
            beta_base = np.full(total_ticks, 0.10)

            # ── 수술실별 특화 시나리오 주입 ────────────────────────
            if room_id == 1:
                # Room 1: t=300초(5분) 전후로 MAP 75 -> 58 하강, 2분간(120초) 지속 후 회복
                # 270~310s: 점진적 하강 (75 -> 58)
                drop_mask = (t >= 270) & (t < 310)
                map_base[drop_mask] = 75.0 - (75.0 - 58.0) * ((t[drop_mask] - 270) / 40.0)

                # 310~430s: 저혈압 유지 (58)
                low_mask = (t >= 310) & (t < 430)
                map_base[low_mask] = 58.0

                # 430~480s: 점진적 회복 (58 -> 75)
                rec_mask = (t >= 430) & (t < 480)
                map_base[rec_mask] = 58.0 + (75.0 - 58.0) * ((t[rec_mask] - 430) / 50.0)

                # SBP/DBP도 연동
                sbp_base = map_base + 40.0
                dbp_base = map_base - 10.0

            elif room_id == 2:
                # Room 2 시나리오 A: t=120~200초 구간에 일시적 각성 경향 (BIS 65 상승 -> 프로포폴 증량 제안 유도)
                bis_high_mask = (t >= 120) & (t < 200)
                bis_base[bis_high_mask] = 65.0
                # Room 2 시나리오 B: t=420초(7분)에 BIS가 0으로 급락 (센서 문제), EEG δ 파워는 정상 유지, t=480초에 복구
                bis_mask = (t >= 420) & (t < 480)
                bis_base[bis_mask] = 0.0
                # EEG delta는 0.70으로 정상 유지 (센서 탈락 특성)

            elif room_id == 3:
                # Room 3: t=600초(10분)부터 SpO2가 98%에서 87%(주의) -> 83%(위험/응급)로 하강 후 회복
                # 600~660s: 하강 (98 -> 87)
                spo2_drop = (t >= 600) & (t < 660)
                spo2_base[spo2_drop] = 98.0 - (98.0 - 87.0) * ((t[spo2_drop] - 600) / 60.0)

                # 660~700s: 주의 저산소증 유지 (87)
                spo2_low = (t >= 660) & (t < 700)
                spo2_base[spo2_low] = 87.0

                # 700~740s: 위험 저산소혈증 급락 (83.0 < 85.0 -> RED 경보 및 EMERGENCY 발령)
                spo2_crit = (t >= 700) & (t < 740)
                spo2_base[spo2_crit] = 83.0

                # 740~820s: 기도 확보 및 환기 개선 조치 후 회복 (86.5 -> 98, 85% 경계 채터링 방지)
                spo2_rec = (t >= 740) & (t < 820)
                spo2_base[spo2_rec] = 86.5 + (98.0 - 86.5) * ((t[spo2_rec] - 740) / 80.0)

                # 저산소증에 따른 가벼운 보상성 빈맥 (HR 증가)
                hr_comp = (t >= 630) & (t < 780)
                hr_base[hr_comp] = 72.0 + 15.0 * np.sin((t[hr_comp] - 630) / 150.0 * np.pi)

            elif room_id == 4:
                # Room 4: 전 구간 안정적 정상 유지 (대조군)
                pass

            # ── 현실적인 미세 잡음(Jitter, ±1~2%) 추가 ───────────────
            noise_map = np.random.normal(0, 0.015, total_ticks)
            noise_hr = np.random.normal(0, 0.012, total_ticks)
            noise_spo2 = np.random.normal(0, 0.002, total_ticks)
            noise_bis = np.random.normal(0, 0.015, total_ticks)
            noise_etco2 = np.random.normal(0, 0.015, total_ticks)
            noise_bt = np.random.normal(0, 0.002, total_ticks)
            noise_eeg = np.random.normal(0, 0.02, total_ticks)

            map_final = np.clip(map_base * (1.0 + noise_map), 40.0, 180.0)
            hr_final = np.clip(hr_base * (1.0 + noise_hr), 35.0, 160.0)
            spo2_final = np.clip(spo2_base * (1.0 + noise_spo2), 70.0, 100.0)
            bis_final = np.where(bis_base == 0.0, 0.0, np.clip(bis_base * (1.0 + noise_bis), 0.0, 100.0))
            etco2_final = np.clip(etco2_base * (1.0 + noise_etco2), 15.0, 60.0)
            bt_final = np.clip(bt_base * (1.0 + noise_bt), 34.0, 40.0)
            rr_final = np.clip(rr_base + np.random.normal(0, 0.3, total_ticks), 8.0, 30.0)

            delta_final = np.clip(delta_base * (1.0 + noise_eeg), 0.1, 1.0)
            alpha_final = np.clip(alpha_base * (1.0 + noise_eeg * 0.5), 0.05, 0.5)
            beta_final = np.clip(beta_base * (1.0 + noise_eeg * 0.5), 0.02, 0.3)

            # VitalSigns 리스트 구성
            vitals_list: List[VitalSigns] = []
            for i in range(total_ticks):
                vs = VitalSigns(
                    room_id=room_id,
                    timestamp=float(i),
                    map=round(float(map_final[i]), 1),
                    sbp=round(float(sbp_base[i] * (1.0 + noise_map[i])), 1),
                    dbp=round(float(dbp_base[i] * (1.0 + noise_map[i])), 1),
                    hr=round(float(hr_final[i]), 1),
                    spo2=round(float(spo2_final[i]), 1),
                    etco2=round(float(etco2_final[i]), 1),
                    bis=round(float(bis_final[i]), 1),
                    rr=round(float(rr_final[i]), 1),
                    pip=round(float(pip_base[i]), 1),
                    tv=round(float(tv_base[i]), 1),
                    mac=round(float(mac_base[i]), 2),
                    bt=round(float(bt_final[i]), 1),
                    eeg_delta=round(float(delta_final[i]), 3),
                    eeg_alpha=round(float(alpha_final[i]), 3),
                    eeg_beta=round(float(beta_final[i]), 3),
                )
                vitals_list.append(vs)

            self._data[room_id] = vitals_list

    def get_vitals(self, room_id: int, tick: int) -> VitalSigns:
        """지정된 방의 특정 틱 생체신호 반환.

        Args:
            room_id: 수술실 번호 (1~4)
            tick: 시뮬레이션 경과 초

        Returns:
            VitalSigns: 해당 시점의 생체신호 객체
        """
        if not self._loaded:
            self._generate_simulated_data()
            self._loaded = True

        if room_id not in self._data or not self._data[room_id]:
            # fallback
            return VitalSigns(room_id=room_id, timestamp=float(tick), map=75.0, hr=72.0, spo2=98.0, bis=45.0)

        clamped_tick = max(0, min(tick, len(self._data[room_id]) - 1))
        return self._data[room_id][clamped_tick]
