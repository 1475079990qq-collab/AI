/* 控制台逻辑：全部走 CONSOLE_URL + /api/admin/...
 * 契约见 CONTRACT.md 第 4 / 10.5 / 10.6 节。
 * 保留 config.js 地址推导、X-Admin-Token、通用容错 api()/asList()/normalizeParams()/pick()。
 * 12 个板块左侧导航分区，表单由 /providers 的 fields 动态生成。
 * 任一分块失败只在该块内显示友好降级提示，绝不整页白屏。
 */
(function () {
  'use strict';

  var CFG = window.COMPANION_CONFIG || {};
  var LS_CONSOLE = 'companion.console';
  var LS_BRAIN = 'companion.brain';
  var LS_TOKEN = 'companion.admin_token';
  var LS_NAV = 'companion.nav_groups';

  var EMOTION_LABELS = ['happy', 'sad', 'angry', 'surprised', 'fear', 'disgust', 'neutral', 'care'];
  var EMOTION_ZH = {
    happy: '开心', sad: '难过', angry: '生气', surprised: '惊讶',
    fear: '害怕', disgust: '厌恶', neutral: '平静', care: '关切'
  };

  // ---- 界面中文化 ----
  // 技术代号（mock / http / onnx …）统一翻成人话；英文原文挪到鼠标悬停提示里，
  // 平时不占视线，排错时还能看到。
  var PROVIDER_ZH = {
    llm: '对话大模型', embedding: '文本向量', judge: '情感判断',
    asr: '语音识别', tts: '语音合成', knowledge: '知识库'
  };
  var MODE_ZH = {
    off: '已关闭', rule: '关键词兜底', http: '独立服务',
    auto: '跟随独立服务', onnx: '进程内模型', mcp: '知识库直连', mcp_http: '知识库直连',
    webhook: '回调接口', openai: '云端接口', ollama: '本机 Ollama', custom: '自定义',
    local: '本机运行', fp32: '高精度(P32)', fp16: '半精度'
  };
  // 下拉选项：按字段名分组，避免「auto」在语言里是自动、在引擎里是跟随
  var OPTION_ZH = {
    api_style: { openai: '云端接口（OpenAI 兼容）', ollama: '本机 Ollama', custom: '自定义地址' },
    engine: { http: '独立服务（推荐）', auto: '跟随独立服务', onnx: '进程内加载（吃内存）', rule: '只用关键词' },
    language: { auto: '自动判断', zh: '中文', en: '英文', yue: '粤语' },
    format: { wav: 'wav（无损）', mp3: 'mp3（体积小）', opus: 'opus', pcm: 'pcm 裸流' },
    emotion_control: { reference_audio: '换参考录音', emotion_vector: '情绪向量', none: '不控制' },
    mode: { mcp_http: '知识库直连', webhook: '回调接口', off: '关闭' },
    retrieval_mode: { hybrid: '混合检索', semantic: '语义检索', keyword: '关键词' },
    remember_on: { manual: '手动', every: '每轮都存', high_intensity: '情绪强烈时' }
  };
  var STAT_ZH = {
    emotion_events: '情感事件', personas: '人格模板', facts: '事实记忆', judge_logs: '判断记录',
    turns: '对话轮次', tokens: '生成字数', avg_latency_ms: '平均延迟(毫秒)', degraded_turns: '降级轮次',
    providers: '模型服务', reference_audios: '参考音频', config_changes: '改动次数',
    sessions: '会话数', kb_documents: '知识库文档', api_calls: '接口调用'
  };
  var KV_ZH = {
    available: '是否可用', status: '状态', reason: '原因', gpus: '显卡', name: '型号',
    memory_total: '总显存', memory_used: '已用显存', memory_free: '空闲显存', memory: '显存',
    utilization: '使用率', temperature: '温度', driver: '驱动', driver_version: '驱动版本',
    nvidia_smi_raw: '原始输出', ollama_ps: 'Ollama 进程', models: '已加载模型',
    device: '设备', backend: '后端', native: '本机驱动', ok: '正常'
  };

  function modeZh(m) {
    var s = String(m == null ? '' : m).trim();
    if (!s) return '未设置';
    return MODE_ZH[s.toLowerCase()] || s;
  }
  function optZh(key, val) {
    var m = OPTION_ZH[key] || {};
    return m[val] || modeZh(val);
  }
  function nameZh(p) {
    var code = (p && p.code) || '';
    return PROVIDER_ZH[code] || (p && p.name) || code || '未命名服务';
  }
  function statZh(k) { return STAT_ZH[k] || k; }
  function kvZh(k) {
    return String(k).replace(/[A-Za-z_]+/g, function (m) { return KV_ZH[m] || m; });
  }

  var personasCache = [];
  var kbBasesCache = [];
  var kbProviderCache = null;
  var expandedProviders = {};
  var flatItems = [];
  var flatKeyword = '';
  var toastTimer = null;
  var currentSection = 'overview';
  var previewUrl = null;

  function $(id) { return document.getElementById(id); }
  function safeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function safeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* noop */ } }
  function getToken() { return safeGet(LS_TOKEN) || ''; }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toBool(v) {
    if (v === true || v === 1) return true;
    return ['1', 'true', 'yes', 'on'].indexOf(String(v == null ? '' : v).toLowerCase()) >= 0;
  }

  function resolveConsoleUrl() {
    if (CFG.CONSOLE_OVERRIDE) {
      safeSet(LS_CONSOLE, CFG.CONSOLE_OVERRIDE);
      return CFG.CONSOLE_OVERRIDE;
    }
    var saved = (safeGet(LS_CONSOLE) || '').trim();
    return saved || CFG.CONSOLE_URL || 'http://localhost:8001';
  }

  function chatHref() {
    var brain = (safeGet(LS_BRAIN) || '').trim() || CFG.BRAIN_OVERRIDE || CFG.BRAIN_URL || '';
    var consoleUrl = CFG.CONSOLE_OVERRIDE || (safeGet(LS_CONSOLE) || '').trim() || CFG.CONSOLE_URL || '';
    var q = [];
    if (brain) q.push('brain=' + encodeURIComponent(brain));
    if (consoleUrl) q.push('console=' + encodeURIComponent(consoleUrl));
    return 'index.html' + (q.length ? ('?' + q.join('&')) : '');
  }

  function toast(msg, isError) {
    var el = $('toast');
    el.textContent = msg;
    el.className = 'toast show' + (isError ? ' toast-error' : '');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = 'toast'; }, 3200);
  }

  // 通用 JSON 请求：所有请求都带 X-Admin-Token（后端对 GET 也校验，避免只读接口泄露密钥）
  async function api(method, path, body) {
    var headers = { 'Accept': 'application/json', 'X-Admin-Token': getToken() };
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
    }
    var resp;
    try {
      resp = await fetch(resolveConsoleUrl() + path, {
        method: method,
        headers: headers,
        body: (body === undefined || body === null) ? undefined : JSON.stringify(body)
      });
    } catch (e) {
      throw new Error('无法连接控制台 ' + resolveConsoleUrl() + '（请确认 :8001 已启动）');
    }
    var text = await resp.text();
    var data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = text; } }
    if (!resp.ok) {
      var msg = 'HTTP ' + resp.status;
      if (data && typeof data === 'object') {
        var d = data.detail != null ? data.detail : data.message;
        if (Array.isArray(d)) {
          msg = d.map(function (x) { return x && x.msg ? x.msg : JSON.stringify(x); }).join('; ');
        } else if (typeof d === 'string') {
          msg = d;
        } else if (d != null) {
          msg = JSON.stringify(d);
        }
      }
      throw new Error(msg);
    }
    return data;
  }

  // 二进制 wav 请求（/tts/preview 返回原始字节），失败降级为 Error
  async function apiWav(method, path, body) {
    var headers = { 'X-Admin-Token': getToken() };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    var resp;
    try {
      resp = await fetch(resolveConsoleUrl() + path, {
        method: method,
        headers: headers,
        body: body === undefined ? undefined : JSON.stringify(body)
      });
    } catch (e) {
      throw new Error('无法连接控制台 ' + resolveConsoleUrl());
    }
    if (!resp.ok) {
      var t = await resp.text();
      var msg = 'HTTP ' + resp.status;
      try { var d = JSON.parse(t); if (d && d.detail != null) msg = String(d.detail); } catch (_) { /* noop */ }
      throw new Error(msg);
    }
    var ct = resp.headers.get('Content-Type') || '';
    if (ct.indexOf('application/json') >= 0) {
      var j = await resp.json();
      if (j && (j.ok === false || j.degraded)) throw new Error(j.detail || j.message || '试听不可用');
    }
    return await resp.blob();
  }

  // 兼容数组，或 {items/data/results/rows/...} 包裹
  function asList(data) {
    if (Array.isArray(data)) return data;
    if (!data || typeof data !== 'object') return [];
    var keys = ['items', 'data', 'results', 'rows', 'list', 'personas', 'emotions', 'logs', 'events',
      'params', 'providers', 'bases', 'sessions', 'turn_logs', 'turns', 'facts', 'audit',
      'voice_profiles', 'reference_audios', 'rules', 'modes'];
    for (var i = 0; i < keys.length; i++) {
      if (Array.isArray(data[keys[i]])) return data[keys[i]];
    }
    return [];
  }

  function renderError(el, err) {
    el.innerHTML = '<div class="empty error-text">加载失败：' +
      esc(err && err.message ? err.message : String(err)) + '</div>';
  }

  // 新增板块统一友好降级：明确提示后端可能未升级，且不影响其它板块
  function renderDegrade(el, err, name) {
    el.innerHTML = '<div class="empty error-text">「' + esc(name) + '」暂不可用：' +
      esc(err && err.message ? err.message : String(err)) +
      '<br><span class="muted">请确认控制平面已升级到 v2（:8001 /api/admin/* 新接口）；其余板块不受影响。</span></div>';
  }

  function simpleTable(headers, rows) {
    var h = '<table class="data-table"><thead><tr>';
    headers.forEach(function (x) { h += '<th>' + esc(x) + '</th>'; });
    h += '</tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr>';
      r.forEach(function (c) { h += '<td>' + c + '</td>'; });
      h += '</tr>';
    });
    return h + '</tbody></table>';
  }

  function kvTable(rows) {
    if (!rows.length) return '<div class="empty">未检测到</div>';
    return '<table class="kv-table"><tbody>' + rows.map(function (r) {
      return '<tr><th>' + esc(r[0]) + '</th><td>' + esc(r[1] == null ? '' : String(r[1])) + '</td></tr>';
    }).join('') + '</tbody></table>';
  }

  function renderKvDeep(o) {
    var rows = [];
    Object.keys(o || {}).forEach(function (k) {
      var v = o[k];
      if (v && typeof v === 'object') {
        if (Array.isArray(v)) {
          v.forEach(function (item, i) {
            if (item && typeof item === 'object') {
              Object.keys(item).forEach(function (ik) { rows.push([k + '[' + i + '].' + ik, item[ik]]); });
            } else {
              rows.push([k + '[' + i + ']', item]);
            }
          });
        } else {
          Object.keys(v).forEach(function (vk) { rows.push([k + '.' + vk, v[vk]]); });
        }
      } else {
        rows.push([k, v]);
      }
    });
    return kvTable(rows.map(function (r) { return [kvZh(r[0]), r[1]]; }));
  }

  function pick(obj, keys) {
    for (var i = 0; i < keys.length; i++) {
      if (obj && obj[keys[i]] != null) return obj[keys[i]];
    }
    return null;
  }

  // ---------- 1 · 概览 ----------
  var EMOTION_COLORS = {
    happy: '#f2b23e', sad: '#5b8def', angry: '#e05a41', surprised: '#9b6bd6',
    fear: '#8a94a6', disgust: '#2eb872', neutral: '#b6c0d4', care: '#ec7fae'
  };
  var PROVIDER_COLORS = {
    llm: '#2f6feb', embedding: '#9b6bd6', judge: '#f2b23e', tts: '#ec7fae', knowledge: '#2eb872'
  };

  // 原生 SVG 环图：8 情绪占比，零 CDN
  function donutSVG(counts, total) {
    if (!total) return '<div class="empty">暂无情感事件</div>';
    var R = 52, C = 2 * Math.PI * R, off = 0, segs = '';
    EMOTION_LABELS.forEach(function (l) {
      var n = counts[l] || 0;
      if (!n) return;
      var frac = n / total;
      segs += '<circle cx="70" cy="70" r="' + R + '" fill="none" stroke="' + EMOTION_COLORS[l] +
        '" stroke-width="20" stroke-dasharray="' + (frac * C).toFixed(2) + ' ' + C.toFixed(2) +
        '" stroke-dashoffset="' + (-off * C).toFixed(2) + '" transform="rotate(-90 70 70)">' +
        '<title>' + esc(l) + ' ' + n + '</title></circle>';
      off += frac;
    });
    return '<div class="donut-wrap"><svg viewBox="0 0 140 140" class="donut" role="img" aria-label="情绪分布">' +
      segs + '<text x="70" y="66" text-anchor="middle" class="donut-num">' + total + '</text>' +
      '<text x="70" y="84" text-anchor="middle" class="donut-cap">事件</text></svg>' +
      '<div class="legend">' + EMOTION_LABELS.filter(function (l) { return counts[l]; }).map(function (l) {
        return '<span class="legend-item"><i class="legend-dot" style="background:' + EMOTION_COLORS[l] + '"></i>' +
          esc(EMOTION_ZH[l] || l) + ' <b>' + counts[l] + '</b></span>';
      }).join('') + '</div></div>';
  }

  // 原生 SVG 面积走势图：每轮延迟，零 CDN
  function trendSVG(vals) {
    if (!vals.length) return '<div class="empty">暂无轮次明细</div>';
    var W = 560, H = 150, P = 10;
    var max = Math.max.apply(null, vals), min = Math.min.apply(null, vals);
    if (max === min) max = min + 1;
    var n = vals.length;
    function x(i) { return P + i * (W - 2 * P) / Math.max(1, n - 1); }
    function y(v) { return H - P - (v - min) / (max - min) * (H - 2 * P); }
    var line = vals.map(function (v, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1); }).join(' ');
    var area = line + ' L' + x(n - 1).toFixed(1) + ' ' + (H - P) + ' L' + x(0).toFixed(1) + ' ' + (H - P) + ' Z';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" class="trend" preserveAspectRatio="none" role="img" aria-label="每轮延迟走势">' +
      '<path d="' + area + '" class="trend-area"></path>' +
      '<path d="' + line + '" class="trend-line" vector-effect="non-scaling-stroke"></path></svg>';
  }

  function kpiCard(k, v, sub, cls) {
    return '<div class="kpi"><div class="kpi-k">' + esc(k) + '</div>' +
      '<div class="kpi-v ' + (cls || '') + '">' + esc(v == null ? '-' : v) + '</div>' +
      (sub ? '<div class="kpi-sub">' + esc(sub) + '</div>' : '') + '</div>';
  }

  function miniStat(k, v, cls) {
    return '<div class="mini-stat"><span class="mk">' + esc(k) + '</span>' +
      '<span class="mv ' + (cls || '') + '">' + esc(v == null ? '-' : v) + '</span></div>';
  }

  async function loadOverview() {
    var kpiEl = $('overview-kpi');
    var barEl = $('overview-statusbar');
    var providersEl = $('overview-providers');
    var modesEl = $('overview-modes');   // 新版 admin.html 已移除，空值保护
    var rawEl = $('overview-raw');        // 新版 admin.html 已移除，空值保护

    // A · overview（extended_stats + /health 代理）
    try {
      var data = await api('GET', '/api/admin/overview');
      var o = (data && typeof data === 'object') ? data : {};
      var stats = o.stats || o.db || o;

      var events = pick(stats, ['emotion_events', 'events', 'event_count']);
      if (events == null) events = pick(o, ['emotion_events', 'events']);
      var personas = pick(stats, ['personas', 'persona_count']);
      if (personas == null) personas = pick(o, ['personas', 'persona_count']);
      var logs = pick(stats, ['judge_logs', 'judge_count', 'logs']);
      if (logs == null) logs = pick(o, ['judge_logs', 'judge_count']);
      var active = pick(stats, ['active_persona']) || pick(o, ['active_persona']) ||
        (o.persona && o.persona.name) || '';

      var turns = pick(o, ['turns', 'total_turns']);
      if (turns == null) turns = pick(stats, ['turns']);
      var tokens = pick(o, ['tokens', 'token_count']);
      if (tokens == null) tokens = pick(stats, ['tokens']);
      var avg = pick(o, ['avg_latency_ms', 'avg_latency']);
      if (avg == null) avg = pick(stats, ['avg_latency_ms', 'avg_latency']);
      var degraded = pick(o, ['degraded_turns', 'degraded']);
      if (degraded == null) degraded = pick(stats, ['degraded_turns', 'degraded']);

      // 大脑存活：overview 内含 /health 代理结果，兼容多种包裹字段名
      var health = o.health || o.brain_health || o.brain || o;
      var alive = null;
      if (typeof o.brain_alive === 'boolean') alive = o.brain_alive;
      else if (health && typeof health === 'object') {
        if (typeof health.alive === 'boolean') alive = health.alive;
        else if (typeof health.reachable === 'boolean') alive = health.reachable;
        else if (health.health && health.health.status != null) alive = (String(health.health.status).toLowerCase() === 'ok');
        else if (health.status != null) alive = (String(health.status).toLowerCase() === 'ok');
        else if (health.available != null) alive = !!health.available;
      }
      var aliveText = alive === true ? '存活' : (alive === false ? '不可用' : '未检测到');
      var aliveCls = alive === true ? 'ok' : (alive === false ? 'bad' : 'muted');

      var degPct = (turns && degraded != null) ? Math.round(degraded / turns * 100) + '% 轮次' : '';
      // judge 只有规则兜底（未加载判断模型）时不算故障，其余降级如实标黄。
      var h = (o.brain_health && o.brain_health.health) || {};
      var judgeMode = String(h.judge || '').toLowerCase();
      var degLabel = '降级次数', degSub = degPct, degCls = degraded ? 'warn' : 'ok';
      if (judgeMode === 'rule') {
        degLabel = '规则兜底';
        degSub = degPct + ' · 判断模型未加载';
        degCls = 'muted';
      }
      kpiEl.innerHTML =
        kpiCard('轮次', turns, '累计对话轮') +
        kpiCard('生成字数', tokens, '累计 token 数') +
        kpiCard('平均延迟', avg == null ? '-' : Math.round(avg), '毫秒 / 轮') +
        kpiCard(degLabel, degraded, degSub, degCls);
      barEl.innerHTML =
        miniStat('大脑存活', aliveText, aliveCls) +
        miniStat('当前人格', active || '-') +
        miniStat('情感事件', events) +
        miniStat('人格模板', personas) +
        miniStat('判断日志', logs);

      if (rawEl) rawEl.innerHTML = '<pre class="raw-pre">' + esc(JSON.stringify(o, null, 2)) + '</pre>';
    } catch (e) {
      if (kpiEl) kpiEl.innerHTML = '';
      if (barEl) barEl.innerHTML = '';
      if (rawEl) renderDegrade(rawEl, e, '概览');
    }

    // B · providers（状态卡 + 组件当前模式），与 A 独立容错
    try {
      var pd = await api('GET', '/api/admin/providers');
      var pl = asList(pd);
      if (!pl.length && pd && Array.isArray(pd.providers)) pl = pd.providers;
      if (!pl.length) {
        providersEl.innerHTML = '<div class="empty">没读到服务清单</div>';
        if (modesEl) modesEl.innerHTML = '<div class="empty">-</div>';
      } else {
        providersEl.innerHTML = pl.map(function (p) {
          var en = !!p.enabled;
          var color = PROVIDER_COLORS[p.code] || '#8a94a6';
          return '<div class="pstatus' + (en ? '' : ' off') + '">' +
            '<div class="pstatus-top"><i class="pdot" style="background:' + color + '"></i>' +
            '<span class="n">' + esc(nameZh(p)) + '</span>' +
            '<span class="tag ' + (en ? 'tag-ok' : '') + '">' + (en ? '启用' : '停用') + '</span></div>' +
            '<div class="pstatus-mode">' + esc(modeZh(p.mode)) + '</div>' +
            '<div class="pstatus-ver muted">第 ' + esc(p.version == null ? '-' : p.version) + ' 版</div>' +
            '</div>';
        }).join('');
        if (modesEl) modesEl.innerHTML = pl.map(function (p) {
          return '<span class="mode-chip"><span class="mc-name">' + esc(nameZh(p)) + '</span>' +
            '<span class="mc-val">' + esc(modeZh(p.mode)) + '</span></span>';
        }).join('');
      }
    } catch (e) {
      providersEl.innerHTML = '<div class="empty">服务状态暂时读不到（不影响其它统计）</div>';
      if (modesEl) modesEl.innerHTML = '<div class="empty">-</div>';
    }

    // C · 情绪分布环图（独立容错）
    try {
      var ed = await api('GET', '/api/admin/emotions?limit=200');
      var el = asList(ed);
      var counts = {};
      el.forEach(function (r) {
        var l = r.emotion_label || r.label;
        if (l) counts[l] = (counts[l] || 0) + 1;
      });
      var total = el.length;
      $('chart-emotion-total').textContent = total ? ('近 ' + total + ' 条') : '';
      $('chart-emotion').innerHTML = donutSVG(counts, total);
    } catch (e) {
      $('chart-emotion').innerHTML = '<div class="empty">情绪分布不可用</div>';
    }

    // D · 每轮延迟走势（独立容错）
    try {
      var td = await api('GET', '/api/admin/turn-logs?limit=40');
      var tl = asList(td);
      var vals = tl.map(function (t) { return Number(t.latency_ms); })
        .filter(function (v) { return isFinite(v) && v >= 0; }).reverse();
      if (vals.length) {
        var sum = vals.reduce(function (a, b) { return a + b; }, 0);
        $('chart-latency-meta').textContent = '均 ' + Math.round(sum / vals.length) +
          ' · 峰 ' + Math.round(Math.max.apply(null, vals)) + ' 毫秒 · ' + vals.length + ' 轮';
      } else {
        $('chart-latency-meta').textContent = '';
      }
      $('chart-latency').innerHTML = trendSVG(vals);
    } catch (e) {
      $('chart-latency').innerHTML = '<div class="empty">延迟走势不可用</div>';
    }
  }

  // ---------- 2 · 人格模板 ----------
  async function loadPersonas() {
    var el = $('personas-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/personas');
      personasCache = asList(data);
      renderPersonas();
    } catch (e) { renderError(el, e); }
  }

  function renderPersonas() {
    var el = $('personas-list');
    if (!personasCache.length) { el.innerHTML = '<div class="empty">暂无人格模板</div>'; return; }
    var rows = personasCache.map(function (p) {
      var pid = p.id == null ? '' : p.id;
      var prompt = String(p.system_prompt || '');
      var short = prompt.length > 60 ? prompt.slice(0, 60) + '…' : prompt;
      return [
        esc(pid),
        esc(p.name || ''),
        esc(p.version == null ? '' : '第 ' + p.version + ' 版'),
        p.is_active ? '<span class="tag tag-ok">启用中</span>' : '<span class="tag">未启用</span>',
        '<span class="cell-clip" title="' + esc(prompt) + '">' + esc(short) + '</span>',
        '<span class="nowrap"><button type="button" class="btn btn-mini" data-persona-edit="' + esc(pid) + '">编辑</button> ' +
          '<button type="button" class="btn btn-mini btn-primary" data-persona-activate="' + esc(pid) + '">启用</button></span>'
      ];
    });
    el.innerHTML = simpleTable(['编号', '名称', '版本', '状态', '提示词', '操作'], rows);

    el.querySelectorAll('[data-persona-edit]').forEach(function (b) {
      b.addEventListener('click', function () { fillPersonaForm(b.getAttribute('data-persona-edit')); });
    });
    el.querySelectorAll('[data-persona-activate]').forEach(function (b) {
      b.addEventListener('click', function () { activatePersona(b.getAttribute('data-persona-activate')); });
    });
  }

  function fillPersonaForm(id) {
    var p = null;
    for (var i = 0; i < personasCache.length; i++) {
      if (String(personasCache[i].id) === String(id)) { p = personasCache[i]; break; }
    }
    if (!p) { toast('未找到该人格模板', true); return; }
    $('persona-id').value = p.id;
    $('persona-name').value = p.name || '';
    $('persona-prompt').value = p.system_prompt || '';
  }

  function clearPersonaForm() {
    $('persona-id').value = '';
    $('persona-name').value = '';
    $('persona-prompt').value = '';
  }

  async function savePersona(e) {
    e.preventDefault();
    var id = $('persona-id').value;
    var body = {
      name: $('persona-name').value.trim(),
      system_prompt: $('persona-prompt').value
    };
    if (id) body.id = Number(id);
    if (!body.name) { toast('名称不能为空', true); return; }
    try {
      await api('POST', '/api/admin/personas', body);
      toast('已保存（保存即生效，无需重启）');
      await loadPersonas();
      await loadOverview();
    } catch (err) { toast('保存失败：' + err.message, true); }
  }

  async function activatePersona(id) {
    if (!id) { toast('请先保存该人格模板', true); return; }
    try {
      await api('POST', '/api/admin/personas/' + encodeURIComponent(id) + '/activate', {});
      toast('已启用：保存即生效，无需重启');
      await loadPersonas();
      await loadOverview();
    } catch (err) { toast('启用失败：' + err.message, true); }
  }

  // ---------- 3 · 模型服务（动态表单）----------
  // 由字段元数据生成单个控件：text/number→input，bool→checkbox，select→下拉，
  // textarea→textarea，secret→password。控件带 data-field / data-ftype 供收集。
  function renderField(f, v) {
    var type = f.type || 'text';
    // 只显示中文标签；英文代号放进 title，鼠标停留才看得到
    var label = '<span class="fl">' + esc(f.label) + '</span>';
    var desc = f.desc ? '<span class="fd">' + esc(f.desc) + '</span>' : '';
    var base = 'data-field="' + esc(f.key) + '" data-ftype="' + esc(type) + '"';
    var ctrl;
    if (type === 'bool') {
      ctrl = '<label class="field-bool"><input type="checkbox" ' + base + ' ' +
        (toBool(v) ? 'checked' : '') + '> 开 / 关</label>';
    } else if (type === 'select') {
      var opts = (f.options || []).map(function (o) {
        return '<option value="' + esc(o) + '"' + (String(v) === String(o) ? ' selected' : '') + '>' + esc(optZh(f.key, o)) + '</option>';
      }).join('');
      // 当前值不在候选中时补一个选中项，避免 silent 改值
      var inOpts = (f.options || []).some(function (o) { return String(o) === String(v); });
      if (!inOpts && v != null && v !== '') {
        opts = '<option value="' + esc(v) + '" selected>' + esc(optZh(f.key, v)) + '</option>' + opts;
      }
      ctrl = '<select class="input field-input" ' + base + '>' + opts + '</select>';
    } else if (type === 'textarea') {
      var tv = (v && typeof v === 'object') ? JSON.stringify(v) : String(v == null ? '' : v);
      ctrl = '<textarea class="input field-input" ' + base + ' rows="3">' + esc(tv) + '</textarea>';
    } else if (type === 'number') {
      ctrl = '<input type="number" class="input field-input" ' + base + ' value="' + esc(v) + '"' +
        (f.min != null ? ' min="' + esc(f.min) + '"' : '') +
        (f.max != null ? ' max="' + esc(f.max) + '"' : '') + ' step="any">';
    } else if (f.secret) {
      ctrl = '<input type="password" class="input field-input" ' + base +
        ' data-fsecret="1" value="' + esc(v == null ? '' : String(v)) + '" autocomplete="off">';
    } else {
      ctrl = '<input type="text" class="input field-input" ' + base +
        ' value="' + esc(v == null ? '' : String(v)) + '">';
    }
    return '<div class="field-item" title="参数代号：' + esc(f.key) + '">' + label + ctrl + desc + '</div>';
  }

  // 常用字段：默认显示；其余字段收进「高级设置」<details>（默认收起）
  var COMMON_FIELDS = {
    llm: ['api_style', 'base_url', 'api_key', 'model', 'num_gpu', 'force_gpu'],
    embedding: ['model', 'dimension', 'force_cpu'],
    judge: ['engine', 'model_path', 'intensity_threshold'],
    asr: ['base_url', 'model', 'language'],
    tts: ['base_url', 'model', 'default_voice', 'speed', 'use_reference_audio'],
    knowledge: ['mode', 'mcp_endpoint', 'mcp_token', 'kb_ids', 'top_k', 'inject_prompt']
  };

  // 方案预设：只列出要改的关键字段，PUT 时后端按这些键合并，不覆盖 endpoint / token
  var PRESETS = {
    offline: {
      name: '本地全离线',
      plan: {
        llm: { api_style: 'openai', base_url: 'http://127.0.0.1:11434', model: 'qwen3:8b', num_gpu: 999, force_gpu: true },
        embedding: { model: 'bge-m3', force_cpu: true },
        tts: { base_url: 'http://127.0.0.1:9880', use_reference_audio: true },
        judge: { engine: 'onnx', precision: 'fp32' }
      }
    },
    cloud: {
      name: '云端 API',
      plan: {
        llm: { api_style: 'openai', model: 'qwen-plus', force_gpu: false },
        tts: { api_style: 'openai', use_reference_audio: false }
      },
      note: '云端密钥请在上面表格的「密钥」列里填'
    }
  };

  // 运行方式下拉：程序里没有模拟路径，只列真实模式。
  // 每个服务允许的模式由后端 mode_options 下发（如语音识别只有「独立服务/已关闭」），
  // 前端不再共用一张大杂烩清单。
  var MODE_CHOICES = ['off', 'rule', 'http', 'onnx', 'mcp_http'];
  var MODE_HINT = {
    off: '关闭该服务，对话链路不会使用它；相关功能会明确报错而不是偷偷造数据',
    http: '调用独立启动的服务（bat 起的那个），填好服务地址即可',
    rule: '不加载模型，只用关键词规则粗判情绪',
    onnx: '直接在大脑进程里加载本机 ONNX 模型，省一个服务但多吃内存',
    mcp_http: '连接现成的知识库服务（MCP 接口）',
    webhook: '回调你自己写的检索接口'
  };
  function modeSelectHtml(cur, options) {
    var c = String(cur == null ? '' : cur);
    var list = Array.isArray(options) && options.length ? options.slice() : MODE_CHOICES.slice();
    if (c && list.indexOf(c) < 0) list.push(c);
    if (!c) list.unshift('');
    return '<select class="input input-sm p-mode" title="' + esc(MODE_HINT[c] || '该服务的运行方式') + '">' +
      list.map(function (m) {
        var txt = m ? modeZh(m) : '未设置';
        var hint = m && MODE_HINT[m] ? ' title="' + esc(MODE_HINT[m]) + '"' : '';
        return '<option value="' + esc(m) + '"' + (m === c ? ' selected' : '') + hint + '>' + esc(txt) + '</option>';
      }).join('') + '</select>';
  }

  // provider 完整表单：常用字段 + 高级折叠 + 操作按钮。
  // 折叠字段的控件仍在 DOM 内，collectConfig 用 querySelectorAll('[data-field]')
  // 会一并收集（折叠只是 CSS 隐藏），保存不会丢字段。
  function renderProviderForm(p, opts) {
    opts = opts || {};
    var fields = Array.isArray(p.fields) ? p.fields : [];
    var cfg = (p.config && typeof p.config === 'object') ? p.config : {};
    var commonKeys = COMMON_FIELDS[p.code] || [];
    var common = [], advanced = [];
    fields.forEach(function (f) {
      var v = cfg[f.key] != null ? cfg[f.key] : f.default;
      var item = renderField(f, v);
      if (commonKeys.indexOf(f.key) >= 0) common.push(item);
      else advanced.push(item);
    });
    var commonHtml = common.length
      ? '<div class="fields-grid">' + common.join('') + '</div>'
      : (fields.length
        ? '' // 字段都在但没配「常用」清单时安静处理，不误报「未返回元数据」
        : '<div class="empty">该 provider 未返回 fields 元数据</div>');
    var advancedHtml = advanced.length
      ? '<details class="adv-details"><summary>高级设置（' + advanced.length + ' 项）</summary>' +
        '<div class="fields-grid">' + advanced.join('') + '</div></details>'
      : '';
    var testBtn = opts.test === false ? '' :
      '<button type="button" class="btn btn-mini" data-pact="test">连接测试</button>';
    var toggleBtn = opts.toggle
      ? '<button type="button" class="btn btn-mini" data-pact="toggle">' + (p.enabled ? '停用' : '启用') + '</button>'
      : '';
    var enabledBox = opts.showEnabled
      ? '<label class="nowrap"><input type="checkbox" class="p-enabled" ' + (p.enabled ? 'checked' : '') + '> 启用</label>'
      : '';
    var head = '<div class="provider-card-head">' +
      (opts.compact ? '' :
        '<h3 title="代号 ' + esc(p.code) + '">' + esc(nameZh(p)) + '</h3>') +
      '<span class="provider-meta">第 ' + esc(p.version == null ? '-' : p.version) + ' 版' +
        ' · ' + esc(p.updated_at || '') + '</span>' +
      enabledBox +
      '<label class="nowrap">运行方式 ' + modeSelectHtml(p.mode, p.mode_options) + '</label>' +
      '</div>';
    return head + commonHtml + advancedHtml +
      '<div class="card-actions">' +
        '<button type="button" class="btn btn-primary btn-mini" data-pact="save">保存</button>' +
        toggleBtn + testBtn + (opts.extra || '') +
        '<span class="hint">常用 ' + common.length + ' 项 · 高级 ' + advanced.length + ' 项</span>' +
      '</div>' +
      '<div class="test-result" style="display:none"></div>';
  }

  // 某 provider 的密钥字段：优先 api_key，其次任意 secret 字段（如 knowledge 的 mcp_token）
  function providerSecretField(p) {
    var fields = Array.isArray(p.fields) ? p.fields : [];
    var i, f;
    for (i = 0; i < fields.length; i++) {
      if (fields[i].key === 'api_key') return fields[i];
    }
    for (i = 0; i < fields.length; i++) {
      f = fields[i];
      if (f.secret) return f;
    }
    return null;
  }

  // 「接口形态」列：优先 api_style，其次 engine / mode，否则 http
  function providerInterface(p) {
    var cfg = (p.config && typeof p.config === 'object') ? p.config : {};
    var fields = Array.isArray(p.fields) ? p.fields : [];
    var keys = fields.map(function (f) { return f.key; });
    var v = null;
    if (keys.indexOf('api_style') >= 0) v = cfg.api_style;
    else if (keys.indexOf('engine') >= 0) v = cfg.engine;
    else if (keys.indexOf('mode') >= 0) v = cfg.mode;
    if (v == null || v === '') return '';
    return String(v);
  }

  // 模型清单：每行一个 provider；行内只有 开关 + API Key，
  // 「更多参数」在下方展开该 provider 的完整表单（不再是一堆大卡片）。
  function renderProviderRow(p) {
    var cfg = (p.config && typeof p.config === 'object') ? p.config : {};
    var sec = providerSecretField(p);
    var keyCell = sec
      ? '<input type="password" class="input input-sm p-apikey" data-code="' + esc(p.code) +
        '" data-secret-key="' + esc(sec.key) + '" data-orig="' +
        esc(cfg[sec.key] == null ? '' : String(cfg[sec.key])) + '" value="' +
        esc(cfg[sec.key] == null ? '' : String(cfg[sec.key])) + '" autocomplete="off" placeholder="' +
        esc(sec.label || '密钥') + '">'
      : '<span class="muted">无密钥</span>';
    var extra = p.code === 'knowledge'
      ? '<button type="button" class="btn btn-mini" data-kbact="load">拉库</button>' +
        '<button type="button" class="btn btn-mini" data-kbact="search">检索测试</button>'
      : '';
    var form = renderProviderForm(p, { showEnabled: false, compact: true, extra: extra });
    return '<tr class="model-row" data-code="' + esc(p.code) + '">' +
      '<td title="代号 ' + esc(p.code) + '"><b>' + esc(nameZh(p)) + '</b></td>' +
      '<td class="nowrap">' + esc(providerInterface(p) ? modeZh(providerInterface(p)) : '—') + '</td>' +
      '<td><label class="nowrap"><input type="checkbox" class="row-enabled" data-code="' + esc(p.code) + '"' +
        (p.enabled ? ' checked' : '') + '> 启用</label></td>' +
      '<td>' + keyCell + '</td>' +
      '<td class="nowrap"><button type="button" class="btn btn-mini" data-more="' + esc(p.code) + '">更多参数 ▾</button></td>' +
      '</tr>' +
      '<tr class="more-row" data-for="' + esc(p.code) + '"' + (expandedProviders[p.code] ? '' : ' hidden') + '>' +
      '<td colspan="5"><div class="more-body" data-pcode="' + esc(p.code) + '">' + form + '</div></td></tr>';
  }

  // provider 整卡（知识库板块仍用它承载 kb_ids 等专有操作）
  function renderProviderCard(p, opts) {
    opts = opts || {};
    return '<div class="provider-card' + (p.enabled ? '' : ' disabled') + '" data-pcode="' + esc(p.code) + '">' +
      renderProviderForm(p, { showEnabled: true, toggle: true, test: opts.test }) +
    '</div>';
  }

  // 模型清单里 knowledge 行的「拉库 / 检索测试」：跳到知识库板块并执行对应动作
  function knowledgeAction(act) {
    switchSection('knowledge');
    setTimeout(function () {
      var el = act === 'search' ? $('kb-search-input') : $('kb-bases-list');
      if (act === 'load') loadKbBases();
      if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      if (act === 'search' && el) el.focus();
    }, 0);
  }

  function collectConfig(card) {
    var cfg = {};
    card.querySelectorAll('[data-field]').forEach(function (el) {
      var key = el.getAttribute('data-field');
      var t = el.getAttribute('data-ftype');
      if (t === 'bool') cfg[key] = !!el.checked;
      else if (t === 'number') { if (el.value !== '') cfg[key] = Number(el.value); }
      // 密钥字段为空时不提交，避免把服务端已存的密钥清空
      else if (el.getAttribute('data-fsecret') === '1' && el.value === '') return;
      else cfg[key] = el.value;
    });
    return cfg;
  }

  // provider 统一事件委托（第 3、4 板块共用）
  function onProviderClick(e) {
    var btn = e.target.closest ? e.target.closest('[data-pact]') : null;
    if (!btn) return;
    var card = btn.closest('[data-pcode]');
    if (!card) return;
    var code = card.getAttribute('data-pcode');
    var act = btn.getAttribute('data-pact');
    if (act === 'save') saveProvider(code, card, btn);
    else if (act === 'toggle') toggleProvider(code, card, btn);
    else if (act === 'test') testProvider(code, card);
  }

  // 找到某 provider 行上的启用开关（清单模式下开关在行内，不在展开的表单里）
  function rowEnabledBox(code) {
    return document.querySelector('.model-row[data-code="' + code + '"] .row-enabled');
  }

  async function saveProvider(code, card, btn) {
    var body = { config: collectConfig(card) };
    var en = card.querySelector('.p-enabled') || rowEnabledBox(code);
    if (en) body.enabled = en.checked ? 1 : 0;
    var modeEl = card.querySelector('.p-mode');
    if (modeEl) body.mode = modeEl.value.trim() || 'http';
    btn.disabled = true;
    try {
      await api('PUT', '/api/admin/providers/' + encodeURIComponent(code), body);
      toast('已保存：' + (PROVIDER_ZH[code] || code) + '（参数、启停、运行方式）');
      if (currentSection === 'providers') loadProviders();
      else if (currentSection === 'knowledge') loadKnowledgeConfig();
      loadOverview();
    } catch (e) {
      toast('保存失败：' + e.message, true);
      btn.disabled = false;
    }
  }

  async function toggleProvider(code, card, btn) {
    var cur = card.querySelector('.p-enabled').checked ? 1 : 0;
    var next = cur ? 0 : 1;
    btn.disabled = true;
    try {
      await api('PUT', '/api/admin/providers/' + encodeURIComponent(code), { enabled: next });
      card.querySelector('.p-enabled').checked = !!next;
      card.classList.toggle('disabled', !next);
      btn.textContent = next ? '停用' : '启用';
      toast((PROVIDER_ZH[code] || code) + (next ? ' 已启用' : ' 已停用'));
      loadOverview();
    } catch (e) {
      toast('启停失败：' + e.message, true);
      btn.disabled = false;
    }
  }

  async function testProvider(code, card) {
    toast('测试中…');
    try {
      var r = await api('POST', '/api/admin/providers/' + encodeURIComponent(code) + '/test', {});
      var o = (r && typeof r === 'object') ? r : {};
      var ok = !!o.ok;
      var msg = (ok ? '✓ 连接正常' : '✗ 连接失败')
        + ' · ' + (o.latency_ms == null ? '-' : Number(o.latency_ms).toFixed(1)) + 'ms'
        + (o.detailZh ? ' · ' + o.detailZh : (o.detail ? ' · ' + o.detail : ''));
      toast(msg, !ok);
    } catch (e) {
      toast('测试失败：' + e.message, true);
    }
  }

  // 行内「启用」开关：立即 PUT /providers/{code} {enabled}
  async function onRowEnabledChange(el) {
    var code = el.getAttribute('data-code');
    if (!code) return;
    el.disabled = true;
    try {
      await api('PUT', '/api/admin/providers/' + encodeURIComponent(code), { enabled: el.checked ? 1 : 0 });
      toast((PROVIDER_ZH[code] || code) + (el.checked ? ' 已启用' : ' 已停用'));
      loadOverview();
    } catch (e) {
      toast('启停失败：' + e.message, true);
      el.checked = !el.checked;
    } finally { el.disabled = false; }
  }

  // 行内 API Key：失焦即保存 PUT /providers/{code} {config:{key: value}}
  async function onRowApiKeyBlur(el) {
    var code = el.getAttribute('data-code');
    var key = el.getAttribute('data-secret-key');
    if (!code || !key) return;
    if (el.value === (el.getAttribute('data-orig') || '')) return; // 没改就不提交
    var patch = {};
    patch[key] = el.value;
    el.disabled = true;
    try {
      await api('PUT', '/api/admin/providers/' + encodeURIComponent(code), { config: patch });
      toast((PROVIDER_ZH[code] || code) + ' 的密钥已保存');
      el.setAttribute('data-orig', el.value);
      // 同步「更多参数」展开区里的同名字段，避免展开区再用旧值覆盖回来
      var form = document.querySelector('.more-body[data-pcode="' + code + '"]');
      if (form) {
        var f = form.querySelector('[data-field="' + key + '"]');
        if (f) f.value = el.value;
      }
      loadOverview();
    } catch (e) {
      toast('保存失败：' + e.message, true);
    } finally { el.disabled = false; }
  }

  async function loadProviders() {
    var el = $('providers-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/providers');
      var list = asList(data);
      if (!list.length && data && Array.isArray(data.providers)) list = data.providers;
      if (!list.length) { el.innerHTML = '<div class="empty">暂无 provider 数据</div>'; return; }

      // ====== macOS 分组行渲染（快速参数可编辑） ======
      var html = list.map(function (p) {
        var cfg = p.config || {};
        var enabled = parseInt(p.enabled || 1) === 1;
        var dotCls = enabled ? 'on' : 'off';
        var dotTitle = enabled ? '已启用' : '已停用';
        // 快速参数：engine / base_url / model 都显示为可编辑 input
        var quickFields = ['engine', 'base_url', 'model'];
        var quickInputs = quickFields.map(function(k) {
          if (cfg[k] == null) return '';
          return '<input class="prov-quick-in" data-k="' + k + '" value="' + esc(String(cfg[k])) + '" title="' + esc(k) + '">';
        }).join('');
        // 高级参数字段
        var skipKeys = ['engine', 'base_url', 'model', '__fields', 'api_key', 'enabled'];
        var extraKeys = (p.fields || []).map(function(f){return f.key;}).filter(function(k){return skipKeys.indexOf(k) < 0;});
        var fieldRows = '';
        if (extraKeys.length) {
          fieldRows = '<div class="prov-detail-row"><div class="prov-fields">' + extraKeys.map(function(k){
            return '<label>' + esc(k) + '<input data-k="' + esc(k) + '" value="' + esc(String(cfg[k] == null ? '' : cfg[k])) + '"></label>';
          }).join('') + '</div></div>';
        }
        return '<div class="prov-wrap" data-code="' + esc(p.code) + '">'
          + '<div class="prov-row">'
            + '<span class="prov-dot ' + dotCls + '" title="' + dotTitle + '"></span>'
            + '<div class="prov-name">' + esc(nameZh(p)) + '</div>'
            + '<div class="prov-detail">' + quickInputs + '</div>'
            + '<div class="prov-actions">'
              + '<button type="button" class="prov-toggle" data-test>测试</button>'
              + (extraKeys.length ? '<button type="button" class="prov-toggle" data-toggle-adv>高级</button>' : '')
              + '<label class="chk-inline"><input type="checkbox" data-toggle' + (enabled ? ' checked' : '') + '></label>'
              + '<button type="button" class="btn btn-primary btn-mini" data-save>保存</button>'
            + '</div>'
          + '</div>'
          + fieldRows
        + '</div>';
      }).join('');
      el.innerHTML = html;

      // 事件绑定
      el.querySelectorAll('.prov-wrap').forEach(function(wrap) {
        var code = wrap.dataset.code;
        var adv = wrap.querySelector('.prov-detail-row');
        var toggAdv = wrap.querySelector('[data-toggle-adv]');
        if (toggAdv && adv) {
          toggAdv.addEventListener('click', function() {
            var open = adv.classList.contains('open');
            adv.classList.toggle('open');
            toggAdv.textContent = open ? '高级' : '收起';
          });
        }
        wrap.querySelector('[data-save]').addEventListener('click', function() {
          // 后端 ProviderUpdate 期望 { config: {...}, enabled: 0|1 } —— extra="forbid"
          var cfg = {};
          wrap.querySelectorAll('.prov-quick-in').forEach(function(inp) {
            var v = inp.value.trim();
            if (v !== '') cfg[inp.dataset.k] = v;
          });
          wrap.querySelectorAll('.prov-detail-row input[data-k]').forEach(function(inp) {
            var v = inp.value.trim();
            if (v !== '') cfg[inp.dataset.k] = v;
          });
          var enabled = wrap.querySelector('[data-toggle]').checked ? 1 : 0;
          var payload = { enabled: enabled, config: cfg };
          api('PUT', '/api/admin/providers/' + code, payload).then(function() {
            toast('已保存：' + nameZh({code: code}));
            loadProviders();
          }).catch(function(e) { toast('保存失败：' + e.message, true); });
        });
        wrap.querySelector('[data-test]').addEventListener('click', function() {
          testProvider(code, wrap);
        });
      });
    } catch (e) { renderDegrade(el, e, '模型服务'); }
  }

  function kv(k, v, mono) { return ''; } // macOS 风格不用

  function applyExpandedState() {
    var root = $('providers-list');
    if (!root) return;
    root.querySelectorAll('.more-row').forEach(function (tr) {
      var code = tr.getAttribute('data-for');
      var open = !!expandedProviders[code];
      tr.hidden = !open;
      var btn = root.querySelector('.model-row[data-code="' + code + '"] [data-more]');
      if (btn) btn.textContent = open ? '收起 ▴' : '更多参数 ▾';
    });
  }

  function toggleMore(code) {
    expandedProviders[code] = !expandedProviders[code];
    applyExpandedState();
  }

  // 方案预设：confirm 后依次 PUT 各 provider 的关键字段；只提交列到的字段，
  // 不覆盖用户已填的 endpoint / token。先 GET 一次拿字段白名单，
  // 用于跳过该 provider 不存在的字段（如老版本 judge 无 precision），避免 422 报错。
  async function applyPreset() {
    var presetSel = $('preset-select');
    if (!presetSel) return;
    var preset = PRESETS[presetSel.value];
    if (!preset) return;
    if (!window.confirm('套用「' + preset.name + '」方案？只会改几个关键字段，已填的地址、口令、密钥都不会动。')) return;
    var btn = $('preset-apply');
    var hint = $('preset-hint');
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = '';
    try {
      var data = await api('GET', '/api/admin/providers');
      var list = asList(data);
      if (!list.length && data && Array.isArray(data.providers)) list = data.providers;
      var keySets = {};
      list.forEach(function (p) {
        keySets[p.code] = (Array.isArray(p.fields) ? p.fields : []).map(function (f) { return f.key; });
      });
      var codes = Object.keys(preset.plan);
      for (var i = 0; i < codes.length; i++) {
        var code = codes[i];
        var allowed = keySets[code];
        var patch = {};
        Object.keys(preset.plan[code]).forEach(function (k) {
          // 拿不到字段清单时保持容错直接提交；否则仅提交存在的字段
          if (!allowed || allowed.indexOf(k) >= 0) patch[k] = preset.plan[code][k];
        });
        if (Object.keys(patch).length === 0) continue;
        await api('PUT', '/api/admin/providers/' + encodeURIComponent(code), { config: patch });
      }
      if (preset.note) hint.textContent = preset.note;
      toast('已套用「' + preset.name + '」');
      loadProviders();
    } catch (e) {
      toast('套用失败：' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- 4 · 知识库 / 长期记忆 ----------
  async function loadKnowledgeConfig() {
    var el = $('kb-provider-form');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/providers');
      var list = asList(data);
      if (!list.length && data && Array.isArray(data.providers)) list = data.providers;
      var kp = null;
      list.forEach(function (p) {
        if (p.code === 'knowledge' || p.category === 'knowledge') kp = p;
      });
      if (!kp) {
        var one = await api('GET', '/api/admin/providers/knowledge');
        kp = (one && typeof one === 'object') ? one : null;
      }
      if (!kp) { el.innerHTML = '<div class="empty">没找到知识库服务的配置</div>'; return; }
      el.innerHTML = renderProviderCard(kp, { test: false });
    } catch (e) { renderDegrade(el, e, '知识库配置'); }
  }

  function currentKbIds() {
    var el = $('kb-provider-form').querySelector('[data-field="kb_ids"]');
    if (!el) return [];
    return String(el.value || '').split(/[,\s]+/).map(function (s) { return s.trim(); })
      .filter(function (s) { return s; });
  }

  async function loadKbBases() {
    var el = $('kb-bases-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/knowledge/bases');
      var o = (data && typeof data === 'object') ? data : {};
      var bases = Array.isArray(o.bases) ? o.bases : asList(data);
      kbBasesCache = bases;
      if (!bases.length) {
        el.innerHTML = '<div class="empty">没读到知识库列表（接入方式：' + esc(modeZh(o.mode)) + '）</div>';
        return;
      }
      var selected = currentKbIds();
      var html = '<table class="data-table"><thead><tr><th>勾选</th><th>编号</th><th>名称</th></tr></thead><tbody>';
      bases.forEach(function (b) {
        var id = String(b.id == null ? '' : b.id);
        var chk = selected.indexOf(id) >= 0 ? ' checked' : '';
        html += '<tr><td><input type="checkbox" class="kb-check" value="' + esc(id) + '"' + chk + '></td>' +
          '<td class="mono">' + esc(id) + '</td><td>' + esc(b.name || '') + '</td></tr>';
      });
      html += '</tbody></table><div class="hint">接入方式：' + esc(modeZh(o.mode)) +
        (o.degraded ? ' · <span class="error-text">降级</span>' : '') + '</div>';
      el.innerHTML = html;
    } catch (e) { renderDegrade(el, e, '知识库列表'); }
  }

  async function saveKbIds() {
    var ids = [];
    $('kb-bases-list').querySelectorAll('.kb-check:checked').forEach(function (c) { ids.push(c.value); });
    try {
      await api('PUT', '/api/admin/providers/knowledge', {
        config: { kb_ids: ids.join(','), known_bases: JSON.stringify(kbBasesCache) }
      });
      toast('已保存，勾选 ' + ids.length + ' 个知识库');
      loadKnowledgeConfig();
    } catch (e) { toast('写入失败：' + e.message, true); }
  }

  async function kbSearch() {
    var q = $('kb-search-input').value.trim();
    var topk = Number($('kb-search-topk').value) || 3;
    var el = $('kb-search-results');
    if (!q) { toast('请输入检索内容', true); return; }
    el.innerHTML = '<div class="empty">检索中…</div>';
    try {
      var r = await api('POST', '/api/admin/knowledge/search', { query: q, top_k: topk });
      var o = (r && typeof r === 'object') ? r : {};
      var results = Array.isArray(o.results) ? o.results : asList(r);
      var head = '<div class="hint">接入方式 ' + esc(modeZh(o.mode)) + ' · 耗时 ' +
        esc(o.latency_ms == null ? '-' : o.latency_ms) + ' 毫秒' + (o.degraded ? ' · 降级' : '') + '</div>';
      if (!results.length) { el.innerHTML = head + '<div class="empty">无召回结果</div>'; return; }
      var rows = results.map(function (x) {
        return [
          '<span class="mono">' + esc(x.score == null ? '-' : Number(x.score).toFixed(3)) + '</span>',
          esc(x.title || ''),
          '<span class="cell-clip" title="' + esc(x.content || '') + '">' + esc(x.content || '') + '</span>',
          esc(x.source || '')
        ];
      });
      el.innerHTML = head + simpleTable(['分数', '标题', '内容', '来源'], rows);
    } catch (e) { renderDegrade(el, e, '检索测试'); }
  }

  async function kbRemember() {
    var textEl = $('kb-remember-text');
    var el = $('kb-remember-result');
    if (!textEl) return;
    var text = textEl.value.trim();
    if (!text) { toast('内容不能为空', true); return; }
    var titleEl = $('kb-remember-title');
    var tagsEl = $('kb-remember-tags');
    var body = {
      text: text,
      title: titleEl ? titleEl.value.trim() : '',
      tags: tagsEl ? tagsEl.value.trim() : ''
    };
    if (el) el.innerHTML = '<div class="empty">写入中…</div>';
    try {
      var r = await api('POST', '/api/admin/knowledge/remember', body);
      var ok = !(r && r.ok === false);
      el.innerHTML = '<div class="test-result ' + (ok ? 'ok' : 'bad') + '">' +
        (ok ? '已沉淀' : '失败') + '：' + esc(JSON.stringify(r)) + '</div>';
    } catch (e) {
      el.innerHTML = '<div class="test-result bad">写入失败：' + esc(e.message) + '</div>';
    }
  }

  function loadKnowledge() { loadKnowledgeConfig(); loadKbBases(); }

  // ---------- 5 · TTS / 语音 ----------
  async function loadVoiceProfiles() {
    var el = $('voice-profiles');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/voice-profiles');
      var list = asList(data);
      if (!list.length && data && Array.isArray(data.voice_profiles)) list = data.voice_profiles;
      if (!list.length) { el.innerHTML = '<div class="empty">暂无情绪映射</div>'; return; }
      var html = '<table class="data-table"><thead><tr>' +
        '<th>情绪</th><th title="对应参考音频的编号">参考音频编号</th>' +
        '<th title="模型侧的声线编号，没有可留空">音色编号</th>' +
        '<th title="8 个数字组成的情绪向量，一般不用动">情绪向量</th>' +
        '<th>语速</th><th>音量</th><th>操作</th></tr></thead><tbody>';
      list.forEach(function (v) {
        var vec = v.emotion_vector;
        var vecStr = (vec && typeof vec === 'object') ? JSON.stringify(vec) : String(vec == null ? '[]' : vec);
        html += '<tr data-emotion="' + esc(v.emotion) + '">' +
          '<td><b>' + esc(EMOTION_ZH[v.emotion] || v.emotion) + '</b>' +
            '<div class="mono muted" style="font-size:11px">' + esc(v.emotion) + '</div></td>' +
          '<td><input class="input input-sm vp-ref" type="text" value="' + esc(v.reference_audio_id || '') + '"></td>' +
          '<td><input class="input input-sm vp-voice" type="text" value="' + esc(v.voice_id || '') + '"></td>' +
          '<td><input class="input input-sm vp-vec" type="text" value="' + esc(vecStr) + '"></td>' +
          '<td><input class="input input-sm vp-speed" type="number" step="0.01" min="0.5" max="2" value="' + esc(v.speed == null ? 1 : v.speed) + '"></td>' +
          '<td><input class="input input-sm vp-volume" type="number" step="0.01" min="0" max="2" value="' + esc(v.volume == null ? 1 : v.volume) + '"></td>' +
          '<td><button type="button" class="btn btn-mini btn-primary vp-save">保存</button></td></tr>';
      });
      html += '</tbody></table>';
      el.innerHTML = html;
      el.querySelectorAll('.vp-save').forEach(function (b) {
        b.addEventListener('click', function () { saveVoiceProfile(b.closest('tr'), b); });
      });
    } catch (e) { renderDegrade(el, e, '情绪映射'); }
  }

  async function saveVoiceProfile(tr, btn) {
    var emotion = tr.getAttribute('data-emotion');
    var vecRaw = tr.querySelector('.vp-vec').value.trim();
    var vector = vecRaw;
    try { vector = JSON.parse(vecRaw); } catch (_) { /* 传字符串让后端自行解析 */ }
    var body = {
      emotion: emotion,
      reference_audio_id: tr.querySelector('.vp-ref').value.trim(),
      voice_id: tr.querySelector('.vp-voice').value.trim(),
      emotion_vector: vector,
      speed: Number(tr.querySelector('.vp-speed').value),
      volume: Number(tr.querySelector('.vp-volume').value)
    };
    btn.disabled = true;
    try {
      await api('PUT', '/api/admin/voice-profiles', body);
      toast(emotion + ' 语音映射已保存');
    } catch (e) {
      toast('保存失败：' + e.message, true);
      btn.disabled = false;
    }
  }

  async function loadRefAudios() {
    var el = $('ref-audios-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/reference-audios');
      var list = asList(data);
      if (!list.length && data && Array.isArray(data.reference_audios)) list = data.reference_audios;
      if (!list.length) { el.innerHTML = '<div class="empty">暂无参考音频</div>'; return; }
      var rows = list.map(function (a) {
        return [
          esc(a.id || ''), esc(a.name || ''),
          '<span class="cell-clip" title="' + esc(a.file_path || '') + '">' + esc(a.file_path || '') + '</span>',
          esc(a.emotion || ''), esc(a.note || ''),
          '<button type="button" class="btn btn-mini btn-danger" data-ref-del="' + esc(a.id) + '">删除</button>'
        ];
      });
      el.innerHTML = simpleTable(['ID', '名称', '路径', '情绪', '备注', '操作'], rows);
      el.querySelectorAll('[data-ref-del]').forEach(function (b) {
        b.addEventListener('click', function () { deleteRefAudio(b.getAttribute('data-ref-del')); });
      });
    } catch (e) { renderDegrade(el, e, '参考音频'); }
  }

  async function addRefAudio(e) {
    e.preventDefault();
    var body = {
      id: $('ref-id').value.trim(),
      name: $('ref-name').value.trim(),
      file_path: $('ref-path').value.trim(),
      emotion: $('ref-emotion').value,
      note: $('ref-note').value.trim()
    };
    if (!body.id || !body.name) { toast('ID 与名称不能为空', true); return; }
    try {
      await api('POST', '/api/admin/reference-audios', body);
      toast('已新增参考音频 ' + body.id);
      $('ref-audio-form').reset();
      loadRefAudios();
    } catch (err) { toast('新增失败：' + err.message, true); }
  }

  async function deleteRefAudio(id) {
    try {
      await api('DELETE', '/api/admin/reference-audios/' + encodeURIComponent(id));
      toast('已删除 ' + id);
      loadRefAudios();
    } catch (e) { toast('删除失败：' + e.message, true); }
  }

  async function ttsPreview() {
    var text = $('tts-preview-text').value.trim();
    var emotion = $('tts-preview-emotion').value;
    var hint = $('tts-preview-hint');  // admin.html 已移除，空值保护
    if (!text) { toast('请输入试听文本', true); return; }
    if (hint) hint.textContent = '合成中…';
    try {
      var blob = await apiWav('POST', '/api/admin/tts/preview', { text: text, emotion: emotion });
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(blob);
      var a = $('tts-preview-audio');
      a.src = previewUrl;
      a.play().catch(function () { /* 浏览器禁止自动播放时由用户点控件 */ });
      if (hint) hint.textContent = '已生成音频（' + blob.size + ' 字节），可播放';
    } catch (e) {
      if (hint) hint.textContent = '试听失败：' + e.message + '（后端可能未实现 /tts/preview）';
    }
  }

  function loadTts() { loadVoiceProfiles(); loadRefAudios(); }

  // ---------- 6 · 运行时参数 / 全局开关（扁平表）----------
  // 参考小智后台：参数管理是一张扁平的 key-value 表（编码 + 说明 + 值 + 分组 + 搜索），
  // 不再按模块堆大表单。provider 字段在前，运行时参数在后，统一走 flat-params 接口。
  var FLAT_GROUP_ORDER = ['llm', 'embedding', 'judge', 'asr', 'tts', 'knowledge', 'runtime'];

  // 通用归一：数组 → [{key,value}]；对象 → 摊平成 [{key,value}]
  function normalizeParams(data) {
    if (Array.isArray(data)) {
      return data.map(function (it) {
        if (it && typeof it === 'object') return { key: it.key || it.name, value: it.value };
        return null;
      }).filter(function (x) { return x && x.key != null; });
    }
    if (data && typeof data === 'object') {
      var container = data.params || data.items || data;
      if (Array.isArray(container)) return normalizeParams(container);
      return Object.keys(container).map(function (k) { return { key: k, value: container[k] }; });
    }
    return [];
  }

  // 把 flat-params 的 items 归一成统一结构；key/value 仍走 normalizeParams 抽取
  function normalizeFlatItems(data) {
    var list = asList(data);
    var raw = [];
    list.forEach(function (it) {
      if (!it || typeof it !== 'object') return;
      var code = it.code || it.key || it.name;
      if (!code) return;
      raw.push({
        code: String(code),
        label: it.label || '',
        group: it.group || 'runtime',
        group_label: it.group_label || '',
        value: it.value,
        type: it.type || 'text',
        min: it.min,
        max: it.max,
        options: it.options || null,
        desc: it.desc || '',
        secret: !!it.secret,
        source: it.source || 'runtime'
      });
    });
    var vals = {};
    normalizeParams(raw.map(function (r) { return { key: r.code, value: r.value }; }))
      .forEach(function (p) { vals[p.key] = p.value; });
    raw.forEach(function (r) { r.value = vals[r.code]; });
    return raw;
  }

  function flatGroupRank(g) {
    var i = FLAT_GROUP_ORDER.indexOf(g);
    return i < 0 ? FLAT_GROUP_ORDER.length : i;
  }

  // 按 type 渲染值控件：number/int/float→数字框，bool→开关，select→下拉，
  // textarea→多行，secret→密码框，其余→文本框
  function flatControl(it) {
    var t = String(it.type || 'text').toLowerCase();
    var v = it.value;
    var orig = (v && typeof v === 'object') ? JSON.stringify(v) : String(v == null ? '' : v);
    var code = esc(it.code);
    if (t === 'bool') {
      return '<label class="nowrap"><input type="checkbox" class="flat-ctl" data-type="bool" data-code="' + code +
        '" data-orig="' + (toBool(v) ? '1' : '0') + '"' + (toBool(v) ? ' checked' : '') + '> 开 / 关</label>';
    }
    if (t === 'select') {
      var fkey = String(it.code || '').split('.').pop();
      var opts = (it.options || []).map(function (o) {
        var ov = (o && typeof o === 'object') ? (o.value != null ? o.value : o.label) : o;
        var ol = (o && typeof o === 'object') ? (o.label != null ? o.label : o.value) : o;
        return '<option value="' + esc(ov) + '"' + (String(v) === String(ov) ? ' selected' : '') + '>' +
          esc(optZh(fkey, ol)) + '</option>';
      }).join('');
      var inOpts = (it.options || []).some(function (o) {
        var ov = (o && typeof o === 'object') ? (o.value != null ? o.value : o.label) : o;
        return String(ov) === String(v);
      });
      if (!inOpts && v != null && v !== '') {
        opts = '<option value="' + esc(v) + '" selected>' + esc(optZh(fkey, v)) + '</option>' + opts;
      }
      return '<select class="input input-sm flat-ctl" data-type="select" data-code="' + code +
        '" data-orig="' + esc(orig) + '">' + opts + '</select>';
    }
    if (t === 'textarea') {
      return '<textarea class="input flat-ctl" data-type="textarea" data-code="' + code +
        '" data-orig="' + esc(orig) + '" rows="2">' + esc(orig) + '</textarea>';
    }
    if (t === 'secret' || it.secret) {
      return '<input type="password" class="input input-sm flat-ctl" data-type="secret" data-code="' + code +
        '" data-orig="' + esc(orig) + '" value="' + esc(orig) + '" autocomplete="off">';
    }
    if (t === 'number' || t === 'int' || t === 'float') {
      var step = (t === 'float') ? '0.05' : (t === 'int' ? '1' : 'any');
      return '<input type="number" step="' + step + '" class="input input-sm flat-ctl" data-type="' + t +
        '" data-code="' + code + '" data-orig="' + esc(orig) + '" value="' + esc(orig) + '"' +
        (it.min != null ? ' min="' + esc(it.min) + '"' : '') +
        (it.max != null ? ' max="' + esc(it.max) + '"' : '') + '>';
    }
    return '<input type="text" class="input input-sm flat-ctl" data-type="text" data-code="' + code +
      '" data-orig="' + esc(orig) + '" value="' + esc(orig) + '">';
  }

  async function loadFlatParams() {
    var el = $('params-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/flat-params');
      flatItems = normalizeFlatItems(data);
      renderFlatParams();
    } catch (e) {
      renderDegrade(el, e, '运行时参数');
    }
  }

  function renderFlatParams() {
    var el = $('params-list');
    var kw = (flatKeyword || '').trim().toLowerCase();
    var list = flatItems.filter(function (it) {
      if (!kw) return true;
      return (it.code + ' ' + it.label + ' ' + it.group + ' ' + it.group_label).toLowerCase().indexOf(kw) >= 0;
    });
    var cntEl = $('flat-params-count');
    if (cntEl) {
      cntEl.textContent = kw
        ? ('匹配 ' + list.length + ' / 共 ' + flatItems.length + ' 项')
        : ('共 ' + flatItems.length + ' 项');
    }
    if (!list.length) {
      el.innerHTML = '<div class="empty">' + (flatItems.length ? '没有匹配的参数' : '暂无参数') + '</div>';
      return;
    }
    // 按 group 分组小标题，组内保持后端顺序（provider 字段在前、运行时在后）
    var groups = [];
    list.forEach(function (it) {
      var g = groups.filter(function (x) { return x.group === it.group; })[0];
      if (!g) groups.push({ group: it.group, group_label: it.group_label, items: [it] });
      else g.items.push(it);
    });
    groups.sort(function (a, b) { return flatGroupRank(a.group) - flatGroupRank(b.group); });

    var html = '<table class="data-table flat-table"><thead><tr>' +
      '<th>参数代号</th><th>它管什么</th><th>当前值</th>' +
      '</tr></thead><tbody>';
    groups.forEach(function (g) {
      html += '<tr class="group-row"><td colspan="3"><b>' + esc(g.group_label || PROVIDER_ZH[g.group] || g.group) + '</b>' +
        '<span class="muted"> · ' + g.items.length + ' 项</span></td></tr>';
      g.items.forEach(function (it) {
        html += '<tr data-code="' + esc(it.code) + '">' +
          '<td><b>' + esc(it.label || it.code) + '</b>' +
            '<div class="mono muted" style="font-size:11px">' + esc(it.code) + '</div></td>' +
          '<td class="muted">' + esc(it.desc || '') + '</td>' +
          '<td>' + flatControl(it) + '</td>' +
          '</tr>';
      });
    });
    html += '</tbody></table>';
    el.innerHTML = html;
  }

  // 参数代号 → 中文名，用于提示语
  function flatLabel(code) {
    var hit = flatItems.filter(function (x) { return x.code === code; })[0];
    return hit ? (hit.label || code) : code;
  }

  // 失焦 / 变更即保存（统一一种方式）：PUT /api/admin/flat-params {code, value}
  async function saveFlatParam(ctl) {
    var code = ctl.getAttribute('data-code');
    var type = ctl.getAttribute('data-type');
    var orig = ctl.getAttribute('data-orig') || '';
    var raw;
    if (type === 'bool') raw = ctl.checked ? '1' : '0';
    else raw = ctl.value;
    if (String(raw) === String(orig)) return; // 值没变不提交
    var body = { code: code, value: raw };
    if (type === 'number' || type === 'int' || type === 'float') {
      if (String(raw).trim() === '') { ctl.value = orig; toast(flatLabel(code) + ' 不能为空', true); return; }
      var n = Number(raw);
      if (!isFinite(n)) { ctl.value = orig; toast(flatLabel(code) + ' 要填数字', true); return; }
      body.value = n;
    } else if (type === 'bool') {
      body.value = ctl.checked;
    }
    ctl.disabled = true;
    try {
      await api('PUT', '/api/admin/flat-params', body);
      ctl.setAttribute('data-orig', String(raw));
      toast(flatLabel(code) + ' 已保存');
      loadOverview();
    } catch (e) {
      toast('保存失败：' + e.message, true);
      ctl.value = orig;
      if (type === 'bool') ctl.checked = orig === '1';
    } finally {
      ctl.disabled = false;
    }
  }

  // ---------- 7 · 情感事件 ----------
  async function loadEmotions() {
    var el = $('emotions-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    var label = $('filter-label').value;
    var intensity = $('filter-intensity').value;
    var limit = $('filter-limit').value || '100';
    var q = 'limit=' + encodeURIComponent(limit);
    if (label) q += '&label=' + encodeURIComponent(label);
    if (intensity !== '') q += '&min_intensity=' + encodeURIComponent(intensity);
    try {
      var data = await api('GET', '/api/admin/emotions?' + q);
      renderEmotions(asList(data));
    } catch (e) { renderError(el, e); }
  }

  function labelOptions(cur) {
    var extra = EMOTION_LABELS.indexOf(cur) < 0
      ? '<option value="' + esc(cur) + '" selected>' + esc(cur) + '</option>' : '';
    return extra + EMOTION_LABELS.map(function (l) {
      return '<option value="' + l + '"' + (l === cur ? ' selected' : '') + '>' +
        (EMOTION_ZH[l] || l) + '</option>';
    }).join('');
  }

  function renderEmotions(list) {
    var el = $('emotions-list');
    if (!list.length) { el.innerHTML = '<div class="empty">暂无情感事件</div>'; return; }
    var html = '<table class="data-table"><thead><tr>' +
      '<th>ID</th><th>时间</th><th>用户</th><th>标签</th><th>强度</th><th>置信</th>' +
      '<th>摘要</th><th>状态</th><th>操作</th></tr></thead><tbody>';
    list.forEach(function (e) {
      var cur = String(e.emotion_label || e.label || 'neutral');
      var inten = e.intensity == null ? '' : e.intensity;
      var flags = [];
      if (e.degraded) flags.push('降级');
      if (e.truncated) flags.push('截断');
      if (e.closed) flags.push('已闭合');
      html += '<tr data-id="' + esc(e.id) + '">' +
        '<td>' + esc(e.id) + '</td>' +
        '<td class="mono nowrap">' + esc(e.timestamp || e.created_at || '') + '</td>' +
        '<td>' + esc(e.user_id || '') + '</td>' +
        '<td class="cell-label">' +
          '<button type="button" class="label-pill" title="点击修改（当前 ' + esc(cur) + '）">' +
            esc(EMOTION_ZH[cur] || cur) + '</button>' +
          '<input type="hidden" class="ev-label" value="' + esc(cur) + '">' +
        '</td>' +
        '<td><input class="input input-sm ev-intensity" type="number" min="0" max="1" step="0.05" value="' + esc(inten) + '"></td>' +
        '<td class="mono">' + esc(e.confidence == null ? '' : Number(e.confidence).toFixed(2)) + '</td>' +
        '<td class="cell-clip" title="' + esc(e.summary || '') + '">' + esc(e.summary || '') + '</td>' +
        '<td class="muted">' + esc(flags.join(' / ') || '-') + '</td>' +
        '<td><button type="button" class="btn btn-mini btn-primary ev-save">保存修正</button></td>' +
        '</tr>';
    });
    html += '</tbody></table>';
    el.innerHTML = html;
    el.querySelectorAll('.label-pill').forEach(function (pill) {
      pill.addEventListener('click', function () { openLabelEditor(pill); });
    });
    el.querySelectorAll('.ev-save').forEach(function (btn) {
      btn.addEventListener('click', function () { saveEmotion(btn.closest('tr'), btn); });
    });
  }

  // 标签列平时只显示当前值（省宽度、避免 8 个选项把表格撑开），点一下才换成下拉
  function openLabelEditor(pill) {
    var td = pill.parentNode;
    var hidden = td.querySelector('.ev-label');
    if (!hidden) return;
    var cur = hidden.value || 'neutral';

    var sel = document.createElement('select');
    sel.className = 'input input-sm label-select';
    sel.innerHTML = labelOptions(cur);
    pill.style.display = 'none';
    td.appendChild(sel);
    sel.focus();

    var done = false;
    function restore() {
      pill.textContent = EMOTION_ZH[hidden.value] || hidden.value;
      pill.setAttribute('title', '点击修改（当前 ' + hidden.value + '）');
      if (sel.parentNode) sel.parentNode.removeChild(sel);
      pill.style.display = '';
    }
    function commit() {
      if (done) return;
      done = true;
      hidden.value = sel.value;
      restore();
    }
    sel.addEventListener('change', commit);
    sel.addEventListener('blur', commit);
    sel.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { done = true; restore(); }
    });
  }

  async function saveEmotion(tr, btn) {
    var id = tr.getAttribute('data-id');
    var body = { emotion_label: tr.querySelector('.ev-label').value };
    var raw = tr.querySelector('.ev-intensity').value;
    if (raw !== '') {
      var n = Number(raw);
      if (!isFinite(n) || n < 0 || n > 1) { toast('强度需在 0~1 之间', true); return; }
      body.intensity = n;
    }
    btn.disabled = true;
    try {
      await api('PATCH', '/api/admin/emotions/' + encodeURIComponent(id), body);
      toast('事件 #' + id + ' 已修正');
    } catch (err) {
      toast('修正失败：' + err.message, true);
      btn.disabled = false;
    }
  }

  // ---------- 8 · 会话与每轮明细 ----------
  async function loadSessions() {
    var el = $('sessions-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/sessions');
      var list = asList(data);
      if (!list.length) { el.innerHTML = '<div class="empty">暂无会话</div>'; return; }
      var rows = list.map(function (s) {
        return [
          esc(s.user_id || ''), esc(s.session_id || ''), esc(s.turns == null ? '-' : s.turns),
          '<span class="mono nowrap">' + esc(s.first_at || '') + '</span>',
          '<span class="mono nowrap">' + esc(s.last_at || '') + '</span>',
          esc(EMOTION_ZH[s.last_emotion] || s.last_emotion || '-') + ' / ' +
            esc(s.last_intensity == null ? '-' : s.last_intensity),
          '<button type="button" class="btn btn-mini btn-primary" data-session="' + esc(s.session_id) + '">查看明细</button>'
        ];
      });
      el.innerHTML = simpleTable(['用户', '会话', '轮次', '开始', '最近', '最近情绪', '操作'], rows);
      el.querySelectorAll('[data-session]').forEach(function (b) {
        b.addEventListener('click', function () {
          $('turn-filter-session').value = b.getAttribute('data-session');
          loadTurnLogs();
          try { $('turn-logs-list').scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (_) { /* noop */ }
        });
      });
    } catch (e) { renderDegrade(el, e, '会话'); }
  }

  async function loadTurnLogs() {
    var el = $('turn-logs-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    var sid = $('turn-filter-session').value.trim();
    var limit = $('turn-filter-limit').value || '100';
    var q = 'limit=' + encodeURIComponent(limit);
    if (sid) q += '&session_id=' + encodeURIComponent(sid);
    try {
      var data = await api('GET', '/api/admin/turn-logs?' + q);
      var list = asList(data);
      if (!list.length) {
        el.innerHTML = '<div class="empty">暂无轮次明细' + (sid ? '（会话 ' + esc(sid) + '）' : '') + '</div>';
        return;
      }
      var rows = list.map(function (t) {
        var snap = t.provider_snapshot;
        var snapStr = (snap && typeof snap === 'object') ? JSON.stringify(snap) : String(snap || '');
        var flags = [];
        if (t.degraded) flags.push('降级');
        if (t.truncated) flags.push('截断');
        return [
          '<span class="mono nowrap">' + esc(t.created_at || '') + '</span>',
          esc(t.user_id || ''), esc(t.session_id || ''),
          esc(t.emotion_label || '') + ' ' + esc(t.intensity == null ? '' : Number(t.intensity).toFixed(2)),
          '<span class="cell-clip" title="' + esc(t.input_text || '') + '">' + esc(t.input_text || '') + '</span>',
          '<span class="cell-clip" title="' + esc(t.reply_text || '') + '">' + esc(t.reply_text || '') + '</span>',
          esc(t.token_count == null ? '-' : t.token_count),
          esc(t.latency_ms == null ? '-' : Number(t.latency_ms).toFixed(1)),
          esc(t.persona_version == null ? '-' : t.persona_version),
          '<span class="cell-clip mono" title="' + esc(snapStr) + '">' + esc(snapStr) + '</span>',
          (t.status === 'ok' ? '<span class="tag tag-ok">正常</span>' : '<span class="tag">' + esc(t.status || '-') + '</span>') +
            ' ' + esc(flags.join('/'))
        ];
      });
      el.innerHTML = simpleTable(
        ['时间', '用户', '会话', '情绪/强度', '输入', '回复', '字数', '耗时(毫秒)', '人格版本', '服务快照', '状态'], rows);
    } catch (e) { renderDegrade(el, e, '轮次明细'); }
  }

  function loadSessionsAll() { loadSessions(); loadTurnLogs(); }

  // ---------- 9 · 事实记忆 ----------
  async function loadFacts() {
    var el = $('facts-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/facts');
      var list = asList(data);
      if (!list.length) { el.innerHTML = '<div class="empty">暂无事实记忆</div>'; return; }
      var rows = list.map(function (f) {
        var md = f.metadata;
        var mdStr = (md && typeof md === 'object') ? JSON.stringify(md) : String(md || '');
        return [
          '<span class="mono">' + esc(f.id || '') + '</span>',
          esc(f.user_id || ''), esc(f.session_id || ''),
          '<span class="cell-clip" title="' + esc(f.text || '') + '">' + esc(f.text || '') + '</span>',
          '<span class="cell-clip mono" title="' + esc(mdStr) + '">' + esc(mdStr) + '</span>',
          '<span class="mono nowrap">' + esc(f.created_at || '') + '</span>',
          '<button type="button" class="btn btn-mini btn-danger" data-fact-del="' + esc(f.id) + '">删除</button>'
        ];
      });
      el.innerHTML = simpleTable(['编号', '用户', '会话', '内容', '附加信息', '时间', '操作'], rows);
      el.querySelectorAll('[data-fact-del]').forEach(function (b) {
        b.addEventListener('click', function () { deleteFact(b.getAttribute('data-fact-del')); });
      });
    } catch (e) { renderDegrade(el, e, '事实记忆'); }
  }

  async function deleteFact(id) {
    try {
      await api('DELETE', '/api/admin/facts?id=' + encodeURIComponent(id));
      toast('已删除事实 ' + id);
      loadFacts();
    } catch (e) { toast('删除失败：' + e.message, true); }
  }

  // ---------- 10 · 配置变更审计 ----------
  async function loadAudit() {
    var el = $('audit-list');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/config-audit');
      var list = asList(data);
      if (!list.length) { el.innerHTML = '<div class="empty">暂无变更记录</div>'; return; }
      var rows = list.map(function (a) {
        return [
          '<span class="mono nowrap">' + esc(a.created_at || '') + '</span>',
          esc(a.actor || ''), esc(a.target || ''), esc(a.action || ''),
          '<span class="cell-clip" title="' + esc(a.detail || '') + '">' + esc(a.detail || '') + '</span>'
        ];
      });
      el.innerHTML = simpleTable(['时间', '操作者', '目标', '动作', '详情'], rows);
    } catch (e) { renderDegrade(el, e, '配置变更审计'); }
  }

  // ---------- 11 · 判断日志 ----------
  async function loadJudgeLogs() {
    var el = $('judge-logs');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/judge-logs?limit=100');
      var list = asList(data);
      if (!list.length) { el.innerHTML = '<div class="empty">暂无判断日志</div>'; return; }
      var rows = list.map(function (l) {
        return [
          '<span class="mono nowrap">' + esc(l.created_at || l.timestamp || '') + '</span>',
          '<span class="mono">' + esc(l.request_id || '') + '</span>',
          '<span class="cell-clip" title="' + esc(l.input_text || '') + '">' + esc(l.input_text || '') + '</span>',
          esc(l.output_label || ''),
          '<span class="mono">' + esc(l.confidence == null ? '' : Number(l.confidence).toFixed(2)) + '</span>',
          '<span class="mono">' + esc(l.intensity == null ? '' : Number(l.intensity).toFixed(2)) + '</span>',
          l.degraded ? '<span class="tag">降级</span>' : '-',
          '<span class="mono">' + esc(l.latency_ms == null ? '' : Number(l.latency_ms).toFixed(1)) + '</span>',
          esc(l.source || '')
        ];
      });
      el.innerHTML = simpleTable(
        ['时间', '请求编号', '输入', '判定情绪', '置信度', '强度', '降级', '耗时(毫秒)', '来源'], rows);
    } catch (e) { renderError(el, e); }
  }

  // ---------- 服务日志（每个 bat 各写一份，这里直接看）----------
  var LOG_SVC = '';        // 当前选中的服务代号
  var LOG_TIMER = null;    // 自动刷新定时器

  // 一行日志该染成什么颜色：报错红、警告黄、启动横幅灰、其余正常
  function logLineCls(line) {
    if (/Traceback|ERROR|Error:|Exception|失败|未就绪/.test(line)) return 'lg-err';
    if (/WARN|警告|\[提示\]/.test(line)) return 'lg-warn';
    if (/^\s*=====/.test(line)) return 'lg-sep';
    return '';
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  async function loadRunLogs() {
    var tabs = $('log-tabs');
    var body = $('log-body');
    var meta = $('log-meta');
    if (!tabs || !body) return;
    bindLogPanel();
    body.textContent = '读取中…';
    try {
      var q = '/api/admin/logs' + (LOG_SVC ? ('?service=' + encodeURIComponent(LOG_SVC)) : '');
      var data = await api('GET', q) || {};
      var items = data.items || [];
      if (!LOG_SVC) {
        var pick = items.filter(function (it) { return it.running && it.file; })[0]
          || items.filter(function (it) { return it.file; })[0];
        if (pick) { LOG_SVC = pick.key; return loadRunLogs(); }
      }
      var dirEl = $('log-dir');
      if (dirEl && data.dir) dirEl.textContent = data.dir;

      tabs.innerHTML = items.map(function (it) {
        // 「全部」是启动器窗口本身，没有自己的端口，不画状态点免得误导
        var dot = it.key === 'all' ? ''
          : '<i class="log-dot' + (it.running ? ' on' : '') + '"></i>';
        return '<button type="button" class="log-tab' + (it.key === LOG_SVC ? ' is-on' : '') +
          '" data-log="' + esc(it.key) + '">' + dot + esc(it.name) + '</button>';
      }).join('');

      var cur = items.filter(function (it) { return it.key === LOG_SVC; })[0] || null;
      if (!cur) { body.textContent = '没有这个服务的日志'; if (meta) meta.textContent = ''; return; }

      if (meta) {
        meta.innerHTML = cur.file
          ? ('<span class="mono">' + esc(cur.file) + '</span>　' + fmtSize(cur.size) +
             '　最后写入 ' + esc(cur.mtime || '—') +
             '　' + (cur.running ? '<b class="ok-text">在运行</b>' : '<b class="muted-text">没在跑</b>'))
          : '这个服务还没有日志文件——它可能一次都没启动过';
      }

      var lines = cur.lines || [];
      if (!lines.length) {
        body.textContent = cur.file
          ? '（日志文件是空的：服务启动后还没输出过内容）'
          : '（没有日志：先双击对应的 bat 把它起起来）';
        return;
      }
      body.innerHTML = lines.map(function (l) {
        var cls = logLineCls(l);
        return cls ? '<span class="' + cls + '">' + esc(l) + '</span>' : esc(l);
      }).join('\n');
      body.scrollTop = body.scrollHeight;
    } catch (e) {
      body.textContent = '读日志失败：' + (e && e.message ? e.message : e);
    }
  }

  function bindLogPanel() {
    var tabs = $('log-tabs');
    if (tabs && !tabs.dataset.bound) {
      tabs.dataset.bound = '1';
      tabs.addEventListener('click', function (ev) {
        var b = ev.target && ev.target.closest ? ev.target.closest('[data-log]') : null;
        if (!b) return;
        LOG_SVC = b.getAttribute('data-log');
        loadRunLogs();
      });
    }
    var auto = $('log-auto');
    if (auto && !auto.dataset.bound) {
      auto.dataset.bound = '1';
      auto.addEventListener('change', function () {
        if (LOG_TIMER) { clearInterval(LOG_TIMER); LOG_TIMER = null; }
        if (this.checked) {
          LOG_TIMER = setInterval(function () {
            if (currentSection === 'runlogs') loadRunLogs();
          }, 3000);
        }
      });
    }
  }

  // ---------- GPU 状态（第 12 板块用）----------
  async function loadGpu() {
    var el = $('gpu-status');
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/gpu');
      var o = (data && typeof data === 'object') ? data : {};
      if (o.available === false || o.status === 'unavailable') {
        el.innerHTML = '<div class="empty">未检测到</div>';
        return;
      }
      el.innerHTML = renderKvDeep(o);
    } catch (e) {
      el.innerHTML = '<div class="empty">未检测到</div>';
    }
  }

  // ---------- 12 · GPU / 系统 ----------
  async function loadSystem() {
    var pe = $('system-providers');
    var se = $('system-stats');
    var re = $('system-rules');  // admin.html 已移除，空值保护
    pe.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var data = await api('GET', '/api/admin/system');
      var o = (data && typeof data === 'object') ? data : {};
      var pm = o.provider_modes || o.modes || o.providers;
      if (Array.isArray(pm)) {
        pe.innerHTML = simpleTable(['服务', '运行方式', '状态', '版本'], pm.map(function (p) {
          return [esc(nameZh(p)), esc(modeZh(p.mode)),
            p.enabled ? '<span class="tag tag-ok">启用</span>' : '<span class="tag">停用</span>',
            esc(p.version == null ? '' : '第 ' + p.version + ' 版')];
        }));
      } else if (pm && typeof pm === 'object' && Object.keys(pm).length) {
        pe.innerHTML = simpleTable(['服务', '运行方式', '状态', '版本'], Object.keys(pm).map(function (k) {
          var v = pm[k];
          if (v && typeof v === 'object') {
            return [esc(PROVIDER_ZH[k] || k), esc(modeZh(v.mode)),
              esc(v.enabled == null ? '' : (toBool(v.enabled) ? '启用' : '停用')),
              esc(v.version == null ? '' : '第 ' + v.version + ' 版')];
          }
          return [esc(PROVIDER_ZH[k] || k), esc(modeZh(v)), '', ''];
        }));
      } else {
        // /system 未给明细时，用 /providers 兜底
        try {
          var pd = await api('GET', '/api/admin/providers');
          var pl = asList(pd);
          if (!pl.length && pd && Array.isArray(pd.providers)) pl = pd.providers;
          pe.innerHTML = pl.length
            ? simpleTable(['服务', '代号', '运行方式', '启用', '版本'], pl.map(function (p) {
                return [esc(nameZh(p)), esc(p.code), esc(modeZh(p.mode)), p.enabled ? '是' : '否',
                  esc(p.version == null ? '' : '第 ' + p.version + ' 版')];
              }))
            : '<div class="empty">没读到服务清单</div>';
        } catch (_) { pe.innerHTML = '<div class="empty">未获取到 provider 明细</div>'; }
      }

      // 统计：extended_stats 字段
      var src = (o.stats && typeof o.stats === 'object') ? o.stats : o;
      var srows = [];
      var statKeys = ['emotion_events', 'personas', 'facts', 'judge_logs', 'turns', 'tokens',
        'avg_latency_ms', 'degraded_turns', 'providers', 'reference_audios', 'config_changes'];
      statKeys.forEach(function (k) {
        var v = o[k];
        if (v == null) return;
        // 数组/对象不能直接进表格，否则显示成 [object Object]
        if (Array.isArray(v)) { srows.push([statZh(k), v.length + ' 项（明细见上表）']); return; }
        if (typeof v === 'object') return;
        srows.push([statZh(k), v]);
      });
      if (!srows.length && src && typeof src === 'object') {
        Object.keys(src).forEach(function (k) {
          var v = src[k];
          if (v == null) return;
          if (Array.isArray(v)) { srows.push([statZh(k), v.length + ' 项']); return; }
          if (typeof v === 'object') return;
          srows.push([statZh(k), v]);
        });
      }
      se.innerHTML = srows.length ? kvTable(srows) : '<div class="empty">无统计字段</div>';

      // 运行时规则（17 项）— admin.html 已移除 system-rules，空值保护
      var rules = o.runtime_rules || o.rules || o.constraints;
      if (re && rules && typeof rules === 'object' && Object.keys(rules).length) {
        re.innerHTML = simpleTable(['参数', '取值范围', '代号'], Object.keys(rules).map(function (k) {
          var v = rules[k];
          if (v && typeof v === 'object') {
            var rng = (v.min != null && v.max != null) ? (v.min + ' ~ ' + v.max) : JSON.stringify(v);
            return [esc(v.desc || k), esc(rng), '<span class="mono muted">' + esc(k) + '</span>'];
          }
          return [esc(k), esc(v), ''];
        }));
      } else if (re) {
        re.innerHTML = '<div class="empty">未返回运行时规则</div>';
      }
    } catch (e) {
      renderDegrade(pe, e, '系统');
      se.innerHTML = '';
      if (re) re.innerHTML = '';
    }
    loadGpu();
  }

  // ---------- 板块调度 ----------
  var LOADERS = {
    overview: loadOverview,
    personas: loadPersonas,
    providers: loadProviders,
    knowledge: loadKnowledge,
    tts: loadTts,
    runlogs: loadRunLogs,
    params: loadFlatParams,
    emotions: loadEmotions,
    sessions: loadSessionsAll,
    facts: loadFacts,
    audit: loadAudit,
    logs: loadJudgeLogs,
    system: loadSystem
  };

  // 侧边分组折叠状态持久化（用户偏好：折叠状态存本地）
  function navState() {
    try { return JSON.parse(safeGet(LS_NAV) || '{}') || {}; } catch (e) { return {}; }
  }
  function setGroupOpen(grp, open) {
    if (!grp) return;
    grp.classList.toggle('open', open);
    var st = navState();
    st[grp.getAttribute('data-group')] = open;
    safeSet(LS_NAV, JSON.stringify(st));
  }
  function applyNavState() {
    var st = navState();
    document.querySelectorAll('#admin-nav .nav-group').forEach(function (g) {
      var k = g.getAttribute('data-group');
      if (k in st) g.classList.toggle('open', !!st[k]);
    });
  }

  function switchSection(name) {
    if (!LOADERS[name]) name = 'overview';
    currentSection = name;
    var activeItem = null;
    document.querySelectorAll('#admin-nav .nav-item').forEach(function (b) {
      var on = b.getAttribute('data-section') === name;
      b.classList.toggle('active', on);
      if (on) activeItem = b;
    });
    document.querySelectorAll('.panel').forEach(function (p) {
      p.classList.toggle('active', p.getAttribute('data-panel') === name);
    });
    // 自动展开当前板块所在分组，确保选中项可见（并持久化）
    if (activeItem && activeItem.closest) {
      setGroupOpen(activeItem.closest('.nav-group'), true);
    }
    // 地址栏带上板块名，方便刷新后还停在同一页、也方便直接发链接
    if (location.hash.slice(1) !== name) {
      try { history.replaceState(null, '', '#' + name); } catch (_) { /* 本地文件打开时可能不允许 */ }
    }
    LOADERS[name]();
  }

  // 顶部按钮：只刷当前板块 + 概览
  function refreshAll() {
    if (currentSection && currentSection !== 'overview' && LOADERS[currentSection]) LOADERS[currentSection]();
    loadOverview();
  }

  function fillEmotionSelect(el, withEmpty) {
    if (!el) return;
    var html = withEmpty ? '<option value="">无 / 不指定</option>' : '';
    html += EMOTION_LABELS.map(function (l) {
      return '<option value="' + l + '">' + (EMOTION_ZH[l] || l) + '</option>';
    }).join('');
    el.innerHTML = html;
  }

  function init() {
    // === 旧 HTML 元素（顶栏 token-input / server-input / server-save / chat-link / refresh-all / preset-apply）已删除 ===
    // 用空值保护避免 init() 崩溃

    var tokenEl = $('token-input');
    if (tokenEl) {
      tokenEl.value = getToken();
      tokenEl.addEventListener('change', function () {
        safeSet(LS_TOKEN, this.value || '');
        toast('Token 已保存');
      });
    }

    var sInput = $('server-input'), sSave = $('server-save'), sHint = $('server-hint');
    if (sInput) sInput.value = resolveConsoleUrl();
    if (sHint && CFG.CONSOLE_OVERRIDE) sHint.textContent = '当前由 URL 参数指定';
    if (sSave && sInput && sHint) {
      sSave.addEventListener('click', function () {
        var v = (sInput.value || '').trim().replace(/\/+$/, '');
        if (!/^https?:\/\//i.test(v)) { sHint.textContent = '需以 http:// 或 https:// 开头'; return; }
        safeSet(LS_CONSOLE, v);
        if (CFG.CONSOLE_OVERRIDE) CFG.CONSOLE_OVERRIDE = null;
        sInput.value = v; sHint.textContent = '已保存：' + v;
        refreshAll();
      });
    }

    var chatLink = $('chat-link');
    if (chatLink) chatLink.href = chatHref();
    var refreshAllBtn = $('refresh-all');
    if (refreshAllBtn) refreshAllBtn.addEventListener('click', refreshAll);

    // 方案预设套用（preset-apply 已删，空值保护）
    var presetApplyBtn = $('preset-apply');
    if (presetApplyBtn) presetApplyBtn.addEventListener('click', applyPreset);

    // 左侧导航切换
    document.querySelectorAll('#admin-nav .nav-item').forEach(function (b) {
      b.addEventListener('click', function () { switchSection(b.getAttribute('data-section')); });
    });
    applyNavState();

    // 各板块头部刷新按钮
    document.querySelectorAll('[data-refresh]').forEach(function (b) {
      b.addEventListener('click', function () {
        var k = b.getAttribute('data-refresh');
        if (LOADERS[k]) LOADERS[k]();
      });
    });

    // 原有 5 板块交互
    $('persona-form').addEventListener('submit', savePersona);
    $('persona-new').addEventListener('click', clearPersonaForm);
    $('emotions-query').addEventListener('click', loadEmotions);

    // === providers-list：新版 loadProviders() 已经给每个按钮单独绑了事件，
    // 不再需要旧的全局 click 委托（旧委托找 data-pact/data-pcode 等已废弃的属性）。
    // 保留 kb-provider-form 的委托（知识库配置仍在用旧渲染）
    $('kb-provider-form').addEventListener('click', onProviderClick);

    // 运行时参数：搜索框 + 统一「change 即保存」
    // change 对文本框在失焦时触发，对 select / checkbox 立即触发 —— 一种机制统一处理
    $('flat-params-search').addEventListener('input', function () {
      flatKeyword = this.value || '';
      renderFlatParams();
    });
    $('params-list').addEventListener('change', function (e) {
      var ctl = e.target.closest ? e.target.closest('.flat-ctl') : null;
      if (ctl) saveFlatParam(ctl);
    });

    // 知识库
    $('kb-bases-load').addEventListener('click', loadKbBases);
    $('kb-ids-save').addEventListener('click', saveKbIds);
    $('kb-search-btn').addEventListener('click', kbSearch);
    var kbRemBtn = $('kb-remember-btn');
    if (kbRemBtn) kbRemBtn.addEventListener('click', kbRemember);

    // TTS
    fillEmotionSelect($('ref-emotion'), true);
    fillEmotionSelect($('tts-preview-emotion'), false);
    $('ref-audio-form').addEventListener('submit', addRefAudio);
    $('tts-preview-btn').addEventListener('click', ttsPreview);

    // 会话明细
    $('turn-logs-refresh').addEventListener('click', loadTurnLogs);

    // 默认显示概览；地址栏带 #板块名 时直接打开那一块
    var start = (location.hash || '').replace('#', '');
    switchSection(LOADERS[start] ? start : 'overview');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
