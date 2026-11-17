/* 训练中心：只展示事实 + 触发用户点过的动作。 */
(function () {
  "use strict";

  var timer = null;
  var LS_TOKEN = "companion.admin_token";
  var lastStatus = null;  // 缓存最近一次 /status，供「开始训练」前置拦截使用

  function el(id) { return document.getElementById(id); }

  // 管理口令：优先取页面输入框的值，否则读 localStorage（与控制台同源）。
  function getToken() {
    var box = el("adminToken");
    var t = (box && box.value) || "";
    try { return t || localStorage.getItem(LS_TOKEN) || ""; } catch (e) { return t; }
  }
  function headers() {
    var h = { "Content-Type": "application/json" };
    var t = getToken();
    if (t) { h["X-Admin-Token"] = t; }
    return h;
  }
  function getJson(url) {
    return fetch(url, { headers: { "Accept": "application/json", "X-Admin-Token": getToken() } })
      .then(function (r) { return r.json(); });
  }

  function tag(text, kind) {
    return '<span class="tag ' + (kind || "ok") + '">' + text + "</span>";
  }

  function row(k, v) {
    return '<div class="k">' + k + "</div><div>" + v + "</div>";
  }

  function renderEnv(s) {
    var t = s.torch || {};
    var torchText = t.installed
      ? (t.cuda ? tag("CUDA 可用", "ok") + " " + t.version
                : tag("CPU 版", "bad") + " " + t.version)
      : tag("未安装", "bad");
    var gpuText = (s.gpus && s.gpus.length) ? s.gpus.join(" / ")
      : tag("nvidia-smi 读不到", "warn");
    var depText = Object.keys(s.deps).map(function (k) {
      return k + (s.deps[k] ? tag("OK", "ok") : tag("缺", "bad"));
    }).join(" ");
    var llamaText = (s.llamacpp && s.llamacpp.length)
      ? s.llamacpp.join(", ") : tag("没找到", "bad");

    el("envRows").innerHTML = [
      row("Python", '<code>' + s.python + "</code>"),
      row("torch", torchText),
      row("显卡", gpuText),
      row("依赖", depText),
      row("口吻语料", s.samples + " 条"),
      row("训练集", s.dataset.train + " 条 / 验证 " + s.dataset.valid + " 条 " +
          (s.samples_ready === false
            ? tag("样本不足", "warn") + " " + (s.reason || "请先补充语料")
            : (s.dataset.ready ? tag("够起步", "ok") : tag("未到 200 条", "warn")))),
      row("llama.cpp", llamaText),
    ].join("");
  }

  function renderOptions(s) {
    var wrap = el("options");
    wrap.innerHTML = (s.options || []).map(function (o) {
      var can = (o.id === "cuda_torch" || o.id === "bnb");
      return '<div class="opt"><div class="grow"><b>' + o.title + "</b><small>" +
        o.size + " · " + o.solves + "</small><code>" + o.cmd + "</code></div>" +
        (can ? '<button data-install="' + o.id + '">执行</button>'
             : '<button disabled>需手动</button>') + "</div>";
    }).join("");
    Array.prototype.forEach.call(wrap.querySelectorAll("[data-install]"), function (b) {
      b.addEventListener("click", function () {
        b.disabled = true;
        post("/api/train/install", { target: b.getAttribute("data-install") })
          .then(function () { refresh(); });
      });
    });
  }

  function renderModels(s) {
    var sel = el("baseModel");
    var hf = (s.models && s.models.hf) || [];
    if (!hf.length) {
      sel.innerHTML = '<option value="">（本机没有 HF 原版权重）</option>';
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = hf.map(function (m) {
      return '<option value="' + m.path + '">' + m.path + "  " + m.size_gb + "GB</option>";
    }).join("");
  }

  function renderJob(j, logTail) {
    var line = el("jobLine");
    if (!j || j.status === "idle" || !j.status) {
      line.textContent = "当前没有任务";
    } else {
      var state = j.status === "running" ? "运行中 " + (j.elapsed_s || 0) + "s"
        : j.status === "done" ? "已完成"
        : j.status === "failed" ? "失败（退出码 " + j.code + "）" : j.status;
      line.textContent = j.name + " · " + state;
    }
    if (logTail) {
      el("log").textContent = logTail;
      el("log").scrollTop = el("log").scrollHeight;
    }
    var running = j && j.status === "running";
    var noSamples = !!(lastStatus && lastStatus.samples_ready === false);
    el("btnTrain").disabled = !!running || noSamples;  // 样本不足时禁用，别起空任务
    el("btnDataset").disabled = !!running;
    el("btnStop").disabled = !running;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      if (r.status === 401 || r.status === 403) {
        alert("口令不对或未配置：请在上方填管理口令（与控制台同一个）。");
      }
      return r.json();
    }).then(function (j) {
      if (j && j.ok === false && j.error) { alert(j.error); }
      return j;
    });
  }

  function refresh() {
    return getJson("/api/train/status")
      .then(function (s) {
        lastStatus = s;
        renderEnv(s);
        renderOptions(s);
        renderModels(s);
        renderJob(s.job, (s.job || {}).log_tail);
        if (s.job && s.job.status === "running") { startPolling(); } else { stopPolling(); }
      });
  }

  function pollJob() {
    return getJson("/api/train/job")
      .then(function (j) {
        renderJob(j, j.log_tail);
        if (j.status !== "running") {
          stopPolling();
          refresh();
        }
      });
  }

  function startPolling() {
    if (timer) { return; }
    timer = setInterval(pollJob, 2000);
  }

  function stopPolling() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  el("btnDataset").addEventListener("click", function () {
    post("/api/train/dataset", { with_persona: true }).then(refresh);
  });

  el("btnTrain").addEventListener("click", function () {
    var base = el("baseModel").value;
    if (!base) { alert("先选一个 HF 原版权重目录"); return; }
    if (lastStatus && lastStatus.samples_ready === false) {
      alert(lastStatus.reason || "样本不足：请先补充训练样本再训练。");
      return;
    }
    post("/api/train/train", {
      base_model: base,
      epochs: Number(el("epochs").value || 3),
      rank: Number(el("rank").value || 16),
      qlora: el("qlora").checked,
      merge: el("merge").checked,
    }).then(refresh);
  });

  el("btnStop").addEventListener("click", function () {
    post("/api/train/stop", {}).then(refresh);
  });

  el("btnRefresh").addEventListener("click", refresh);

  refresh();
}());
