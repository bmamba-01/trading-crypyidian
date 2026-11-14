// Cập nhật KPI và bảng bot mỗi 5 giây; tách khỏi template để tuân thủ CSP `default-src 'self'`.
(function() {
  'use strict';

  var pollIntervalMs = 5000;
  var timerId = null;
  var isPaused = false;
  var isFetching = false;
  var currentDashboardFp = null;

  var toggleBtn = document.getElementById('toggle-realtime-btn');
  var pulseDot = document.getElementById('live-pulse-dot');
  var timeEl = document.getElementById('live-updated-at');
  var statusMsgEl = document.getElementById('live-status-msg');
  var kpiBotCount = document.getElementById('kpi-bot-count');
  var kpiReadyCount = document.getElementById('kpi-ready-count');
  var kpiKillStatus = document.getElementById('kpi-kill-status');
  var kpiKillState = document.getElementById('kpi-kill-state');
  var kpiRevision = document.getElementById('kpi-workspace-revision');
  var releaseRevisionInput = document.getElementById('workspace-release-revision');
  var labelsEl = document.getElementById('dashboard-status-labels');
  var statusLabels = labelsEl ? JSON.parse(labelsEl.textContent) : {};

  function labelVi(value) {
    var key = String(value || '');
    return Object.prototype.hasOwnProperty.call(statusLabels, key)
      ? statusLabels[key]
      : 'Chưa xác định';
  }

  function updateDashboard(data) {
    if (!data) return;

    // 1. Cập nhật thời gian
    if (data.server_time && data.server_time.vn_time) {
      if (timeEl) {
        timeEl.textContent = data.server_time.vn_time;
        timeEl.setAttribute('datetime', data.server_time.iso || '');
      }
    }

    if (data.status === 'unchanged') {
      if (pulseDot) {
        pulseDot.className = 'pulse-dot';
      }
      return;
    }
    if (data.fingerprint) {
      currentDashboardFp = data.fingerprint;
    }

    // 2. Cập nhật KPI số lượng bot
    if (typeof data.bot_count === 'number' && kpiBotCount) {
      kpiBotCount.textContent = String(data.bot_count);
    }
    if (typeof data.ready_count === 'number' && kpiReadyCount) {
      kpiReadyCount.textContent = String(data.ready_count);
    }

    // 3. Cập nhật Workspace kill status
    if (data.workspace && kpiKillStatus && kpiKillState) {
      var killState = data.workspace.kill_state;
      var killLabel = labelVi(killState);
      var killClass = 'badge badge-warning';
      if (killState === 'ACTIVE') {
        killLabel = 'Đang hoạt động';
        killClass = 'badge badge-success';
      } else if (killState === 'KILLED') {
        killLabel = 'Đã dừng khẩn cấp';
        killClass = 'badge badge-danger';
      }
      kpiKillState.textContent = killLabel;
      kpiKillState.className = killClass;

      if (typeof data.workspace.revision !== 'undefined') {
        if (kpiRevision) {
          kpiRevision.textContent = 'Lần thay đổi: ' + data.workspace.revision;
        }
        if (releaseRevisionInput) {
          releaseRevisionInput.value = String(data.workspace.revision);
        }
      }
    }

    // 4. Cập nhật danh sách bot
    if (Array.isArray(data.bots)) {
      data.bots.forEach(function(bot) {
        if (!bot || !bot.id) return;
        var row = document.querySelector('tr[data-bot-id="' + bot.id + '"]');
        if (row) {
          var statusEl = row.querySelector('[data-bot-field="status"]');
          if (statusEl && bot.status) {
            statusEl.textContent = labelVi(bot.status);
          }
          var readinessEl = row.querySelector('[data-bot-field="readiness"]');
          if (readinessEl && bot.readiness) {
            readinessEl.textContent = labelVi(bot.readiness);
            readinessEl.className = 'badge ' + (bot.readiness === 'READY' ? 'badge-success' : 'badge-warning');
          }
          var entryEl = row.querySelector('[data-bot-field="entry"]');
          if (entryEl && typeof bot.entry_allowed === 'boolean') {
            entryEl.textContent = bot.entry_allowed ? 'Cho phép' : 'Chặn';
            entryEl.className = 'status-indicator ' + (bot.entry_allowed ? 'status-allowed' : 'status-blocked');
          }
        }
      });
    }

    // Phục hồi trạng thái kết nối bình thường nếu trước đó có lỗi
    if (statusMsgEl) {
      statusMsgEl.textContent = '';
      statusMsgEl.className = 'live-status-msg';
    }
    if (pulseDot) {
      pulseDot.className = 'pulse-dot';
    }
  }

  function fetchDashboardData() {
    if (isPaused || isFetching) return;
    isFetching = true;

    var url = currentDashboardFp ? '/admin/api/dashboard/realtime?fp=' + encodeURIComponent(currentDashboardFp) : '/admin/api/dashboard/realtime';
    fetch(url, {
      method: 'GET',
      headers: {
        'Accept': 'application/json'
      },
      cache: 'no-store'
    })
    .then(function(response) {
      if (response.status === 401) {
        stopPolling();
        if (toggleBtn) {
          toggleBtn.disabled = true;
        }
        if (pulseDot) {
          pulseDot.className = 'pulse-dot pulse-error';
        }
        if (statusMsgEl) {
          statusMsgEl.textContent = 'Phiên đăng nhập đã hết hạn. Vui lòng tải lại trang.';
          statusMsgEl.className = 'live-status-msg live-msg-error';
        }
        return null;
      }
      if (!response.ok) {
        throw new Error('HTTP ' + response.status);
      }
      return response.json();
    })
    .then(function(data) {
      if (data) {
        updateDashboard(data);
      }
    })
    .catch(function(err) {
      if (pulseDot) {
        pulseDot.className = 'pulse-dot pulse-warning';
      }
      if (statusMsgEl) {
        statusMsgEl.textContent = 'Mất kết nối — Đang thử lại...';
        statusMsgEl.className = 'live-status-msg live-msg-warning';
      }
    })
    .finally(function() {
      isFetching = false;
    });
  }

  function startPolling() {
    isPaused = false;
    if (timerId) clearInterval(timerId);
    timerId = setInterval(fetchDashboardData, pollIntervalMs);
    if (pulseDot) {
      pulseDot.className = 'pulse-dot';
    }
    if (toggleBtn) {
      toggleBtn.setAttribute('aria-pressed', 'true');
      toggleBtn.textContent = 'Tạm dừng';
    }
    if (statusMsgEl) {
      statusMsgEl.textContent = '';
      statusMsgEl.className = 'live-status-msg';
    }
  }

  function stopPolling() {
    isPaused = true;
    if (timerId) {
      clearInterval(timerId);
      timerId = null;
    }
    if (pulseDot) {
      pulseDot.className = 'pulse-dot pulse-paused';
    }
    if (toggleBtn) {
      toggleBtn.setAttribute('aria-pressed', 'false');
      toggleBtn.textContent = 'Tiếp tục';
    }
    if (statusMsgEl) {
      statusMsgEl.textContent = 'Đã tạm dừng tự động cập nhật';
      statusMsgEl.className = 'live-status-msg';
    }
  }

  if (toggleBtn) {
    toggleBtn.addEventListener('click', function() {
      if (isPaused) {
        startPolling();
        fetchDashboardData();
      } else {
        stopPolling();
      }
    });
  }

  window.addEventListener('beforeunload', function() {
    if (timerId) clearInterval(timerId);
  });

  // Khởi động chu kỳ polling
  startPolling();
  // Gọi lần đầu để cập nhật thời gian
  fetchDashboardData();
})();
