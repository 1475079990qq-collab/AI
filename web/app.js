/* 对话页逻辑：SSE 流式解析 + 情绪徽标 + TTS 播放
 * 约定见 CONTRACT.md 第 4 / 7 节。
 * 关键点：
 *  - fetch + response.body.getReader() 读字节流；
 *  - TextDecoder('utf-8', {stream:true}) 增量解码，绝不对单个 chunk 独立解码（中文会被切碎）；
 *  - 文本缓冲区按 \n\n（兼容 \r\n\r\n）切帧，一个 TCP 包可能半条或多条帧；
 *  - 跳过 ":" 开头的注释帧，只处理 data: 行；
 *  - 只有 done 才算正常流结束；error 保留半句 + 重试按钮、不触发 TTS。
 */
(function () {
  'use strict';

  var CFG = window.COMPANION_CONFIG || {};
  var LS_BRAIN = 'companion.brain';
  var LS_CONSOLE = 'companion.console';
  var LS_USER = 'companion.user_id';
  var LS_SESSION = 'companion.session_id';

  var EMOTION_ZH = {
    happy: '开心', sad: '难过', angry: '生气', surprised: '惊讶',
    fear: '害怕', disgust: '厌恶', neutral: '平静', care: '关切'
  };

  var $messages, $input, $sendBtn, $badge, $metaInfo;
  var $serverInput, $serverSave, $serverHint, $clearBtn, $tts, $mic;

  var isStreaming = false;
  var userId = null;
  var sessionId = null;
  var currentObjectUrl = null;
  var abortController = null;

  // 渲染优化：rAF 批量刷新（一帧多 token 只写一次 DOM）+ 智能吸底滚动
  var SCROLL_SLACK = 48;        // 距底 < 48px 视为「吸底」，用户上滑看历史则不打断
  var stickToBottom = true;
  var rafId = 0;
  var pendingNode = null;
  var pendingText = '';

  function $(id) { return document.getElementById(id); }

  function safeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function safeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式忽略 */ } }
  function safeRemove(k) { try { localStorage.removeItem(k); } catch (e) { /* 隐私模式忽略 */ } }

  function randomId(prefix) {
    return prefix + '-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  }

  function getOrCreateId(key, prefix) {
    var v = safeGet(key);
    if (!v) { v = randomId(prefix); safeSet(key, v); }
    return v;
  }

  // ---- brain 地址缓存：带签发上下文 ----
  // 旧版 LS 里只存纯字符串地址，一旦早期版本存过 :8001 / 旧 IP，升级后旧值会被优先使用。
  // 现在缓存值升级为 JSON：{ url, port, host }，port/host 是写入那一刻当前配置的端口与页面主机身份；
  // 读取时只有「同一端口、同一回环身份」下签发的缓存才允许优先于当前配置，否则视为过期自愈清除。
  function urlParts(u) {
    try { var p = new URL(u); return { host: p.hostname, port: p.port || '80' }; }
    catch (e) { return null; }
  }

  function isLoopback(h) {
    return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]';
  }

  function currentBrainContext() {
    var local = window.COMPANION_LOCAL || {};
    var pageHost = (typeof location !== 'undefined' && location.hostname) || 'localhost';
    var port = String(local.brain_port || 8000);
    var cur = urlParts(CFG.BRAIN_URL || '');
    if (cur && cur.port) port = cur.port;
    return { port: port, host: pageHost };
  }

  function persistBrainUrl(url) {
    var ctx = currentBrainContext();
    safeSet(LS_BRAIN, JSON.stringify({ url: url, port: ctx.port, host: ctx.host }));
  }

  function readValidBrainUrl() {
    var raw = (safeGet(LS_BRAIN) || '').trim();
    if (!raw) return null;
    var rec = null;
    try { rec = JSON.parse(raw); } catch (e) { rec = null; }
    // ① 老格式纯字符串 / 非法 JSON / 缺 url 字段 → 过期，丢弃并清除
    if (!rec || typeof rec !== 'object' || typeof rec.url !== 'string' || !rec.url) {
      safeRemove(LS_BRAIN);
      return null;
    }
    var ctx = currentBrainContext();
    // ② 签发端口与当前 brain 端口不一致 → 过期
    if (String(rec.port) !== String(ctx.port)) { safeRemove(LS_BRAIN); return null; }
    // ③ 回环 / 非回环主机身份对调 → 过期（本机缓存不能拿去连局域网，反之亦然）
    if (isLoopback(String(rec.host || '')) !== isLoopback(ctx.host)) {
      safeRemove(LS_BRAIN);
      return null;
    }
    return rec.url;
  }

  // 优先级：URL 参数显式覆盖 > 带上下文校验通过的缓存 > 按 hostname 推导
  function resolveBrainUrl() {
    if (CFG.BRAIN_OVERRIDE) {
      persistBrainUrl(CFG.BRAIN_OVERRIDE);
      return CFG.BRAIN_OVERRIDE;
    }
    var saved = readValidBrainUrl();
    return saved || CFG.BRAIN_URL || 'http://localhost:8000';
  }

  function adminHref() {
    var brain = resolveBrainUrl();
    var consoleUrl = CFG.CONSOLE_OVERRIDE || CFG.CONSOLE_URL || '';
    var q = [];
    if (brain) q.push('brain=' + encodeURIComponent(brain));
    if (consoleUrl) q.push('console=' + encodeURIComponent(consoleUrl));
    return 'admin.html' + (q.length ? ('?' + q.join('&')) : '');
  }

  function scrollToBottom() { $messages.scrollTop = $messages.scrollHeight; }

  function nearBottom() {
    return $messages.scrollHeight - $messages.scrollTop - $messages.clientHeight < SCROLL_SLACK;
  }

  // 一帧内多个 token 合并成一次 textContent 写入 + 一次滚动，消除逐字强制重排
  function flushRender() {
    rafId = 0;
    if (pendingNode) { pendingNode.textContent = pendingText; pendingNode = null; }
    if (stickToBottom) scrollToBottom();
  }

  function setBubbleText(bubble, text) {
    pendingNode = bubble._text || bubble.querySelector('.text');
    pendingText = text;
    if (!rafId) rafId = requestAnimationFrame(flushRender);
  }

  // 终帧（done/error/clarify/停止）立即落字，不等下一帧，保证半句/末字不丢
  function renderNow(bubble, text) {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    pendingNode = null;
    var t = bubble._text || bubble.querySelector('.text');
    if (t) t.textContent = text;   // textContent：天然转义，避免注入
    if (stickToBottom) scrollToBottom();
  }

  function appendUserBubble(text) {
    var wrap = document.createElement('div');
    wrap.className = 'msg user';
    var b = document.createElement('div');
    b.className = 'bubble';
    b.textContent = text;
    wrap.appendChild(b);
    $messages.appendChild(wrap);
    stickToBottom = true;   // 用户刚发言，强制回到底部
    scrollToBottom();
  }

  function appendAssistantBubble() {
    var wrap = document.createElement('div');
    wrap.className = 'msg assistant';
    var b = document.createElement('div');
    b.className = 'bubble streaming';
    var t = document.createElement('span');
    t.className = 'text';
    b.appendChild(t);
    b._text = t;            // 缓存文本节点，避免每 token querySelector
    wrap.appendChild(b);
    $messages.appendChild(wrap);
    stickToBottom = true;   // 新回复开始，强制吸底
    scrollToBottom();
    return b;
  }

  function markStreamingEnd(bubble) { bubble.classList.remove('streaming'); }

  function markBubbleError(bubble, note) {
    bubble.classList.remove('streaming');
    bubble.classList.add('error');
    if (note && !bubble.querySelector('.bubble-note')) {
      var small = document.createElement('div');
      small.className = 'bubble-note';
      small.textContent = note;   // 已生成的半句保留在 .text 中，不清空
      bubble.appendChild(small);
    }
  }

  // 用户主动停止：中性提示（非报错），保留半句 + 重试
  function markBubbleStopped(bubble, note) {
    bubble.classList.remove('streaming');
    bubble.classList.add('stopped');
    if (note && !bubble.querySelector('.bubble-note')) {
      var small = document.createElement('div');
      small.className = 'bubble-note stopped-note';
      small.textContent = note;
      bubble.appendChild(small);
    }
  }

  function appendSystemNotice(text) {
    var d = document.createElement('div');
    d.className = 'system-notice';
    d.textContent = text;
    $messages.appendChild(d);
    if (stickToBottom) scrollToBottom();
  }

  function addRetry(bubble, text) {
    if (!bubble.parentNode || bubble.querySelector('.retry-btn')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-danger retry-btn';
    btn.textContent = '重试';
    btn.addEventListener('click', function () {
      if (isStreaming) return;
      var wrap = bubble.parentNode;
      if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
      streamChat(text);   // 复用同一份文本，不重复追加 user 气泡
    });
    bubble.appendChild(btn);
  }

  // ---- 交付卡：它真的「递」一张过来（Web 与 App 同一套协议）----
  function appendDeliverCard(bubble, evt) {
    if (!bubble || bubble.querySelector('.deliver-card')) return;
    var card = document.createElement('div');
    card.className = 'deliver-card';
    var title = document.createElement('div');
    title.className = 'card-title';
    title.textContent = evt.title || '给你的';
    card.appendChild(title);

    var items = evt.items || [];
    for (var i = 0; i < items.length; i++) {
      var row = document.createElement('div');
      row.className = 'card-item';
      var no = document.createElement('span');
      no.className = 'card-no';
      no.textContent = String(i + 1);
      var tx = document.createElement('span');
      tx.className = 'card-text';
      tx.textContent = String(items[i] == null ? '' : items[i]);
      row.appendChild(no);
      row.appendChild(tx);
      card.appendChild(row);
    }
    if (evt.note) {
      var note = document.createElement('div');
      note.className = 'card-note';
      note.textContent = String(evt.note);
      card.appendChild(note);
    }
    var rate = document.createElement('div');
    rate.className = 'card-rate';
    rate.appendChild(makeRateBtn(card, evt, '对了', 1));
    rate.appendChild(makeRateBtn(card, evt, '不对', -1));
    card.appendChild(rate);
    bubble.appendChild(card);
    if (stickToBottom) scrollToBottom();
  }

  function makeRateBtn(card, evt, label, verdict) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'card-rate-btn';
    btn.textContent = label;
    btn.addEventListener('click', function () {
      if (card.getAttribute('data-rated')) return;   // 只认第一次，避免来回改
      card.setAttribute('data-rated', String(verdict));
      var all = card.querySelectorAll('.card-rate-btn');
      for (var i = 0; i < all.length; i++) all[i].classList.remove('picked');
      btn.classList.add('picked');
      postFeedback(evt, verdict, function (ok) {
        if (!ok) {   // 发不出去就撤回勾，绝不假装记下了
          card.removeAttribute('data-rated');
          btn.classList.remove('picked');
          appendSystemNotice('反馈没发出去，等会儿再点一次');
        }
      });
    });
    return btn;
  }

  function postFeedback(evt, verdict, done) {
    var body = JSON.stringify({
      request_id: evt.request_id || '',
      verdict: verdict,
      delivery_id: evt.delivery_id || 0,
      target: evt.title || ''
    });
    fetch(resolveBrainUrl() + '/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body
    }).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    }).then(function () { if (done) done(true); })
      .catch(function () { if (done) done(false); });
  }

  // 它自己先开的口：轮询拿一条，就当场显示成一条普通消息
  function pollProactive() {
    if (!userId) return;
    fetch(resolveBrainUrl() + '/proactive?user_id=' + encodeURIComponent(userId),
          { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var items = (d && d.items) || [];
        for (var i = 0; i < items.length; i++) {
          var bubble = appendAssistantBubble();
          renderNow(bubble, String(items[i].text || ''));
          markStreamingEnd(bubble);
        }
      })
      .catch(function () { /* 主动开口失败不打扰用户 */ });
  }

  function renderEmotion(emotion, intensity) {    var zh = EMOTION_ZH[emotion] || emotion || '平静';
    $badge.textContent = (typeof intensity === 'number') ? (zh + ' · ' + intensity.toFixed(2)) : zh;
    $badge.setAttribute('data-emotion', emotion || 'neutral');
    $badge.title = 'emotion=' + (emotion || 'neutral');
  }

  function renderMeta(meta) {
    var parts = [];
    if (meta.persona_version != null) parts.push('人格 v' + meta.persona_version);
    if (meta.degraded) parts.push('降级');
    $metaInfo.textContent = parts.length ? parts.join(' · ') : '已连接';
    $metaInfo.title = meta.request_id ? ('request_id=' + meta.request_id) : '';
  }

  function releaseObjectUrl() {
    if (currentObjectUrl) {
      try { URL.revokeObjectURL(currentObjectUrl); } catch (e) { /* noop */ }
      currentObjectUrl = null;
    }
  }

  // 只有 done 之后才异步取 wav 并播放；任何失败都静默降级，不影响对话
  function triggerTTS(text, emotion, requestId) {
    if (!text) return;
    var body = { text: text, emotion: emotion || 'neutral', reference_audio_id: null };
    if (requestId) body.request_id = requestId;

    (async function () {
      releaseObjectUrl();
      try {
        var resp = await fetch(resolveBrainUrl() + '/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        if (!resp.ok) return;                       // 503 TTS_UNAVAILABLE：静默跳过
        var blob = await resp.blob();
        if (!blob || blob.size === 0) return;
        currentObjectUrl = URL.createObjectURL(blob);
        $tts.src = currentObjectUrl;
        $tts.onended = releaseObjectUrl;
        $tts.onerror = releaseObjectUrl;
        var p = $tts.play();
        if (p && p.catch) p.catch(releaseObjectUrl); // 自动播放被拦截也不报错
      } catch (e) {
        releaseObjectUrl();
      }
    })();
  }

  // 麦克风开启/按住说话时立刻停掉 TTS：外放场景下麦克风会把 AI 的播报收进去，
  // 上一轮回复就会被当成用户输入（日志里那种「AI 自问自答」的根因之一）。
  function stopPlayback() {
    if (!$tts) return;
    try {
      if (!$tts.paused) { $tts.pause(); $tts.currentTime = 0; }
    } catch (e) { /* ignore */ }
  }

  function updateComposerState() {
    // 生成中按钮变「停止」（可点击中断），空闲时为「发送」
    $sendBtn.disabled = false;
    $sendBtn.textContent = isStreaming ? '停止' : '发送';
    $sendBtn.classList.toggle('btn-primary', !isStreaming);
    $sendBtn.classList.toggle('btn-danger', isStreaming);
    $sendBtn.title = isStreaming ? '停止生成（保留已输出的半句）' : '发送（Enter）';
  }

  function stopStreaming() {
    if (abortController) {
      try { abortController.abort(); } catch (e) { /* noop */ }
    }
  }

  function send() {
    if (isStreaming) return;
    var text = ($input.value || '').trim();
    if (!text) return;
    $input.value = '';
    autoGrow();
    appendUserBubble(text);
    streamChat(text);
  }

  function streamChat(text) {
    if (isStreaming) return;
    isStreaming = true;
    updateComposerState();

    var bubble = appendAssistantBubble();
    var fullText = '';
    var emotion = 'neutral';
    var intensity = null;
    var requestId = null;
    var meta = {};
    var terminal = false;   // 收到 done / error / clarify 之一
    var failed = false;
    var stopped = false;    // 用户主动点「停止」
    abortController = (typeof AbortController !== 'undefined') ? new AbortController() : null;

    var reader = null;
    var buffer = '';
    var decoder = null;
    var SEP_RE = /\r?\n\r?\n/;

    // 一帧内可含多行 data:（SSE 规范合并），注释行 / 其他字段一律跳过
    function handleFrame(raw) {
      var lines = String(raw).split(/\r?\n/);
      var data = [];
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (line === '') continue;
        if (line.charAt(0) === ':') continue;                 // 心跳 ": ping" 注释帧
        if (line.slice(0, 5) === 'data:') {
          var payload = line.slice(5);
          if (payload.charAt(0) === ' ') payload = payload.slice(1); // 去掉可选前导空格
          data.push(payload);
        }
        // event: / id: / retry: 等其他 SSE 字段忽略
      }
      if (!data.length) return;
      var evt;
      try { evt = JSON.parse(data.join('\n')); }
      catch (e) { return; }                                   // 坏帧忽略，绝不白屏
      handleEvent(evt);
    }

    function handleEvent(evt) {
      if (!evt || typeof evt !== 'object') return;
      switch (evt.type) {
        case 'meta':
          meta = {
            request_id: evt.request_id || null,
            persona_version: evt.persona_version,
            degraded: !!evt.degraded
          };
          if (evt.request_id) requestId = evt.request_id;
          renderMeta(meta);
          break;

        case 'token':
          fullText += (evt.delta == null ? '' : String(evt.delta));
          setBubbleText(bubble, fullText);
          break;

        case 'clarify':
          // 低信息量：直接显示温柔追问，不进 LLM；不触发 TTS
          fullText = String(evt.question || '能再多说一点吗？');
          renderNow(bubble, fullText);
          markStreamingEnd(bubble);
          terminal = true;
          if (evt.emotion) {
            emotion = evt.emotion;
            if (typeof evt.intensity === 'number') intensity = evt.intensity;
            renderEmotion(emotion, intensity);
          }
          break;

        case 'ping':
          break;                                              // 注释帧已跳过，data ping 也忽略

        case 'done':
          // 唯一正常的流结束信号：取 emotion、更新徽标、异步播放 TTS
          terminal = true;
          renderNow(bubble, fullText);   // 冲刷末帧，保证最后几个字落地
          markStreamingEnd(bubble);
          emotion = evt.emotion || emotion;
          if (typeof evt.intensity === 'number') intensity = evt.intensity;
          renderEmotion(emotion, intensity);
          triggerTTS(fullText, emotion, requestId);
          break;

        case 'card':
          appendDeliverCard(bubble, evt);   // 一张薄片：给了什么，一目了然
          break;

        case 'error':
          // 保留已生成的半句，标红 + 重试，不自动 TTS
          terminal = true;
          failed = true;
          renderNow(bubble, fullText);   // 先把已收到的半句落地，再标红
          markBubbleError(bubble, '出错了：' + (evt.message || evt.code || '未知错误'));
          addRetry(bubble, text);
          break;

        default:
          break;
      }
    }

    // 按 \n\n 从缓冲区切出完整帧；未凑齐的分隔符留在 buffer 等下一个 chunk
    function drainFrames() {
      var guard = 0;
      while (guard++ < 10000) {
        var m = SEP_RE.exec(buffer);
        if (!m) break;
        var frame = buffer.slice(0, m.index);
        buffer = buffer.slice(m.index + m[0].length);
        if (frame.length) handleFrame(frame);
      }
    }

    function flushTail() {
      var tail = buffer.replace(/^\s+|\s+$/g, '');
      buffer = '';
      if (tail) handleFrame(tail);   // 容错：末帧缺少结尾空行
    }

    (async function run() {
      try {
        var resp = await fetch(resolveBrainUrl() + '/chat', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream'
          },
          body: JSON.stringify({ user_id: userId, session_id: sessionId, text: text }),
          signal: abortController ? abortController.signal : undefined
        });

        if (!resp.ok) {
          throw new Error('HTTP ' + resp.status + (resp.statusText ? ' ' + resp.statusText : ''));
        }
        if (!resp.body || !resp.body.getReader) {
          throw new Error('浏览器不支持流式响应');
        }

        reader = resp.body.getReader();
        // 构造里带 stream:true 表达意图；真正生效的是 decode(value, {stream:true})：
        // 跨 chunk 的多字节 UTF-8 会留在解码器内部，中文不会被切成乱码。
        decoder = new TextDecoder('utf-8', { stream: true });

        while (!terminal) {
          var read = await reader.read();
          if (read.done) {
            buffer += decoder.decode();   // 冲刷解码器残余字节
            drainFrames();
            flushTail();
            break;
          }
          buffer += decoder.decode(read.value, { stream: true });
          drainFrames();                  // 缓冲区按 \n\n 切帧（一个包可能半条/多条）
        }
      } catch (err) {
        var isAbort = (err && err.name === 'AbortError') ||
                      (abortController && abortController.signal.aborted);
        renderNow(bubble, fullText);   // 无论停止还是断连，先保住已输出的半句
        if (isAbort) {
          stopped = true;
          markBubbleStopped(bubble, '已停止生成。');
          addRetry(bubble, text);
        } else {
          failed = true;
          markBubbleError(bubble, '连接中断：' + (err && err.message ? err.message : String(err)));
          appendSystemNotice('连接已断开，可点「重试」重新发送。');
        }
      } finally {
        try { if (reader) await reader.cancel(); } catch (e) { /* noop */ }
        abortController = null;
        isStreaming = false;            // 任何路径都复位，绝不卡在 isStreaming
        updateComposerState();
        markStreamingEnd(bubble);
        if (stopped) {
          // 已在 catch 标注「停止」+ 重试，无需再处理
        } else if (!terminal && !failed) {
          // 中途关掉后端：既没 done 也没 error，同样标红 + 重试
          markBubbleError(bubble, '连接提前结束，未收到完成信号。');
          addRetry(bubble, text);
        } else if (failed && !terminal) {
          addRetry(bubble, text);
        }
        $input.focus();
      }
    })();
  }

  function autoGrow() {
    $input.style.height = 'auto';
    $input.style.height = Math.min($input.scrollHeight, 140) + 'px';
  }

  function initServerInput() {
    $serverInput.value = resolveBrainUrl();
    if (CFG.BRAIN_OVERRIDE) $serverHint.textContent = '当前由 URL 参数指定';
  }

  // 开页先探一次活：否则在第一次回复之前，状态位永远停在「未连接」，看着像服务挂了
  function probeHealth() {
    var base = resolveBrainUrl();
    try {
      fetch(base + '/health', { method: 'GET', cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) { throw new Error('bad health'); }
          $metaInfo.textContent = '已连接';
          $metaInfo.classList.remove('is-down');
        })
        .catch(function () {
          $metaInfo.textContent = '未连接 · 服务未启动';
          $metaInfo.classList.add('is-down');
        });
    } catch (e) {
      /* 探活失败不影响正常对话 */
    }
  }

  // ---------------- 语音输入：按住说话 → 松手转成文字 ----------------
  // 采集用 Web Audio，自己拼 16k 单声道 WAV 发给大脑的 /asr。
  // 不用 MediaRecorder：它给的是 webm/opus，后端还得装 ffmpeg 才能解码。
  // 自己编码 wav，后端就只认一种格式，零转码依赖。
  var MIC_IDLE_TITLE = '按住说话（松手转成文字）';
  var mic = {
    el: null, ctx: null, stream: null, node: null, sink: null,
    chunks: [], frames: 0, recording: false, busy: false, t0: 0, maxSec: 60
  };

  function micState(cls, label, title) {
    if (!mic.el) return;
    mic.el.classList.remove('is-recording', 'is-busy', 'is-down');
    if (cls) mic.el.classList.add(cls);
    if (label) {
      var l = mic.el.querySelector('.mic-label');
      if (l) l.textContent = label;
    }
    mic.el.title = title || MIC_IDLE_TITLE;
  }

  function micSupported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
      (window.AudioContext || window.webkitAudioContext));
  }

  function micTeardown() {
    try { if (mic.node) { mic.node.onaudioprocess = null; mic.node.disconnect(); } } catch (e) { /* ignore */ }
    try { if (mic.sink) mic.sink.disconnect(); } catch (e) { /* ignore */ }
    try { if (mic.stream) mic.stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { /* ignore */ }
    try { if (mic.ctx && mic.ctx.state !== 'closed') mic.ctx.close(); } catch (e) { /* ignore */ }
    mic.node = mic.sink = mic.stream = mic.ctx = null;
  }

  function concatF32(chunks) {
    var len = 0, i;
    for (i = 0; i < chunks.length; i++) len += chunks[i].length;
    var out = new Float32Array(len), off = 0;
    for (i = 0; i < chunks.length; i++) { out.set(chunks[i], off); off += chunks[i].length; }
    return out;
  }

  // 粗算 RMS（每 8 个采样取一个点，够判断「有没有人说话」且几乎不耗时）
  function rmsOf(chunks) {
    var sum = 0, n = 0, i, j;
    for (i = 0; i < chunks.length; i++) {
      var c = chunks[i];
      for (j = 0; j < c.length; j += 8) { sum += c[j] * c[j]; n++; }
    }
    return n ? Math.sqrt(sum / n) : 0;
  }

  // 设备多半是 48k，模型只吃 16k。线性插值对语音识别够用，且不引第三方库
  function resampleF32(data, from, to) {
    if (!data.length || !from || from === to) return data;
    var ratio = from / to;
    var len = Math.max(1, Math.round(data.length / ratio));
    var out = new Float32Array(len);
    for (var i = 0; i < len; i++) {
      var pos = i * ratio;
      var i0 = Math.floor(pos);
      var i1 = i0 + 1 < data.length ? i0 + 1 : data.length - 1;
      var frac = pos - i0;
      out[i] = data[i0] * (1 - frac) + data[i1] * frac;
    }
    return out;
  }

  function encodeWav(samples, sampleRate) {
    var buf = new ArrayBuffer(44 + samples.length * 2);
    var view = new DataView(buf);
    function str(off, s) { for (var i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); }
    str(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    str(8, 'WAVE');
    str(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);              // PCM
    view.setUint16(22, 1, true);              // 单声道
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true); // 字节率
    view.setUint16(32, 2, true);              // 块对齐
    view.setUint16(34, 16, true);             // 位深
    str(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    var off = 44;
    for (var i = 0; i < samples.length; i++) {
      var s = samples[i];
      s = s < -1 ? -1 : (s > 1 ? 1 : s);
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      off += 2;
    }
    return new Blob([view], { type: 'audio/wav' });
  }

  async function micRecognize(pcm, rate) {
    var pcm16 = resampleF32(pcm, rate, 16000);
    var wav = encodeWav(pcm16, 16000);
    var resp = await fetch(resolveBrainUrl() + '/asr', {
      method: 'POST',
      headers: { 'Content-Type': 'audio/wav' },
      body: wav
    });
    var data = null;
    try { data = await resp.json(); } catch (e) { /* ignore */ }
    if (!resp.ok || !data || !data.ok) {
      throw new Error((data && data.detail) || ('语音识别失败（HTTP ' + resp.status + '）'));
    }
    return (data.text || '').trim();
  }

  async function micStart() {
    if (mic.recording || mic.busy) return;
    if (!micSupported()) {
      appendSystemNotice('这个浏览器不支持录音。换成 Chrome / Edge，或者直接用键盘打字。');
      return;
    }
    try {
      mic.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });
    } catch (e) {
      appendSystemNotice('麦克风打不开。看一眼地址栏右边那个权限图标，允许麦克风后再按住说话。');
      return;
    }

    var AC = window.AudioContext || window.webkitAudioContext;
    // 直接按 16k 建上下文：由浏览器做高质量重采样，避免我们自己做线性插值时
    // 产生的混叠失真（48k → 16k 直接抽点会让 s/sh/x 这类音糊掉，识别就容易错字）。
    try {
      mic.ctx = new AC({ sampleRate: 16000 });
    } catch (e) {
      mic.ctx = new AC();   // 老浏览器不支持指定采样率时退回设备默认，micRecognize 里再重采样
    }
    if (mic.ctx.state === 'suspended') { try { await mic.ctx.resume(); } catch (e) { /* ignore */ } }

    stopPlayback();   // 先掐掉 TTS，再开麦，避免把 AI 的声音录进去

    var src = mic.ctx.createMediaStreamSource(mic.stream);
    mic.node = mic.ctx.createScriptProcessor(4096, 1, 1);
    mic.sink = mic.ctx.createGain();
    mic.sink.gain.value = 0;   // 哑节点：ScriptProcessor 必须接到 destination 才回调，
                               // 但直接接会把自己说的话放出来，所以串一个 0 增益
    mic.chunks = [];
    mic.frames = 0;
    mic.node.onaudioprocess = function (ev) {
      if (!mic.recording) return;
      var buf = ev.inputBuffer.getChannelData(0);
      mic.chunks.push(new Float32Array(buf));
      mic.frames += buf.length;
      if (mic.frames > mic.maxSec * mic.ctx.sampleRate) micStop();   // 防止忘了松手
    };
    src.connect(mic.node);
    mic.node.connect(mic.sink);
    mic.sink.connect(mic.ctx.destination);

    mic.recording = true;
    mic.t0 = Date.now();
    micState('is-recording', '松手结束', '正在录音…松手就送去识别');
  }

  async function micStop() {
    if (!mic.recording) return;
    mic.recording = false;

    var seconds = (Date.now() - mic.t0) / 1000;
    var rate = mic.ctx ? mic.ctx.sampleRate : 48000;
    var chunks = mic.chunks;
    micTeardown();

    if (seconds < 0.35 || !chunks.length) {
      micState('', '按住说话');
      return;
    }
    // 全程没声音就别送去识别：静音/底噪会被模型强行「猜」出一句似是而非的话
    if (rmsOf(chunks) < 0.004) {
      micState('', '按住说话');
      appendSystemNotice('没听到声音。确认麦克风没被静音，靠近一点再说一遍。');
      return;
    }

    mic.busy = true;
    micState('is-busy', '识别中…', '正在把语音转成文字');
    try {
      var text = await micRecognize(concatF32(chunks), rate);
      if (text) {
        var cur = $input.value.replace(/\s+$/, '');
        $input.value = cur ? (cur + ' ' + text) : text;
        autoGrow();
        $input.focus();
      } else {
        appendSystemNotice('没听清，再说一遍？（也可能是还没开口就松手了）');
      }
    } catch (e) {
      appendSystemNotice((e && e.message) || '语音识别失败');
    } finally {
      mic.busy = false;
      micState('', '按住说话');
    }
  }

  function initMic() {
    mic.el = $mic;
    if (!mic.el) return;
    if (!micSupported()) {
      micState('is-down', '按住说话', '这个浏览器不支持录音');
      return;
    }

    mic.el.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      micStart();
    });
    // 手指可能滑出按钮才抬起，所以松手监听挂在 document 上
    document.addEventListener('pointerup', function () {
      if (mic.recording) micStop();
    });
    document.addEventListener('pointercancel', function () {
      if (mic.recording) micStop();
    });
    // 长按弹出的右键菜单会打断录音体验
    mic.el.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    // 切走页面还继续录会白占麦克风
    document.addEventListener('visibilitychange', function () {
      if (document.hidden && mic.recording) micStop();
    });
  }

  function init() {
    $messages = $('messages');
    $input = $('input');
    $sendBtn = $('send-btn');
    $badge = $('emotion-badge');
    $metaInfo = $('meta-info');
    $serverInput = $('server-input');
    $serverSave = $('server-save');
    $serverHint = $('server-hint');
    $clearBtn = $('clear-btn');
    $tts = $('tts-player');
    $mic = $('mic-btn');

    // user_id 固定成 u1：和 App 共用同一份记忆（手机上说过的事，网页上也记得）。
    // session_id 仍按浏览器持久化——会话只是「这一次聊天」的上下文，不该跨端串。
    // 以前这里用随机 id，导致同一个人在网页和 App 上各有一套互不相通的记忆。
    userId = 'u1';
    sessionId = getOrCreateId(LS_SESSION, 's');

    initServerInput();
    $('admin-link').href = adminHref();
    renderEmotion('neutral', null);
    updateComposerState();
    appendSystemNotice('输入一句话开始对话。');
    probeHealth();
    initMic();
    // 它可能先开口：进来先问一次，之后每 5 分钟问一次
    setTimeout(pollProactive, 1500);
    setInterval(pollProactive, 5 * 60 * 1000);

    // 智能吸底：用户上滑看历史时 stickToBottom=false，不再被新 token 拽回底部
    $messages.addEventListener('scroll', function () {
      stickToBottom = nearBottom();
    }, { passive: true });

    $sendBtn.addEventListener('click', function () {
      if (isStreaming) stopStreaming();
      else send();
    });
    $input.addEventListener('input', autoGrow);
    $input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    });

    $serverSave.addEventListener('click', function () {
      var v = ($serverInput.value || '').trim().replace(/\/+$/, '');
      if (!v) { $serverHint.textContent = '地址不能为空'; return; }
      if (!/^https?:\/\//i.test(v)) { $serverHint.textContent = '需以 http:// 或 https:// 开头'; return; }
      persistBrainUrl(v);
      if (CFG.BRAIN_OVERRIDE) CFG.BRAIN_OVERRIDE = null;  // 手动编辑优先于 URL 覆盖
      $serverInput.value = v;
      $serverHint.textContent = '已保存：' + v;
      probeHealth();
    });

    $clearBtn.addEventListener('click', function () {
      if (isStreaming) return;   // 流式期间不清，避免 reader 引用悬空
      $messages.innerHTML = '';
      stickToBottom = true;
      appendSystemNotice('会话界面已清空（user_id / session_id 保持不变，记忆仍可跨轮生效）。');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
