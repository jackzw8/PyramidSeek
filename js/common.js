/* 金字塔选股 PyramidSeek —— 公共工具（导航 / 搜索 / 收藏 / 格式化 / SVG）
 *
 * 数据不再内嵌：一律经 `PS.api`（api.js）从产物加载（M4-2）。
 */
(function () {
  const PS = (window.PS = window.PS || {});
  const MISSING = (v) => v === null || v === undefined || Number.isNaN(v);

  // ── 格式化（缺失值显示「—」，技术说明书 §7.6；数值来自产物，见契约 §3.5）──
  PS.fmt = {
    pct: (x, d = 2) => (MISSING(x) ? '—' : (x * 100).toFixed(d) + '%'),
    pct1: (x) => PS.fmt.pct(x, 1),
    num: (x, d = 0) =>
      MISSING(x) ? '—' : Number(x).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }),
    score: (x) => (MISSING(x) ? '—' : Number(x).toFixed(1)),
    growth: (x) => (MISSING(x) ? '—' : (x >= 0 ? '+' : '') + Number(x).toFixed(1)),
    int: (x) => (MISSING(x) ? '—' : String(Math.round(x))),
  };
  PS.MISSING = MISSING;

  // ── 收藏（游客标识，localStorage；技术说明书 §7.4）──
  const WL_KEY = 'ps_watchlist';
  const VID_KEY = 'ps_visitorId';
  function uuid() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
  }
  PS.visitorId = () => {
    let v = null;
    try { v = localStorage.getItem(VID_KEY); } catch (e) { /* 隐私模式：忽略 */ }
    if (!v) { v = 'guest-' + uuid(); try { localStorage.setItem(VID_KEY, v); } catch (e) { /* 忽略 */ } }
    return v;
  };
  PS.watchlist = {
    get() { try { return JSON.parse(localStorage.getItem(WL_KEY) || '[]'); } catch (e) { return []; } },
    has(code) { return this.get().indexOf(code) >= 0; },
    add(code) { const a = this.get(); if (a.indexOf(code) < 0) { a.push(code); this._save(a); } },
    remove(code) { this._save(this.get().filter((c) => c !== code)); },
    toggle(code) { this.has(code) ? this.remove(code) : this.add(code); return this.has(code); },
    _save(a) { try { localStorage.setItem(WL_KEY, JSON.stringify(a)); } catch (e) { /* 忽略 */ } },
  };

  // ── 导航 ──
  PS.nav = function (active) {
    const items = [
      ['index.html', '金字塔排行'],
      ['watchlist.html', '观察池'],
      ['index.html#method', '评分说明'],
    ];
    const links = items.map(([h, t]) =>
      `<a href="${h}" class="${active && h.startsWith(active) ? 'on' : ''}">${t}</a>`).join('');
    return `
    <header class="nav">
      <div class="wrap nav-in">
        <a class="brand" href="index.html"><b>金字塔选股</b><span>PyramidSeek</span></a>
        <nav class="links">${links}</nav>
        <div class="searchbox">
          <input id="ps-search" type="search" placeholder="搜索代码 / 名称 / 拼音，如 600900 或 gzmt" autocomplete="off"/>
          <div id="ps-search-res" class="search-res"></div>
        </div>
      </div>
    </header>`;
  };

  // ── 免责声明（家法 R20：页面显著位置；文案与产物 notes 一致）──
  PS.DISCLAIMER = '本项目为学习/研究用途，所有评分与数据不构成任何投资建议。';

  // ── SVG 小工具（图表全部手绘 SVG，零第三方库；技术说明书 §7.5）──
  const SVGNS = 'http://www.w3.org/2000/svg';
  PS.svg = function (tag, attrs, kids) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in (attrs || {})) e.setAttribute(k, attrs[k]);
    (kids || []).forEach((k) => e.appendChild(k));
    return e;
  };
  PS.esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  PS.qs = (k) => new URLSearchParams(location.search).get(k);

  PS.footer = function () {
    const meta = PS.meta;
    const snapshot = meta
      ? `快照 ${meta.snapshotVersion} · 公式 ${meta.formulaVersion} · 财报截止 ${(meta.dataDate || {}).financial || '—'}${(meta.dataDate || {}).price ? ' · 行情截止 ' + meta.dataDate.price : ''}`
      : '快照信息未加载';
    return `<footer class="foot"><div class="wrap">
      <p>${PS.DISCLAIMER}</p>
      <p class="m">PyramidSeek · ${PS.esc(snapshot)}</p>
    </div></footer>`;
  };

  // ── 页面壳（导航 + 页脚 + 搜索；各页在数据就绪后调用）──
  PS.shell = function (active) {
    const nav = document.getElementById('ps-nav');
    if (nav) nav.innerHTML = PS.nav(active);
    const foot = document.getElementById('ps-foot');
    if (foot) foot.innerHTML = PS.footer();
    if (PS.initSearch) PS.initSearch();   // 搜索在 search.js（M4-6）
  };
})();
