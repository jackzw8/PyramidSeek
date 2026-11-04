/* 前端按需取行情（M4-7 / D-56，2026-11-04）
 *
 * 行情只做**展示**：评分、排名、财务表全部来自产物 `stock/{code}.json`，本文件不参与任何计算口径。
 * 数据源：腾讯两个接口，均带 `Access-Control-Allow-Origin: *`，浏览器可直连（Pages 同为 HTTPS）。
 *
 *   K 线  https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=<tx>,day,,,320,qfq
 *   实时  https://qt.gtimg.cn/q=<tx>
 *
 * 四个必须处理的坑（都有单测思路，见 README / 计划 M4-7）：
 *   1. **行序是 [日期, 开, 收, 高, 低, 量]** —— 不是 OHLC，写错会让 K 线实体与影线全反；
 *   2. **港股不支持复权** —— `qfq` 参数对港股无效，返回键是 `day` 而非 `qfqday`，须标「不复权」；
 *   3. **键名动态** —— A 股前复权是 `qfqday`/`qfqmonth`，港股是 `day`/`month`，不能写死；
 *   4. **实时接口是 GBK** —— `res.text()` 按 UTF-8 解会把名称解成乱码，须 `TextDecoder('gbk')`。
 *
 * 失败一律返回 `null`，由调用方静默降级（隐藏图表），**不抛错、不弹窗、不影响分数区**。
 */
(function () {
  const PS = (window.PS = window.PS || {});

  const KLINE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get';
  const RT_URL = 'https://qt.gtimg.cn/q=';
  const TIMEOUT = 8000;
  const BARS = { day: 320, month: 120 };   // 日K ≈1.3 年；月K ≈10 年

  /** `SH600519` / `HK00700` → 腾讯格式 `sh600519` / `hk00700` */
  function toTx(code) {
    return String(code || '').toLowerCase();
  }

  function isHK(code) {
    return /^hk/i.test(String(code || ''));
  }

  /** 带超时的 fetch —— 接口卡住时不让详情页一直转圈 */
  async function fetchWithTimeout(url, asBuffer) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT);
    try {
      const res = await fetch(url, { signal: ctrl.signal, cache: 'no-store' });
      if (!res.ok) return null;
      return asBuffer ? await res.arrayBuffer() : await res.json();
    } catch (e) {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * 日/月 K 线。
   *
   * **复权口径（2026-11-04 实测后定）**：
   * - 日K 取 `qfq`（只取 320 根 ≈1.3 年，复权因子接近 1，实测与真实价一致）；
   * - 月K **取不复权** —— 实测腾讯与东财的「前复权月线」在长跨度上因子失真：
   *   茅台 2016-12 不复权 334.15，两家前复权都返回 **42.57**（因子 7.85），
   *   而真实区间涨幅仅 ≈4.3 倍。若用它会画出「10 年 34 倍」的假趋势。
   *   月K 只做长周期形态观察，用不复权 + 图注更可信（除权日跳空属正常市场现象）。
   *
   * @param {string} code 产物代码（`SH600519` / `HK00700`）
   * @param {'day'|'month'} period
   * @returns {Promise<{bars: Array, period: string, adjusted: boolean, note: string}|null>}
   */
  async function kline(code, period) {
    const p = period === 'month' ? 'month' : 'day';
    const tx = toTx(code);
    const adj = p === 'month' ? '' : 'qfq';           // 月K 不复权（见上）
    const url = `${KLINE_URL}?param=${tx},${p},,,${BARS[p]},${adj}`;
    const json = await fetchWithTimeout(url, false);
    if (!json || json.code !== 0) return null;
    const node = (json.data || {})[tx];
    if (!node) return null;

    // 键名动态：A 股 `qfqday` / `qfqmonth`，港股 `day` / `month`（无视 qfq 参数）
    const key = Object.keys(node).find((k) => new RegExp('^(qfq)?' + p + '$').test(k));
    if (!key) return null;
    const rows = node[key];
    if (!Array.isArray(rows) || !rows.length) return null;

    const bars = [];
    rows.forEach((r) => {
      // [日期, 开, 收, 高, 低, 量] —— 注意第 2 位是收盘
      const o = +r[1], c = +r[2], h = +r[3], l = +r[4];
      if (![o, c, h, l].every(Number.isFinite)) return;
      bars.push({ date: r[0], o, c, h, l, v: +r[5] || 0 });
    });
    if (!bars.length) return null;

    const adjusted = key.indexOf('qfq') === 0;
    return {
      bars,
      period: p,
      adjusted,
      note: adjusted ? '' : '不复权',
    };
  }

  /**
   * 实时报价。
   * @param {string} code
   * @returns {Promise<{name, price, prevClose, open, high, low, volume, change, changePct, time}|null>}
   */
  async function realtime(code) {
    const buf = await fetchWithTimeout(RT_URL + toTx(code), true);
    if (!buf) return null;
    let text;
    try {
      text = new TextDecoder('gbk').decode(buf);       // GBK！用 text() 会把名称解成乱码
    } catch (e) {
      text = new TextDecoder('utf-8').decode(buf);     // 极老浏览器兜底（名称可能乱码，价格仍可读）
    }
    const m = text.match(/="([^"]*)"/);
    if (!m) return null;
    const f = m[1].split('~');
    if (f.length < 35) return null;

    const num = (i) => (f[i] === '' || f[i] === undefined ? null : +f[i]);
    const rt = {
      name: f[1],
      code: f[2],
      price: num(3),
      prevClose: num(4),
      open: num(5),
      volume: num(6),
      time: f[30],
      change: num(31),
      changePct: num(32),
      high: num(33),
      low: num(34),
      market: isHK(code) ? 'HK' : 'A',
    };
    return Number.isFinite(rt.price) ? rt : null;
  }

  PS.quote = { kline, realtime, toTx, isHK, BARS };
})();
