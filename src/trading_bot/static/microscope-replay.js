// Tương tác Microscope Replay; dữ liệu thẻ và preset đọc từ khối JSON trong trang.
(function() {
  'use strict';

  const cardId = JSON.parse(document.getElementById("microscope-card-id").textContent);
  const presetsScript = document.getElementById('presets-data');
  const presets = presetsScript ? JSON.parse(presetsScript.textContent) : [];

  const inspectForm = document.getElementById('microscope-inspect-form');
  const openInput = document.getElementById('input-open');
  const highInput = document.getElementById('input-high');
  const lowInput = document.getElementById('input-low');
  const closeInput = document.getElementById('input-close');
  const volumeInput = document.getElementById('input-volume');
  const minAtrInput = document.getElementById('input-min-atr');
  const directionSelect = document.getElementById('input-direction');
  const submitBtn = document.getElementById('btn-inspect-submit');

  const presetButtons = document.querySelectorAll('.preset-item-btn');

  // Gán sự kiện khi click vào Preset
  presetButtons.forEach(btn => {
    btn.addEventListener('click', function() {
      const presetId = this.getAttribute('data-preset-id');
      const target = presets.find(p => p.id === presetId);
      if (!target) return;

      // Cập nhật trạng thái active của nút
      presetButtons.forEach(b => {
        b.classList.remove('preset-active');
        b.setAttribute('aria-pressed', 'false');
      });
      this.classList.add('preset-active');
      this.setAttribute('aria-pressed', 'true');

      // Điền thông số vào Form
      if (target.ohlcv) {
        if (target.ohlcv.open !== undefined) openInput.value = target.ohlcv.open;
        if (target.ohlcv.high !== undefined) highInput.value = target.ohlcv.high;
        if (target.ohlcv.low !== undefined) lowInput.value = target.ohlcv.low;
        if (target.ohlcv.close !== undefined) closeInput.value = target.ohlcv.close;
        if (target.ohlcv.volume !== undefined) volumeInput.value = target.ohlcv.volume;
      }
      if (target.context) {
        if (target.context.min_atr !== undefined) minAtrInput.value = target.context.min_atr;
        if (target.context.direction) directionSelect.value = target.context.direction;
      }

      // Tự động gửi lệnh soi vết
      triggerInspect();
    });
  });

  // Gán sự kiện submit form
  if (inspectForm) {
    inspectForm.addEventListener('submit', function(e) {
      e.preventDefault();
      triggerInspect();
    });
  }

  function triggerInspect() {
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = '⏳ Đang phân tích...';
    }

    const payload = {
      ohlcv: {
        open: parseFloat(openInput.value),
        high: parseFloat(highInput.value),
        low: parseFloat(lowInput.value),
        close: parseFloat(closeInput.value),
        volume: parseFloat(volumeInput.value)
      },
      context: {
        min_atr: parseFloat(minAtrInput.value || '5.0'),
        direction: directionSelect.value
      }
    };

    fetch('/admin/api/strategy-cards/' + encodeURIComponent(cardId) + '/inspect-bar', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify(payload)
    })
    .then(res => {
      if (!res.ok) {
        throw new Error('Lỗi máy chủ: ' + res.status);
      }
      return res.json();
    })
    .then(data => {
      if (data.status === 'success' && data.trace) {
        updateMicroscopeUI(data.trace);
      }
    })
    .catch(err => {
      console.error('Lỗi soi kính hiển vi:', err);
      alert('Không thể thực thi soi vết nến: ' + err.message);
    })
    .finally(() => {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = '⚡ Soi vết nến tức thời (Inspect Bar)';
      }
    });
  }

  function updateMicroscopeUI(trace) {
    const statusSection = document.getElementById('pipeline-status-section');

    // 1. Cập nhật Banner Ngắt mạch / Thông suốt
    if (trace.is_short_circuited) {
      statusSection.innerHTML = `
        <div class="alert-banner banner-short-circuit" role="alert" id="status-banner">
          <div class="banner-icon" aria-hidden="true">🛑</div>
          <div class="banner-body">
            <h4 class="banner-heading">NGẮT MẠCH TỨC THÌ (SHORT-CIRCUITED)</h4>
            <p class="banner-message" id="banner-reason">${escapeHTML(trace.short_circuit_reason || '')}</p>
            <p class="banner-hint">Các tầng tiếp theo bị cô lập an toàn, tuyệt đối không kích hoạt ngòi nổ L5 và không tạo vùng giá L6.</p>
          </div>
        </div>
      `;
    } else {
      statusSection.innerHTML = `
        <div class="alert-banner banner-success" role="status" id="status-banner">
          <div class="banner-icon" aria-hidden="true">✅</div>
          <div class="banner-body">
            <h4 class="banner-heading">QUY TRÌNH THÔNG SUỐT (PIPELINE PASSED)</h4>
            <p class="banner-message" id="banner-reason">Nến đã vượt qua thành công toàn bộ rào chắn bối cảnh L4 và kích hoạt ngòi nổ tín hiệu.</p>
            <p class="banner-hint">Đã đóng gói thành công StrategyCandidate với cấu trúc rủi ro hình học L6 Zones.</p>
          </div>
        </div>
      `;
    }

    // 2. Cập nhật Tầng L0
    const l0Badge = document.getElementById('l0-status-badge');
    const l0Content = document.getElementById('l0-trace-content');
    const l0Trace = trace.layer_traces.find(t => t.layer === 'L0_DATA_HYGIENE');
    if (l0Trace) {
      if (l0Trace.status === 'SUCCESS') {
        l0Badge.innerHTML = '<span class="status-dot dot-green" aria-hidden="true"></span> ĐẠT CHUẨN (PASSED)';
      } else {
        l0Badge.innerHTML = '<span class="status-dot dot-red" aria-hidden="true"></span> VI PHẠM (FAILED)';
      }
      if (l0Content) {
        l0Content.innerHTML = `<pre class="trace-pre"><code>${escapeHTML(String(l0Trace.output || ''))}</code></pre>`;
      }
    }

    // 3. Cập nhật Tầng L1 (Candle Anatomy)
    const l1Badge = document.getElementById('l1-status-badge');
    const l1Grid = document.getElementById('l1-metrics-grid');
    const l1Traces = trace.layer_traces.filter(t => t.layer === 'L1_CANDLE_ANATOMY');
    if (l1Badge) {
      if (trace.short_circuit_reason && trace.short_circuit_reason.includes('L0')) {
        l1Badge.innerHTML = '<span class="status-dot dot-gray" aria-hidden="true"></span> BỎ QUA (SKIPPED)';
      } else {
        l1Badge.innerHTML = '<span class="status-dot dot-green" aria-hidden="true"></span> HOÀN THÀNH (SUCCESS)';
      }
    }
    if (l1Grid) {
      let html = '';
      l1Traces.forEach(t => {
        const valStr = typeof t.output === 'number' ? t.output.toFixed(4) : (t.output !== null ? t.output : 'N/A');
        html += `
          <div class="anatomy-metric-box">
            <span class="metric-key">${escapeHTML(t.node_id)} (${escapeHTML(t.operation)})</span>
            <span class="metric-value">${escapeHTML(String(valStr))}</span>
          </div>
        `;
      });
      l1Grid.innerHTML = html;
    }

    // 4. Cập nhật Tầng L2 (Indicators)
    const l2Badge = document.getElementById('l2-status-badge');
    const l2Grid = document.getElementById('l2-metrics-grid');
    const l2Traces = trace.layer_traces.filter(t => t.layer === 'L2_INDICATORS');
    if (l2Badge) {
      if (trace.short_circuit_reason && trace.short_circuit_reason.includes('L0')) {
        l2Badge.innerHTML = '<span class="status-dot dot-gray" aria-hidden="true"></span> BỎ QUA (SKIPPED)';
      } else {
        l2Badge.innerHTML = '<span class="status-dot dot-green" aria-hidden="true"></span> HOÀN THÀNH (SUCCESS)';
      }
    }
    if (l2Grid) {
      let html = '';
      l2Traces.forEach(t => {
        const valStr = typeof t.output === 'number' ? t.output.toFixed(2) : (t.output !== null ? t.output : 'N/A');
        html += `
          <div class="indicator-item">
            <span class="ind-node">${escapeHTML(t.node_id)}:</span>
            <span class="ind-val">${escapeHTML(String(valStr))}</span>
          </div>
        `;
      });
      l2Grid.innerHTML = html;
    }

    // 5. Cập nhật Tầng L4 (Context Gate)
    const l4Card = document.getElementById('card-layer-l4');
    const l4Badge = document.getElementById('l4-status-badge');
    const l4Content = document.getElementById('l4-trace-content');
    const l4Traces = trace.layer_traces.filter(t => t.layer === 'L4_CONTEXT_GATE');

    if (trace.context_gate_passed) {
      if (l4Card) l4Card.classList.remove('layer-blocked');
      if (l4Badge) l4Badge.innerHTML = '<span class="status-dot dot-green" aria-hidden="true"></span> CHO PHÉP (PASS)';
    } else if (trace.is_short_circuited && trace.short_circuit_reason && trace.short_circuit_reason.includes('L0')) {
      if (l4Card) l4Card.classList.remove('layer-blocked');
      if (l4Badge) l4Badge.innerHTML = '<span class="status-dot dot-gray" aria-hidden="true"></span> BỎ QUA (SKIPPED)';
    } else {
      if (l4Card) l4Card.classList.add('layer-blocked');
      if (l4Badge) l4Badge.innerHTML = '<span class="status-dot dot-red" aria-hidden="true"></span> CHẶN ĐỨNG (BLOCK)';
    }

    if (l4Content) {
      let html = '';
      l4Traces.forEach(t => {
        const isBlock = t.status === 'BLOCK';
        html += `
          <div class="gate-outcome ${isBlock ? 'outcome-block' : 'outcome-pass'}">
            <span class="gate-node-name"><strong>Nút kiểm tra:</strong> ${escapeHTML(t.node_id)} (${escapeHTML(t.operation)})</span>
            <div class="gate-output-text">${escapeHTML(String(t.output || ''))}</div>
          </div>
        `;
      });
      l4Content.innerHTML = html;
    }

    // 6. Cập nhật Tầng L5 (Strategy Trigger)
    const l5Badge = document.getElementById('l5-status-badge');
    const l5Content = document.getElementById('l5-trace-content');
    const l5Traces = trace.layer_traces.filter(t => t.layer === 'L5_STRATEGY_TRIGGER');

    if (l5Badge) {
      if (trace.trigger_signal === 'LONG_ENTRY' || trace.trigger_signal === 'SHORT_ENTRY') {
        l5Badge.innerHTML = `<span class="status-dot dot-green" aria-hidden="true"></span> KÍCH HOẠT (${trace.trigger_signal})`;
      } else if (trace.is_short_circuited) {
        l5Badge.innerHTML = '<span class="status-dot dot-gray" aria-hidden="true"></span> BỎ QUA (SKIPPED)';
      } else {
        l5Badge.innerHTML = '<span class="status-dot dot-yellow" aria-hidden="true"></span> KHÔNG TÍN HIỆU (NO_SIGNAL)';
      }
    }
    if (l5Content) {
      let html = '';
      l5Traces.forEach(t => {
        html += `
          <div class="trigger-status-box">
            <span class="trigger-node"><strong>Node:</strong> ${escapeHTML(t.node_id)}</span>
            <span class="trigger-signal-badge">${escapeHTML(String(t.output || ''))}</span>
          </div>
        `;
      });
      l5Content.innerHTML = html;
    }

    // 7. Cập nhật Tầng L6 (Execution Zones)
    const l6Badge = document.getElementById('l6-status-badge');
    const l6Wrapper = document.getElementById('l6-zones-wrapper');

    if (l6Badge) {
      if (trace.zones) {
        l6Badge.innerHTML = '<span class="status-dot dot-green" aria-hidden="true"></span> ĐÃ THIẾT LẬP (ZONES READY)';
      } else if (trace.is_short_circuited) {
        l6Badge.innerHTML = '<span class="status-dot dot-gray" aria-hidden="true"></span> BỎ QUA (SKIPPED)';
      } else {
        l6Badge.innerHTML = '<span class="status-dot dot-yellow" aria-hidden="true"></span> CHƯA KÍCH HOẠT';
      }
    }

    if (l6Wrapper) {
      if (trace.zones) {
        l6Wrapper.innerHTML = `
          <div class="zones-metrics-row">
            <div class="zone-box zone-entry">
              <span class="zone-label">Điểm vào lệnh (Entry):</span>
              <span class="zone-val" id="zone-entry-val">${trace.zones.entry_price.toFixed(2)}</span>
            </div>
            <div class="zone-box zone-sl">
              <span class="zone-label">Dừng lỗ ban đầu (Stop Loss):</span>
              <span class="zone-val" id="zone-sl-val">${trace.zones.stop_loss.toFixed(2)}</span>
            </div>
            <div class="zone-box zone-tp">
              <span class="zone-label">Chốt lời mục tiêu (Take Profit):</span>
              <span class="zone-val" id="zone-tp-val">${trace.zones.take_profit.toFixed(2)}</span>
            </div>
            <div class="zone-box zone-rr">
              <span class="zone-label">Tỷ lệ Risk : Reward:</span>
              <span class="zone-val" id="zone-rr-val">1 : ${trace.zones.risk_reward_ratio.toFixed(1)}</span>
            </div>
          </div>
          <div class="candidate-id-box">
            <span class="candidate-label">Mã Đề xuất Candidate ID (SHA-256 tất định):</span>
            <code class="candidate-code" id="candidate-id-val" title="${escapeHTML(trace.candidate_id || '')}">
              ${escapeHTML(trace.candidate_id || '')}
            </code>
          </div>
        `;
      } else {
        l6Wrapper.innerHTML = `
          <div class="empty-zones-placeholder" id="empty-zones-msg">
            <p class="text-muted">Chưa có vùng thực thi do quy trình bị ngắt mạch hoặc ngòi nổ không kích hoạt.</p>
          </div>
        `;
      }
    }
  }

  function escapeHTML(str) {
    if (!str) return '';
    return str.replace(/[&<>'"]/g, tag => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;'
    }[tag] || tag));
  }
})();
