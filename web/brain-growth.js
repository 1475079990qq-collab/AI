/* brain-growth.js — 脑区「真实生长」纯逻辑模块（零依赖、同种子确定性）。
 *
 * 与 brain.js 的分叉接口（阶段二由 brain.js 转走本模块；本文件不碰 DOM、不碰画布）：
 *   window.BrainGrowth.build(facts, lessons, opts) -> {
 *     neurons: [{ nx, ny, region, size, delay, fid, kind }],
 *     edges: [[i, j]],            // 突触：最近邻 + 跨区长投射
 *     assocPairs: [[i, j]],       // 共享两字词（中文 bigram）的联想边
 *     memAnchors: { fid: index }, // 每条事实自己那颗记忆区神经元的下标
 *     lessonAnchors: { fid: index },
 *     counts: [9 个数],          // 0听见 1情感判断 2记忆检索 3提示词 4工具 5思考 6出字 7声音 8收尾
 *     total, cap: { fact, lesson }, factCap, lessonCap,
 *     factTotal, lessonTotal,
 *     overCap: { facts, lessons }, baseCount, seed
 *   }
 *
 * 规则要点（与 _审核/生长策略-2026-11-16.md 对应）：
 *   - 结构神经元基数 baseCount（默认 680）不缩水；
 *   - 每条有效事实占一颗记忆区(region=2)神经元，每条有效教训占一颗收尾区(region=8)神经元；
 *   - 事实硬上限 factCap（默认 600），教训硬上限 lessonCap（默认 60）；超上限不丢事实，
 *     多余条目按 id 哈希轮询挂靠到已有神经元上，锚点仍能定位；
 *   - 所有随机来自固定种子（默认 20261116，与 brain.js 旧种子一致），同一份输入两次构建逐点相同；
 *   - 坐标必须落在 brain.js insideBrain 轮廓之内，不允许 NaN / 越界。
 */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.BrainGrowth = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var SEED = 20261116;

  // 九区下标与 brain.js REGIONS 保持一致（cx/cy 为区中心归一化坐标，weight 为分区权重）
  var REGIONS = [
    { key: 'input',   name: '听见',     cx: 0.50, cy: 0.88, weight: 0.09 },
    { key: 'judge',   name: '情感判断', cx: 0.25, cy: 0.68, weight: 0.12 },
    { key: 'memory',  name: '记忆检索', cx: 0.74, cy: 0.64, weight: 0.12 },
    { key: 'prompt',  name: '提示词',   cx: 0.79, cy: 0.42, weight: 0.10 },
    { key: 'tool',    name: '工具',     cx: 0.63, cy: 0.76, weight: 0.08 },
    { key: 'llm',     name: '思考',     cx: 0.40, cy: 0.42, weight: 0.22 },
    { key: 'token',   name: '出字',     cx: 0.55, cy: 0.24, weight: 0.12 },
    { key: 'voice',   name: '声音',     cx: 0.29, cy: 0.30, weight: 0.09 },
    { key: 'done',    name: '收尾',     cx: 0.17, cy: 0.50, weight: 0.07 }
  ];

  var DEFAULTS = {
    seed: SEED,
    baseCount: 680,    // 结构神经元基数（不随事实条数缩水）
    factCap: 600,      // 记忆区硬上限：最多 600 颗事实神经元
    lessonCap: 60,     // 收尾区硬上限：最多 60 颗教训神经元
    synCap: 3200,      // 突触边总量上限（去重后截断，避免画布过重）
    assocCap: 4000,    // 联想边总量上限
    nearK: 3           // 每个神经元接最近邻的个数
  };

  // 与 brain.js 完全一致的固定种子伪随机（mulberry32 风格），保证跨文件同种子结果可比
  function rng(seed) {
    var s = seed | 0;
    return function () {
      s |= 0; s = (s + 0x6D2B79F5) | 0;
      var t = Math.imul(s ^ (s >>> 15), 1 | s);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // 按下标派生一条独立随机流：第 k 颗生长神经元的坐标只依赖 k，不依赖总数，
  // 这样后续追加事实时，已存在的点坐标不漂（增量生长不洗牌）。
  function rngAt(seed, k) {
    var s = (seed ^ Math.imul(k + 1, 0x9E3779B9)) | 0;
    return rng(s);
  }

  // 脑形轮廓：逐字沿用 brain.js insideBrain（两个半球 + 额叶/底部收口）
  function insideBrain(nx, ny) {
    if (typeof nx !== 'number' || typeof ny !== 'number' || !isFinite(nx) || !isFinite(ny)) return false;
    var cx = 0.5, cy = 0.52;
    var dx = (nx - cx) / 0.40, dy = (ny - cy) / 0.40;
    var r = Math.sqrt(dx * dx + dy * dy);
    if (r > 1) return false;
    if (ny < 0.32 && Math.abs(nx - 0.5) < 0.16 && r > 0.72) return false;
    if (ny > 0.78 && Math.abs(nx - 0.5) < 0.20 && r > 0.80) return false;
    return true;
  }

  function hashStr(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  // 按「权重 + 距离」给一个坐标分区（沿用 brain.js build 的打分方式）
  function assignRegion(nx, ny) {
    var best = 0, bestScore = 1e9;
    for (var i = 0; i < REGIONS.length; i++) {
      var r = REGIONS[i];
      var d = Math.hypot((nx - r.cx) * 1.35, ny - r.cy);
      var score = d / r.weight;
      if (score < bestScore) { bestScore = score; best = i; }
    }
    return best;
  }

  function clamp01(v) { return Math.min(0.97, Math.max(0.03, v)); }

  // 在某区中心周围的椭圆补丁里取一个脑形内坐标；环形回退保证绝不落到脑外 / NaN。
  function patchPoint(seed, k, cx, cy, rx, ry) {
    var rand = rngAt(seed, k);
    var a, rr, nx, ny, ok = false;
    for (var attempt = 0; attempt < 14 && !ok; attempt++) {
      a = rand() * Math.PI * 2;
      rr = Math.sqrt(rand());
      nx = cx + Math.cos(a) * rx * rr;
      ny = cy + Math.sin(a) * ry * rr;
      if (insideBrain(nx, ny)) ok = true;
    }
    if (!ok) {
      // 黄金角环形排布：确定性、均匀、不重叠；半径先松后紧
      var golden = 2.399963229728653;
      var frac = ((k % 97) + 1) / 97;
      a = k * golden;
      rr = 0.5 + 0.5 * frac;
      nx = cx + Math.cos(a) * rx * rr;
      ny = cy + Math.sin(a) * ry * rr;
      if (!insideBrain(clamp01(nx), clamp01(ny))) { nx = cx; ny = cy; }
    }
    return { nx: clamp01(nx), ny: clamp01(ny) };
  }

  function factId(f) { return f && f.id != null ? String(f.id) : null; }
  function factText(f) {
    if (!f) return '';
    return String(f.text != null ? f.text : (f.note != null ? f.note : ''));
  }

  // 清洗输入：去空 id、按 id 去重（重复 id 挂靠到首条那颗），返回有序数组
  function normalize(list, textKey) {
    var out = [], seen = {};
    if (!Array.isArray(list)) return out;
    for (var i = 0; i < list.length; i++) {
      var f = list[i];
      var id = factId(f);
      if (!id) continue;
      var text = String(textKey === 'note' && f && f.note != null ? f.note
        : (f && f.text != null ? f.text : ''));
      if (seen[id] !== undefined) continue;
      seen[id] = out.length;
      out.push({ id: id, text: text });
    }
    return out;
  }

  function build(facts, lessons, opts) {
    opts = opts || {};
    var seed = opts.seed != null ? opts.seed | 0 : DEFAULTS.seed;
    var baseCount = opts.baseCount != null ? opts.baseCount | 0 : DEFAULTS.baseCount;
    var factCap = opts.factCap != null ? opts.factCap | 0 : DEFAULTS.factCap;
    var lessonCap = opts.lessonCap != null ? opts.lessonCap | 0 : DEFAULTS.lessonCap;
    var synCap = opts.synCap != null ? opts.synCap | 0 : DEFAULTS.synCap;
    var assocCap = opts.assocCap != null ? opts.assocCap | 0 : DEFAULTS.assocCap;
    var nearK = opts.nearK != null ? opts.nearK | 0 : DEFAULTS.nearK;

    var factList = normalize(facts, 'text');
    var lessonList = normalize(lessons, 'note');
    var factTotal = factList.length;
    var lessonTotal = lessonList.length;

    var neurons = [];
    var memAnchors = {};
    var lessonAnchors = {};
    var factNeuronIds = [];
    var lessonNeuronIds = [];

    // ---- 1. 结构神经元基数：沿用旧 build 的采样与分区，固定种子 ----
    var rand = rng(seed);
    var guard = 0;
    while (neurons.length < baseCount && guard < baseCount * 30) {
      guard++;
      var nx = rand(), ny = 0.12 + rand() * 0.80;
      if (!insideBrain(nx, ny)) continue;
      neurons.push({
        nx: nx, ny: ny, region: assignRegion(nx, ny),
        size: 1.0 + rand() * 1.2, delay: rand() * 0.4,
        fid: null, kind: 'base'
      });
    }
    var baseCountActual = neurons.length;

    // ---- 2. 事实神经元：记忆检索区(2)，一条一颗；超上限的挂靠到已有颗 ----
    var memCenter = REGIONS[2];
    var factN = Math.min(factTotal, factCap);
    var ft = factN / Math.max(1, factCap);           // 0..1，补丁随条数略微撑开
    var fRx = 0.105 + 0.065 * ft, fRy = 0.085 + 0.055 * ft;
    for (var k = 0; k < factN; k++) {
      var f = factList[k];
      var p = patchPoint(seed, k, memCenter.cx, memCenter.cy, fRx, fRy);
      var idx = neurons.length;
      neurons.push({
        nx: p.nx, ny: p.ny, region: 2,
        size: 1.0 + rngAt(seed, k ^ 0x51)() * 0.9,
        delay: rngAt(seed, k ^ 0x52)() * 0.4,
        fid: f.id, kind: 'fact'
      });
      memAnchors[f.id] = idx;
      factNeuronIds.push(idx);
    }
    // 超上限事实：不丢，按 id 哈希均匀挂靠到已有记忆神经元；锚点仍可定位到该颗
    for (var e = factCap; e < factTotal; e++) {
      var ef = factList[e];
      var anchor = factNeuronIds.length
        ? factNeuronIds[hashStr(ef.id) % factNeuronIds.length] : null;
      if (anchor !== null) memAnchors[ef.id] = anchor;
    }

    // ---- 3. 教训神经元：收尾区(8)，一条一颗 ----
    var doneCenter = REGIONS[8];
    var lessonN = Math.min(lessonTotal, lessonCap);
    var lt = lessonN / Math.max(1, lessonCap);
    var lRx = 0.075 + 0.045 * lt, lRy = 0.105 + 0.055 * lt;
    for (var l = 0; l < lessonN; l++) {
      var ls = lessonList[l];
      var lp = patchPoint(seed, l ^ 0x7A1, doneCenter.cx, doneCenter.cy, lRx, lRy);
      var lidx = neurons.length;
      neurons.push({
        nx: lp.nx, ny: lp.ny, region: 8,
        size: 1.0 + rngAt(seed, l ^ 0x61)() * 0.6,
        delay: rngAt(seed, l ^ 0x62)() * 0.4,
        fid: ls.id, kind: 'lesson'
      });
      lessonAnchors[ls.id] = lidx;
      lessonNeuronIds.push(lidx);
    }
    for (var le = lessonCap; le < lessonTotal; le++) {
      var el = lessonList[le];
      var lanchor = lessonNeuronIds.length
        ? lessonNeuronIds[hashStr(el.id) % lessonNeuronIds.length] : null;
      if (lanchor !== null) lessonAnchors[el.id] = lanchor;
    }

    // ---- 4. 突触边：每个神经元接最近 nearK 个（同区优先），再加跨区「长投射」 ----
    var N = neurons.length;
    var edges = [];
    for (var i = 0; i < N; i++) {
      var a = neurons[i];
      var near = [];
      for (var j = 0; j < N; j++) {
        if (i === j) continue;
        var b = neurons[j];
        var d = Math.hypot((a.nx - b.nx) * 1.35, a.ny - b.ny);
        if (b.region === a.region) d *= 0.72;
        near.push([d, j]);
      }
      near.sort(function (p, q) { return p[0] - q[0]; });
      for (var k2 = 0; k2 < nearK && k2 < near.length; k2++) {
        var id = near[k2][1];
        if (i < id) edges.push([i, id]);
      }
      if (i % 26 === 0) {
        var nextRegion = (a.region + 1) % REGIONS.length;
        var bestId = -1, bestD = 1e9;
        for (var m = 0; m < N; m++) {
          if (neurons[m].region !== nextRegion) continue;
          var dd = Math.hypot((a.nx - neurons[m].nx) * 1.35, a.ny - neurons[m].ny);
          if (dd < bestD) { bestD = dd; bestId = m; }
        }
        if (bestId >= 0 && bestD < 0.22) edges.push([i, bestId]);
      }
    }
    var seenEdge = {};
    edges = edges.filter(function (e2) {
      var key = e2[0] + ':' + e2[1];
      if (seenEdge[key]) return false;
      seenEdge[key] = true;
      return true;
    }).slice(0, synCap);

    // ---- 5. 联想边：事实文本共享两字词（中文 bigram），沿用召回同款词重合思路 ----
    var assocPairs = [];
    var grams = {};
    factList.forEach(function (f) {
      var id = f.id;
      var text = f.text;
      for (var g = 0; g + 1 < text.length; g++) {
        var gram = text.slice(g, g + 2);
        if (grams[gram] === undefined) grams[gram] = [];
        if (grams[gram].length < 6 && grams[gram].indexOf(id) < 0) grams[gram].push(id);
      }
    });
    var seenPair = {};
    Object.keys(grams).forEach(function (gram) {
      var ids = grams[gram];
      for (var x = 0; x < ids.length; x++) {
        for (var y = x + 1; y < ids.length; y++) {
          var u = memAnchors[ids[x]], v = memAnchors[ids[y]];
          if (u === undefined || v === undefined || u === v) continue;
          var pk = u < v ? u + ':' + v : v + ':' + u;
          if (seenPair[pk]) continue;
          seenPair[pk] = true;
          assocPairs.push([u, v]);
        }
      }
    });
    assocPairs = assocPairs.slice(0, assocCap);

    // ---- 6. 汇总 ----
    var counts = [0, 0, 0, 0, 0, 0, 0, 0, 0];
    neurons.forEach(function (n) { counts[n.region]++; });

    return {
      neurons: neurons,
      edges: edges,
      assocPairs: assocPairs,
      memAnchors: memAnchors,
      lessonAnchors: lessonAnchors,
      counts: counts,
      total: N,
      cap: { fact: factCap, lesson: lessonCap },
      factCap: factCap,
      lessonCap: lessonCap,
      factTotal: factTotal,
      lessonTotal: lessonTotal,
      overCap: {
        facts: Math.max(0, factTotal - factCap),
        lessons: Math.max(0, lessonTotal - lessonCap)
      },
      baseCount: baseCountActual,
      seed: seed
    };
  }

  return {
    build: build,
    insideBrain: insideBrain,
    REGIONS: REGIONS,
    DEFAULTS: DEFAULTS,
    version: '1.0.0'
  };
});
