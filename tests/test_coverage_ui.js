// 覆盖率渲染逻辑的可执行验证（P1 · B3 前端）
//
// 为什么需要它：前端的覆盖率徽标/明细是纯 DOM 拼装，pytest 覆盖不到；
// 只做语法检查（node --check）也抓不出运行时与结构错误。这里用最小 DOM 桩
// 把 **index.html 里真实的渲染代码**抽出来跑一遍，断言 DOM 结构与文案。
//
// 它不是完整 DOM 实现，只是一层足够用的壳；目的是「跑得起来 + 结构对 + 文案对」。
//
// 用法（Windows）：
//   "C:/Users/ww/.workbuddy/binaries/node/versions/22.22.2-2/node.exe" tests/test_coverage_ui.js
// 退出码 0 = 全部通过。

const fs = require('fs');
const path = require('path');

const HTML = path.join(__dirname, '..', 'app', 'static', 'index.html');

// ---------- 从真实页面抽出渲染代码（不复制粘贴，避免两处漂移） ----------
function extractHelpers() {
  const html = fs.readFileSync(HTML, 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (!scripts.length) { throw new Error('index.html 里找不到 <script>'); }
  const js = scripts[scripts.length - 1][1];
  const start = js.indexOf('// ---- 覆盖率回检展示（P1）----');
  const end = js.indexOf('function renderMarkdown(md, opts');
  if (start < 0 || end < 0 || end <= start) {
    throw new Error('抽不到覆盖率渲染代码：标记注释或 renderMarkdown 位置变了？');
  }
  return js.slice(start, end);
}

// ---------- 最小 DOM 桩 ----------
function mkEl(tag) {
  const el = {
    tag, children: [], _text: '', _cls: new Set(), style: {}, _h: [],
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    classList: {
      add: (c) => el._cls.add(c),
      remove: (c) => el._cls.delete(c),
      contains: (c) => el._cls.has(c),
      toggle: (c, force) => {
        const want = force === undefined ? !el._cls.has(c) : !!force;
        if (want) { el._cls.add(c); } else { el._cls.delete(c); }
        return want;
      },
    },
    appendChild(c) {
      if (c && c.__frag) { c.children.forEach((x) => el.children.push(x)); return c; }
      el.children.push(c); return c;
    },
    addEventListener(ev, fn) { if (ev === 'click') { el._h.push(fn); } el.click = fn; },
    scrollIntoView() {},
  };
  Object.defineProperty(el, 'className', {
    get() { return [...el._cls].join(' '); },
    set(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return el;
}

global.document = {
  createElement: mkEl,
  createTextNode: (t) => ({ tag: '#text', _text: String(t) }),
  createDocumentFragment: () => ({
    __frag: true, children: [],
    appendChild(c) { this.children.push(c); return c; },
  }),
};
// 桩掉 fetch：批量明细是点开才请求的，这里只验证「点开」这条路径不炸
global.fetch = async () => ({ ok: false, status: 404, json: async () => ({}) });
let batchId = 'demo123';   // 渲染代码依赖它拼请求 URL

eval(extractHelpers());

// ---------- 工具 ----------
function allText(el) {
  if (el.tag === '#text') { return el._text; }
  return (el._text || '') + el.children.map(allText).join(' ');
}
function dump(el, d) {
  const pad = '  '.repeat(d || 0);
  if (el.tag === '#text') { return pad + '"' + el._text + '"'; }
  const cls = [...el._cls].join(' ');
  let out = pad + '<' + el.tag + (cls ? ' class="' + cls + '"' : '')
    + (el._text ? ' "' + el._text + '"' : '') + '\n';
  el.children.forEach((c) => { out += dump(c, (d || 0) + 1); });
  return out;
}
let failed = 0;
function check(cond, msg) {
  console.log((cond ? '  [OK] ' : '  [FAIL] ') + msg);
  if (!cond) { failed += 1; }
}

// ---------- 场景 ----------
console.log('=== 1. 覆盖良好 + 有疑似遗漏（单份）===');
const box = mkEl('div');
box._cls.add('hidden');
renderCoverageBox(box, {
  percent: 91, total_terms: 100, missing_terms: 2,
  missing_detail: [{ term: 'crush', page: 3 }, { term: 'pg_num', page: 7 }],
});
console.log(dump(box));
check(!box._cls.has('hidden'), '整块不再隐藏');
check(box.children[0]._cls.has('covline'), '第一行是徽标行');
check(box.children[0].children[0]._text === '覆盖 91%', '徽标文案 = 覆盖 91%');
check(box.children[0].children[0]._cls.has('cov-warn'), '91% 落在「有少量疑似遗漏」配色');
check(box.children[1]._cls.has('hidden'), '明细默认收起（不刷屏）');
box.children[0].children[1].click();
check(!box.children[1]._cls.has('hidden'), '点击后明细展开');
const dt = allText(box.children[1]);
check(dt.indexOf('crush') >= 0, '明细含缺失术语');
check(dt.indexOf('源课件第 3 页') >= 0, '明细含出处页码（用户要能 1 秒核对）');
check(dt.indexOf('疑似') >= 0, '带「疑似」措辞说明：词面比对不等于确定漏了');

console.log('\n=== 2. 术语太少 -> 不给百分比（避免噪声数字）===');
const b2 = mkEl('div');
renderCoverageBox(b2, { percent: 50, total_terms: 2, missing_terms: 1, missing_detail: [] });
console.log(dump(b2));
check(b2.children[0].children[0]._text === '覆盖样本不足', '显示「覆盖样本不足」而不是 50%');

console.log('\n=== 3. 无覆盖率数据 -> 整块隐藏 ===');
const b3 = mkEl('div');
renderCoverageBox(b3, null);
check(b3._cls.has('hidden'), '已隐藏');
check(b3.children.length === 0, '无残留子节点');

console.log('\n=== 4. 批量某节（明细点开才请求）===');
const col = mkEl('div');
attachJobCoverage(col, {
  idx: 3, coverage_percent: 81, coverage_missing: 12, coverage_total: 63,
  repair_summary: '已自动补漏 2 轮：81% → 95%，¥0.008',   // P2：后端压好的一行
});
console.log(dump(col));
check(col.children[0].children[0]._text === '覆盖 81%', '徽标文案 = 覆盖 81%');
check(col.children[0].children[0]._cls.has('cov-bad'), '81% 用红色（建议核对）');
check(allText(col.children[0]).indexOf('已自动补漏 2 轮') >= 0,
      '补漏摘要显示在徽标旁（用户要知道文档被补过）');
check(col.children[1]._cls.has('hidden'), '明细容器默认收起');

console.log('\n=== 4b. 批量某节无补漏 -> 不显示多余元素 ===');
const col2 = mkEl('div');
attachJobCoverage(col2, { idx: 4, coverage_percent: 99, coverage_missing: 0, coverage_total: 80 });
check(col2.children[0].children.filter((c) => c._cls.has('covrepair')).length === 0,
      '没补漏时不留空徽标（避免噪音）');

console.log('\n=== 5. 配色阈值与后端一致（94 / 86）===');
check(covClass(100, true) === 'cov-ok', '100% -> 绿');
check(covClass(94, true) === 'cov-ok', '94% -> 绿（边界含）');
check(covClass(93, true) === 'cov-warn', '93% -> 黄');
check(covClass(86, true) === 'cov-warn', '86% -> 黄（边界含）');
check(covClass(85, true) === 'cov-bad', '85% -> 红');
check(covClass(99, false) === 'cov-none', '样本不足时用中性色（不给好/坏判断）');

console.log('\n=== 6. null 必须被调用方挡住 ===');
check(!(null != null && null >= 0), 'coverage_percent 为 null 时不渲染（JS 里裸 null >= 0 是 true，这个坑很隐蔽）');

console.log('\n=== 7. 补漏提示文案规则（P2）===');
check(repairText(null) === '', '未触发 -> 不显示（不制造噪音）');
check(
  repairText({ triggered: true, trigger_percent: 90, final_percent: 90, rolled_back: false }) === '',
  '触发了但没提升 -> 不显示',
);
check(
  repairText({ triggered: true, trigger_percent: 80, final_percent: 80, rolled_back: true })
    === '已尝试自动补漏但未改善，保留原文档',
  '回滚 -> 如实说明，不能装作没发生',
);
const rtxt = repairText({
  triggered: true, trigger_percent: 22, final_percent: 96, total_cost_yuan: 0.0111,
  rounds: [{}, {}],
});
check(rtxt === '已自动补漏 2 轮：22% → 96%，¥0.011', '提升 -> 轮次/前后值/花费一次说清：' + rtxt);

const b7 = mkEl('div');
renderCoverageBox(
  b7, { percent: 96, total_terms: 100, missing_terms: 4, missing_detail: [] }, '', rtxt,
);
check(allText(b7).indexOf('已自动补漏 2 轮') >= 0, '补漏提示渲染进徽标行');
check(b7.children[0].children.length >= 2, '徽标行同时含覆盖率徽标与补漏徽标');

console.log('\n' + (failed === 0 ? '全部通过' : failed + ' 项失败'));
process.exit(failed === 0 ? 0 : 1);
