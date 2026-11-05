/* 金字塔排行（M4-3）——数据来自产物 `ranking/{market}.json`（《产物契约》§3.3）
 *
 * - `rows` = **主榜**：按金字塔分降序；高亮用产物 `isTop10pct`（**组内**前 10%，06 §7）；
 * - `excluded` = **单列**：ST / 停牌 / 退市 / 次新 / 行业未分类 / 关键指标缺失，单独一块展示；
 * - 排名列 = 当前筛选结果的全局序号（跨页连续，PRD §5.2），不直接用产物 `rank`。
 */
(function () {
  const PS = window.PS;
  const f = { market: 'ALL', l1: 'ALL', l2: 'ALL' };
  let page = 1;
  let pageSize = 10;
  let MAIN = [];
  let EXCLUDED = [];

  function uniq(arr) { return [...new Set(arr)]; }
  const industryOf = (row) => [row.l1, row.l2].filter(Boolean).join(' · ');

  function renderHead() {
    PS.shell('index.html');
  }

  function buildFilters() {
    const markets = [['ALL', '全部'], ['A', 'A股'], ['HK', '港股']];
    const mstocks = f.market === 'ALL' ? MAIN : MAIN.filter((s) => s.market === f.market);
    const l1s = ['ALL', ...uniq(mstocks.map((s) => s.l1).filter(Boolean))];
    const l2s = uniq(
      (f.l1 === 'ALL' ? mstocks : mstocks.filter((s) => s.l1 === f.l1)).map((s) => s.l2).filter(Boolean)
    );
    const el = document.getElementById('filters');
    el.innerHTML = `
      <label>市场</label>
      <select id="sel0">${markets.map(([v, t]) => `<option value="${v}" ${f.market === v ? 'selected' : ''}>${t}</option>`).join('')}</select>
      <label>板块</label>
      <select id="sel1">${l1s.map((v) => `<option value="${v}" ${f.l1 === v ? 'selected' : ''}>${v === 'ALL' ? '全部' : PS.esc(v)}</option>`).join('')}</select>
      <label>行业</label>
      <select id="sel2">${['ALL', ...l2s].map((v) => `<option value="${v}" ${f.l2 === v ? 'selected' : ''}>${v === 'ALL' ? '全部' : PS.esc(v)}</option>`).join('')}</select>
      <span class="cnt">共 ${mstocks.length} 只（主榜）</span>`;
    document.getElementById('sel0').onchange = (e) => { f.market = e.target.value; f.l1 = 'ALL'; f.l2 = 'ALL'; page = 1; draw(); };
    document.getElementById('sel1').onchange = (e) => { f.l1 = e.target.value; f.l2 = 'ALL'; page = 1; draw(); };
    document.getElementById('sel2').onchange = (e) => { f.l2 = e.target.value; page = 1; draw(); };
  }

  function filterByFacets(rows) {
    return rows.filter(
      (s) =>
        (f.market === 'ALL' || s.market === f.market) &&
        (f.l1 === 'ALL' || s.l1 === f.l1) &&
        (f.l2 === 'ALL' || s.l2 === f.l2)
    );
  }

  /** 主榜：金字塔分降序（同分按代码升序，与产物同口径）。 */
  function filtered() {
    return filterByFacets(MAIN).sort(
      (a, b) => b.score - a.score || String(a.code).localeCompare(String(b.code))
    );
  }

  // 页码序列（当前页前后各 2 页，首末页常驻，其余以 … 省略）
  function pageList(n) {
    const out = [];
    for (let i = 1; i <= n; i++) {
      if (i === 1 || i === n || Math.abs(i - page) <= 2) out.push(i);
      else if (out[out.length - 1] !== '…') out.push('…');
    }
    return out;
  }

  function drawPager(total) {
    const el = document.getElementById('pager');
    const n = Math.max(1, Math.ceil(total / pageSize));
    if (page > n) page = n;
    if (!total) { el.innerHTML = ''; return; }
    const from = (page - 1) * pageSize + 1, to = Math.min(total, page * pageSize);
    el.innerHTML = `
      <span class="pg-info">共 <b>${total}</b> 只 ｜ 显示第 ${from}~${to} 只</span>
      <label>每页</label>
      <select id="selpg">${[10, 20, 50].map((v) => `<option value="${v}" ${pageSize === v ? 'selected' : ''}>${v} 条</option>`).join('')}</select>
      <button class="pg" data-p="${page - 1}" ${page <= 1 ? 'disabled' : ''} title="上一页">‹</button>
      ${pageList(n).map((p) => (p === '…' ? '<span class="pg-dot">…</span>'
        : `<button class="pg ${p === page ? 'on' : ''}" data-p="${p}">${p}</button>`)).join('')}
      <button class="pg" data-p="${page + 1}" ${page >= n ? 'disabled' : ''} title="下一页">›</button>`;
    document.getElementById('selpg').onchange = (e) => { pageSize = +e.target.value; page = 1; draw(); };
    el.querySelectorAll('[data-p]').forEach((b) => { b.onclick = () => { page = +b.dataset.p; draw(); }; });
  }

  function drawExcluded() {
    const rows = filterByFacets(EXCLUDED).sort(
      (a, b) => b.score - a.score || String(a.code).localeCompare(String(b.code))
    );
    const card = document.getElementById('excluded-card');
    const sub = document.getElementById('ex-sub');
    const body = document.getElementById('ex-tbody');
    if (!rows.length) {
      card.style.display = 'none';
      return;
    }
    card.style.display = '';
    sub.textContent = `${rows.length} 只（ST / 停牌 / 退市 / 次新 / 行业未分类 / 关键指标缺失，不参与主榜）`;
    body.innerHTML = rows.map((row) => `<tr>
        <td class="name-cell"><a href="stock.html?code=${row.code}"><b>${PS.esc(row.name)}</b></a>
          <span class="code">${row.code}</span></td>
        <td>${row.market}</td>
        <td>${PS.esc(industryOf(row) || '—')}</td>
        <td><span class="pill warn">${PS.esc(row.status || '—')}</span></td>
        <td>${PS.esc(row.reason || '—')}</td>
        <td class="score">${PS.fmt.score(row.score)}</td>
        <td>${PS.fmt.score(row.quality)}</td>
        <td class="${PS.MISSING(row.growth) ? '' : row.growth >= 0 ? 'pos' : 'neg'}">${PS.fmt.growth(row.growth)}</td>
      </tr>`).join('');
  }

  function draw() {
    buildFilters();
    const rows = filtered();
    const total = rows.length;
    const tb = document.getElementById('tbody');
    if (!total) {
      tb.innerHTML = '<tr><td colspan="10" class="empty">该筛选下暂无股票</td></tr>';
      drawPager(0);
      drawExcluded();
      return;
    }
    const n = Math.max(1, Math.ceil(total / pageSize));
    if (page > n) page = n;
    const start = (page - 1) * pageSize;
    tb.innerHTML = rows.slice(start, start + pageSize).map((row, i) => {
      const seq = start + i;                     // 全局序号（0 起，跨页连续）
      const top = row.isTop10pct === true;       // **组内**前 10%（产物字段）
      const lv = row.level === 'L2' ? 'l2' : 'l1';
      return `<tr class="${top ? 'row-top' : ''}">
        <td><span class="ranknum">${seq + 1}</span></td>
        <td class="name-cell"><a href="stock.html?code=${row.code}"><b>${PS.esc(row.name)}</b></a>
          <span class="code">${row.code}</span>${top ? '<span class="pill">组内前10%</span>' : ''}</td>
        <td>${row.market}</td>
        <td>${PS.esc(industryOf(row) || '—')}</td>
        <td><span class="pill ${lv}">${PS.esc(row.group || '—')}</span> <span class="m">${row.level || ''}</span></td>
        <td class="score">${PS.fmt.score(row.score)}</td>
        <td>${PS.fmt.pct1(row.roe)}</td>
        <td>${PS.fmt.pct1(row.gross_margin)}</td>
        <td>${PS.fmt.pct1(row.cash_ratio)}</td>
        <td>${PS.fmt.pct1(row.debt_ratio)}</td>
      </tr>`;
    }).join('');
    drawPager(total);
    drawExcluded();
  }

  async function init() {
    renderHead();
    try {
      await PS.api.boot();
      const shards = await PS.api.rankings();
      MAIN = shards.flatMap((s) => s.rows || []);
      EXCLUDED = shards.flatMap((s) => s.excluded || []);
    } catch (err) {
      PS.api.banner('error', `数据加载失败：${err.message}`);
      document.getElementById('tbody').innerHTML =
        '<tr><td colspan="10" class="empty">产物不可用 —— 请先运行 <code>python -m src.batch --offline</code> 生成 <code>public/data/</code></td></tr>';
      document.getElementById('filters').innerHTML = '';
      return;
    }
    draw();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
