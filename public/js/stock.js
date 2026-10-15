/* 个股详情（M4-4）——数据来自产物 `stock/{code}.json`（《产物契约》§3.4 / §3.5）
 *
 * - 评分构成 / 雷达六维 / 四张财务表 / 同业列表：**全部读产物字段**，前端不算分；
 * - 月K·MACD·年ROE：月K 读产物 `kline`（前复权主序列 + `hfq` 后复权子对象），
 *   MACD(12,26,9) 由月K 收盘价现算，年ROE 阶梯读产物 `years` + `profitability.roe`
 *   （年报自「报告期 + 4 个月」起生效，家法 R9）；
 * - 缺失值（null）显示「—」（技术说明书 §7.6）；无行情数据时月K 卡给出提示。
 */
(function () {
  const PS = window.PS;
  const F = PS.fmt;
  const svg = PS.svg;
  const UP = '#c0392b';
  const DN = '#1e8e5a';
  const GOLD = '#b8860b';

  let self = null;          // 当前个股产物
  let rankInfo = null;      // 市场内主榜排名 { rank, total, top }
  let peerStock = null;     // 当前对比股产物
  const peerCache = new Map();

  async function getPeerStock(code) {
    if (!peerCache.has(code)) peerCache.set(code, await PS.api.stock(code));
    return peerCache.get(code);
  }

  // ── 通用小工具 ──
  function niceTicks(lo, hi, count) {
    const span = (hi - lo) || 1;
    const mag = Math.pow(10, Math.floor(Math.log10(span / count)));
    let best = null;
    for (const k of [1, 2, 2.5, 5, 10]) {                 // 选刻度数最接近 count+1 的步长
      const stp = k * mag;
      const n = Math.floor(hi / stp) - Math.ceil(lo / stp) + 1;
      if (n >= 2 && (!best || Math.abs(n - (count + 1)) < Math.abs(best.n - (count + 1)))) best = { stp, n };
    }
    const step = best ? best.stp : (span / count || 1);
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(2));
    return out;
  }
  function ema(arr, p) {
    const k = 2 / (p + 1), out = []; let prev = arr[0];
    arr.forEach((v, i) => { prev = i === 0 ? v : v * k + prev * (1 - k); out.push(prev); });
    return out;
  }
  function macd(closes) {
    const e12 = ema(closes, 12), e26 = ema(closes, 26);
    const dif = closes.map((_, i) => e12[i] - e26[i]);
    const dea = ema(dif, 9);
    return { dif, dea, hist: dif.map((d, i) => (d - dea[i]) * 2) };
  }

  // ── 雷达对比图（六顶点 + 20~100 刻度环 + 双系列 + 图例；PRD §5.4 / 技术说明书 §7.5）──
  const RAD = [['roe', 'ROE'], ['gross_margin', '毛利率'], ['asset_turnover', '资产周转率'], ['eps', 'EPS'], ['cash_ratio', '现金比率'], ['net_margin', '净利率']];
  const C_SELF = '#1a5fb4';
  const C_PEER = '#c0392b';
  function radar(host, a, b) {
    const W = 440, H = 400, cx = W / 2, cy = 205, R = 115, k = Math.PI / 180;
    const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H });
    const clamp = (v) => Math.max(0, Math.min(100, +v || 0));
    const pt = (i, v) => {
      const ang = (-90 + i * 60) * k, r = (R * clamp(v)) / 100;
      return [cx + r * Math.cos(ang), cy + r * Math.sin(ang)];
    };
    [20, 40, 60, 80, 100].forEach((ring) => {
      const pts = RAD.map((_, i) => pt(i, ring).join(',')).join(' ');
      s.appendChild(svg('polygon', { points: pts, fill: 'none', stroke: '#dfe6ee', 'stroke-width': 1 }));
    });
    [20, 40, 60, 80, 100].forEach((ring) => {
      const p = pt(0, ring);
      const t = svg('text', { x: p[0] - 8, y: p[1] + 3.5, 'text-anchor': 'end', 'font-size': 10.5, fill: '#8a97a3' });
      t.textContent = ring; s.appendChild(t);
    });
    RAD.forEach((_, i) => { const p = pt(i, 100); s.appendChild(svg('line', { x1: cx, y1: cy, x2: p[0], y2: p[1], stroke: '#dfe6ee' })); });
    RAD.forEach(([, label], i) => {
      const p = pt(i, 128);
      const t = svg('text', { x: p[0], y: p[1], 'text-anchor': 'middle', 'dominant-baseline': 'middle', 'font-size': 12.5, 'font-weight': 600, fill: '#3a4a5a' });
      t.textContent = label; s.appendChild(t);
    });
    function series(vals, color, dash) {
      const pts = RAD.map(([key], i) => pt(i, vals[key]).join(',')).join(' ');
      s.appendChild(svg('polygon', {
        points: pts, fill: dash ? 'none' : color, 'fill-opacity': dash ? 0 : 0.22,
        stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round',
        ...(dash ? { 'stroke-dasharray': '6 4' } : {}),
      }));
      RAD.forEach(([key], i) => {
        const p = pt(i, vals[key]);
        s.appendChild(svg('circle', { cx: p[0], cy: p[1], r: 3.4, fill: '#fff', stroke: color, 'stroke-width': 2 }));
      });
    }
    if (b) series(b.radar || {}, C_PEER, true);
    series(a.radar || {}, C_SELF, false);
    const items = (b ? [[C_PEER, b.name], [C_SELF, a.name]] : [[C_SELF, a.name]])
      .map(([c, name]) => ({ c, name, w: 22 + name.length * 12.5 }));
    const gap = 36;
    const total = items.reduce((sum, x) => sum + x.w, 0) + (items.length - 1) * gap;
    let lx = cx - total / 2;
    const ly = H - 16;
    items.forEach(({ c, name, w }) => {
      s.appendChild(svg('circle', { cx: lx + 6, cy: ly - 4, r: 5, fill: c }));
      const t = svg('text', { x: lx + 16, y: ly, 'font-size': 12.5, fill: '#3a4a5a' });
      t.textContent = name; s.appendChild(t);
      lx += w + gap;
    });
    host.innerHTML = ''; host.appendChild(s);
  }

  // ── 月K · MACD · 年ROE（读数来自产物 kline；年ROE 自「报告期 + 4 个月」生效）──
  const MONTHS_RECENT = 96;                       // 「近8年」
  let kRange = '8y';
  let kAdj = 'qfq';

  function roeSteps() {
    const out = [];
    const roes = (self.profitability || {}).roe || [];
    (self.years || []).forEach((year, i) => {
      const roe = roes[i];
      const num = parseInt(year, 10);
      if (PS.MISSING(roe) || !num) return;
      out.push({ year, roe: Number(roe), eff: `${num + 1}-04` });   // 报告期 + 4 个月
    });
    return out;
  }

  function priceChart(host) {
    const kline = self.kline;
    if (!kline || !Array.isArray(kline.months) || !kline.months.length) {
      host.innerHTML = '<div class="empty">该股暂无行情数据（产物未包含 <code>kline</code>）。<br/>待行情采集就绪后重新跑批即可显示月K · MACD · 年ROE。</div>';
      return;
    }
    const months = kline.months;
    const qfq = { o: kline.o, h: kline.h, l: kline.l, c: kline.c };
    const hfq = kline.hfq || qfq;
    const steps = roeSteps();
    const roeAt = (label) => {
      let v = null;
      steps.forEach((st) => { if (st.eff <= label) v = st; });
      return v;
    };

    const W = 900, ML = 8, MR = 46;
    const H1 = 250, H2 = 84, H3 = 64, GAP = 10, HX = 18;
    const H = H1 + H2 + H3 + GAP * 2 + HX;
    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    const s1 = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H });
    const tip = document.createElement('div');
    tip.className = 'tip';
    wrap.appendChild(s1); wrap.appendChild(tip);
    host.innerHTML = ''; host.appendChild(wrap);

    function draw() {
      s1.innerHTML = '';
      const src = kAdj === 'hfq' ? hfq : qfq;
      const indexes = [];
      for (let i = 0; i < months.length; i++) {
        if (!PS.MISSING(src.c[i])) indexes.push(i);           // 缺失月跳过（家法 R11）
      }
      const n8 = Math.min(MONTHS_RECENT, indexes.length);
      const idx = kRange === '8y' ? indexes.slice(-n8) : indexes;
      const n = idx.length;
      const bars = idx.map((i) => ({
        o: src.o[i], h: src.h[i], l: src.l[i], c: src.c[i], month: months[i],
      }));
      const md = macd(bars.map((b) => b.c));
      const step = (W - ML - MR) / n, bw = Math.max(1.6, step - 2);
      const x = (i) => ML + step * (i + 0.5);
      const lo = Math.min(...bars.map((b) => b.l)), hi = Math.max(...bars.map((b) => b.h));
      const py = (v) => 8 + (H1 - 16) * (1 - (v - lo) / ((hi - lo) || 1));

      niceTicks(lo, hi, 4).forEach((tv) => {
        const yy = py(tv);
        s1.appendChild(svg('line', { x1: ML, y1: yy, x2: W - MR, y2: yy, stroke: '#eef2f6', 'stroke-dasharray': '2 3' }));
        const t = svg('text', { x: W - MR + 6, y: yy + 3.5, 'font-size': 10.5, fill: '#8a97a3' });
        t.textContent = tv; s1.appendChild(t);
      });
      bars.forEach((b, i) => {
        const up = b.c >= b.o, col = up ? UP : DN;
        s1.appendChild(svg('line', { x1: x(i), y1: py(b.h), x2: x(i), y2: py(b.l), stroke: col, 'stroke-width': 1 }));
        s1.appendChild(svg('rect', { x: x(i) - bw / 2, y: Math.min(py(b.o), py(b.c)), width: bw, height: Math.max(1, Math.abs(py(b.o) - py(b.c))), fill: col }));
      });

      // MACD 面板
      const y2 = H1 + GAP;
      const mmax = Math.max(...md.hist.map(Math.abs), ...md.dif.map(Math.abs), ...md.dea.map(Math.abs), 0.01);
      const my = (v) => y2 + H2 / 2 - (v / mmax) * (H2 / 2 - 6);
      s1.appendChild(svg('line', { x1: ML, y1: y2 + H2 / 2, x2: W - MR, y2: y2 + H2 / 2, stroke: '#e6ebf0' }));
      md.hist.forEach((h, i) => {
        s1.appendChild(svg('rect', { x: x(i) - bw / 2, y: Math.min(my(0), my(h)), width: bw, height: Math.max(1, Math.abs(my(h) - my(0))), fill: h >= 0 ? UP : DN, opacity: .55 }));
      });
      const pathOf = (arr, col) => svg('path', { d: arr.map((v, i) => (i ? 'L' : 'M') + x(i) + ' ' + my(v)).join(' '), fill: 'none', stroke: col, 'stroke-width': 1.2 });
      s1.appendChild(pathOf(md.dif, '#1a5fb4')); s1.appendChild(pathOf(md.dea, GOLD));
      const t2 = svg('text', { x: ML + 2, y: y2 + 11, 'font-size': 10, fill: '#8a97a3' });
      t2.textContent = 'MACD(12,26,9)'; s1.appendChild(t2);

      // 年ROE 面板（阶梯柱，报告期 + 4 个月生效）
      const y3 = y2 + H2 + GAP;
      const rmax = Math.max(...steps.map((st) => st.roe), 0.01);
      const rh = (v) => (v / rmax) * (H3 - 16);
      const firstShown = bars[0].month;
      let lastYear = null;
      bars.forEach((b, i) => {
        const st = roeAt(b.month);
        if (!st) return;
        s1.appendChild(svg('rect', { x: x(i) - bw / 2, y: y3 + H3 - 8 - rh(st.roe), width: bw, height: Math.max(1, rh(st.roe)), fill: '#2e5496', opacity: .85 }));
        if (st.year !== lastYear) {                       // 每段阶梯起点标年份
          lastYear = st.year;
          const t = svg('text', { x: x(i), y: y3 + H3 + 2, 'text-anchor': 'middle', 'font-size': 9.5, fill: '#8a97a3' });
          t.textContent = st.year; s1.appendChild(t);
        }
      });
      const t3 = svg('text', { x: ML + 2, y: y3 + 11, 'font-size': 10, fill: '#8a97a3' });
      t3.textContent = '年ROE'; s1.appendChild(t3);

      // 月份刻度（每年 1 月标年份，末月标年月）
      bars.forEach((b, i) => {
        const [yy, mm] = b.month.split('-');
        if (mm === '01' || i === n - 1) {
          const t = svg('text', { x: x(i), y: H - 4, 'text-anchor': 'middle', 'font-size': 9.5, fill: '#8a97a3' });
          t.textContent = mm === '01' ? yy : b.month;
          s1.appendChild(t);
        }
      });
      if (steps.length && steps[0].eff > firstShown) {    // 起点早于首个生效年报时，提示生效规则
        const t = svg('text', { x: ML + 2, y: y3 + H3 + 12, 'font-size': 10, fill: '#8a97a3' });
        t.textContent = `年报自「报告期 + 4 个月」起生效（首个生效：${steps[0].eff}）`;
        s1.appendChild(t);
      }

      // 十字线 + 悬浮明细
      const cross = svg('line', { y1: 4, y2: y3 + H3, stroke: '#9fb4c8', 'stroke-dasharray': '4 3', visibility: 'hidden' });
      s1.appendChild(cross);
      s1.onmousemove = (e) => {
        const r = s1.getBoundingClientRect();
        const vx = (e.clientX - r.left) / r.width * W;
        const i = Math.max(0, Math.min(n - 1, Math.floor((vx - ML) / step)));
        cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i));
        cross.setAttribute('visibility', 'visible');
        const b = bars[i], st = roeAt(b.month);
        tip.innerHTML =
          `<div class="t-m">${b.month}${kAdj === 'hfq' ? '（后复权）' : ''}</div>` +
          `<div class="t-row">开 <b>${b.o.toFixed(2)}</b>　高 <b>${b.h.toFixed(2)}</b>　低 <b>${b.l.toFixed(2)}</b>　收 <b>${b.c.toFixed(2)}</b></div>` +
          `<div class="t-macd">MACD　HIST ${md.hist[i] >= 0 ? '+' : ''}${md.hist[i].toFixed(2)}　DIF ${md.dif[i].toFixed(2)}　DEA ${md.dea[i].toFixed(2)}</div>` +
          `<div class="t-roe">ROE ${st ? (st.roe * 100).toFixed(2) + '%（年报 ' + st.year + '-12-31）' : '—（生效前）'}</div>`;
        const px = x(i) / W * r.width;
        tip.style.display = 'block';
        tip.style.left = (px + 210 > r.width ? px - 216 : px + 14) + 'px';
        tip.style.top = '10px';
      };
      s1.onmouseleave = () => { tip.style.display = 'none'; cross.setAttribute('visibility', 'hidden'); };
    }
    draw();
  }

  // ── 明细表（逐年数组，缺失显示「—」）──
  function table(host, years, seriesObj, metas) {
    let h = '<table class="mini"><thead><tr><th>指标</th>' + years.map((y) => `<th>${y}</th>`).join('') + '</tr></thead><tbody>';
    metas.forEach(([label, key, fmt]) => {
      const series = seriesObj ? seriesObj[key] || [] : [];
      h += `<tr><td>${label}</td>` + years.map((_, i) => `<td>${fmt(series[i])}</td>`).join('') + '</tr>';
    });
    host.innerHTML = h + '</tbody></table>';
  }

  // ── 渲染 ──
  function renderHead() {
    const exch = { A: '沪深交易所', HK: '香港交易所' }[self.market] || self.exchange || '';
    const peerList = self.peers || [];
    const rankText = rankInfo
      ? `市场排名 <b>${rankInfo.rank}/${rankInfo.total}</b>${rankInfo.top ? ' <span class="pill">组内前10%</span>' : ''}`
      : '<b>未入主榜</b>';
    document.getElementById('head').innerHTML = `
      <a class="btn" href="index.html">← 返回榜单</a>
      <div class="detail-head" style="margin-top:12px">
        <div class="ttl">
          <h1>${PS.esc(self.name)} <span class="code">${self.code}</span></h1>
          <div class="kv">
            <span>市场 <b>${self.market}</b></span><span>交易所 <b>${exch}</b></span>
            <span>板块 <b>${PS.esc(self.l1)}</b></span><span>行业 <b>${PS.esc(self.l2)}</b></span>
            <span>对比池 <b>${PS.esc(self.group || '未分类')}${self.level ? ' · ' + self.level : ''}</b>（${peerList.length} 只）</span>
            <span>上市 <b>${PS.esc(self.listedAt || '—')}</b></span>
            <span>${rankText}</span>
          </div>
        </div>
        <div class="score-badge"><div class="v">${F.score(self.score)}</div><div class="l">金字塔分</div>
          <div class="l">质量 ${F.score(self.quality)} ｜ 成长 ${F.growth(self.growth)}</div></div>
        <button class="btn star${PS.watchlist.has(self.code) ? ' on' : ''}" id="wl">${PS.watchlist.has(self.code) ? '★ 已收藏' : '☆ 加入观察池'}</button>
      </div>`;
    document.getElementById('wl').onclick = (e) => {
      const on = PS.watchlist.toggle(self.code);
      e.target.classList.toggle('on', on);
      e.target.textContent = on ? '★ 已收藏' : '☆ 加入观察池';
    };
  }

  function renderRadarAndPanel() {
    const peerList = self.peers || [];
    const others = peerList.filter((p) => p.code !== self.code);
    const sel = document.getElementById('peer');
    sel.innerHTML = '<option value="">不对比</option>' + others
      .map((p) => `<option value="${p.code}">${PS.esc(p.name)} (${p.code})</option>`).join('');

    const drawRadar = () => radar(document.getElementById('radar'), self, peerStock);

    async function pickPeer(code) {
      peerStock = code ? await getPeerStock(code) : null;
      sel.value = code || '';
      drawRadar();
    }
    // 默认对比：近五年 ROE 第一名（peers 已按 roe5y 降序）
    const first = others[0];
    sel.value = first ? first.code : '';
    pickPeer(first ? first.code : '');
    sel.onchange = () => pickPeer(sel.value);

    // 同行业对比面板（悬停预览 / 点击跳转）
    document.getElementById('pp-sub').textContent = `${self.group || '全市场'} · 近五年ROE对比`;
    const ppList = document.getElementById('pp-list');
    ppList.innerHTML = peerList.map((p, i) => `
      <div class="pp-row ${p.code === self.code ? 'self' : ''}" data-code="${p.code}" title="点击进入 ${PS.esc(p.name)} 详情">
        <span class="pp-rank">${i + 1}</span>
        <span class="pp-name">${PS.esc(p.name)}${p.code === self.code ? ' ←' : ''}<span class="pp-code">${p.code}</span></span>
        <span class="pp-roe">${F.pct1(p.roe5y)}</span>
      </div>`).join('');
    ppList.querySelectorAll('.pp-row').forEach((row) => {
      const code = row.dataset.code;
      row.onmouseenter = () => { if (code !== self.code) pickPeer(code); };
      row.onclick = () => { location.href = 'stock.html?code=' + code; };
    });
    const pp = document.getElementById('peer-panel');
    document.getElementById('pp-toggle').onclick = () => pp.classList.toggle('collapsed');
    if (window.innerWidth <= 900) pp.classList.add('collapsed');
  }

  function renderTables() {
    const years = self.years || [];
    table(document.getElementById('t-profit'), years, self.profitability, [
      ['净资产收益率 ROE', 'roe', (x) => F.pct(x, 1)],
      ['总资产收益率 ROA', 'roa', (x) => F.pct(x, 1)],
      ['毛利率', 'gross_margin', (x) => F.pct(x, 1)],
      ['净利率', 'net_margin', (x) => F.pct(x, 1)],
      ['每股收益 EPS(元)', 'eps', (x) => F.num(x, 2)],
    ]);
    table(document.getElementById('t-struct'), years, self.structure, [
      ['现金占总资产比率', 'cash_ratio', (x) => F.pct(x, 1)],
      ['负债占总资产比率', 'debt_ratio', (x) => F.pct(x, 1)],
    ]);
    table(document.getElementById('t-oper'), years, self.operation, [
      ['应收账款周转率(次)', 'ar_turnover', (x) => F.num(x, 1)],
      ['平均收现日数(天)', 'ar_days', (x) => F.num(x, 0)],
      ['固定资产周转率(次)', 'fa_turnover', (x) => F.num(x, 1)],
      ['总资产周转率', 'asset_turnover', (x) => F.num(x, 2)],
    ]);
    table(document.getElementById('t-cash'), years, self.cashflow, [
      ['现金及现金等价物', 'cash', (x) => F.num(x, 0)],
      ['净利润', 'net_profit', (x) => F.num(x, 0)],
      ['经营活动现金流', 'ocf', (x) => F.num(x, 0)],
      ['投资活动现金流', 'icf', (x) => F.num(x, 0)],
      ['筹资活动现金流', 'fcf', (x) => F.num(x, 0)],
      ['净现金流量', 'ncf', (x) => F.num(x, 0)],
    ]);
  }

  function renderSubs() {
    const labels = {
      roe: 'ROE', net_profit_scale: '净利润规模', roa: 'ROA',
      gross_margin: '毛利率', cash_ratio: '现金占比', asset_turnover: '周转率',
    };
    const weights = (PS.meta && PS.meta.weights) || {};      // 权重读产物（家法 R12）
    const subs = self.qualitySubs || {};
    const keys = Object.keys(labels).filter((k) => k in subs);
    const gd = self.growthDetail || {};
    document.getElementById('t-sub').innerHTML =
      '<table class="mini"><thead><tr><th>质量分构成</th><th>子分</th><th>权重</th><th>加权分</th></tr></thead><tbody>'
      + keys.map((k) => `<tr><td>${labels[k]}</td><td>${F.score(subs[k])}</td><td>${PS.MISSING(weights[k]) ? '—' : weights[k]}</td><td>${PS.MISSING(weights[k]) ? '—' : (subs[k] * weights[k] / 100).toFixed(1)}</td></tr>`).join('')
      + `<tr><td><b>质量分 Q</b></td><td></td><td></td><td><b>${F.score(self.quality)}</b></td></tr>`
      + `<tr><td>成长分·净利润</td><td colspan="2">CAGR × 稳定性</td><td>${F.growth(gd.netProfit)}</td></tr>`
      + `<tr><td>成长分·经营现金流</td><td colspan="2">CAGR × 稳定性</td><td>${F.growth(gd.opCashflow)}</td></tr>`
      + `<tr><td><b>金字塔分</b></td><td colspan="2">clamp(Q+G, 0, 1000)</td><td><b>${F.score(self.score)}</b></td></tr></tbody></table>`;
  }

  function bindKlineControls() {
    const syncTg = (id, btn) => document.querySelectorAll('#' + id + ' button').forEach((x) => x.classList.toggle('on', x === btn));
    document.querySelectorAll('#tg-range button').forEach((b) => {
      b.onclick = () => { kRange = b.dataset.v; syncTg('tg-range', b); priceChart(document.getElementById('kline')); };
    });
    document.querySelectorAll('#tg-adj button').forEach((b) => {
      b.onclick = () => { kAdj = b.dataset.v; syncTg('tg-adj', b); priceChart(document.getElementById('kline')); };
    });
  }

  function render() {
    renderHead();
    renderRadarAndPanel();
    renderTables();
    renderSubs();
    priceChart(document.getElementById('kline'));
    bindKlineControls();
  }

  async function init() {
    const code = PS.qs('code');
    try {
      await PS.api.boot();
      if (!code) throw new Error('缺少 ?code= 参数');
      self = await PS.api.stock(code);
      try {   // 市场内排名（来自榜单产物；不入主榜则无）
        const shard = await PS.api.ranking(self.market);
        const rows = shard.rows || [];
        const idx = rows.findIndex((row) => row.code === code);
        rankInfo = idx >= 0 ? { rank: rows[idx].rank, total: rows.length, top: rows[idx].isTop10pct === true } : null;
      } catch (e) { rankInfo = null; }
    } catch (err) {
      PS.shell('stock.html');
      PS.api.banner('error', `数据加载失败：${err.message}`);
      document.getElementById('head').innerHTML =
        '<div class="empty">个股产物不可用 —— 请先运行 <code>python -m src.batch --offline</code> 生成 <code>public/data/</code>，再带 ?code= 参数访问。</div>';
      return;
    }
    PS.shell('stock.html');
    render();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
