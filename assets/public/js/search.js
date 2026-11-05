/* 搜索（M4-6）——索引来自产物 `search-index.json`（《产物契约》§3.6）
 *
 * 匹配规则：代码（含纯数字部分）/ 名称 / **拼音首字母**（如 `gzmt` → 贵州茅台）。
 * 索引缺失时搜索静默不可用（页面其余功能不受影响）。
 */
(function () {
  const PS = (window.PS = window.PS || {});

  PS.initSearch = async function () {
    const inp = document.getElementById('ps-search');
    const box = document.getElementById('ps-search-res');
    if (!inp || !box) return;

    let entries = [];
    try {
      entries = (await PS.api.searchIndex()).entries || [];
    } catch (err) {
      entries = [];
    }

    inp.addEventListener('input', () => {
      const q = inp.value.trim().toLowerCase();
      if (!q) { box.style.display = 'none'; return; }
      const digits = q.replace(/[^0-9]/g, '');
      const hits = entries.filter((entry) => {
        const code = String(entry.code || '').toLowerCase();
        const name = String(entry.name || '').toLowerCase();
        const py = String(entry.py || '').toLowerCase();
        return (
          code.includes(q) ||
          name.includes(q) ||
          (py && py.includes(q)) ||
          (digits && code.replace(/[^0-9]/g, '').includes(digits))
        );
      }).slice(0, 8);
      box.innerHTML = hits.length
        ? hits.map((entry) => `<a href="stock.html?code=${entry.code}"><b>${PS.esc(entry.name)}</b>
            <span class="m">${entry.code} · ${entry.market === 'HK' ? '港股' : 'A股'}</span></a>`).join('')
        : '<div class="none">未找到匹配的股票</div>';
      box.style.display = 'block';
    });

    document.addEventListener('click', (e) => {
      if (!e.target.closest('.searchbox')) box.style.display = 'none';
    });
  };
})();
