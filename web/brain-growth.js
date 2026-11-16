/* brain-growth.js — 脑区「真实生长」纯逻辑模块（零依赖、同种子确定性、不设数量上限）。
 *
 * 与 brain.js 的分叉接口（阶段二由 brain.js 转走本模块；本文件不碰 DOM、不碰画布）：
 *   window.BrainGrowth.build(facts, lessons, opts) -> {
 *     neurons: [{ nx, ny, region, size, delay, fid, kind }],
 *     edges: [[i, j]],            // 突触：网格近邻 + 稀疏跨区长投射（边预算自适应）
 *     assocPairs: [[i, j]],       // 共享两字词（中文 bigram）的联想边（距离分桶降采样）
 *     memAnchors: { fid: index }, // 每条事实自己那颗记忆区神经元的下标（1:1，不共享）
 *     lessonAnchors: { fid: index },
 *     counts: [8 个数],          // 0听见 1情绪 2回忆 3联想 4判断 5表达 6反思 7沉淀
 *     total, cap: { fact: null, lesson: null }, factCap, lessonCap,
 *     factTotal, lessonTotal, overCap: { facts: 0, lessons: 0 },
 *     baseCount, seed, edgeStats
 *   }
 *
 * 规则要点：
 *   - 结构神经元基数 baseCount（默认 680）不缩水；
 *   - 每条有效事实一颗回忆区(region=2)神经元、每条有效教训一颗沉淀区(region=7)神经元，
 *     永久 1:1：不截断、不合并、不挂靠；memAnchors[fid] 精确定位到自己那颗；
 *   - 不设事实/教训数量上限（cap=null）；护栏是性能策略，不是数量上限：
 *     突触走空间网格近邻（O(N·k)），联想边按距离分桶 + 每桶上限；
 *   - 所有随机来自固定种子（默认 20261116），同一份输入两次构建逐点相同；
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

  // 八区冻结下标：0 听见 / 1 情绪 / 2 回忆 / 3 联想 / 4 判断 / 5 表达 / 6 反思 / 7 沉淀
  // cx/cy 为区中心归一化坐标，weight 为分区权重；坐标仍须落在 insideBrain 轮廓内。
  var REGIONS = [
    { key: 'input',   name: '听见', cx: 0.50, cy: 0.88, weight: 0.09 },
    { key: 'emotion', name: '情绪', cx: 0.25, cy: 0.68, weight: 0.12 },
    { key: 'memory',  name: '回忆', cx: 0.74, cy: 0.64, weight: 0.12 },
    { key: 'assoc',   name: '联想', cx: 0.63, cy: 0.76, weight: 0.10 },
    { key: 'judge',   name: '判断', cx: 0.40, cy: 0.42, weight: 0.15 },
    { key: 'express', name: '表达', cx: 0.55, cy: 0.24, weight: 0.16 },
    { key: 'reflect', name: '反思', cx: 0.29, cy: 0.30, weight: 0.09 },
    { key: 'settle',  name: '沉淀', cx: 0.17, cy: 0.50, weight: 0.07 }
  ];

  var DEFAULTS = {
    seed: SEED,
    baseCount: 680,    // 结构神经元基数（不随事实条数缩水）
    nearK: 3,          // 每神经元保留的最近邻短边数（另留 1 条中距边）
    grid: 10,          // 空间网格边数（归一化坐标分桶，近邻只查本格 + 8 邻格）
    synCeiling: 6000,  // 突触全局硬顶（性能护栏，不是神经元数量上限）
    // 联想边按两端距离分桶，每桶独立上限；越远的桶越先被降采样
    assocBuckets: [
      { max: 0.08, cap: 2000 },   // 近邻联想
      { max: 0.16, cap: 1200 },   // 中距联想
      { max: Infinity, cap: 400 }  // 远端联想（最稀）
    ]
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

  // 空间网格：把归一化坐标分到 grid×grid 格子，近邻搜索只查本格 + 8 邻格，
  // 边构建复杂度从 O(N²) 降到 O(N·k)，保证构建耗时随 N 近似线性。
  function makeGrid(grid) {
    var size = grid * grid;
    var buckets = new Array(size);
    for (var i = 0; i < size; i++) buckets[i] = [];
    return { grid: grid, buckets: buckets };
  }
  function gridCell(g, nx, ny) {
    var cx = Math.min(g.grid - 1, Math.max(0, Math.floor(nx * g.grid)));
    var cy = Math.min(g.grid - 1, Math.max(0, Math.floor(ny * g.grid)));
    return cy * g.grid + cx;
  }
  function forNeighborCells(g, cell, cb) {
    var gx = cell % g.grid, gy = Math.floor(cell / g.grid);
    for (var dy = -1; dy <= 1; dy++) {
      for (var dx = -1; dx <= 1; dx++) {
        var x = gx + dx, y = gy + dy;
        if (x < 0 || y < 0 || x >= g.grid || y >= g.grid) continue;
        cb(y * g.grid + x);
      }
    }
  }

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
    var nearK = opts.nearK != null ? opts.nearK | 0 : DEFAULTS.nearK;
    var gridN = opts.grid != null ? opts.grid | 0 : DEFAULTS.grid;
    var synCeiling = opts.synCeiling != null ? opts.synCeiling | 0 : DEFAULTS.synCeiling;
    var assocBuckets = opts.assocBuckets || DEFAULTS.assocBuckets;

    var factList = normalize(facts, 'text');
    var lessonList = normalize(lessons, 'note');
    var factTotal = factList.length;
    var lessonTotal = lessonList.length;

    var neurons = [];
    var memAnchors = {};
    var lessonAnchors = {};

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

    // ---- 2. 事实神经元：回忆区(2)，一条一颗，永久 1:1，不截断不合并 ----
    var memCenter = REGIONS[2];
    var fRx = 0.16, fRy = 0.13;   // 补丁固定在脑形内；密度随条数上升，不向外扩出轮廓
    for (var k = 0; k < factTotal; k++) {
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
    }

    // ---- 3. 教训神经元：沉淀区(7)，一条一颗，永久 1:1 ----
    var doneCenter = REGIONS[7];
    var lRx = 0.075, lRy = 0.105;
    for (var l = 0; l < lessonTotal; l++) {
      var ls = lessonList[l];
      var lp = patchPoint(seed, l ^ 0x7A1, doneCenter.cx, doneCenter.cy, lRx, lRy);
      var lidx = neurons.length;
      neurons.push({
        nx: lp.nx, ny: lp.ny, region: 7,
        size: 1.0 + rngAt(seed, l ^ 0x61)() * 0.6,
        delay: rngAt(seed, l ^ 0x62)() * 0.4,
        fid: ls.id, kind: 'lesson'
      });
      lessonAnchors[ls.id] = lidx;
    }

    // ---- 4. 突触边：网格近邻 + 稀疏跨区长投射；边预算自适应，复杂度 O(N·k) ----
    var N = neurons.length;
    var edges = [];
    var grid = makeGrid(gridN);
    neurons.forEach(function (n, i) { grid.buckets[gridCell(grid, n.nx, n.ny)].push(i); });
    var regionMembers = [];
    for (var r0 = 0; r0 < REGIONS.length; r0++) regionMembers.push([]);
    neurons.forEach(function (n, i) { regionMembers[n.region].push(i); });

    var seenEdge = {};
    function pushEdge(a, b) {
      if (a === b) return;
      var lo = a < b ? a : b, hi = a < b ? b : a;
      var key = lo + ':' + hi;
      if (seenEdge[key]) return;
      seenEdge[key] = true;
      edges.push([lo, hi]);
    }

    for (var i = 0; i < N; i++) {
      var a = neurons[i];
      var keeper = [];   // 只留最近的 keepMax 个：小插入表，避免对候选全量排序
      var keepMax = nearK + 1;
      forNeighborCells(grid, gridCell(grid, a.nx, a.ny), function (cell) {
        var bucket = grid.buckets[cell];
        for (var c = 0; c < bucket.length; c++) {
          var j = bucket[c];
          if (j === i) continue;
          var b = neurons[j];
          var d = Math.hypot((a.nx - b.nx) * 1.35, a.ny - b.ny);
          if (b.region === a.region) d *= 0.72;
          var pos = keeper.length;
          while (pos > 0 && keeper[pos - 1][0] > d) pos--;
          if (keeper.length < keepMax) {
            keeper.splice(pos, 0, [d, j]);
          } else if (pos < keeper.length) {
            keeper.pop();
            keeper.splice(pos, 0, [d, j]);
          }
        }
      });
      var shortKept = 0, midKept = 0;
      for (var ci = 0; ci < keeper.length; ci++) {
        var dd = keeper[ci][0], jd = keeper[ci][1];
        if (dd < 0.06) {                       // 短边：最近 nearK 个
          if (shortKept >= nearK) continue;
          pushEdge(i, jd); shortKept++;
        } else if (dd < 0.12) {                // 中距边：每神经元最多 1 条
          if (midKept >= 1) continue;
          pushEdge(i, jd); midKept++;
        }
        // 更远的本地边直接降采样丢弃，避免长直线铺满画布
      }
      // 跨区稀疏长投射：每 26 颗取一颗，连隔壁区最近的一颗（只扫该区成员）
      if (i % 26 === 0) {
        var nextRegion = (a.region + 1) % REGIONS.length;
        var members = regionMembers[nextRegion];
        var bestId = -1, bestD = 1e9;
        for (var m = 0; m < members.length; m++) {
          var mm = members[m];
          var dd2 = Math.hypot((a.nx - neurons[mm].nx) * 1.35, a.ny - neurons[mm].ny);
          if (dd2 < bestD) { bestD = dd2; bestId = mm; }
        }
        if (bestId >= 0 && bestD < 0.22) pushEdge(i, bestId);
      }
    }
    if (edges.length > synCeiling) edges = edges.slice(0, synCeiling);

    // ---- 5. 联想边：事实文本共享两字词（中文 bigram）；按距离分桶降采样，远端桶最稀 ----
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
    var assocCount = new Array(assocBuckets.length).fill(0);
    function assocBucket(d) {
      for (var bi = 0; bi < assocBuckets.length; bi++) {
        if (d < assocBuckets[bi].max) return bi;
      }
      return assocBuckets.length - 1;
    }
    Object.keys(grams).sort().forEach(function (gram) {
      var ids = grams[gram];
      for (var x = 0; x < ids.length; x++) {
        for (var y = x + 1; y < ids.length; y++) {
          var u = memAnchors[ids[x]], v = memAnchors[ids[y]];
          if (u === undefined || v === undefined || u === v) continue;
          var pk = u < v ? u + ':' + v : v + ':' + u;
          if (seenPair[pk]) continue;
          var d = Math.hypot((neurons[u].nx - neurons[v].nx) * 1.35, neurons[u].ny - neurons[v].ny);
          var bi = assocBucket(d);
          if (assocCount[bi] >= assocBuckets[bi].cap) continue;
          seenPair[pk] = true;
          assocCount[bi]++;
          assocPairs.push([u, v]);
        }
      }
    });

    // ---- 6. 汇总 ----
    var counts = [0, 0, 0, 0, 0, 0, 0, 0];
    neurons.forEach(function (n) { counts[n.region]++; });

    return {
      neurons: neurons,
      edges: edges,
      assocPairs: assocPairs,
      memAnchors: memAnchors,
      lessonAnchors: lessonAnchors,
      counts: counts,
      total: N,
      cap: { fact: null, lesson: null },   // 不设数量上限
      factCap: null,
      lessonCap: null,
      factTotal: factTotal,
      lessonTotal: lessonTotal,
      overCap: { facts: 0, lessons: 0 },
      edgeStats: {
        syn: edges.length,
        assoc: assocPairs.length,
        assocBuckets: assocCount.slice()
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
    version: '1.1.0-unbounded'
  };
});
