/* 思考现场：用真实流水线事件驱动的一张脑图。
 * 约束：画面里每一次亮起都必须对应一条来自 /api/admin/live/stream 的事件；
 * 空闲时只有「呼吸」的低亮度，不凭空造脉冲。
 */
(function () {
  'use strict';

  var ACCENT = '#5fd0b4';
  var WARM = '#e8a67a';
  var COOL = '#7aa8e8';
  var RED = '#e07a72';

  // 九站：顺序就是真实链路顺序（pipeline 的阶段名 + /tts）。
  var STATIONS = [
    { key: 'input', name: '听见', color: COOL, hint: '收到一句输入' },
    { key: 'judge', name: '情感判断', color: WARM, hint: 'onnx 判定情绪' },
    { key: 'memory', name: '记忆检索', color: '#b49ae0', hint: 'SQLite 取事实' },
    { key: 'prompt', name: '提示词', color: '#9aa8b0', hint: '组装上下文' },
    { key: 'tool', name: '工具', color: '#d9c37a', hint: '查 / 记 / 提醒' },
    { key: 'llm', name: '思考', color: ACCENT, hint: '大模型在生成' },
    { key: 'token', name: '出字', color: ACCENT, hint: '逐字流出' },
    { key: 'voice', name: '声音', color: '#7fd0e8', hint: 'GPT-SoVITS 合成' },
    { key: 'done', name: '收尾', color: '#cfd8da', hint: '落库 / 交付' }
  ];
  var EDGES = [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8]];

  var canvas = document.getElementById('atlas');
  var ctx = canvas.getContext('2d');
  var W = 0, H = 0, DPR = 1;
  var nodes = [];
  var particles = [];
  var t0 = performance.now();
  var evCount = 0;
  var lastTurnMs = null;
  var connState = 'connecting';
  var feed = [];

  // ---- 固定编组：椭圆环，节点位置不随数据变化（人才能记住哪一站是哪一站）----
  function layout() {
    nodes = STATIONS.map(function (s, i) {
      var n = STATIONS.length;
      var ang = -Math.PI / 2 + (i / n) * Math.PI * 2;
      var r = { heat: 0, last: 0, count: 0, value: '', flash: 0 };
      return Object.assign({}, s, r, { ang: ang, x: 0, y: 0, sats: [] });
    });
    nodes.forEach(function (n, i) {
      // 每站一圈细点：位置固定（用序号做种子），只随热度亮灭
      var seed = (i + 1) * 13;
      for (var k = 0; k < 14; k++) {
        seed = (seed * 9301 + 49297) % 233280;
        var a = (seed / 233280) * Math.PI * 2;
        seed = (seed * 9301 + 49297) % 233280;
        var rr = 30 + (seed / 233280) * 26;
        n.sats.push({ a: a, r: rr, ph: (k / 14) * Math.PI * 2, size: 1 + (k % 3) * 0.45 });
      }
    });
  }

  function resize() {
    var rect = canvas.getBoundingClientRect();
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = Math.max(320, rect.width);
    H = Math.max(320, rect.height);
    canvas.width = Math.round(W * DPR);
    canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    var cx = W / 2, cy = H / 2;
    var rx = Math.min(W * 0.36, 430);
    var ry = Math.min(H * 0.36, 320);
    nodes.forEach(function (n) {
      n.x = cx + Math.cos(n.ang) * rx;
      n.y = cy + Math.sin(n.ang) * ry;
    });
  }

  function stationIndex(kind) {
    switch (kind) {
      case 'turn_start': return 0;
      case 'judge': return 1;
      case 'memory': return 2;
      case 'prompt': return 3;
      case 'tool': return 4;
      case 'llm_start': return 5;
      case 'first_token': return 6;
      case 'tokens': return 5;
      case 'voice_start': return 7;
      case 'voice_done': return 7;
      case 'voice_error': return 7;
      case 'turn_done': return 8;
      case 'turn_error': return 8;
      case 'bg_start': return 8;
      case 'bg_done': return 8;
      default: return -1;
    }
  }

  function describe(ev) {
    switch (ev.kind) {
      case 'turn_start': return '输入 ' + (ev.text_len || 0) + ' 字';
      case 'judge': return (ev.emotion || '?') + ' · ' + (ev.ms || 0) + 'ms';
      case 'memory': return '事实 ' + (ev.facts || 0) + ' / 情绪 ' + (ev.emotions || 0) + ' · ' + (ev.ms || 0) + 'ms';
      case 'prompt': return (ev.chars || 0) + ' 字上下文';
      case 'tool': return (ev.name ? ev.name : '未命中') + (ev.ok ? ' ✓' : '');
      case 'llm_start': return '开始生成';
      case 'first_token': return '首字 ' + (ev.ms || 0) + 'ms';
      case 'tokens': return '已出 ' + (ev.chars || 0) + ' 字';
      case 'voice_start': return '合成 ' + (ev.chars || 0) + ' 字';
      case 'voice_done': return (ev.kb || 0) + 'KB · ' + (ev.ms || 0) + 'ms';
      case 'voice_error': return '合成失败';
      case 'turn_done': return (ev.ms || 0) + 'ms / ' + (ev.chars || 0) + ' 字';
      case 'turn_error': return '出错 · ' + (ev.code || '');
      case 'bg_start': return '后台消化中…';
      case 'bg_done': return '消化完 · ' + (ev.ms || 0) + 'ms';
      default: return '';
    }
  }

  function spawn(fromIdx, toIdx, color, strength) {
    particles.push({
      from: fromIdx, to: toIdx, t: 0,
      speed: 0.9 + Math.random() * 0.35, color: color, size: 1.6 + strength * 1.4,
      alpha: 1
    });
    if (particles.length > 320) particles.splice(0, particles.length - 320);
  }

  var lastEventAt = 0;
  function onEvent(ev) {
    if (!ev || !ev.kind) return;
    evCount++;
    lastEventAt = Date.now();
    document.getElementById('ev-count').textContent = String(evCount);
    setConn('on', '实时');

    var idx = stationIndex(ev.kind);
    if (idx >= 0) {
      var n = nodes[idx];
      n.heat = Math.min(1, n.heat + (ev.kind === 'tokens' ? 0.18 : 0.9));
      n.flash = 1;
      n.count++;
      n.last = ev.t ? ev.t * 1000 : Date.now();
      n.value = describe(ev);
      // 脉冲沿「上一站 → 本站」走，位置即真实路径
      var prev = idx > 0 ? idx - 1 : -1;
      spawn(prev >= 0 ? prev : idx, idx, n.color, 1);
      if (ev.kind === 'tokens') {
        spawn(5, 6, ACCENT, 0.6);
        spawn(5, 6, ACCENT, 0.6);
      }
      if (ev.kind === 'turn_done' || ev.kind === 'turn_error') {
        lastTurnMs = ev.ms || null;
        document.getElementById('turn-ms').textContent = lastTurnMs ? Math.round(lastTurnMs) + 'ms' : '—';
        if (ev.kind === 'turn_error') { n.color = RED; }
        // 收尾后余晖：热度的自然衰减就是「它停下来但还在消化」的可见反馈
      }
      if (ev.kind === 'voice_error') n.color = RED;
    }
    pushFeed(ev);
  }

  function pushFeed(ev) {
    var t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    feed.unshift({ t: t, txt: (STATIONS[Math.max(0, stationIndex(ev.kind))] || { name: '事件' }).name + ' · ' + describe(ev) });
    if (feed.length > 6) feed.pop();
    var box = document.getElementById('feed-lines');
    box.innerHTML = '';
    feed.forEach(function (f) {
      var d = document.createElement('div');
      d.className = 'line';
      d.innerHTML = '<span class="k">' + f.t + '</span>' + escapeHtml(f.txt);
      box.appendChild(d);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function setConn(state, txt) {
    connState = state;
    var dot = document.getElementById('conn-dot');
    dot.className = 'dot' + (state === 'on' ? ' on' : (state === 'err' ? ' err' : ''));
    document.getElementById('conn-txt').textContent = txt;
  }

  // ---- 绘制 ----
  function frame(now) {
    var dt = Math.min(0.05, (now - t0) / 1000);
    t0 = now;

    ctx.clearRect(0, 0, W, H);

    // 底：极淡的环，提示这是一条固定链路
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,.05)';
    ctx.setLineDash([3, 7]);
    ctx.beginPath();
    var cx = W / 2, cy = H / 2;
    nodes.forEach(function (n) { ctx.lineTo(n.x, n.y); });
    ctx.closePath();
    ctx.stroke();
    ctx.restore();

    // 连线：亮度取两端热度
    EDGES.forEach(function (e) {
      var a = nodes[e[0]], b = nodes[e[1]];
      var h = Math.max(a.heat, b.heat);
      ctx.strokeStyle = 'rgba(140,170,165,' + (0.10 + h * 0.35) + ')';
      ctx.lineWidth = 1 + h * 1.4;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });

    // 脉冲
    for (var i = particles.length - 1; i >= 0; i--) {
      var p = particles[i];
      p.t += dt * p.speed;
      if (p.t >= 1) { particles.splice(i, 1); continue; }
      var A = nodes[p.from], B = nodes[p.to];
      var x = A.x + (B.x - A.x) * p.t;
      var y = A.y + (B.y - A.y) * p.t;
      var alpha = Math.sin(Math.PI * p.t) * 0.9;
      ctx.globalAlpha = alpha;
      ctx.fillStyle = p.color;
      ctx.beginPath();
      ctx.arc(x, y, p.size, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // 节点
    var breath = 0.5 + 0.5 * Math.sin(now / 2600);
    nodes.forEach(function (n) {
      n.heat *= Math.exp(-dt / 3.2);      // 余晖半衰 ~2.2s：停下后仍能看到余温在退
      n.flash *= Math.exp(-dt / 0.45);

      var base = 15;
      var r = base + n.heat * 9 + breath * 1.6;
      var g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 2.6);
      g.addColorStop(0, hexA(n.color, 0.30 + n.heat * 0.55));
      g.addColorStop(0.55, hexA(n.color, 0.10 + n.heat * 0.22));
      g.addColorStop(1, hexA(n.color, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r * 2.6, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = hexA(n.color, 0.5 + n.heat * 0.5);
      ctx.beginPath();
      ctx.arc(n.x, n.y, r * 0.42, 0, Math.PI * 2);
      ctx.fill();

      // 细点：固定位置，只有热度上来才亮 —— 亮 = 该站最近确实通过
      n.sats.forEach(function (s) {
        var ang = s.a + now / 22000;
        var sx = n.x + Math.cos(ang) * s.r;
        var sy = n.y + Math.sin(ang) * s.r;
        var tw = 0.5 + 0.5 * Math.sin(now / 900 + s.ph);
        ctx.fillStyle = hexA(n.color, 0.05 + n.heat * (0.19 + tw * 0.22));
        ctx.beginPath();
        ctx.arc(sx, sy, s.size, 0, Math.PI * 2);
        ctx.fill();
      });

      var tx, nameY, valY;
      var dx = n.x - W / 2, dy = n.y - H / 2;
      var len = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      var ux = dx / len, uy = dy / len;
      tx = n.x + ux * 40;
      nameY = n.y + uy * 40 + 6;
      valY = nameY + 15;
      ctx.textAlign = 'center';
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(11,15,17,.75)';
      ctx.font = '600 12.5px -apple-system, "Microsoft YaHei", sans-serif';
      ctx.strokeText(n.name, tx, nameY);
      ctx.fillStyle = 'rgba(230,236,233,' + (0.55 + n.heat * 0.45) + ')';
      ctx.fillText(n.name, tx, nameY);
      if (n.value) {
        var v = n.value.length > 20 ? n.value.slice(0, 19) + '…' : n.value;
        ctx.font = '500 11px -apple-system, "Microsoft YaHei", sans-serif';
        ctx.strokeText(v, tx, valY);
        ctx.fillStyle = 'rgba(168,180,182,' + (0.4 + n.heat * 0.5) + ')';
        ctx.fillText(v, tx, valY);
      }
      ctx.textAlign = 'start';
    });
  }

  function hexA(hex, a) {
    var h = hex.replace('#', '');
    var r = parseInt(h.substring(0, 2), 16);
    var g2 = parseInt(h.substring(2, 4), 16);
    var b = parseInt(h.substring(4, 6), 16);
    return 'rgba(' + r + ',' + g2 + ',' + b + ',' + Math.max(0, Math.min(1, a)) + ')';
  }

  function loop(now) {
    frame(now);
    requestAnimationFrame(loop);
  }

  // ---- 图例（右侧数值跟着真实事件更新）----
  function renderLegend() {
    var box = document.getElementById('legend-rows');
    box.innerHTML = '';
    nodes.forEach(function (n) {
      var row = document.createElement('div');
      row.className = 'row' + (n.heat > 0.08 ? '' : ' idle');
      row.innerHTML =
        '<span class="sw" style="background:' + n.color + '"></span>' +
        '<span class="nm">' + escapeHtml(n.name) + '<br><span style="color:#6e797d;font-size:11px">' + escapeHtml(n.hint) + '</span></span>' +
        '<span class="val">' + (n.value ? escapeHtml(n.value) : '—') + '</span>';
      box.appendChild(row);
    });
  }
  setInterval(renderLegend, 700);

  // ---- SSE ----
  function connect() {
    var token = '';
    try { token = localStorage.getItem('companion.admin_token') || ''; } catch (e) { /* noop */ }
    var url = '/api/admin/live/stream' + (token ? '?token=' + encodeURIComponent(token) : '');
    var es = new EventSource(url);
    es.onopen = function () { setConn('on', '实时'); };
    es.onmessage = function (m) {
      try { onEvent(JSON.parse(m.data)); } catch (e) { /* 一条坏帧不该打断整条流 */ }
    };
    es.onerror = function () {
      // 刚收到过事件就先不喊断线：EventSource 自己会重连，别把正常结束误报成故障
      if (Date.now() - lastEventAt > 4000) setConn('err', '断线重连中…');
    };
  }

  // ---- 启动 ----
  layout();
  resize();
  renderLegend();
  window.addEventListener('resize', function () { resize(); });
  connect();
  requestAnimationFrame(loop);

  // 页面刚打开先补最近 20 条，避免「等我打开时已经说完了」什么都看不到
  (function prime() {
    var token = '';
    try { token = localStorage.getItem('companion.admin_token') || ''; } catch (e) { /* noop */ }
    var headers = token ? { 'X-Admin-Token': token } : {};
    fetch('/api/admin/live/recent?limit=20', { headers: headers })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        (d.items || []).forEach(function (ev) {
          var idx = stationIndex(ev.kind);
          if (idx >= 0) {
            var n = nodes[idx];
            n.heat = Math.max(n.heat, 0.45);
            n.count++;
            n.value = describe(ev);
          }
        });
        if (d.items && d.items.length) pushFeed(d.items[d.items.length - 1]);
      })
      .catch(function () { /* 补历史失败不影响直播 */ });
  })();
})();
