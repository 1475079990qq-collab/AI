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
  var NEURON_COUNT = 680;      // BrainGrowth 缺失/异常时的回退基数
  var BG = (typeof window !== 'undefined' && window.BrainGrowth) ? window.BrainGrowth : null;
  var lessonTotal = 0;
  var factCap = null;           // 不设数量上限（模块返回 null，前端不显示封顶值）
  var lessonCap = null;
  var lessonAnchors = {};

  // 八个认知区（冻结契约，编号三路共用）：听见 / 情绪 / 回忆 / 联想 / 判断 / 表达 / 反思 / 沉淀
  var REGIONS = [
    { key: 'hear',    name: '听见', color: COOL,       hint: '他刚说的那句',         weight: 0.09, cx: 0.50, cy: 0.86 },
    { key: 'mood',    name: '情绪', color: WARM,       hint: '这一轮读到的情绪',     weight: 0.12, cx: 0.26, cy: 0.66 },
    { key: 'recall',  name: '回忆', color: '#b49ae0',  hint: '这一轮想起的旧事',     weight: 0.12, cx: 0.74, cy: 0.62 },
    { key: 'assoc',   name: '联想', color: '#7fd0e8',  hint: '被同一个词牵出来的',   weight: 0.08, cx: 0.60, cy: 0.78 },
    { key: 'judge',   name: '判断', color: '#d9c37a',  hint: '这一轮拿的主意',       weight: 0.14, cx: 0.46, cy: 0.54 },
    { key: 'speak',   name: '表达', color: ACCENT,     hint: '正在说出的字',         weight: 0.18, cx: 0.55, cy: 0.26 },
    { key: 'reflect', name: '反思', color: '#cfd8da',  hint: '说完之后的回头看',     weight: 0.12, cx: 0.28, cy: 0.34 },
    { key: 'store',   name: '沉淀', color: '#e0a0c0',  hint: '真的写进记忆的东西',   weight: 0.15, cx: 0.17, cy: 0.52 }
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
  var socketErr = false;
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

  // 把模块算出的整图接进当前画面：先算完再换引用，旧图在新图就绪前继续画
  function applyGrowth(res) {
    neurons = res.neurons.map(function (n) {
      return {
        nx: n.nx, ny: n.ny, region: n.region, heat: 0,
        size: n.size, delay: n.delay, fid: n.fid, kind: n.kind
      };
    });
    edges = res.edges || [];
    assocPairs = res.assocPairs || [];
    memAnchors = res.memAnchors || {};
    lessonAnchors = res.lessonAnchors || {};
    factTotal = res.factTotal || 0;
    lessonTotal = res.lessonTotal || 0;
    factCap = res.factCap == null ? null : res.factCap;
    lessonCap = res.lessonCap == null ? null : res.lessonCap;
    adj = neurons.map(function () { return []; });
    edges.forEach(function (e) {
      if (adj[e[0]].length < 8) adj[e[0]].push(e[1]);
      if (adj[e[1]].length < 8) adj[e[1]].push(e[0]);
    });
    REGIONS[2].hint = '一条事实一颗回忆神经元（不设上限）';
    if (W >= 320) position();
  }

  function build() {
    if (BG) {
      try {
        applyGrowth(BG.build([], []));
        return;
      } catch (e) {
        console.warn('[brain] BrainGrowth 构建失败，退回固定 ' + NEURON_COUNT + '：', e && e.message || e);
        BG = null;
      }
    }
    buildLegacy();
  }

  // 回退路径：BrainGrowth 缺失/异常时的旧固定 680 逻辑，保证页面不白屏
  function buildLegacy() {
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
    // 画布尺寸跟着面板走：面板多高，画布就多高，不再固定上限把下半截留黑
    W = rect.width > 0 ? rect.width : 320;
    H = rect.height > 0 ? rect.height : 320;
    canvas.width = Math.round(W * DPR);
    canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    position();
  }

  function position() {
    var padX = 20, padY = 18;
    neurons.forEach(function (n) {
      // 留边：四周均匀留白，脑形铺满整个画布，不再缩在上半区
      n.x = padX + n.nx * (W - padX * 2);
      n.y = padY + n.ny * (H - padY * 2);
    });
  }

  // ---- 真实事件 → 亮多少个神经元（只认八个认知区；工序事件不点亮）----
  function magnitude(ev) {
    switch (ev.kind) {
      case 'turn_start': return 16;
      case 'judge': return 18 + Math.round((ev.intensity || 0) * 34);
      case 'memory': return 10 + (ev.facts || 0) * 9 + (ev.emotions || 0) * 5;
      case 'llm_start': return 26;
      case 'think_draft': return 14 + Math.round((ev.chars || 0) / 4);
      case 'first_token': return 34;
      case 'tokens': return 10 + Math.round((ev.chars || 0) / 12);
      case 'self_check': return ev.verdict === 'pass' ? 14 : 34;
      case 'ruminate_start': return 10;
      case 'ruminate_done': return ev.verdict === 'pass' ? 12 : 22;
      case 'bg_start': return 10;
      case 'bg_done': return 8;
      case 'fact_saved': case 'fact_added': case 'fact_stored': case 'lesson_saved': return 12;
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

  // 生长已由 BrainGrowth 按条长神经元，不再对数调权重；保留空壳避免调用点报错
  function growMemoryRegion() {
  }

  // 图例计数行：事实 N（不设上限） · 教训 M（不设上限）（数字来自模块，不写死）
  function updateFactLoad() {
    var load = document.getElementById('fact-load');
    if (load) load.textContent = '事实 ' + factTotal + '（不设上限） · 教训 ' + lessonTotal + '（不设上限）';
  }

  // 把真实 facts / lessons 交给模块重建脑图；共享两字词的联想边由模块产出
  function assignFacts(facts, lessons) {
    facts = facts || [];
    lessons = lessons || [];
    if (!BG) { assignFactsLegacy(facts); return; }

    var HEAVY = 300;   // 超过这个量级，先出基数图分帧再补事实点，避免打开页面主线程长卡
    var CHUNK = 300;
    // 用累计切片重建：同种子下已存在事实的坐标由 seed⊕k 派生，跨帧不漂；
    // 每次先算完再换引用，旧图在新图就绪前继续画，不会白屏。
    function rebuildSlice(slice) {
      try {
        var res = BG.build(slice, lessons);
        applyGrowth(res);
        renderLegend();
      } catch (e) {
        console.warn('[brain] BrainGrowth 重建失败，保留当前脑图：', e && e.message || e);
      }
      updateFactLoad();
    }

    if (facts.length <= HEAVY) { rebuildSlice(facts); return; }

    // 第一帧先出基数 + 教训图，事实点随后分帧补上
    try { applyGrowth(BG.build([], lessons)); } catch (e) { /* 保留现状 */ }
    if (W >= 320) position();
    renderLegend();
    updateFactLoad();
    var done = 0;
    function nextChunk() {
      done = Math.min(facts.length, done + CHUNK);
      rebuildSlice(facts.slice(0, done));
      if (done < facts.length) setTimeout(nextChunk, 30);
    }
    setTimeout(nextChunk, 30);
  }

  // 回退路径：模块缺失时沿用旧的 hash 挂靠（多条事实挤一颗，不真实增长）
  function assignFactsLegacy(facts) {
    memAnchors = {};
    assocPairs = [];
    factTotal = facts.length;
    var memIds = [];
    neurons.forEach(function (n, i) { if (n.region === 2) memIds.push(i); });
    if (!memIds.length) { updateFactLoad(); return; }
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
    updateFactLoad();
  }

  function loadFacts() {
    var token = '';
    try { token = localStorage.getItem('companion.admin_token') || ''; } catch (e) { /* noop */ }
    var headers = token ? { 'X-Admin-Token': token } : {};
    Promise.all([
      fetch('/api/admin/facts?limit=20000', { headers: headers }).then(function (r) { return r.json(); }),
      fetch('/api/admin/lessons?limit=20000', { headers: headers }).then(function (r) { return r.json(); }).catch(function () { return { items: [] }; })
    ]).then(function (pair) {
      var facts = Array.isArray(pair[0]) ? pair[0] : ((pair[0] && pair[0].items) || []);
      var lessons = (pair[1] && pair[1].items) || [];
      assignFacts(facts, lessons);
    }).catch(function () { /* 读不到事实就只按条数放电，不影响直播 */ });
  }

  // 事件 → 认知区下标；工序事件（拼上下文 / 合成语音 / 落库 / 工具）不设脑区，返回 -1
  function regionOf(kind) {
    switch (kind) {
      case 'turn_start': return 0;
      case 'judge': return 1;
      case 'memory': return 2;
      case 'llm_start': case 'think_draft': return 4;
      case 'tokens': case 'first_token': return 5;
      case 'self_check': case 'ruminate_start': case 'ruminate_done': return 6;
      case 'bg_start': case 'bg_done':
      case 'fact_saved': case 'fact_added': case 'fact_stored': case 'lesson_saved': return 7;
      // 以下事件只进左下事件流，不上画布、不点亮：
      // prompt 拼上下文 / tool 动手 / voice_* 合成语音 / turn_done 落库
      case 'prompt': case 'tool':
      case 'voice_start': case 'voice_done': case 'voice_error':
      case 'turn_done': case 'turn_error': return -1;
      default: return -1;
    }
  }

  // 副文案截断：超长一律加「…」，画布标签两行都放得下
  function clipText(s, n) {
    s = String(s == null ? '' : s).trim();
    if (!s) return '';
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function linkText(linked) {
    var arr = Array.isArray(linked) ? linked :
      String(linked || '').split(/[、,，\s]+/).filter(Boolean);
    if (!arr.length) return '没连到别的';
    return clipText(arr.join(' · '), 14);
  }

  // 后端情绪枚举 → 人话（只做展示映射，不改判定）
  var MOOD_CN = {
    neutral: '平静', sad: '难过', happy: '开心', angry: '生气',
    fear: '担心', disgust: '反感', surprised: '意外', care: '在意'
  };
  function moodLabel(label) {
    return MOOD_CN[label] || label || '';
  }

  // 区标签第二行：这一区此刻「现实是什么内容 / 在说什么 / 想什么」；字段未到就走默认句，绝不编
  function describe(ev) {
    switch (ev.kind) {
      case 'turn_start':
        return ev.text ? '“' + clipText(ev.text, 12) + '”' : '这轮没有听见话';
      case 'judge':
        if (!ev.emotion) return '没读出情绪';
        return moodLabel(ev.emotion) + ' · ' + (ev.intensity != null ? Number(ev.intensity).toFixed(2) : '—');
      case 'memory':
        return ev.fact_text ? clipText(ev.fact_text, 14) : '这次没想起旧事';
      case 'llm_start':
        return ev.intent || '先按直觉回';
      case 'think_draft':
        return '先在心里过一遍';
      case 'tokens':
        return ev.text ? '…' + clipText(ev.text.slice(-10), 10) : '还没开口';
      case 'first_token':
        return '正要开口…';
      case 'self_check':
        return '说完先自查一遍';
      case 'ruminate_start':
        return '正在回头想…';
      case 'ruminate_done':
        var why = ev.why || ev.how || '';
        return why ? clipText(why, 14) : '这一轮还没回头看';
      case 'bg_start': return REGIONS[7].value || '在后台消化…';
      case 'bg_done': return REGIONS[7].value || '这轮没留下新东西';
      case 'fact_saved': case 'fact_added': case 'fact_stored': case 'lesson_saved':
        return ev.fact_text ? '记住：' + clipText(ev.fact_text, 12) : '这轮没留下新东西';
      default: return '';
    }
  }

  // 事件流短句：工序事件降为灰字「内部：…」，工具为正常色「行动：…」，其余归到对应认知区
  function feedMeta(ev) {
    switch (ev.kind) {
      case 'prompt': return { txt: '内部：拼上下文', region: '内部', internal: true };
      case 'voice_start': case 'voice_done': case 'voice_error':
        return { txt: '内部：合成语音', region: '内部', internal: true };
      case 'turn_done': case 'turn_error':
        return { txt: '内部：落库', region: '内部', internal: true };
      case 'tool':
        return ev.hit && ev.name
          ? { txt: '行动：' + ev.name, region: '行动', internal: false }
          : { txt: '这轮没动手', region: '行动', internal: false };
      case 'turn_start': return { txt: '他说了一句', region: '听见', internal: false };
      case 'judge': return { txt: moodLabel(ev.emotion) || '没读出情绪', region: '情绪', internal: false };
      case 'memory': return { txt: '想起 ' + (ev.facts || 0) + ' 条旧事', region: '回忆', internal: false };
      case 'llm_start': return { txt: '拿主意', region: '判断', internal: false };
      case 'think_draft': return { txt: '先在心里过一遍', region: '判断', internal: false };
      case 'tokens': return { txt: '正在说（' + (ev.chars || 0) + ' 字）', region: '表达', internal: false };
      case 'first_token': return { txt: '开口了', region: '表达', internal: false };
      case 'self_check': return { txt: '说完先自查一遍', region: '反思', internal: false };
      case 'ruminate_start': return { txt: '回头想刚才那句…', region: '反思', internal: false };
      case 'ruminate_done':
        return { txt: ev.why ? '复盘：' + clipText(ev.why, 12) : '复盘了一轮', region: '反思', internal: false };
      case 'bg_start': return { txt: '后台消化…', region: '沉淀', internal: false };
      case 'bg_done': return { txt: '后台消化完', region: '沉淀', internal: false };
      case 'fact_saved': case 'fact_added': case 'fact_stored': case 'lesson_saved':
        return { txt: ev.fact_text ? '记住：' + clipText(ev.fact_text, 12) : '留下一条', region: '沉淀', internal: false };
      default: return { txt: describe(ev) || '事件', region: '事件', internal: false };
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

    // 密度自适应：点变多以后，半径 / 亮度 / 呼吸幅度按总量收缩，避免糊成一片白光
    var N = neurons.length || 1;
    var dens = Math.min(1, Math.sqrt(680 / N));   // 面积等效收缩，680 点时为 1
    var dense = N > 900;

    // 背景：极暗的脑雾，帮助看出轮廓
    var bg = ctx.createRadialGradient(W * 0.5, H * 0.52, 20, W * 0.5, H * 0.52, Math.max(W, H) * 0.62);
    bg.addColorStop(0, 'rgba(60,90,88,.055)');
    bg.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);

    // 脑廓：一颗脑的外缘 + 一条纵裂（不叠两个圈，避免看起来像两个靶子）
    ctx.save();
    var cx2 = W * 0.50, cy2 = H * 0.52, rx = W * 0.40, ry = H * 0.40;
    ctx.beginPath();
    ctx.ellipse(cx2, cy2, rx, ry, 0, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(86,150,140,.055)';
    ctx.fill();
    ctx.strokeStyle = 'rgba(120,200,185,.16)';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx2, cy2 - ry * 0.95);
    ctx.quadraticCurveTo(cx2 + W * 0.008, cy2 - ry * 0.2, cx2, cy2 + ry * 0.62);
    ctx.strokeStyle = 'rgba(120,200,185,.06)';
    ctx.stroke();
    ctx.restore();

    // 突触：冷线一次批量画，热线单独画，成本可控
    ctx.strokeStyle = 'rgba(150,178,172,' + Math.max(0.10, 0.20 * dens) + ')';
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

    // 联想：共享两字词的事实之间那根线（静息压很淡，记忆区有余温时才提亮；
    // 总量越大只留最淡一层，透明度随总量线性下降，边的取舍仍由生长模块决定）
    if (assocPairs.length) {
      var memHeat = regionHeat(REGIONS[2]);
      var assocFade = Math.max(0.4, 680 / N);
      var assocAlpha = (0.032 + memHeat * 0.085) * assocFade;
      ctx.strokeStyle = 'rgba(170,145,215,' + assocAlpha + ')';
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
    var restAlpha = (active ? 0.15 : 0.38) * (0.7 + 0.3 * dens);
    var breathAmp = 0.05 * dens;
    for (var i = 0; i < neurons.length; i++) {
      var n = neurons[i];
      // 记忆的余温更长：想起过的事会在心里留得久一点（其余按 1.9 秒退）
      var tau = n.region === 2 ? 5.2 : 1.35;
      if (!HOLD) n.heat *= Math.exp(-dt / tau);
      if (n.heat > peak) peak = n.heat;
      var hot = n.heat > 0.06;
      var r = n.size * dens + (hot ? n.heat * 1.7 * dens : 0);
      if (hot) {
        ctx.fillStyle = 'rgba(120,230,205,' + Math.min(0.9, n.heat * 0.9 * (0.75 + 0.25 * dens)) + ')';
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fill();
        if (n.heat > 0.55) {
          var halo = r * (dense ? 3.4 : 4.2);
          var g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, halo);
          g.addColorStop(0, 'rgba(120,230,205,' + (n.heat * 0.22 * (0.7 + 0.3 * dens)) + ')');
          g.addColorStop(1, 'rgba(120,230,205,0)');
          ctx.fillStyle = g;
          ctx.beginPath();
          ctx.arc(n.x, n.y, halo, 0, Math.PI * 2);
          ctx.fill();
        }
      } else {
        // 静息神经元：整片脑要看得清（不然停止对话时几乎全黑），但不喧宾夺主
        var rr = Math.max(0.7, (n.size + 0.35) * dens);
        ctx.fillStyle = 'rgba(186,210,205,' + (restAlpha + breath * breathAmp) + ')';
        ctx.beginPath();
        ctx.arc(n.x, n.y, rr, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // 脑区标签：名字 + 一行副文案（这一区此刻在听 / 在想 / 在说什么），暗底板 + 按宽度避让
    var peakEl = document.getElementById('peak');
    if (peakEl) peakEl.textContent = peak.toFixed(2);
    var labels = [];
    REGIONS.forEach(function (r) {
      var x = 20 + r.cx * (W - 40);
      var y = 18 + r.cy * (H - 36);
      var heat = regionHeat(r);
      var cut = dense ? 14 : 20;
      var v = r.value ? (r.value.length > cut ? r.value.slice(0, cut - 1) + '…' : r.value) : '';

      ctx.textAlign = 'center';
      ctx.font = '600 12px -apple-system, "Microsoft YaHei", sans-serif';
      var nameW = ctx.measureText(r.name).width;
      var valW = 0;
      if (v) {
        ctx.font = '500 10.5px -apple-system, "Microsoft YaHei", sans-serif';
        valW = ctx.measureText(v).width;
      }
      var plateW = Math.max(nameW, valW) + 16;
      var plateH = v ? 30 : 20;
      labels.push({ r: r, x: x, v: v, heat: heat, plateW: plateW, plateH: plateH,
        top: y - 32, left: x - plateW / 2 });
    });
    // 两两避让：纵向推开叠在一起的底板，再钳回画布内
    for (var pass = 0; pass < 2; pass++) {
      for (var a = 0; a < labels.length; a++) {
        for (var b = a + 1; b < labels.length; b++) {
          var A = labels[a], B = labels[b];
          var ox = Math.min(A.left + A.plateW, B.left + B.plateW) - Math.max(A.left, B.left);
          var oy = Math.min(A.top + A.plateH, B.top + B.plateH) - Math.max(A.top, B.top);
          if (ox > 2 && oy > 0) {
            var lower = A.top > B.top ? A : B;
            lower.top += oy + 4;
            lower.top = Math.min(H - lower.plateH - 4, Math.max(8, lower.top));
          }
        }
      }
    }
    labels.forEach(function (L) {
      var r = L.r, x = L.x, top = L.top;
      var px = x - L.plateW / 2, py = top, rad = 6;
      // 半透明暗底板：即使压在联想线 / 神经网上，字也读得清
      ctx.fillStyle = 'rgba(8,12,14,' + (dense ? 0.70 : 0.62) + ')';
      ctx.beginPath();
      ctx.moveTo(px + rad, py);
      ctx.lineTo(px + L.plateW - rad, py);
      ctx.quadraticCurveTo(px + L.plateW, py, px + L.plateW, py + rad);
      ctx.lineTo(px + L.plateW, py + L.plateH - rad);
      ctx.quadraticCurveTo(px + L.plateW, py + L.plateH, px + L.plateW - rad, py + L.plateH);
      ctx.lineTo(px + rad, py + L.plateH);
      ctx.quadraticCurveTo(px, py + L.plateH, px, py + L.plateH - rad);
      ctx.lineTo(px, py + rad);
      ctx.quadraticCurveTo(px, py, px + rad, py);
      ctx.closePath();
      ctx.fill();

      ctx.textAlign = 'center';
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(8,12,14,.8)';
      ctx.font = '600 12px -apple-system, "Microsoft YaHei", sans-serif';
      ctx.strokeText(r.name, x, top + 14);
      ctx.fillStyle = 'rgba(225,235,232,' + (0.5 + L.heat * 0.45) + ')';
      ctx.fillText(r.name, x, top + 14);
      if (L.v) {
        ctx.font = '500 10.5px -apple-system, "Microsoft YaHei", sans-serif';
        ctx.strokeText(L.v, x, top + 26);
        ctx.fillStyle = 'rgba(165,178,180,' + (0.35 + L.heat * 0.4) + ')';
        ctx.fillText(L.v, x, top + 26);
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

  function relFeedTime(at) {
    var s = Math.max(0, Math.round(Date.now() / 1000 - at));
    if (s < 60) return s + ' 秒前';
    if (s < 3600) {
      var m = Math.floor(s / 60), sec = s % 60;
      return m + ' 分 ' + sec + ' 秒前';
    }
    var d = new Date(at * 1000);
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  function renderFeed() {
    var box = document.getElementById('feed-lines');
    if (!box) return;
    box.innerHTML = '';
    feed.forEach(function (f) {
      var d = document.createElement('div');
      d.className = 'line' + (f.internal ? ' internal' : '');
      d.innerHTML =
        '<span class="k">' + escapeHtml(relFeedTime(f.at)) + '</span>' +
        '<span class="region">' + escapeHtml(f.region) + '</span>' +
        escapeHtml(f.txt);
      box.appendChild(d);
    });
  }

  function pushFeed(ev) {
    var at = (ev.t && isFinite(ev.t)) ? ev.t : Date.now() / 1000;
    var meta = feedMeta(ev);
    feed.unshift({ at: at, region: meta.region, txt: meta.txt, internal: !!meta.internal });
    // 主事件最多 8 条；后台工序的灰字记录另留 4 条，链路里始终看得见
    function dropOldestInternal() {
      for (var i = feed.length - 1; i >= 0; i--) {
        if (feed[i].internal) { feed.splice(i, 1); return; }
      }
    }
    var ni = 0, ic = 0;
    feed.forEach(function (f) { if (f.internal) ic++; else ni++; });
    while (ni > 6) {
      for (var i = feed.length - 1; i >= 0; i--) {
        if (!feed[i].internal) { feed.splice(i, 1); ni--; break; }
      }
      if (feed.length === 0) break;
    }
    while (ic > 3) { dropOldestInternal(); ic--; }
    // 联想区：这一轮被同一个词牵出来的，linked 非空时让它也亮一小片（onEvent 不改，在此补一次）
    if (ev.kind === 'memory' && ev.linked) {
      REGIONS[3].value = linkText(ev.linked);
      fire(3, 10, performance.now());
    }
    // 工具不设脑区，但「先动手」这个判断仍写进判断区副文案
    if (ev.kind === 'tool' && ev.hit && ev.name) {
      REGIONS[4].value = '先动手：' + ev.name;
    }
    renderFeed();
  }

  // 相对时间每秒往前走一格，同秒到达的事件也能看出先后
  setInterval(renderFeed, 1000);

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
      var sw = document.createElement('span');
      sw.className = 'sw';
      sw.style.background = r.color;
      var main = document.createElement('div');
      main.className = 'main';
      var line1 = document.createElement('div');
      line1.className = 'line1';
      var nm = document.createElement('span');
      nm.className = 'nm';
      nm.textContent = r.name;
      var val = document.createElement('span');
      val.className = 'val';
      val.textContent = r.value || '—';
      line1.appendChild(nm);
      line1.appendChild(val);
      var hint = document.createElement('div');
      hint.className = 'hint';
      hint.textContent = r.hint;
      main.appendChild(line1);
      main.appendChild(hint);
      row.appendChild(sw);
      row.appendChild(main);
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
    socketErr = false;
    var es = new EventSource('/api/admin/live/stream' + (token ? '?token=' + encodeURIComponent(token) : ''));
    es.onopen = function () { socketErr = false; setConn('on', '实时'); };
    es.onmessage = function (m) {
      try { onEvent(JSON.parse(m.data)); } catch (e) { /* 坏帧不打断整条流 */ }
    };
    es.onerror = function () { socketErr = true; };
    // 不凭一次 onerror 就报断线：以最近一次真实事件为准，超过 10 秒仍无帧才分级提示；
    // 从启动到现在一帧都没收到过，先按「等待事件」灰显，不直接标红。
    setInterval(function () {
      var stale = !lastEventAt || (Date.now() - lastEventAt > 10000);
      if (!stale) setConn('on', '实时');
      else if (socketErr && lastEventAt) setConn('err', '正在重连…');
      else setConn('wait', '等待事件…');
    }, 1000);
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
    fetch('/api/admin/live/recent?limit=24', { headers: token ? { 'X-Admin-Token': token } : {} })
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
