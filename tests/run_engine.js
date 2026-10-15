/* M2-6：用 node 跑 `engine.js`（浏览器 IIFE），对黄金样本输出可对账的 JSON。
   用法：node run_engine.js <fixtures.json> <engine.js>
   输出：stdout 上的 JSON 数组（字段与 Python 侧 `StockScore` 的对账子集同名同构）。 */
"use strict";
const fs = require("fs");
const [, , fixturesPath, enginePath] = process.argv;
if (!fixturesPath || !enginePath) {
  console.error("usage: node run_engine.js <fixtures.json> <engine.js>");
  process.exit(2);
}
const fixtures = JSON.parse(fs.readFileSync(fixturesPath, "utf-8"));
global.window = {}; // engine.js 是 IIFE，往 window.PS 上挂接口
eval(fs.readFileSync(enginePath, "utf-8"));
const out = global.window.PS.scoreUniverse(fixtures.stocks);
const picked = out.map((s) => ({
  code: s.code,
  compare_level: s._level,
  compare_group: s._group,
  quality: s.quality,
  growth: s.growth,
  growth_np: s.growth_np,
  growth_ocf: s.growth_ocf,
  score: s.score,
  subs: s.subs,
  radar: s.radar,
}));
process.stdout.write(JSON.stringify(picked, null, 1));
