// LIVE-MON (CRY-429): màn hình theo dõi bot trực tiếp, chỉ đọc.
// Nhận dữ liệu qua Server-Sent Events (/admin/api/live/stream); nếu trình duyệt/proxy không giữ
// được luồng thì chuyển sang poll /admin/api/live/snapshot và định kỳ thử lại SSE.
// Không có thao tác điều khiển nào ở đây: kill/flatten vẫn qua form có CSRF + MFA.
(function () {
  'use strict';

  const root = document.querySelector('[data-live-monitor]');
  if (!root) return;

  const FEED_MAX = 300;
  const ALERT_MAX = 5;
  const SSE_FAILURES_BEFORE_POLL = 3;
  const SSE_RETRY_FROM_POLL_MS = 60000;
  const pollMs = Number(root.dataset.pollMs) || 4000;
  const botIds = (root.dataset.botIds || '').split(',').filter(Boolean);
  const nf = new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 8 });
  const nf2 = new Intl.NumberFormat('vi-VN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const $ = (selector) => root.querySelector(selector);
  const conn = $('[data-live-connection]');
  const connText = $('[data-live-connection-text]');
  const feed = $('[data-live-feed]');
  const feedEmpty = $('[data-live-feed-empty]');
  const alerts = $('[data-live-alerts]');
  const showMuted = $('[data-live-show-muted]');
  const soundToggle = $('[data-live-sound]');

  const seen = new Set();
  let cursor = null;
  let source = null;
  let sseFailures = 0;
  let pollTimer = null;
  let retryTimer = null;
  let pollInFlight = false;
  let audioContext = null;
  const botNames = new Map();

  function query(extra) {
    const params = new URLSearchParams();
    botIds.forEach((id) => params.append('bot', id));
    Object.entries(extra || {}).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    return params.toString();
  }

  function setConnection(state, text) {
    conn.dataset.state = state;
    connText.textContent = text;
  }

  function num(value, fixed) {
    if (value === null || value === undefined || value === '') return '—';
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return String(value);
    return fixed ? nf2.format(parsed) : nf.format(parsed);
  }

  function tone(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed === 0) return '';
    return parsed > 0 ? 'pnl-pos' : 'pnl-neg';
  }

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    const s = Math.max(0, Math.floor(seconds));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (d) return `${d}n ${h}g`;
    if (h) return `${h}g ${m}p`;
    return `${m}p ${String(sec).padStart(2, '0')}s`;
  }

  function timeText(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleTimeString('vi-VN', { hour12: false });
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      if (key === 'text') node.textContent = value;
      else if (key === 'className') node.className = value;
      else node.setAttribute(key, value);
    });
    (children || []).forEach((child) => node.append(child));
    return node;
  }

  function beep(severity) {
    if (!soundToggle.checked) return;
    try {
      audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.frequency.value = severity === 'alert' ? 880 : 660;
      gain.gain.value = 0.05;
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start();
      oscillator.stop(audioContext.currentTime + (severity === 'alert' ? 0.35 : 0.15));
    } catch (_error) {
      // Trình duyệt chặn âm thanh: bỏ qua, highlight vẫn hoạt động.
    }
  }

  function pushAlert(event) {
    const item = el('p', { className: `live-mon__alert live-mon__alert--${event.severity}`, role: 'status' }, [
      el('strong', { text: `${event.symbol || ''} ${event.title_vi}`.trim() }),
      document.createTextNode(` — ${event.detail_vi} · ${timeText(event.occurred_at)}`),
    ]);
    alerts.prepend(item);
    while (alerts.children.length > ALERT_MAX) alerts.lastElementChild.remove();
  }

  function renderEvent(event, highlight) {
    const metrics = event.metrics || {};
    const bits = [];
    if (metrics.funding && metrics.funding !== '0') bits.push(`funding ${num(metrics.funding)}`);
    const item = el(
      'li',
      {
        className: `live-mon__event live-mon__event--${event.severity}${highlight ? ' is-new' : ''}`,
        'data-event-id': event.id,
        'data-kind': event.kind,
        'data-severity': event.severity,
        'data-recorded-at': event.recorded_at,
      },
      [
        el('time', { className: 'live-mon__event-time', datetime: event.occurred_at, text: timeText(event.occurred_at) }),
        el('span', { className: 'live-mon__event-bot', text: botNames.get(event.bot_id) || '' }),
        el('span', { className: 'live-mon__event-symbol', text: `${event.symbol} ${event.side}`.trim() }),
        el('span', { className: 'live-mon__event-title', text: event.title_vi }),
        el('span', { className: `live-mon__event-detail ${tone(metrics.net_pnl)}`, text: [event.detail_vi].concat(bits).join(' · ') }),
      ],
    );
    if (event.severity === 'muted' && !showMuted.checked) item.hidden = true;
    return item;
  }

  function insertEvents(events, initial) {
    const fresh = events.filter((event) => !seen.has(event.id));
    fresh.forEach((event) => seen.add(event.id));
    // Sự kiện đến theo thứ tự ghi tăng dần; danh sách hiển thị mới nhất ở trên cùng.
    fresh.forEach((event) => {
      const important = event.severity === 'notice' || event.severity === 'alert';
      feed.prepend(renderEvent(event, !initial && important));
      if (!initial && important) {
        pushAlert(event);
        beep(event.severity);
      }
    });
    while (feed.children.length > FEED_MAX) {
      const last = feed.lastElementChild;
      seen.delete(last.dataset.eventId);
      last.remove();
    }
    feedEmpty.hidden = feed.children.length > 0;
    root.dataset.eventCount = String(feed.children.length);
  }

  function statusBadge(target, text, status) {
    target.textContent = text;
    target.className = `live-mon__status live-mon__status--${status || 'unknown'}`;
    target.dataset.status = status || 'unknown';
  }

  function renderHealth(state) {
    const health = state.health || {};
    const kill = (state.workspace || {}).kill_state || 'ACTIVE';
    statusBadge($('[data-live-kill]'), kill === 'ACTIVE' ? 'Đang hoạt động' : `Đã kích hoạt (${kill})`, kill === 'ACTIVE' ? 'ok' : 'stale');
    const breaker = health.breaker || {};
    statusBadge(
      $('[data-live-breaker]'),
      breaker.state === 'CLOSED' ? 'Đóng — cho phép vào lệnh' : `${breaker.state || '—'}: ${(breaker.reasons || []).join(', ')}`,
      breaker.state === 'CLOSED' ? 'ok' : 'stale',
    );
    const feedInfo = health.feed || {};
    statusBadge($('[data-live-feed-age]'), feedInfo.age_seconds == null ? 'Chưa có nến' : `${feedInfo.age_seconds} s`, feedInfo.status);
    const mark = health.mark || {};
    statusBadge($('[data-live-mark]'), mark.age_seconds == null ? 'Chưa có (dùng giá đóng nến 1m)' : `${mark.age_seconds} s`, mark.age_seconds == null ? 'unknown' : mark.status);
    const outbox = health.outbox || {};
    statusBadge(
      $('[data-live-outbox]'),
      `${outbox.pending || 0} chờ (trễ ${outbox.lag_seconds || 0} s) · ${outbox.unconsumed_pending || 0} bản ghi chưa có consumer`,
      outbox.status,
    );
    const clock = health.clock || {};
    statusBadge($('[data-live-clock]'), clock.skew_ms == null ? '—' : `${clock.skew_ms} ms`, clock.status);
    $('[data-live-observed]').textContent = timeText(state.observed_at);
  }

  function renderEquity(state) {
    const equity = state.equity || {};
    $('[data-live-equity-value]').textContent = num(equity.equity, true);
    const upnl = $('[data-live-upnl]');
    upnl.textContent = num(equity.unrealized_pnl, true);
    upnl.className = tone(equity.unrealized_pnl);
    const today = $('[data-live-today]');
    today.textContent = num(equity.net_today, true);
    today.className = tone(equity.net_today);
    $('[data-live-dd]').textContent = equity.drawdown_pct == null ? '—' : `${num(equity.drawdown, true)} (${equity.drawdown_pct}%)`;
    const values = (equity.curve || []).map((point) => Number(point[1])).filter(Number.isFinite);
    const line = $('[data-live-spark] polyline');
    if (values.length < 2) {
      line.setAttribute('points', '');
      return;
    }
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    line.setAttribute(
      'points',
      values.map((value, index) => `${(index / (values.length - 1)) * 300},${58 - ((value - min) / span) * 56}`).join(' '),
    );
  }

  function renderBots(state) {
    const body = $('[data-live-bots]');
    body.replaceChildren();
    (state.bots || []).forEach((bot) => {
      botNames.set(bot.id, bot.name || bot.id.slice(0, 8));
      const equity = bot.equity || {};
      const entry = bot.entry_allowed ? 'Cho phép' : `Chặn (${bot.entry_state || '—'} / ${bot.readiness || '—'})`;
      const heartbeat = bot.heartbeat_age_seconds == null
        ? 'Chưa có'
        : `${bot.heartbeat_stale ? 'Chậm · ' : ''}${bot.heartbeat_age_seconds} s`;
      const metric = (label, value, className, modifier = '') => el('div', {
        className: `live-mon__bot-metric ${modifier}`.trim(),
      }, [
        el('dt', { text: label }),
        el('dd', { className, text: value }),
      ]);
      body.append(
        el('article', {
          'data-bot-row': bot.id,
          role: 'listitem',
          className: 'live-mon__bot-card',
          'aria-label': `Bot ${bot.name || bot.id}`,
        }, [
          el('header', { className: 'live-mon__bot-head' }, [
            el('h3', {}, [el('a', { href: `/admin/bots/${bot.id}`, text: bot.name || bot.id })]),
          ]),
          el('dl', { className: 'live-mon__bot-metrics' }, [
            metric(
              'Vào lệnh',
              entry,
              bot.entry_allowed ? 'live-mon__ok' : 'live-mon__warn',
              'live-mon__bot-metric--entry',
            ),
            metric('Vị thế', String(bot.open_positions)),
            metric('Vốn', num(equity.equity, true)),
            metric('DD', equity.drawdown_pct == null ? '—' : `${num(equity.drawdown_pct, true)}%`),
            metric('Hôm nay', num(bot.net_today, true), tone(bot.net_today)),
            metric(
              'Nhịp runtime',
              heartbeat,
              bot.heartbeat_stale ? 'live-mon__warn' : 'live-mon__ok',
            ),
          ]),
        ]),
      );
    });
    if (!body.children.length) {
      body.append(el('p', {
        role: 'listitem',
        className: 'live-mon__empty',
        text: 'Chưa chọn bot nào.',
      }));
    }
  }

  function renderPositions(state) {
    const body = $('[data-live-positions]');
    const positions = state.positions || [];
    $('[data-live-pos-count]').textContent = String(positions.length);
    body.replaceChildren();
    positions.forEach((position) => {
      const stopNear = Number(position.stop_distance_pct) <= 0.5;
      body.append(
        el('tr', { 'data-trade-id': position.trade_id, className: stopNear ? 'live-mon__row--danger' : '' }, [
          el('th', { scope: 'row' }, [
            el('span', { text: position.symbol }),
            el('small', { className: 'live-mon__sub', text: botNames.get(position.bot_id) || '' }),
          ]),
          el('td', {}, [el('span', { className: position.side === 'SHORT' ? 'badge badge-short' : 'badge badge-long', text: position.side })]),
          el('td', { text: num(position.entry_price) }),
          el('td', { title: `Nguồn ${position.mark_source || '—'}, ${position.mark_age_seconds ?? '—'} s trước`, text: num(position.mark_price) }),
          el('td', { className: tone(position.unrealized_pnl), text: num(position.unrealized_pnl, true) }),
          el('td', { className: tone(position.r_multiple), text: position.r_multiple == null ? '—' : `${position.r_multiple}R` }),
          el('td', {
            text: position.stop_distance_pct == null ? 'Không có SL' : `${position.stop_distance_pct}% (${num(position.stop_price)}${position.stop_moved ? ', đã dời' : ''})`,
          }),
          el('td', { text: position.target_distance_pct == null ? '—' : `${position.target_distance_pct}% (${num(position.target_price)})` }),
          el('td', { 'data-opened-at': position.opened_at, text: duration(position.hold_seconds) }),
          el('td', { className: tone(position.funding_total), text: num(position.funding_total) }),
        ]),
      );
    });
    if (!positions.length) body.append(el('tr', {}, [el('td', { colspan: '10', text: 'Không có vị thế mở.' })]));
  }

  function renderState(state) {
    if (!state) return;
    renderBots(state);
    renderHealth(state);
    renderEquity(state);
    renderPositions(state);
    root.dataset.stateAt = state.observed_at || '';
  }

  function onEvents(payload, initial) {
    if (payload.cursor) cursor = payload.cursor;
    insertEvents(payload.events || [], initial);
  }

  // --- poll dự phòng ---------------------------------------------------------------------
  async function pollOnce() {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      const response = await fetch(`${root.dataset.snapshotUrl}?${query({ since: cursor })}`, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error(String(response.status));
      const payload = await response.json();
      const initial = cursor === null;
      onEvents(payload, initial);
      renderState(payload.state);
      setConnection('poll', `Poll ${Math.round(pollMs / 1000)} s (dự phòng)`);
    } catch (_error) {
      setConnection('error', 'Mất kết nối — đang thử lại');
    } finally {
      pollInFlight = false;
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollOnce();
    pollTimer = window.setInterval(pollOnce, pollMs);
    retryTimer = window.setTimeout(() => {
      stopPolling();
      startStream();
    }, SSE_RETRY_FROM_POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) window.clearInterval(pollTimer);
    if (retryTimer) window.clearTimeout(retryTimer);
    pollTimer = null;
    retryTimer = null;
  }

  // --- SSE ----------------------------------------------------------------------------------
  function parse(message) {
    try {
      return JSON.parse(message.data);
    } catch (_error) {
      return null;
    }
  }

  function startStream() {
    if (!('EventSource' in window) || root.dataset.forcePoll === 'true') {
      startPolling();
      return;
    }
    const url = `${root.dataset.streamUrl}?${query({ since: cursor })}`;
    source = new EventSource(url, { withCredentials: true });
    setConnection('connecting', 'Đang kết nối…');
    source.addEventListener('open', () => {
      sseFailures = 0;
      setConnection('sse', 'Trực tiếp (SSE)');
    });
    source.addEventListener('events', (message) => {
      const payload = parse(message);
      if (payload) onEvents(payload, Boolean(payload.initial) && seen.size === 0);
    });
    source.addEventListener('state', (message) => {
      renderState(parse(message));
      setConnection('sse', 'Trực tiếp (SSE)');
    });
    source.addEventListener('degraded', () => setConnection('error', 'Ledger tạm không đọc được'));
    source.addEventListener('auth', () => {
      source.close();
      // fetch đi qua session-guard.js: 401 → chuyển về trang đăng nhập kèm next.
      pollOnce();
    });
    source.addEventListener('error', () => {
      sseFailures += 1;
      if (sseFailures >= SSE_FAILURES_BEFORE_POLL || source.readyState === EventSource.CLOSED) {
        source.close();
        source = null;
        startPolling();
      } else {
        setConnection('connecting', 'Đang kết nối lại…');
      }
    });
  }

  showMuted.addEventListener('change', () => {
    feed.querySelectorAll('[data-severity="muted"]').forEach((item) => {
      item.hidden = !showMuted.checked;
    });
  });
  soundToggle.addEventListener('change', () => {
    if (soundToggle.checked) beep('notice');
  });

  // Đồng hồ thời gian giữ lệnh chạy cục bộ mỗi giây giữa các lần nhận trạng thái.
  window.setInterval(() => {
    const now = Date.now();
    root.querySelectorAll('[data-opened-at]').forEach((cell) => {
      const opened = Date.parse(cell.dataset.openedAt);
      if (Number.isFinite(opened)) cell.textContent = duration((now - opened) / 1000);
    });
  }, 1000);

  window.addEventListener('pagehide', () => {
    if (source) source.close();
    stopPolling();
  });

  if (!botIds.length) {
    setConnection('error', 'Chưa chọn bot');
    return;
  }
  startStream();
})();
