/* 思考现场 v2：一张「神经网络」而不是九个灯泡。
 * 画面里 600+ 个点，每个点就是一个神经元；亮起不是随机的——
 * 它由 /api/admin/live/stream 的真实事件驱动：情感判断、记忆取回几条、
 * 上下文多少字、模型出了几个字、语音合成多少 KB，都会决定「亮多少个、亮多久」。
 */
(function () {
  'use strict';

  var ACCENT = '#5fd0b4';
  var WARM = '#e8a67a';
  var COOL = '#7aa8e8';
  var RED = '#e07a72';
  var NEURON_COUNT = 680;

  var REGIONS = [
    { key: 'input', name: '听见', color: COOL, hint: '收到一句输入', weight: 0.09, cx: 0.50, cy: 0.88 },
    { key: 'judge', name: '情感判断', color: WARM, hint: 'onnx 判定情绪', weight: 0.12, cx: 0.25, cy: 0.68 },
    { key: 'memory', name: '记忆检索', color: '#b49ae0', hint: 'SQLite 取事实', weight: 0.12, cx: 0.74, cy: 0.64 },
    { key: 'prompt', name: '提示词', color: '#9aa8b0', hint: '组装上下文', weight: 0.10, cx: 0.79, cy: 0.42 },
    { key: 'tool', name: '工具', color: '#d9c37a', hint: '查 / 记 / 提醒', weight: 0.08, cx: 0.63, cy: 0.76 },
    { key: 'llm', name: '思考', color: ACCENT, hint: '大模型在生成', weight: 0.22, cx: 0.40, cy: 0.42 },
    { key: 'token', name: '出字', color: ACCENT, hint: '逐字流出', weight: 0.12, cx: 0.55, cy: 0.24 },
    { key: 'voice', name: '声音', color: '#7fd0e8', hint: 'GPT-SoVITS 合成', weight: 0.09, cx: 0.29, cy: 0.30 },
    { key: 'done', name: '收尾', color: '#cfd8da', hint: '落库 / 交付', weight: 0.07, cx: 0.17, cy: 0.50 }
  ];

  var canvas = document.getElementById('atlas');
  var ctx = canvas.getContext('2d');
  var W = 0, H = 0, DPR = 1;
  var neurons = [];
  var edges = [];
  var adj = [];
  var cascade = [];
  var memAnchors = {};       // fact id → 记忆区里的「那一颗」神经元
  var assocPairs = [];       // 共享两字词的 facts = 真实联想（召回本身也是按 bigram 打的）
  var factTotal = 0;
  var lastTurnMs = null;
  var evCount = 0;
  var firedTotal = 0;
  var lastEventAt = 0;
  var t0 = performance.now();
  var feed = [];
  // 「定格」：方便看清刚刚亮过哪些神经元；定格时不再衰减，也不假装还在放电
  var HOLD = /hold=1/.test(location.search);

  // 固定种子：每天打开，神经元位置都一样，人才认得出「这是它的脑」
  function rng(seed) {
    return function () {
      seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
      var t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // 脑形轮廓：两个半球 + 额叶收口，不是圆环
  function insideBrain(nx, ny) {
    var cx = 0.5, cy = 0.52;
    var dx = (nx - cx) / 0.40, dy = (ny - cy) / 0.40;
    var r = Math.sqrt(dx * dx + dy * dy);
    if (r > 1) return false;
    // 额叶（上方中央）凹一点，两侧颞叶略鼓
    if (ny < 0.32 && Math.abs(nx - 0.5) < 0.16 && r > 0.72) return false;
    if (ny > 0.78 && Math.abs(nx - 0.5) < 0.20 && r > 0.80) return false;
    return true;
  }

  function build() {
    var rand = rng(20261116);
    neurons = [];
    edges = [];
    var guard = 0;
    while (neurons.length < NEURON_COUNT && guard < NEURON_COUNT * 30) {
      guard++;
      var nx = rand(), ny = 0.12 + rand() * 0.80;
      if (!insideBrain(nx, ny)) continue;
      // 分到「功能脑区」：按权重 + 距离，不是均分
      var best = 0, bestScore = 1e9;
      REGIONS.forEach(function (r, i) {
        var d = Math.hypot((nx - r.cx) * 1.35, ny - r.cy);
        var score = d / r.weight;
        if (score < bestScore) { bestScore = score; best = i; }
      });
      neurons.push({
        nx: nx, ny: ny, region: best, heat: 0, size: 1.0 + rand() * 1.2,
        delay: rand() * 0.4
      });
    }
    // 突触连线：每个神经元接最近的 3 个（同脑区优先），再加几条跨区「长投射」
    var N = neurons.length;
    for (var i = 0; i < N; i++) {
      var a = neurons[i];
      var near = [];
      for (var j = 0; j < N; j++) {
        if (i === j) continue;
        var b = neurons[j];
        var d = Math.hypot((a.nx - b.nx) * W, (a.ny - b.ny) * H);
        if (b.region === a.region) d *= 0.72;      // 同区更近，容易连
        near.push([d, j]);
      }
      near.sort(function (p, q) { return p[0] - q[0]; });
      for (var k = 0; k < 3; k++) {
        var id = near[k][1];
        if (i < id) edges.push([i, id]);
      }
      if (i % 26 === 0) {
        // 跨区投射：只连「隔壁脑区里最近的那个」，不让细长直线横穿整个脑
        var nextRegion = (a.region + 1) % REGIONS.length;
        var bestId = -1, bestD = 1e9;
        for (var m = 0; m < N; m++) {
          if (neurons[m].region !== nextRegion) continue;
          var dd = Math.hypot((a.nx - neurons[m].nx) * W, (a.ny - neurons[m].ny) * H);
          if (dd < bestD) { bestD = dd; bestId = m; }
        }
        if (bestId >= 0 && bestD < 170) edges.push([i, bestId]);
      }
    }
    // 去重，限制总边数，避免画布过重
    var seen = {};
    edges = edges.filter(function (e) {
      var key = e[0] + ':' + e[1];
      if (seen[key]) return false;
      seen[key] = true;
      return true;
    }).slice(0, 2600);

    // 邻接表：放电扩散时按真实突触走，不再每帧扫 2600 条边
    adj = neurons.map(function () { return []; });
    edges.forEach(function (e) {
      if (adj[e[0]].length < 8) adj[e[0]].push(e[1]);
      if (adj[e[1]].length < 8) adj[e[1]].push(e[0]);
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
    position();
  }

  function position() {
    neurons.forEach(function (n) {
      // 留边：脑形不要贴边，右边留给文字
      n.x = 24 + n.nx * (W - 140);
      n.y = 30 + n.ny * (H - 90);
    });
  }

  // ---- 真实事件 → 亮多少个神经元 ----
  function magnitude(ev) {
    switch (ev.kind) {
      case 'judge': return 18 + Math.round((ev.intensity || 0) * 34);
      case 'memory': return 10 + (ev.facts || 0) * 9 + (ev.emotions || 0) * 5;
      case 'prompt': return Math.min(90, 8 + Math.round((ev.chars || 0) / 26));
      case 'tool': return ev.hit ? 46 : 7;
      case 'llm_start': return 26;
      case 'first_token': return 34;
      case 'tokens': return 10 + Math.round((ev.chars || 0) / 12);
      case 'voice_start': return 26;
      case 'voice_done': return Math.min(80, 10 + Math.round((ev.kb || 0) / 4));
      case 'voice_error': return 40;
      case 'turn_start': return 16;
      case 'turn_done': return 40;
      case 'turn_error': return 38;
      case 'bg_start': return 14;
      case 'bg_done': return 10;
      default: return 0;
    }
  }

  function hashStr(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  // 把真实 facts 挂到记忆区的神经元上；共享两字词的两条事实之间连一根「联想」
  function assignFacts(facts) {
    memAnchors = {};
    assocPairs = [];
    factTotal = facts.length;
    var memIds = [];
    neurons.forEach(function (n, i) { if (n.region === 2) memIds.push(i); });
    if (!memIds.length) return;
    var grams = {};
    facts.forEach(function (f) {
      var id = String(f.id || '');
      if (!id) return;
      memAnchors[id] = memIds[hashStr(id) % memIds.length];
      var text = String(f.text || '');
      for (var i = 0; i + 1 < text.length; i++) {
        var g = text.slice(i, i + 2);
        if (grams[g] === undefined) grams[g] = [];
        if (grams[g].length < 6 && grams[g].indexOf(id) < 0) grams[g].push(id);
      }
    });
    Object.keys(grams).forEach(function (g) {
      var ids = grams[g];
      for (var a = 0; a < ids.length; a++) {
        for (var b = a + 1; b < ids.length; b++) {
          var x = memAnchors[ids[a]], y = memAnchors[ids[b]];
          if (x !== undefined && y !== undefined && x !== y) assocPairs.push([x, y]);
        }
      }
    });
    assocPairs = assocPairs.slice(0, 4000);
    var load = document.getElementById('fact-load');
    if (load) load.textContent = factTotal + ' 条事实已接入';
  }

  function loadFacts() {
    var token = '';
    try { token = localStorage.getItem('companion.admin_token') || ''; } catch (e) { /* noop */ }
    fetch('/api/admin/facts?limit=200', { headers: token ? { 'X-Admin-Token': token } : {} })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        assignFacts(Array.isArray(d) ? d : (d.items || []));
      })
      .catch(function () { /* 读不到事实就只按条数放电，不影响直播 */ });
  }

  function regionOf(kind) {
    switch (kind) {
      case 'turn_start': return 0;
      case 'judge': return 1;
      case 'memory': return 2;
      case 'prompt': return 3;
      case 'tool': return 4;
      case 'llm_start': return 5;
      case 'tokens': return 5;
      case 'first_token': return 6;
      case 'voice_start': case 'voice_done': case 'voice_error': return 7;
      case 'turn_done': case 'turn_error': case 'bg_start': case 'bg_done': return 8;
      default: return -1;
    }
  }

  function describe(ev) {
    switch (ev.kind) {
      case 'turn_start': return '输入 ' + (ev.text_len || 0) + ' 字';
      case 'judge': return (ev.emotion || '?') + ' · ' + (ev.ms || 0) + 'ms';
      case 'memory': return '事实 ' + (ev.facts || 0) + ' / 情绪 ' + (ev.emotions || 0);
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
      case 'bg_start': return '后台消化…';
      case 'bg_done': return '消化完 · ' + (ev.ms || 0) + 'ms';
      default: return '';
    }
  }

  // 让某个脑区「放电」：从区中心向外，按真实量级决定点亮多少个神经元
  function fire(regionIdx, count, now) {
    if (regionIdx < 0 || count <= 0) return;
    var ids = [];
    neurons.forEach(function (n, i) {
      if (n.region === regionIdx) {
        var r = REGIONS[regionIdx];
        ids.push([Math.hypot((n.nx - r.cx), n.ny - r.cy), i]);
      }
    });
    ids.sort(function (a, b) { return a[0] - b[0]; });
    var take = Math.min(ids.length, Math.max(4, count));
    for (var k = 0; k < take; k++) {
      cascade.push({ i: ids[k][1], due: now + ids[k][0] * 26, hops: 0, energy: 1 });
    }
    firedTotal += take;
    document.getElementById('fired').textContent = String(firedTotal);
  }

  function onEvent(ev) {
    if (!ev || !ev.kind) return;
    var now = performance.now();
    evCount++;
    lastEventAt = Date.now();
    document.getElementById('ev-count').textContent = String(evCount);
    setConn('on', '实时');

    var ri = regionOf(ev.kind);
    if (ri >= 0) {
      REGIONS[ri].value = describe(ev);
      REGIONS[ri].count = (REGIONS[ri].count || 0) + 1;
      fire(ri, magnitude(ev), now);
      // 记忆不是「整片一起亮」：先点亮这次真正召回的几条，再顺着联想带出一串
      if (ev.kind === 'memory' && ev.ids && ev.ids.length) {
        ev.ids.forEach(function (id, k) {
          var anchor = memAnchors[String(id)];
          if (anchor === undefined) return;
          cascade.push({ i: anchor, due: now + 18 + k * 22, hops: 0, energy: 1.05 });
        });
        var touched = ev.ids.map(function (id) { return memAnchors[String(id)]; });
        for (var p = 0; p < assocPairs.length; p++) {
          var pair = assocPairs[p];
          if (touched.indexOf(pair[0]) >= 0) {
            cascade.push({ i: pair[1], due: now + 90, hops: 1, energy: .55 });
          } else if (touched.indexOf(pair[1]) >= 0) {
            cascade.push({ i: pair[0], due: now + 90, hops: 1, energy: .55 });
          }
        }
      }
      // 不再凭空「投射到下一区」：联络只沿真实突触（adj）传，事件本身按真实时序到达
    }
    if (ev.kind === 'turn_done' || ev.kind === 'turn_error') {
      lastTurnMs = ev.ms || null;
      document.getElementById('turn-ms').textContent = lastTurnMs ? Math.round(lastTurnMs) + 'ms' : '—';
    }
    pushFeed(ev);
  }

  // ---- 画面 ----
  function frame(now) {
    var dt = Math.min(0.05, (now - t0) / 1000);
    t0 = now;
    ctx.clearRect(0, 0, W, H);

    // 背景：极暗的脑雾，帮助看出轮廓
    var bg = ctx.createRadialGradient(W * 0.5, H * 0.52, 20, W * 0.5, H * 0.52, Math.max(W, H) * 0.62);
    bg.addColorStop(0, 'rgba(60,90,88,.055)');
    bg.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);

    // 脑廓：一颗脑的外缘 + 一条纵裂（不叠两个圈，避免看起来像两个靶子）
    ctx.save();
    var cx2 = W * 0.53, cy2 = H * 0.45, rx = W * 0.315, ry = H * 0.285;
    ctx.beginPath();
    ctx.ellipse(cx2, cy2, rx, ry, 0, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(86,150,140,.032)';
    ctx.fill();
    ctx.strokeStyle = 'rgba(120,200,185,.075)';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx2, cy2 - ry * 0.95);
    ctx.quadraticCurveTo(cx2 + W * 0.008, cy2 - ry * 0.2, cx2, cy2 + ry * 0.62);
    ctx.strokeStyle = 'rgba(120,200,185,.06)';
    ctx.stroke();
    ctx.restore();

    // 突触：冷线一次批量画，热线单独画，成本可控
    ctx.strokeStyle = 'rgba(150,178,172,.085)';
    ctx.lineWidth = 0.75;
    ctx.beginPath();
    for (var e = 0; e < edges.length; e++) {
      var A = neurons[edges[e][0]], B = neurons[edges[e][1]];
      if (!A || !B) continue;
      ctx.moveTo(A.x, A.y);
      ctx.lineTo(B.x, B.y);
    }
    ctx.stroke();

    ctx.strokeStyle = 'rgba(130,220,200,.20)';
    ctx.lineWidth = 1.1;
    for (var e2 = 0; e2 < edges.length; e2++) {
      var A2 = neurons[edges[e2][0]], B2 = neurons[edges[e2][1]];
      var h = Math.max(A2.heat, B2.heat);
      if (h < 0.28) continue;
      ctx.globalAlpha = h * 0.65;
      ctx.beginPath();
      ctx.moveTo(A2.x, A2.y);
      ctx.lineTo(B2.x, B2.y);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // 联想：共享两字词的事实之间那根线（只在有相关事实时才画，冷态很淡）
    if (assocPairs.length) {
      ctx.strokeStyle = 'rgba(170,145,215,.075)';
      ctx.lineWidth = 0.75;
      ctx.beginPath();
      for (var q = 0; q < assocPairs.length; q++) {
        var P1 = neurons[assocPairs[q][0]], P2 = neurons[assocPairs[q][1]];
        if (!P1 || !P2) continue;
        ctx.moveTo(P1.x, P1.y);
        ctx.lineTo(P2.x, P2.y);
      }
      ctx.stroke();
    }

    // 放电队列
    for (var c = cascade.length - 1; c >= 0; c--) {
      var item = cascade[c];
      if (now >= item.due) {
        var n0 = neurons[item.i];
        if (n0) {
          n0.heat = Math.min(1.35, n0.heat + item.energy * 1.2);
          // 沿真实突触再传一跳：神经元之间真连着的才传
          if (item.hops < 2) {
            var links = adj[item.i] || [];
            for (var L = 0; L < links.length; L++) {
              if (Math.random() < 0.55 * item.energy) {
                cascade.push({
                  i: links[L], due: now + 22 + Math.random() * 26,
                  hops: item.hops + 1, energy: item.energy * 0.62
                });
              }
            }
          }
        }
        cascade.splice(c, 1);
      }
    }
    if (cascade.length > 900) cascade.splice(0, cascade.length - 900);

    // 神经元
    var breath = 0.5 + 0.5 * Math.sin(now / 2400);
    var peak = 0;
    var active = Date.now() - lastEventAt < 4000;   // 正在想/说的时候，注意力只落在放电区
    for (var i = 0; i < neurons.length; i++) {
      var n = neurons[i];
      // 记忆的余温更长：想起过的事会在心里留得久一点（其余按 1.9 秒退）
      var tau = n.region === 2 ? 5.2 : 1.35;
      if (!HOLD) n.heat *= Math.exp(-dt / tau);
      if (n.heat > peak) peak = n.heat;
      var hot = n.heat > 0.06;
      var r = n.size + (hot ? n.heat * 1.7 : 0);
      if (hot) {
        ctx.fillStyle = 'rgba(120,230,205,' + Math.min(0.95, n.heat * 0.9) + ')';
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fill();
        if (n.heat > 0.55) {
          var g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 4.2);
          g.addColorStop(0, 'rgba(120,230,205,' + (n.heat * 0.22) + ')');
          g.addColorStop(1, 'rgba(120,230,205,0)');
          ctx.fillStyle = g;
          ctx.beginPath();
          ctx.arc(n.x, n.y, r * 4.2, 0, Math.PI * 2);
          ctx.fill();
        }
      } else {
        // 静息神经元：能看见这一整片脑，但不喧宾夺主
        ctx.fillStyle = 'rgba(186,210,205,' + ((active ? 0.09 : 0.16) + breath * 0.05) + ')';
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.size, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // 脑区标签：只写九个名字与最近一次真实数值，不遮挡神经元
    var peakEl = document.getElementById('peak');
    if (peakEl) peakEl.textContent = peak.toFixed(2);
    REGIONS.forEach(function (r) {
      var x = 24 + r.cx * (W - 140);
      var y = 30 + r.cy * (H - 90);
      var heat = regionHeat(r);
      ctx.textAlign = 'center';
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(8,12,14,.8)';
      ctx.font = '600 12px -apple-system, "Microsoft YaHei", sans-serif';
      ctx.strokeText(r.name, x, y - 26);
      ctx.fillStyle = 'rgba(225,235,232,' + (0.5 + heat * 0.45) + ')';
      ctx.fillText(r.name, x, y - 26);
      if (r.value) {
        var v = r.value.length > 18 ? r.value.slice(0, 17) + '…' : r.value;
        ctx.font = '500 10.5px -apple-system, "Microsoft YaHei", sans-serif';
        ctx.strokeText(v, x, y - 12);
        ctx.fillStyle = 'rgba(165,178,180,' + (0.35 + heat * 0.4) + ')';
        ctx.fillText(v, x, y - 12);
      }
      ctx.textAlign = 'start';
    });
  }

  function regionHeat(r) {
    var h = 0;
    neurons.forEach(function (n) { if (n.region === REGIONS.indexOf(r)) h = Math.max(h, n.heat); });
    return Math.min(1, h);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pushFeed(ev) {
    var t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    var r = REGIONS[Math.max(0, regionOf(ev.kind))] || { name: '事件' };
    feed.unshift({ t: t, txt: r.name + ' · ' + describe(ev) });
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

  function setConn(state, txt) {
    document.getElementById('conn-dot').className = 'dot' + (state === 'on' ? ' on' : (state === 'err' ? ' err' : ''));
    document.getElementById('conn-txt').textContent = txt;
  }

  function renderLegend() {
    var box = document.getElementById('legend-rows');
    box.innerHTML = '';
    REGIONS.forEach(function (r) {
      var row = document.createElement('div');
      row.className = 'row' + (regionHeat(r) > 0.08 ? '' : ' idle');
      row.innerHTML =
        '<span class="sw" style="background:' + r.color + '"></span>' +
        '<span class="nm">' + escapeHtml(r.name) +
        '<br><span style="color:#6e797d;font-size:11px">' + escapeHtml(r.hint) + '</span></span>' +
        '<span class="val">' + (r.value ? escapeHtml(r.value) : '—') + '</span>';
      box.appendChild(row);
    });
    document.getElementById('legend-count').textContent = String(neurons.length);
  }
  setInterval(renderLegend, 900);

  function loop(now) {
    frame(now);
    requestAnimationFrame(loop);
  }

  function connect() {
    var token = '';
    try { token = localStorage.getItem('companion.admin_token') || ''; } catch (e) { /* noop */ }
    var es = new EventSource('/api/admin/live/stream' + (token ? '?token=' + encodeURIComponent(token) : ''));
    es.onopen = function () { setConn('on', '实时'); };
    es.onmessage = function (m) {
      try { onEvent(JSON.parse(m.data)); } catch (e) { /* 坏帧不打断整条流 */ }
    };
    es.onerror = function () {
      if (Date.now() - lastEventAt > 4000) setConn('err', '断线重连中…');
    };
  }

  // 定格按钮：不衰减，方便数「刚才亮了多少个神经元」
  (function holdBtn() {
    var btn = document.getElementById('hold-btn');
    if (!btn) return;
    function sync() {
      btn.textContent = HOLD ? '继续' : '定格';
      btn.className = 'pill pill-btn' + (HOLD ? ' on' : '');
    }
    btn.addEventListener('click', function () { HOLD = !HOLD; sync(); });
    sync();
  })();

  build();
  loadFacts();
  resize();
  renderLegend();
  window.addEventListener('resize', function () { resize(); });
  requestAnimationFrame(loop);
  connect();

  // 刚打开先补最近事件：直接让对应脑区有真实余温，不是凭空亮
  (function prime() {
    var token = '';
    try { token = localStorage.getItem('companion.admin_token') || ''; } catch (e) { /* noop */ }
    fetch('/api/admin/live/recent?limit=12', { headers: token ? { 'X-Admin-Token': token } : {} })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        (d.items || []).forEach(function (ev) {
          var ri = regionOf(ev.kind);
          if (ri >= 0) {
            REGIONS[ri].value = describe(ev);
            fire(ri, Math.min(30, Math.round(magnitude(ev) / 2)), performance.now());
          }
          pushFeed(ev);
        });
      })
      .catch(function () { /* 补历史失败不影响直播 */ });
  })();
})();
