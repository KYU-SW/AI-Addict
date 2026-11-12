/**
 * AI-Addict 마취 안전감시 플랫폼 - 실시간 클라이언트 애플리케이션
 * 
 * 주요 기능:
 * - 4개 수술실 실시간 생체신호(MAP, HR, SpO2, BIS, EtCO2 등) 수신 및 렌더링
 * - Chart.js 기반 60초/120초 스파크라인 트렌드 차트
 * - 자동 재연결 웹소켓 클라이언트 (Exponential Backoff)
 * - 수술실 위험도 우선순위 실시간 동적 재정렬
 * - AI 약물 투약 제안 승인/반려 인터랙션 (/api/approve, /api/reject)
 * - 통합 이벤트 로그 및 필터링
 * - 재생 배속 제어 (/api/speed)
 * - 종합 통계 요약 오버레이 및 응급 상황(Emergency) 모달 알림
 * - Web Audio API 기반 의료용 알람 음향
 */

(() => {
  'use strict';

  // ── 1. 전역 상태 관리 (State Management) ──────────────────────────────────
  const state = {
    // 4개 수술실 상태
    rooms: {
      1: createInitialRoomState(1),
      2: createInitialRoomState(2),
      3: createInitialRoomState(3),
      4: createInitialRoomState(4),
    },
    // 대기 중인 투약 제안 목록 (Map: id -> suggestion)
    pendingSuggestions: new Map(),
    // 이벤트 로그 배열 (최대 200개 유지)
    eventLogs: [],
    // 시스템 정보
    system: {
      tick: 0,
      maxTicks: 960,
      speed: 1,
      paused: false,
      connected: false,
    },
    // 통계 누적치
    stats: {
      totalAlerts: 0,
      yellowAlerts: 0,
      redAlerts: 0,
      totalSuggestions: 0,
      approvedSuggestions: 0,
      rejectedSuggestions: 0,
    },
    // 알람 사운드 활성화 여부
    audioEnabled: false,
    // 현재 이벤트 필터
    currentLogFilter: 'all',
  };

  /**
   * 수술실 초기 상태 객체 생성
   * @param {number} roomId - 수술실 번호 (1~4)
   */
  function createInitialRoomState(roomId) {
    return {
      roomId,
      alertLevel: 'green',        // 'green' | 'yellow' | 'red'
      isEmergency: false,
      priorityScore: 0.0,
      priorityRank: roomId,
      phase: '유지기',
      latestVitals: {
        map: null,
        hr: null,
        spo2: null,
        bis: null,
        etco2: null,
        sbp: null,
        dbp: null,
        rr: null,
        bt: null,
        pip: null,
      },
      // 스파크라인용 최근 기록 (최대 120개 유지)
      history: {
        map: [],
        hr: [],
        spo2: [],
      },
      // 활성 경보 목록 (Map: alertId -> alertObject)
      activeAlerts: new Map(),
      // TCI 약물 목표치
      drugTargets: {
        propofol_ce: 3.0,
        remifentanil_ce: 3.0,
      },
      // Chart.js 인스턴스
      charts: {
        map: null,
        hr: null,
        spo2: null,
      },
    };
  }

  // ── 2. DOM 요소 캐싱 ──────────────────────────────────────────────────────
  const DOM = {
    // 헤더
    systemTime: document.getElementById('system-time'),
    maxTime: document.getElementById('max-time'),
    timeProgressBar: document.getElementById('time-progress-bar'),
    timeProgressFill: document.getElementById('time-progress-fill'),
    simPausedBadge: document.getElementById('sim-paused-badge'),
    jumpButtons: document.querySelectorAll('.btn-jump'),
    btnPauseToggle: document.getElementById('btn-pause-toggle'),
    pauseIcon: document.getElementById('pause-icon'),
    pauseText: document.getElementById('pause-text'),
    btnResetSim: document.getElementById('btn-reset-sim'),
    connectionStatus: document.getElementById('connection-status'),
    connectionText: document.getElementById('connection-text'),
    speedButtons: document.querySelectorAll('.btn-speed'),
    btnAudioToggle: document.getElementById('btn-audio-toggle'),
    audioIcon: document.getElementById('audio-icon'),
    btnSummaryToggle: document.getElementById('btn-summary-toggle'),

    // 우선순위 패널
    priorityList: document.getElementById('priority-list'),

    // 투약 제안 패널
    suggestionsList: document.getElementById('suggestions-list'),
    suggestionsCountBadge: document.getElementById('suggestions-count-badge'),
    noSuggestionsPlaceholder: document.getElementById('no-suggestions-placeholder'),

    // 이벤트 로그
    eventLogContainer: document.getElementById('event-log-container'),
    eventLogList: document.getElementById('event-log-list'),
    eventLogEmpty: document.getElementById('event-log-empty'),
    logCounter: document.getElementById('log-counter'),
    filterButtons: document.querySelectorAll('.btn-filter'),
    btnClearLog: document.getElementById('btn-clear-log'),

    // 요약 모달
    summaryModal: document.getElementById('summary-modal'),
    btnCloseSummary: document.getElementById('btn-close-summary'),
    btnConfirmSummary: document.getElementById('btn-confirm-summary'),
    btnRefreshSummary: document.getElementById('btn-refresh-summary'),
    kpiMatchRate: document.getElementById('kpi-match-rate'),
    kpiTotalAlerts: document.getElementById('kpi-total-alerts'),
    kpiYellowAlerts: document.getElementById('kpi-yellow-alerts'),
    kpiRedAlerts: document.getElementById('kpi-red-alerts'),
    kpiTotalSuggestions: document.getElementById('kpi-total-suggestions'),
    kpiApprovedSuggestions: document.getElementById('kpi-approved-suggestions'),
    kpiRejectedSuggestions: document.getElementById('kpi-rejected-suggestions'),
    kpiElapsedTime: document.getElementById('kpi-elapsed-time'),
    kpiTicksCount: document.getElementById('kpi-ticks-count'),
    summaryTableBody: document.getElementById('summary-table-body'),

    // 응급 모달
    emergencyModal: document.getElementById('emergency-modal'),
    emergencyRoomLabel: document.getElementById('emergency-room-label'),
    emergencyMessage: document.getElementById('emergency-message'),
    emergencyTime: document.getElementById('emergency-time'),
    btnEmergencyDismiss: document.getElementById('btn-emergency-dismiss'),

    // 토스트
    toastContainer: document.getElementById('toast-container'),
  };

  // ── 3. Web Audio API 사운드 알림 ──────────────────────────────────────────
  let audioCtx = null;

  function initAudioContext() {
    if (!audioCtx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        audioCtx = new AudioContext();
      }
    }
    if (audioCtx && audioCtx.state === 'suspended') {
      audioCtx.resume();
    }
  }

  /**
   * 의료 장비 경보 비프음 합성 재생
   * @param {'info' | 'yellow' | 'red' | 'emergency'} severity 
   */
  function playAlarmSound(severity) {
    if (!state.audioEnabled) return;
    initAudioContext();
    if (!audioCtx) return;

    try {
      const now = audioCtx.currentTime;
      if (severity === 'yellow') {
        // 부드러운 2음 경보 (주의)
        playTone(587.33, now, 0.12, 'sine', 0.2);       // D5
        playTone(880.00, now + 0.15, 0.15, 'sine', 0.2); // A5
      } else if (severity === 'red' || severity === 'emergency') {
        // 긴박한 3음 고주파 경보 (위험)
        playTone(987.77, now, 0.10, 'triangle', 0.35);        // B5
        playTone(987.77, now + 0.14, 0.10, 'triangle', 0.35);
        playTone(987.77, now + 0.28, 0.18, 'triangle', 0.4);
      } else {
        // 단일 알림음 (승인/일반)
        playTone(523.25, now, 0.08, 'sine', 0.15); // C5
      }
    } catch (e) {
      console.warn('Audio playback error:', e);
    }
  }

  function playTone(freq, startTime, duration, type, volume) {
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, startTime);
    gain.gain.setValueAtTime(volume, startTime);
    gain.gain.exponentialRampToValueAtTime(0.001, startTime + duration);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(startTime);
    osc.stop(startTime + duration);
  }

  // ── 4. Chart.js 스파크라인 차트 초기화 ─────────────────────────────────────
  /**
   * 각 수술실 타일의 미니 스파크라인 차트 3종(MAP, HR, SpO2) 초기화
   */
  function initSparklineCharts() {
    // 오프라인 시연 등으로 Chart.js CDN 로드 실패 시에도 대시보드 전체가 멈추지 않도록 보호
    if (typeof Chart === 'undefined') {
      console.warn('[AI-Addict] Chart.js 로드 실패 (오프라인?) — 스파크라인 없이 계속 진행합니다.');
      return;
    }
    const configTemplate = (color, minVal, maxVal) => ({
      type: 'line',
      data: {
        labels: Array(60).fill(''),
        datasets: [{
          data: Array(60).fill(null),
          borderColor: color,
          borderWidth: 1.8,
          pointRadius: 0,
          fill: false,
          tension: 0.35,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false },
        },
        scales: {
          x: { display: false },
          y: {
            display: false,
            suggestedMin: minVal,
            suggestedMax: maxVal,
          }
        }
      }
    });

    for (let r = 1; r <= 4; r++) {
      const room = state.rooms[r];

      // MAP 차트
      const ctxMap = document.getElementById(`chart-${r}-map`)?.getContext('2d');
      if (ctxMap) {
        room.charts.map = new Chart(ctxMap, configTemplate('#ffaa00', 40, 120));
      }

      // HR 차트
      const ctxHr = document.getElementById(`chart-${r}-hr`)?.getContext('2d');
      if (ctxHr) {
        room.charts.hr = new Chart(ctxHr, configTemplate('#00d68f', 40, 130));
      }

      // SpO2 차트
      const ctxSpo2 = document.getElementById(`chart-${r}-spo2`)?.getContext('2d');
      if (ctxSpo2) {
        room.charts.spo2 = new Chart(ctxSpo2, configTemplate('#48dbfb', 75, 100));
      }
    }
  }

  /**
   * 특정 수술실의 스파크라인 차트 갱신
   * @param {number} roomId 
   * @param {'map' | 'hr' | 'spo2'} param 
   * @param {number} value 
   */
  function updateSparkline(roomId, param, value) {
    const room = state.rooms[roomId];
    if (!room) return;

    const history = room.history[param];
    history.push(value);
    if (history.length > 120) {
      history.shift();
    }

    // 최근 60개 포인트를 차트에 바인딩
    const displayData = history.slice(-60);
    const chart = room.charts[param];
    if (chart) {
      chart.data.labels = displayData.map(() => '');
      chart.data.datasets[0].data = displayData;

      // 경보 레벨에 따른 선 색상 동적 반영
      let strokeColor = '#00d68f';
      if (param === 'map') {
        strokeColor = value < 55 ? '#ff3d71' : (value < 65 ? '#ffaa00' : '#7c5cbf');
      } else if (param === 'hr') {
        strokeColor = (value < 40 || value > 120) ? '#ff3d71' : ((value < 50 || value > 100) ? '#ffaa00' : '#00d68f');
      } else if (param === 'spo2') {
        strokeColor = value < 85 ? '#ff3d71' : (value < 90 ? '#ffaa00' : '#00d2d3');
      }
      chart.data.datasets[0].borderColor = strokeColor;
      chart.update('none');
    }

    // 스파크라인 상단 현재값 텍스트 표시
    const sparkValEl = document.getElementById(`spark-val-${roomId}-${param}`);
    if (sparkValEl) {
      sparkValEl.textContent = Math.round(value);
    }
  }

  // ── 5. 실시간 UI 렌더링 헬퍼 ──────────────────────────────────────────────
  /**
   * 포맷된 시간 문자열 (MM:SS) 반환
   * @param {number} seconds 
   */
  function formatTime(seconds) {
    if (seconds == null || isNaN(seconds)) return '00:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }

  /**
   * 토스트 메시지 팝업
   * @param {string} message 
   * @param {'success' | 'danger' | 'info'} type 
   */
  function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    let icon = 'ℹ️';
    if (type === 'success') icon = '✓';
    if (type === 'danger') icon = '⚠️';

    toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;
    DOM.toastContainer.appendChild(toast);

    setTimeout(() => {
      toast.remove();
    }, 3000);
  }

  /**
   * 수술실 타일의 바이탈 수치 및 스타일 갱신
   * @param {number} roomId 
   * @param {Object} vitals 
   * @param {string} alertLevel 
   */
  function renderRoomVitals(roomId, vitals, alertLevel) {
    const room = state.rooms[roomId];
    if (!room || !vitals) return;

    room.latestVitals = { ...room.latestVitals, ...vitals };
    if (alertLevel) room.alertLevel = alertLevel;

    // 1. 핵심 지표 5종 업데이트
    updateVitalCard(roomId, 'map', vitals.map, v => Math.round(v), v => {
      if (v < 55) return 'critical';
      if (v < 65) return 'warning';
      return '';
    });

    updateVitalCard(roomId, 'hr', vitals.hr, v => Math.round(v), v => {
      if (v < 40 || v > 120) return 'critical';
      if (v < 50 || v > 100) return 'warning';
      return '';
    });

    updateVitalCard(roomId, 'spo2', vitals.spo2, v => Math.round(v), v => {
      if (v < 85) return 'critical';
      if (v < 90) return 'warning';
      return '';
    });

    updateVitalCard(roomId, 'bis', vitals.bis, v => Math.round(v), v => {
      if (v < 30 || v > 70) return 'critical';
      if (v < 40 || v > 60) return 'warning';
      return '';
    });

    updateVitalCard(roomId, 'etco2', vitals.etco2, v => Math.round(v), v => {
      if (v > 50 || v < 25) return 'critical';
      if (v > 45 || v < 30) return 'warning';
      return '';
    });

    // 2. 보조 생체 지표 업데이트
    const bpEl = document.getElementById(`val-${roomId}-bp`);
    if (bpEl) {
      const sbp = vitals.sbp != null ? Math.round(vitals.sbp) : '--';
      const dbp = vitals.dbp != null ? Math.round(vitals.dbp) : '--';
      bpEl.textContent = `${sbp} / ${dbp}`;
    }

    const rrEl = document.getElementById(`val-${roomId}-rr`);
    if (rrEl && vitals.rr != null) rrEl.textContent = Math.round(vitals.rr);

    const btEl = document.getElementById(`val-${roomId}-bt`);
    if (btEl && vitals.bt != null) btEl.textContent = Number(vitals.bt).toFixed(1);

    const pipEl = document.getElementById(`val-${roomId}-pip`);
    if (pipEl && vitals.pip != null) pipEl.textContent = Math.round(vitals.pip);

    // 3. TCI 약물 목표치 업데이트 (데이터에 포함된 경우)
    if (vitals.propofol_ce != null) {
      room.drugTargets.propofol_ce = vitals.propofol_ce;
      const el = document.getElementById(`val-${roomId}-propofol`);
      if (el) el.textContent = Number(vitals.propofol_ce).toFixed(1);
    }
    if (vitals.remifentanil_ce != null) {
      room.drugTargets.remifentanil_ce = vitals.remifentanil_ce;
      const el = document.getElementById(`val-${roomId}-remifentanil`);
      if (el) el.textContent = Number(vitals.remifentanil_ce).toFixed(1);
    }

    // 4. 스파크라인 차트 데이터 추가
    if (vitals.map != null) updateSparkline(roomId, 'map', vitals.map);
    if (vitals.hr != null) updateSparkline(roomId, 'hr', vitals.hr);
    if (vitals.spo2 != null) updateSparkline(roomId, 'spo2', vitals.spo2);

    // 5. 방 타일 레벨 스타일 갱신
    updateRoomTileBadgeAndBorder(roomId);
  }

  /**
   * 단일 바이탈 카드 수치 및 이상 색상 클래스 적용
   */
  function updateVitalCard(roomId, param, value, formatter, validator) {
    const card = document.getElementById(`vital-card-${roomId}-${param}`);
    const valEl = document.getElementById(`val-${roomId}-${param}`);
    if (!card || !valEl) return;

    if (value == null || isNaN(value)) {
      valEl.textContent = '--';
      card.classList.remove('warning', 'critical');
      return;
    }

    valEl.textContent = formatter(value);
    const severity = validator(value);
    card.classList.remove('warning', 'critical');
    if (severity) card.classList.add(severity);
  }

  /**
   * 수술실 타일의 테두리 색상 및 상태 뱃지 갱신
   * @param {number} roomId 
   */
  function updateRoomTileBadgeAndBorder(roomId) {
    const room = state.rooms[roomId];
    const tile = document.getElementById(`room-tile-${roomId}`);
    const badge = document.getElementById(`room-badge-${roomId}`);
    if (!tile || !badge) return;

    // 응급 상태가 아닌 경우 타일 클래스 재지정
    if (!room.isEmergency) {
      tile.className = `room-tile ${room.alertLevel}`;
    }

    badge.className = `room-status-badge ${room.alertLevel}`;
    if (room.alertLevel === 'red') {
      badge.textContent = '위험 경보';
    } else if (room.alertLevel === 'yellow') {
      badge.textContent = '주의 경보';
    } else {
      badge.textContent = '정상';
    }
  }

  /**
   * 활성 경보 목록 UI 갱신
   * @param {number} roomId 
   */
  function renderActiveAlerts(roomId) {
    const room = state.rooms[roomId];
    const container = document.getElementById(`alerts-${roomId}`);
    if (!container) return;

    if (room.activeAlerts.size === 0) {
      container.innerHTML = '<span class="no-alerts-msg">✓ 정상 생체징후 유지 중</span>';
      return;
    }

    let html = '';
    room.activeAlerts.forEach((alert) => {
      const isRed = alert.level === 'red';
      const pillClass = isRed ? 'alert-item-pill red' : 'alert-item-pill';
      html += `
        <div class="${pillClass}">
          <span class="alert-msg-text">⚠ ${alert.message || (alert.parameter.toUpperCase() + ' 이상')}</span>
          <span class="alert-val-text">${alert.value != null ? alert.value : ''}</span>
        </div>
      `;
    });
    container.innerHTML = html;
  }

  // ── 6. 우선순위 패널 동적 정렬 (Priority Panel) ───────────────────────────
  /**
   * 우선순위 랭킹 데이터 수신 시 좌측 패널 재정렬 렌더링
   * @param {Array} rankings - [{ room_id, score, level }, ...]
   */
  function updatePriorityRankings(rankings) {
    if (!Array.isArray(rankings)) return;

    // 각 방 점수 반영
    rankings.forEach((item, index) => {
      const room = state.rooms[item.room_id];
      if (room) {
        room.priorityScore = item.score;
        room.priorityRank = index + 1;
        if (item.level) room.alertLevel = item.level;

        // 타일 상단 점수 칩 업데이트
        const scoreChip = document.getElementById(`room-score-${item.room_id}`);
        if (scoreChip) scoreChip.textContent = `위험도: ${item.score.toFixed(1)}`;
      }
    });

    // 랭킹 순서대로 DOM 재배치
    const fragment = document.createDocumentFragment();
    rankings.forEach((item, index) => {
      const rank = index + 1;
      const roomId = item.room_id;
      const room = state.rooms[roomId];
      const level = item.level || (room ? room.alertLevel : 'green');
      const score = item.score != null ? item.score.toFixed(1) : '0.0';

      const card = document.createElement('div');
      card.className = `priority-card rank-${rank}`;
      card.dataset.room = roomId;
      card.id = `priority-card-${roomId}`;

      let levelText = '정상';
      if (level === 'red') levelText = '위험';
      else if (level === 'yellow') levelText = '주의';

      card.innerHTML = `
        <div class="rank-badge">${rank}</div>
        <div class="priority-info">
          <div class="priority-room-title">제 ${roomId} 수술실</div>
          <div class="priority-meta">
            <span class="priority-status-pill ${level}">${levelText}</span>
            <span class="priority-score">점수: ${score}</span>
          </div>
        </div>
      `;

      // 클릭 시 해당 수술실 타일로 스크롤 및 일시 하이라이트
      card.addEventListener('click', () => {
        highlightRoomTile(roomId);
      });

      fragment.appendChild(card);
    });

    DOM.priorityList.innerHTML = '';
    DOM.priorityList.appendChild(fragment);
  }

  /**
   * 특정 수술실 타일로 포커스 및 시각적 피드백
   * @param {number} roomId 
   */
  function highlightRoomTile(roomId) {
    const tile = document.getElementById(`room-tile-${roomId}`);
    if (tile) {
      tile.scrollIntoView({ behavior: 'smooth', block: 'center' });
      tile.style.transform = 'scale(1.02)';
      tile.style.transition = 'transform 0.25s ease';
      setTimeout(() => {
        tile.style.transform = 'scale(1)';
      }, 400);
    }
  }

  // ── 7. 투약 제안 패널 관리 (Drug Suggestions) ─────────────────────────────
  /**
   * 신규 투약 제안 추가
   * @param {Object} suggestion 
   */
  function addDrugSuggestion(suggestion) {
    if (!suggestion || !suggestion.id) return;
    state.pendingSuggestions.set(suggestion.id, suggestion);
    state.stats.totalSuggestions++;
    renderSuggestionsList();

    // 알림음 및 토스트
    playAlarmSound('info');
    const drugName = suggestion.drug_type === 'propofol' ? '프로포폴' : '레미펜타닐';
    const actionName = suggestion.action === 'increase' ? '증량' : '감량';
    showToast(`[OR ${suggestion.room_id}] AI ${drugName} ${actionName} 제안 도착`, 'info');
  }

  /**
   * 투약 제안 목록 렌더링
   */
  function renderSuggestionsList() {
    const count = state.pendingSuggestions.size;
    DOM.suggestionsCountBadge.textContent = `${count}건 대기`;

    if (count === 0) {
      DOM.suggestionsList.innerHTML = '';
      DOM.suggestionsList.appendChild(DOM.noSuggestionsPlaceholder);
      return;
    }

    DOM.suggestionsList.innerHTML = '';
    state.pendingSuggestions.forEach((sug) => {
      const card = document.createElement('div');
      card.className = 'suggestion-card';
      card.id = `suggestion-card-${sug.id}`;

      const isPropofol = sug.drug_type === 'propofol';
      const drugTitle = isPropofol ? '프로포폴 (Propofol Ce)' : '레미펜타닐 (Remifentanil Ce)';
      const unit = isPropofol ? 'mcg/mL' : 'ng/mL';

      let actionClass = 'maintain';
      let actionText = '유지';
      if (sug.action === 'increase') {
        actionClass = 'increase';
        actionText = '증량 ▲';
      } else if (sug.action === 'decrease') {
        actionClass = 'decrease';
        actionText = '감량 ▼';
      }

      const diff = (sug.suggested_value - sug.current_value).toFixed(1);
      const sign = Number(diff) > 0 ? `+${diff}` : diff;

      card.innerHTML = `
        <div class="suggestion-card-header">
          <span class="suggestion-room-badge">OR ${sug.room_id}</span>
          <span class="suggestion-action-tag ${actionClass}">${actionText}</span>
        </div>
        <div class="suggestion-drug-title">${drugTitle}</div>
        <div class="suggestion-target-row">
          <div class="target-change">
            <span class="val-old">${sug.current_value.toFixed(1)}</span>
            <span class="arrow">➔</span>
            <span class="val-new">${sug.suggested_value.toFixed(1)}</span>
            <span style="font-size: 0.7rem; color: var(--text-muted);">${unit}</span>
          </div>
          <span class="target-amount-badge">${sign}</span>
        </div>
        <div class="suggestion-reason">${sug.reason || '활력 징후 목표 범위 유지 필요'}</div>
        <div class="suggestion-actions">
          <button type="button" class="btn-approve" data-id="${sug.id}">
            <span>✓</span> 승인
          </button>
          <button type="button" class="btn-reject" data-id="${sug.id}">
            <span>✕</span> 반려
          </button>
        </div>
      `;

      // 버튼 이벤트 리스너 바인딩
      const btnApprove = card.querySelector('.btn-approve');
      btnApprove.addEventListener('click', () => handleApproveSuggestion(sug.id));

      const btnReject = card.querySelector('.btn-reject');
      btnReject.addEventListener('click', () => handleRejectSuggestion(sug.id));

      DOM.suggestionsList.appendChild(card);
    });
  }

  /**
   * 투약 제안 승인 API 호출 (/api/approve/{id})
   * @param {string} id 
   */
  async function handleApproveSuggestion(id) {
    const sug = state.pendingSuggestions.get(id);
    if (!sug) return;

    try {
      const res = await fetch(`/api/approve/${id}`, { method: 'POST' });
      if (!res.ok) {
        // 서버에서 만료/거부된 제안(예: 응급 전환으로 취소)은 투약 변경을 반영하지 않음
        removeSuggestionFromList(id);
        showToast(`[OR ${sug.room_id}] 만료되었거나 처리할 수 없는 제안입니다.`, 'danger');
        return;
      }
      state.stats.approvedSuggestions++;
      removeSuggestionFromList(id);

      // 로컬 TCI 약물 목표치 즉시 반영
      const room = state.rooms[sug.room_id];
      if (room) {
        if (sug.drug_type === 'propofol') {
          room.drugTargets.propofol_ce = sug.suggested_value;
          const el = document.getElementById(`val-${sug.room_id}-propofol`);
          if (el) el.textContent = sug.suggested_value.toFixed(1);
        } else {
          room.drugTargets.remifentanil_ce = sug.suggested_value;
          const el = document.getElementById(`val-${sug.room_id}-remifentanil`);
          if (el) el.textContent = sug.suggested_value.toFixed(1);
        }
      }

      showToast(`[OR ${sug.room_id}] 투약 변경 승인됨 (${sug.suggested_value})`, 'success');
      playAlarmSound('info');

      // 로그 기록
      addEventLogEntry({
        timestamp: state.system.tick,
        roomId: sug.room_id,
        category: 'drug',
        level: 'green',
        typeText: '투약 승인',
        description: `${sug.drug_type === 'propofol' ? '프로포폴' : '레미펜타닐'} 목표 농도 ${sug.suggested_value} 변경 확정`,
        details: `원인: ${sug.reason}`,
      });
    } catch (err) {
      console.error('Approve API error:', err);
      // 오프라인/데모 환경에서도 로컬 상태는 처리
      state.stats.approvedSuggestions++;
      removeSuggestionFromList(id);
      showToast(`[OR ${sug.room_id}] 투약 변경 승인 처리됨`, 'success');
    }
  }

  /**
   * 투약 제안 반려 API 호출 (/api/reject/{id})
   * @param {string} id 
   */
  async function handleRejectSuggestion(id) {
    const sug = state.pendingSuggestions.get(id);
    if (!sug) return;

    try {
      const rejRes = await fetch(`/api/reject/${id}`, { method: 'POST' });
      if (!rejRes.ok) {
        removeSuggestionFromList(id);
        showToast(`[OR ${sug.room_id}] 만료되었거나 처리할 수 없는 제안입니다.`, 'danger');
        return;
      }
      state.stats.rejectedSuggestions++;
      removeSuggestionFromList(id);
      showToast(`[OR ${sug.room_id}] 투약 변경 반려됨`, 'danger');

      addEventLogEntry({
        timestamp: state.system.tick,
        roomId: sug.room_id,
        category: 'drug',
        level: 'yellow',
        typeText: '투약 반려',
        description: `${sug.drug_type === 'propofol' ? '프로포폴' : '레미펜타닐'} 제안 반려됨 (현재 유지)`,
        details: `원인: ${sug.reason}`,
      });
    } catch (err) {
      console.error('Reject API error:', err);
      state.stats.rejectedSuggestions++;
      removeSuggestionFromList(id);
      showToast(`[OR ${sug.room_id}] 투약 제안 반려 처리됨`, 'danger');
    }
  }

  function removeSuggestionFromList(id) {
    state.pendingSuggestions.delete(id);
    renderSuggestionsList();
  }

  // ── 8. 이벤트 로그 시스템 (Event Log) ──────────────────────────────────────
  /**
   * 이벤트 로그 항목 추가 (최신 이벤트 상단 삽입)
   * @param {Object} entry 
   */
  function addEventLogEntry(entry) {
    const logItem = {
      id: Math.random().toString(36).substr(2, 9),
      timestamp: entry.timestamp != null ? entry.timestamp : state.system.tick,
      roomId: entry.roomId || 1,
      category: entry.category || 'info', // 'alert' | 'drug' | 'emergency' | 'info'
      level: entry.level || 'green',       // 'green' | 'yellow' | 'red' | 'emergency' | 'purple'
      typeText: entry.typeText || '정보',
      description: entry.description || '',
      details: entry.details || '',
    };

    state.eventLogs.unshift(logItem);
    if (state.eventLogs.length > 200) {
      state.eventLogs.pop();
    }

    renderEventLog();
  }

  /**
   * 이벤트 로그 DOM 렌더링 (필터 적용)
   */
  function renderEventLog() {
    DOM.logCounter.textContent = `총 ${state.eventLogs.length}건`;

    // 필터링 적용
    const filtered = state.eventLogs.filter((item) => {
      if (state.currentLogFilter === 'all') return true;
      if (state.currentLogFilter === 'alert') return item.category === 'alert';
      if (state.currentLogFilter === 'drug') return item.category === 'drug';
      if (state.currentLogFilter === 'emergency') return item.category === 'emergency';
      return true;
    });

    if (filtered.length === 0) {
      DOM.eventLogEmpty.style.display = 'flex';
      DOM.eventLogList.innerHTML = '';
      return;
    }

    DOM.eventLogEmpty.style.display = 'none';
    const fragment = document.createDocumentFragment();

    filtered.slice(0, 100).forEach((item) => {
      const row = document.createElement('div');
      row.className = `log-entry level-${item.level}`;

      let badgeCategory = 'info';
      if (item.category === 'alert') badgeCategory = 'alert';
      else if (item.category === 'emergency') badgeCategory = 'emergency';
      else if (item.category === 'drug') badgeCategory = 'drug';

      row.innerHTML = `
        <span class="log-time">${formatTime(item.timestamp)}</span>
        <span class="log-room">OR ${item.roomId}</span>
        <span class="log-type-badge ${badgeCategory}">${item.typeText}</span>
        <span class="log-desc" title="${item.description}">${item.description}</span>
        <span class="log-details">${item.details}</span>
      `;
      fragment.appendChild(row);
    });

    DOM.eventLogList.innerHTML = '';
    DOM.eventLogList.appendChild(fragment);
  }

  // ── 9. 재생 배속 제어 (/api/speed/{n}) ─────────────────────────────────────
  async function setPlaybackSpeed(speedMultiplier) {
    try {
      await fetch(`/api/speed/${speedMultiplier}`, { method: 'POST' });
      applySpeedButtonState(speedMultiplier);
      showToast(`재생 배속: ${speedMultiplier}x 설정`, 'info');
    } catch (err) {
      console.warn('Speed API request failed, applying locally:', err);
      applySpeedButtonState(speedMultiplier);
    }
  }

  function applySpeedButtonState(speed) {
    state.system.speed = speed;
    DOM.speedButtons.forEach((btn) => {
      const btnSpeed = Number(btn.dataset.speed);
      if (btnSpeed === speed) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });
  }

  // ── 9-B. 시연용 타임라인 제어 (Pause / Seek / Reset) ────────────────────────
  function applyPausedState(paused) {
    state.system.paused = Boolean(paused);
    if (DOM.btnPauseToggle) {
      DOM.btnPauseToggle.classList.toggle('paused', state.system.paused);
    }
    if (DOM.pauseIcon) {
      DOM.pauseIcon.textContent = state.system.paused ? '▶' : '⏸';
    }
    if (DOM.pauseText) {
      DOM.pauseText.textContent = state.system.paused ? '재개' : '일시정지';
    }
    if (DOM.simPausedBadge) {
      DOM.simPausedBadge.classList.toggle('hidden', !state.system.paused);
    }
  }

  async function togglePauseSimulation() {
    try {
      const res = await fetch('/api/pause', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        applyPausedState(data.paused);
        showToast(data.paused ? '시뮬레이션 일시정지됨' : '시뮬레이션 재생 재개됨', 'info');
      }
    } catch (err) {
      console.warn('Pause API error:', err);
    }
  }

  // 연속 클릭으로 인한 중복 요청 방지 플래그
  let timelineRequestBusy = false;

  async function seekSimulation(targetTick) {
    if (timelineRequestBusy) return;
    timelineRequestBusy = true;
    try {
      const res = await fetch(`/api/seek/${targetTick}`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        showToast(`타임라인 이동: ${formatTime(data.tick)} (${data.tick}초)`, 'info');
      } else {
        showToast('타임라인 이동에 실패했습니다.', 'danger');
      }
    } catch (err) {
      console.warn('Seek API error:', err);
      showToast('서버에 연결할 수 없습니다.', 'danger');
    } finally {
      timelineRequestBusy = false;
    }
  }

  async function resetSimulation() {
    // 발표 중 오클릭으로 전체 기록이 지워지는 사고 방지
    if (!window.confirm('시뮬레이션을 00:00으로 초기화합니다.\n경보·투약 기록과 통계가 모두 지워집니다. 계속할까요?')) {
      return;
    }
    if (timelineRequestBusy) return;
    timelineRequestBusy = true;
    try {
      const res = await fetch('/api/reset', { method: 'POST' });
      // 화면 초기화는 WebSocket의 system(reset) 이벤트에서 1회만 수행합니다.
      // (여기서 다시 초기화하면 이미 도착한 00:00 데이터가 지워짐)
      if (res.ok) {
        showToast('시뮬레이션이 00:00으로 초기화되었습니다.', 'info');
      } else {
        showToast('초기화에 실패했습니다.', 'danger');
      }
    } catch (err) {
      console.warn('Reset API error:', err);
      showToast('서버에 연결할 수 없습니다.', 'danger');
    } finally {
      timelineRequestBusy = false;
    }
  }

  /**
   * 리셋 또는 타임라인 점프 시 클라이언트 UI 및 메모리 상태를 정리합니다.
   * @param {boolean} isFullReset - true면 누적 통계, 로그, 약물 농도까지 완전 초기화
   */
  function resetClientState(isFullReset = true) {
    state.pendingSuggestions.clear();
    renderSuggestionsList();
    DOM.emergencyModal.classList.add('hidden');

    for (let r = 1; r <= 4; r++) {
      const room = state.rooms[r];
      room.alertLevel = 'green';
      room.isEmergency = false;
      room.priorityScore = 0.0;
      room.priorityRank = r;
      room.activeAlerts.clear();
      room.history.map = [];
      room.history.hr = [];
      room.history.spo2 = [];

      if (isFullReset) {
        room.drugTargets.propofol_ce = 3.0;
        room.drugTargets.remifentanil_ce = 3.0;
        const pEl = document.getElementById(`val-${r}-propofol`);
        if (pEl) pEl.textContent = '3.0';
        const rEl = document.getElementById(`val-${r}-remifentanil`);
        if (rEl) rEl.textContent = '3.0';
      }

      ['map', 'hr', 'spo2'].forEach((param) => {
        const chart = room.charts[param];
        if (chart) {
          chart.data.labels = Array(60).fill('');
          chart.data.datasets[0].data = Array(60).fill(null);
          chart.update('none');
        }
      });

      renderActiveAlerts(r);
      updateRoomTileBadgeAndBorder(r);
    }

    updatePriorityRankings([1, 2, 3, 4].map((rid) => ({
      room_id: rid,
      score: 0.0,
      level: 'green',
    })));

    if (isFullReset) {
      state.stats.totalAlerts = 0;
      state.stats.yellowAlerts = 0;
      state.stats.redAlerts = 0;
      state.stats.totalSuggestions = 0;
      state.stats.approvedSuggestions = 0;
      state.stats.rejectedSuggestions = 0;
      state.eventLogs = [];
      renderEventLog();
      applyPausedState(false);
    }
  }

  // ── 10. 응급 상황 핸들링 (Emergency Handling) ──────────────────────────────
  /**
   * 응급 상황 메시지 수신 시 처리
   * @param {Object} data - { room_id, message }
   */
  function handleEmergency(data) {
    const roomId = data.room_id || 1;
    const room = state.rooms[roomId];
    if (room) {
      room.isEmergency = true;
      room.alertLevel = 'red';
    }

    // 1. 해당 수술실 타일 애니메이션 펄스 발동
    const tile = document.getElementById(`room-tile-${roomId}`);
    if (tile) {
      tile.className = 'room-tile emergency';
    }

    // 2. 응급 모달 팝업
    DOM.emergencyRoomLabel.textContent = `제 ${roomId} 수술실 (OR ${roomId})`;
    DOM.emergencyMessage.textContent = data.message || '심각한 생체징후 위험 감지!';
    DOM.emergencyTime.textContent = `발생 시각: ${formatTime(state.system.tick)}`;
    DOM.emergencyModal.classList.remove('hidden');

    // 3. 고주파 긴급 사이렌 음향
    playAlarmSound('emergency');

    // 4. 이벤트 로그 추가
    addEventLogEntry({
      timestamp: state.system.tick,
      roomId: roomId,
      category: 'emergency',
      level: 'emergency',
      typeText: '🚨 응급 상황',
      description: data.message || '위험 수준 임계치 초과',
      details: '즉각 조치 요망',
    });
  }

  /**
   * 응급 상황 인지 및 모달 해제
   */
  function dismissEmergencyModal() {
    DOM.emergencyModal.classList.add('hidden');
    // 모든 방의 긴급 펄스 해제하고 현재 레벨로 복귀
    for (let r = 1; r <= 4; r++) {
      state.rooms[r].isEmergency = false;
      updateRoomTileBadgeAndBorder(r);
    }
    showToast('응급 경보 상황 인지 완료', 'info');
  }

  // ── 11. 종합 통계 요약 오버레이 (Summary Overlay) ─────────────────────────
  async function openSummaryModal() {
    DOM.summaryModal.classList.remove('hidden');
    await fetchSummaryData();
  }

  function closeSummaryModal() {
    DOM.summaryModal.classList.add('hidden');
  }

  async function fetchSummaryData() {
    try {
      const res = await fetch('/api/summary');
      if (res.ok) {
        const data = await res.json();
        renderSummaryData(data);
      } else {
        renderSummaryFromClientState();
      }
    } catch (err) {
      console.warn('Summary API not available, computing from client state:', err);
      renderSummaryFromClientState();
    }
  }

  /**
   * 통계 API 응답을 기반으로 모달 데이터 렌더링
   */
  function renderSummaryData(rawData) {
    if (!rawData) return;
    const data = rawData.summary || rawData;

    // 일치율(수용률) 계산
    const totalSugs = data.total_suggestions ?? state.stats.totalSuggestions ?? 0;
    const approvedSugs = data.approved_suggestions ?? state.stats.approvedSuggestions ?? 0;
    const matchRate = data.overall_match_rate != null && totalSugs > 0
      ? Math.round(data.overall_match_rate * 100)
      : (totalSugs > 0 ? Math.round((approvedSugs / totalSugs) * 100) : 100);

    DOM.kpiMatchRate.textContent = matchRate;
    DOM.kpiTotalAlerts.textContent = data.total_alerts ?? state.stats.totalAlerts;
    DOM.kpiYellowAlerts.textContent = `주의 ${data.yellow_alerts ?? state.stats.yellowAlerts}건`;
    DOM.kpiRedAlerts.textContent = `위험 ${data.red_alerts ?? state.stats.redAlerts}건`;

    DOM.kpiTotalSuggestions.textContent = totalSugs;
    DOM.kpiApprovedSuggestions.textContent = `승인 ${approvedSugs}`;
    DOM.kpiRejectedSuggestions.textContent = `반려 ${data.rejected_suggestions ?? state.stats.rejectedSuggestions}`;

    DOM.kpiElapsedTime.textContent = formatTime(data.elapsed_seconds ?? state.system.tick);
    DOM.kpiTicksCount.textContent = `${state.system.tick} / ${state.system.maxTicks} 틱 완료`;

    // 테이블 렌더링
    renderSummaryTable(data.rooms);
  }

  /**
   * 클라이언트 로컬 상태 기반 통계 요약 계산 (Fallback)
   */
  function renderSummaryFromClientState() {
    const totalSugs = state.stats.totalSuggestions;
    const approved = state.stats.approvedSuggestions;
    const matchRate = totalSugs > 0 ? Math.round((approved / totalSugs) * 100) : 100;

    DOM.kpiMatchRate.textContent = matchRate;
    DOM.kpiTotalAlerts.textContent = state.stats.totalAlerts;
    DOM.kpiYellowAlerts.textContent = `주의 ${state.stats.yellowAlerts}건`;
    DOM.kpiRedAlerts.textContent = `위험 ${state.stats.redAlerts}건`;

    DOM.kpiTotalSuggestions.textContent = totalSugs;
    DOM.kpiApprovedSuggestions.textContent = `승인 ${approved}`;
    DOM.kpiRejectedSuggestions.textContent = `반려 ${state.stats.rejectedSuggestions}`;

    DOM.kpiElapsedTime.textContent = formatTime(state.system.tick);
    DOM.kpiTicksCount.textContent = `${state.system.tick} / ${state.system.maxTicks} 틱 완료`;

    renderSummaryTable();
  }

  function renderSummaryTable(roomsData) {
    let rowsHtml = '';
    for (let r = 1; r <= 4; r++) {
      const room = state.rooms[r];
      const v = room.latestVitals;
      const mapStr = v.map != null ? Math.round(v.map) : '--';
      const hrStr = v.hr != null ? Math.round(v.hr) : '--';
      const spo2Str = v.spo2 != null ? `${Math.round(v.spo2)}%` : '--';
      const bisStr = v.bis != null ? Math.round(v.bis) : '--';

      let statusBadge = '<span class="kpi-tag green">정상</span>';
      if (room.alertLevel === 'red') statusBadge = '<span class="kpi-tag red">위험</span>';
      else if (room.alertLevel === 'yellow') statusBadge = '<span class="kpi-tag yellow">주의</span>';

      rowsHtml += `
        <tr>
          <td><strong>OR ${r}</strong> (수술실 ${r})</td>
          <td>${statusBadge}</td>
          <td>${mapStr} mmHg</td>
          <td>${hrStr} bpm</td>
          <td>${spo2Str}</td>
          <td>${bisStr}</td>
          <td>${room.activeAlerts.size}건 활성</td>
          <td>Ce P:${room.drugTargets.propofol_ce} / R:${room.drugTargets.remifentanil_ce}</td>
          <td>순위 ${room.priorityRank}위 (${room.priorityScore.toFixed(1)}점)</td>
        </tr>
      `;
    }
    DOM.summaryTableBody.innerHTML = rowsHtml;
  }

  // ── 12. 웹소켓 연결 및 프로토콜 파싱 (WebSocket Protocol) ───────────────────
  let ws = null;
  let reconnectAttempts = 0;
  const BASE_RECONNECT_DELAY = 1000;
  const MAX_RECONNECT_DELAY = 10000;

  function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = location.host || 'localhost:8000';
    const wsUrl = `${protocol}//${host}/ws`;

    updateConnectionStatus('connecting');

    try {
      ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        console.log('[WebSocket] Connected successfully to', wsUrl);
        state.system.connected = true;
        reconnectAttempts = 0;
        updateConnectionStatus('connected');
        showToast('실시간 모니터링 서버에 연결되었습니다.', 'success');
      };

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          handleWebSocketMessage(payload);
        } catch (err) {
          console.error('[WebSocket] Message parsing error:', err, event.data);
        }
      };

      ws.onerror = (err) => {
        console.warn('[WebSocket] Connection error:', err);
      };

      ws.onclose = () => {
        console.warn('[WebSocket] Disconnected. Reconnecting...');
        state.system.connected = false;
        updateConnectionStatus('disconnected');
        scheduleReconnect();
      };
    } catch (err) {
      console.error('[WebSocket] Init failed:', err);
      scheduleReconnect();
    }
  }

  function scheduleReconnect() {
    reconnectAttempts++;
    const delay = Math.min(BASE_RECONNECT_DELAY * Math.pow(1.5, reconnectAttempts - 1), MAX_RECONNECT_DELAY);
    DOM.connectionText.textContent = `재연결 중 (${Math.round(delay / 1000)}s)...`;
    setTimeout(() => {
      connectWebSocket();
    }, delay);
  }

  function updateConnectionStatus(status) {
    DOM.connectionStatus.className = `connection-status-pill ${status}`;
    if (status === 'connected') {
      DOM.connectionText.textContent = '실시간 연동됨';
    } else if (status === 'disconnected') {
      DOM.connectionText.textContent = '연결 끊김';
    } else {
      DOM.connectionText.textContent = '연결 대기중...';
    }
  }

  /**
   * WebSocket 수신 메시지 타입별 디스패처
   * @param {Object} msg - { type, data }
   */
  function handleWebSocketMessage(msg) {
    if (!msg || !msg.type) return;

    const { type, data } = msg;

    switch (type) {
      // 1. 1초 주기 바이탈 사인 갱신
      case 'vital_update': {
        const roomId = data.room_id;
        const vitals = data.vitals || data;
        const alertLevel = data.alert_level || (state.rooms[roomId] ? state.rooms[roomId].alertLevel : 'green');
        renderRoomVitals(roomId, vitals, alertLevel);
        break;
      }

      // 2. 경보 발생 (Alert)
      case 'alert': {
        const roomId = data.room_id;
        const room = state.rooms[roomId];
        if (room) {
          // 같은 파라미터의 기존 경보(예: YELLOW → RED 격상)는 교체하여 잔존 경보 방지
          room.activeAlerts.forEach((a, key) => {
            if (a.parameter === data.parameter) room.activeAlerts.delete(key);
          });
          room.activeAlerts.set(data.id, data);
          room.alertLevel = data.level || 'yellow';
          renderActiveAlerts(roomId);
          updateRoomTileBadgeAndBorder(roomId);
        }

        // 통계 집계
        state.stats.totalAlerts++;
        if (data.level === 'red') state.stats.redAlerts++;
        else state.stats.yellowAlerts++;

        // 경보음 재생
        playAlarmSound(data.level || 'yellow');

        // 이벤트 로그 기록
        addEventLogEntry({
          timestamp: state.system.tick,
          roomId: roomId,
          category: 'alert',
          level: data.level || 'yellow',
          typeText: data.level === 'red' ? '위험 경보' : '주의 경보',
          description: data.message || `${(data.parameter || '').toUpperCase()} 이상 감지`,
          details: `측정치: ${data.value != null ? data.value : '--'}`,
        });
        break;
      }

      // 3. 경보 해제 (Alert Cleared)
      case 'alert_cleared': {
        const roomId = data.room_id;
        const alertId = data.id || data.alert_id;
        const room = state.rooms[roomId];
        if (room) {
          if (alertId) room.activeAlerts.delete(alertId);
          // 남은 경보가 없으면 정상(green)으로 복귀
          if (room.activeAlerts.size === 0) {
            room.alertLevel = 'green';
          } else {
            // 남은 경보 중 가장 심각한 레벨 선정
            let maxLevel = 'green';
            room.activeAlerts.forEach((a) => {
              if (a.level === 'red') maxLevel = 'red';
              else if (a.level === 'yellow' && maxLevel !== 'red') maxLevel = 'yellow';
            });
            room.alertLevel = maxLevel;
          }
          renderActiveAlerts(roomId);
          updateRoomTileBadgeAndBorder(roomId);
        }

        addEventLogEntry({
          timestamp: state.system.tick,
          roomId: roomId,
          category: 'info',
          level: 'green',
          typeText: '경보 해제',
          description: data.message || '생체징후 정상 범위 회복',
          details: alertId ? `ID: ${alertId}` : '',
        });
        break;
      }

      // 4. 투약 제안 수신 (Drug Suggestion)
      case 'drug_suggestion': {
        addDrugSuggestion(data);
        break;
      }

      // 5. 우선순위 순위 갱신 (Priority Update)
      case 'priority_update': {
        if (data && data.rankings) {
          updatePriorityRankings(data.rankings);
        } else if (data && Array.isArray(data.rooms)) {
          updatePriorityRankings(data.rooms.map((r) => ({
            room_id: r.room_id,
            score: r.priority_score || 0.0,
            level: r.alert_level || 'green',
          })));
        }
        break;
      }

      // 6. 응급 상황 (Emergency & Emergency Cleared)
      case 'emergency': {
        handleEmergency(data);
        break;
      }

      case 'emergency_cleared': {
        const roomId = data.room_id;
        if (roomId && state.rooms[roomId]) {
          state.rooms[roomId].isEmergency = false;
          updateRoomTileBadgeAndBorder(roomId);
        }
        addEventLogEntry({
          timestamp: state.system.tick,
          roomId: roomId || 1,
          category: 'emergency',
          level: 'green',
          typeText: '응급 해제',
          description: data.message || '응급 상태 해제 및 정상 모드 복귀',
          details: '',
        });
        break;
      }

      // 7. 방 초기 상태 동기화 및 승인 응답
      case 'room_status': {
        const roomId = data.room_id;
        if (roomId && state.rooms[roomId]) {
          const room = state.rooms[roomId];
          if (data.propofol_ce != null) room.drugTargets.propofol_ce = data.propofol_ce;
          if (data.remifentanil_ce != null) room.drugTargets.remifentanil_ce = data.remifentanil_ce;
          if (data.vitals) renderRoomVitals(roomId, data.vitals, data.alert_level || 'green');
        }
        break;
      }

      case 'approval_response': {
        const sugId = data.suggestion_id;
        if (sugId && state.pendingSuggestions.has(sugId)) {
          removeSuggestionFromList(sugId);
        }
        break;
      }

      // 7. 기록 항목 (Record)
      case 'record': {
        if (data && Array.isArray(data.entries)) {
          data.entries.forEach((e) => {
            addEventLogEntry({
              timestamp: e.timestamp,
              roomId: e.room_id,
              category: 'info',
              level: 'purple',
              typeText: e.event_type || '기록',
              description: e.description || '',
              details: '',
            });
          });
        }
        break;
      }

      // 8. 시스템 상태 (System: tick, speed, max_ticks, paused, reset, seek)
      case 'system': {
        if (data.reset) {
          resetClientState(true);
        } else if (data.seek) {
          resetClientState(false);
          addEventLogEntry({
            timestamp: data.tick ?? state.system.tick,
            roomId: 1,
            category: 'info',
            level: 'purple',
            typeText: '구간 이동',
            description: `시연 타임라인 이동 (${formatTime(data.tick ?? 0)})`,
            details: '',
          });
        }

        if (data.paused != null) {
          applyPausedState(data.paused);
        }

        if (data.tick != null) {
          state.system.tick = data.tick;
          DOM.systemTime.textContent = formatTime(data.tick);

          const maxTicks = data.max_ticks || state.system.maxTicks;
          state.system.maxTicks = maxTicks;
          DOM.maxTime.textContent = `/ ${formatTime(maxTicks)}`;

          const pct = Math.min((data.tick / maxTicks) * 100, 100);
          DOM.timeProgressFill.style.width = `${pct}%`;
        }

        if (data.speed != null && data.speed !== state.system.speed) {
          applySpeedButtonState(data.speed);
        }
        break;
      }

      default:
        console.log('[WebSocket] Unhandled message type:', type, data);
    }
  }

  // ── 13. UI 이벤트 리스너 바인딩 ───────────────────────────────────────────
  function setupEventListeners() {
    // 1. 배속 제어 버튼들
    DOM.speedButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        const speed = Number(btn.dataset.speed);
        setPlaybackSpeed(speed);
      });
    });

    // 1-B. 일시정지/재개, 리셋, 시나리오 점프, 타임라인 프로그레스 바 클릭 탐색
    if (DOM.btnPauseToggle) {
      DOM.btnPauseToggle.addEventListener('click', togglePauseSimulation);
    }
    if (DOM.btnResetSim) {
      DOM.btnResetSim.addEventListener('click', resetSimulation);
    }
    DOM.jumpButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        const targetTick = Number(btn.dataset.tick);
        if (!isNaN(targetTick)) {
          seekSimulation(targetTick);
        }
      });
    });
    if (DOM.timeProgressBar) {
      DOM.timeProgressBar.addEventListener('click', (e) => {
        const rect = DOM.timeProgressBar.getBoundingClientRect();
        if (rect.width <= 0) return;
        const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        const targetTick = Math.floor(ratio * state.system.maxTicks);
        seekSimulation(targetTick);
      });
    }

    // 2. 음향 토글
    DOM.btnAudioToggle.addEventListener('click', () => {
      state.audioEnabled = !state.audioEnabled;
      initAudioContext();
      if (state.audioEnabled) {
        DOM.audioIcon.textContent = '🔊';
        DOM.btnAudioToggle.style.borderColor = 'var(--accent-purple-light)';
        DOM.btnAudioToggle.title = '경보 음향 토글 (현재: 켜짐)';
        showToast('알람 음향이 활성화되었습니다.', 'info');
        playAlarmSound('info');
      } else {
        DOM.audioIcon.textContent = '🔇';
        DOM.btnAudioToggle.style.borderColor = 'var(--border-subtle)';
        DOM.btnAudioToggle.title = '경보 음향 토글 (현재: 꺼짐)';
        showToast('알람 음향이 음소거되었습니다.', 'info');
      }
    });

    // 3. 통계 요약 팝업
    DOM.btnSummaryToggle.addEventListener('click', openSummaryModal);
    DOM.btnCloseSummary.addEventListener('click', closeSummaryModal);
    DOM.btnConfirmSummary.addEventListener('click', closeSummaryModal);
    DOM.btnRefreshSummary.addEventListener('click', fetchSummaryData);

    DOM.summaryModal.addEventListener('click', (e) => {
      if (e.target === DOM.summaryModal) closeSummaryModal();
    });

    // 4. 응급 모달 해제
    DOM.btnEmergencyDismiss.addEventListener('click', dismissEmergencyModal);

    // 5. 이벤트 로그 필터 버튼들
    DOM.filterButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        DOM.filterButtons.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        state.currentLogFilter = btn.dataset.filter || 'all';
        renderEventLog();
      });
    });

    // 6. 로그 지우기
    DOM.btnClearLog.addEventListener('click', () => {
      state.eventLogs = [];
      renderEventLog();
      showToast('이벤트 로그가 초기화되었습니다.', 'info');
    });

    // 7. 키보드 단축키: ESC(모달 닫기), Space(일시정지/재개)
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if (!DOM.summaryModal.classList.contains('hidden')) {
          closeSummaryModal();
        }
        if (!DOM.emergencyModal.classList.contains('hidden')) {
          dismissEmergencyModal();
        }
      }

      // 버튼/입력창에 포커스가 있으면 Space가 해당 요소를 클릭하므로 중복 토글 방지
      if (e.code === 'Space' && !e.repeat && !e.target.closest('button, input, textarea, select')) {
        e.preventDefault();
        togglePauseSimulation();
      }
    });
  }

  // ── 14. 애플리케이션 부트스트랩 ───────────────────────────────────────────
  function initApp() {
    console.log('[AI-Addict] Dashboard App initializing...');
    initSparklineCharts();
    setupEventListeners();
    connectWebSocket();

    // 초기 시스템 시간 표시
    DOM.systemTime.textContent = '00:00';
    DOM.maxTime.textContent = '/ 16:00';

    console.log('[AI-Addict] Dashboard App initialized successfully.');
  }

  // DOM 로드 완료 후 실행
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initApp);
  } else {
    initApp();
  }

})();
