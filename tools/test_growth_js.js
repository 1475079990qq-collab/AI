// 脑图增量生长的验收：新事实插到列表最前面时，老节点不许挪位，新节点前后两次算出来的坐标必须一致。
//   跑法： node tools/test_growth_js.js
const path = require('path');
const BG = require(path.join(__dirname, '..', 'web', 'brain-growth.js'));

let pass = 0;
let fail = 0;
function ok(cond, label, detail) {
  if (cond) { pass++; console.log('PASS  ' + label); }
  else { fail++; console.log('FAIL  ' + label + '  ' + (detail || '')); }
}

const facts = [
  { id: 'f1', text: '我叫阿伟，在成都做运维' },
  { id: 'f2', text: '显卡驱动装不上，显存不够' },
  { id: 'f3', text: '不吃香菜，点了会犯恶心' },
  { id: 'f4', text: '最近在折腾本地大模型' }
];
const lessons = [{ id: 'l1', note: '答得太空，要给具体参数' }];

const s1 = BG.build(facts, lessons, {});
const before = {};
s1.neurons.forEach(n => { if (n.fid) before[String(n.region) + ':' + n.fid] = [n.nx, n.ny]; });

const newFact = { id: 'f0', text: '今天给一台老机器清了灰' };
const grown = BG.addFact(s1, newFact);
ok(grown && typeof grown.index === 'number', '增量长出一颗新节点', JSON.stringify(grown));
ok(s1.factTotal === facts.length + 1, '事实计数 +1', String(s1.factTotal));

const pGrown = s1.neurons[grown.index];
ok(BG.insideBrain(pGrown.nx, pGrown.ny), '新节点落在脑形内', `${pGrown.nx},${pGrown.ny}`);

// 接口按 created_at DESC 返回，新事实会排在最前面 —— 模拟整图重建
const s2 = BG.build([newFact].concat(facts), lessons, {});
const pRebuild = s2.neurons.find(n => n.fid === 'f0');
ok(pRebuild && Math.abs(pRebuild.nx - pGrown.nx) < 1e-9 &&
   Math.abs(pRebuild.ny - pGrown.ny) < 1e-9,
   '增量坐标与重建坐标一致', JSON.stringify([pGrown.nx, pGrown.ny, pRebuild && pRebuild.nx, pRebuild && pRebuild.ny]));

let moved = 0;
s2.neurons.forEach(n => {
  if (!n.fid || n.fid === 'f0') return;
  const key = String(n.region) + ':' + n.fid;
  const old = before[key];
  if (!old) return;
  if (Math.abs(old[0] - n.nx) > 1e-9 || Math.abs(old[1] - n.ny) > 1e-9) moved++;
});
ok(moved === 0, '插新事实后老节点一个都没挪', 'moved=' + moved);

// 同样的 id 再长一次 = 幂等，不长第二颗
const again = BG.addFact(s1, newFact);
ok(again === null && s1.neurons.filter(n => n.fid === 'f0').length === 1, '同一条事实不会长两颗点');

const lgrown = BG.addLesson(s1, { id: 'l2', note: '别用客服腔' });
ok(lgrown && s1.neurons[lgrown.index].region === 7, '新教训长在沉淀区');
ok(s1.counts[7] === s2.counts[7] + 1, '沉淀区计数 +1', `${s1.counts[7]} vs ${s2.counts[7]}`);

const dupEdges = new Set();
let edgeDup = 0;
s1.edges.forEach(e => {
  const k = e[0] + ':' + e[1];
  if (dupEdges.has(k)) edgeDup++;
  dupEdges.add(k);
});
ok(edgeDup === 0, '增量后没有重复连线', 'dup=' + edgeDup);

/* ---------- 真实网络：neurons / synapses → 画面 ---------- */
const base = BG.build([], []);
const graph = {
  nodes: [
    { id: 'n_fact_1', kind: 'fact', ref_id: 'f1', importance: 0.9, activation: 0.4 },
    { id: 'n_fact_2', kind: 'fact', ref_id: 'f2', importance: 0.5, activation: 0 },
    { id: 'n_lesson_1', kind: 'lesson', ref_id: 'l1', importance: 0.6, activation: 0 },
    { id: 'n_concept_1', kind: 'concept', ref_id: null, label: '显卡', importance: 0.43 }
  ],
  edges: [
    { src: 'n_fact_1', dst: 'n_concept_1', relation: 'about', weight: 0.55 },
    { src: 'n_fact_2', dst: 'n_concept_1', relation: 'about', weight: 0.55 },
    { src: 'n_fact_1', dst: 'n_fact_2', relation: 'semantic', weight: 0.45 },
    { src: 'n_lesson_1', dst: 'n_concept_1', relation: 'lesson', weight: 0.5 },
    { src: 'n_fact_1', dst: 'n_missing', relation: 'about', weight: 0.55 }
  ]
};
const rg = BG.mergeGraph(base, graph, {});
ok(rg && rg.real === true, '真实图模式标记');
ok(rg.nodes === undefined && rg.neurons.length === base.neurons.length + 4,
   '4 个真实节点全部落到画面上', 'total=' + (rg && rg.neurons.length));
const concept = rg.neurons.find(n => n.kind === 'concept');
ok(concept && concept.region === 3, '概念节点落在联想区');
ok(rg.memAnchors['f1'] !== undefined && rg.memAnchors['f2'] !== undefined, '事实锚点可定位');
ok(rg.edges.length === 4, '断头边被丢掉，其余 4 条保留', 'edges=' + rg.edges.length);
ok(rg.edgeStats.byRelation.about === 2 && rg.edgeStats.byRelation.semantic === 1 &&
   rg.edgeStats.byRelation.lesson === 1, '关系类型统计正确', JSON.stringify(rg.edgeStats));
ok(rg.edgeMeta && rg.edgeMeta[0].w > 0, '边携带权重（粗细按它画）', JSON.stringify(rg.edgeMeta[0]));
const baseCounts = base.counts.slice();
ok(rg.counts[3] === baseCounts[3] + 1 && rg.counts[2] === baseCounts[2] + 2 &&
   rg.counts[7] === baseCounts[7] + 1, '分区计数在基数之上按真实节点增加',
   JSON.stringify([baseCounts, rg.counts]));

// 同一份图再算一次：坐标必须完全相同（刷新页面不许洗牌）
const rg2 = BG.mergeGraph(base, graph, {});
const movedReal = rg2.neurons.filter((n, i) => {
  const a = rg.neurons[i];
  return Math.abs(a.nx - n.nx) > 1e-9 || Math.abs(a.ny - n.ny) > 1e-9;
}).length;
ok(movedReal === 0, '真实图重复构建坐标不变', 'moved=' + movedReal);

// 新增一条事实后重拉图：老节点不许挪
const graph2 = {
  nodes: graph.nodes.concat([{ id: 'n_fact_3', kind: 'fact', ref_id: 'f3', importance: 0.8 }]),
  edges: graph.edges.concat([{ src: 'n_fact_3', dst: 'n_concept_1', relation: 'about', weight: 0.55 }])
};
const rg3 = BG.mergeGraph(base, graph2, {});
const movedAfter = rg.neurons.filter((n, i) => {
  const b = rg3.neurons[i];
  return !b || Math.abs(n.nx - b.nx) > 1e-9 || Math.abs(n.ny - b.ny) > 1e-9;
}).length;
ok(movedAfter === 0, '新记忆落库重拉图后老节点没挪位', 'moved=' + movedAfter);

console.log(`\nPASS=${pass}  FAIL=${fail}`);
process.exitCode = fail === 0 ? 0 : 1;
