/* 观察池（M4-5）——收藏存本机（visitorId + localStorage，技术说明书 §7.4）
 *
 * 展示信息（名称 / 分数 / 行业）取自榜单产物（`ranking/{market}.json` 的 rows + excluded）；
 * 当前快照未收录的代码给出占位提示，收藏本身不丢失。
 */
(function () {
  const PS = window.PS;
  const F = PS.fmt;
  let rowsByCode = new Map();

  function render() {
    const codes = PS.watchlist.get();
    const rows = codes.map((code) => rowsByCode.get(code)).filter(Boolean);
    const unknown = codes.filter((code) => !rowsByCode.has(code));

    document.getElementById('vid').textContent = PS.visitorId();

    const box = document.getElementById('list');
    if (!codes.length) {
      box.innerHTML = `<div class="empty">观察池还是空的。<br/>去 <a href="index.html">金字塔排行</a> 挑几只加进来吧。</div>`;
      return;
    }
    box.innerHTML = `<table class="grid"><thead><tr>
        <th>#</th><th>名称 / 代码</th><th>市场</th><th>行业 / 对比池</th><th>金字塔分</th><th>质量分</th><th>成长分</th><th>状态</th><th>操作</th>
      </tr></thead><tbody>${rows.map((row, i) => `<tr>
        <td><span class="ranknum">${i + 1}</span></td>
        <td class="name-cell"><a href="stock.html?code=${row.code}"><b>${PS.esc(row.name)}</b></a><span class="code">${row.code}</span></td>
        <td>${row.market}</td>
        <td>${PS.esc(row.group || row.l1 || '—')}${row.level ? ` <span class="m">${row.level}</span>` : ''}</td>
        <td class="score">${F.score(row.score)}</td>
        <td>${F.score(row.quality)}</td>
        <td class="${PS.MISSING(row.growth) ? '' : row.growth >= 0 ? 'pos' : 'neg'}">${F.growth(row.growth)}</td>
        <td>${row.isTop10pct ? '<span class="pill">组内前10%</span>' : row.reason ? `<span class="pill warn">${PS.esc(row.reason)}</span>` : ''}</td>
        <td><button class="btn danger" data-rm="${row.code}">移除</button></td></tr>`).join('')}</tbody></table>`
      + (unknown.length
        ? `<p class="note">以下代码在当前快照中未收录（可能已退市 / 未采集）：${unknown.map(PS.esc).join('、')}</p>`
        : '');
    box.querySelectorAll('[data-rm]').forEach((b) => {
      b.onclick = () => { PS.watchlist.remove(b.dataset.rm); render(); };
    });
  }

  function exportJson() {
    const data = { visitorId: PS.visitorId(), watchlist: PS.watchlist.get() };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'pyramidseek-watchlist.json';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function importJson() {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = '.json';
    inp.onchange = () => {
      const file = inp.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const data = JSON.parse(reader.result);
          (data.watchlist || []).forEach((code) => PS.watchlist.add(code));
          render();
        } catch (e) {
          alert('导入失败：不是有效的观察池文件');
        }
      };
      reader.readAsText(file);
    };
    inp.click();
  }

  async function init() {
    PS.shell('watchlist.html');
    try {
      await PS.api.boot();
      const rows = await PS.api.allRows();
      rowsByCode = new Map(rows.map((row) => [row.code, row]));
    } catch (err) {
      PS.api.banner('error', `榜单产物加载失败：${err.message}（观察池仍可查看 / 导出）`);
    }
    render();
    document.getElementById('btn-export').onclick = exportJson;
    document.getElementById('btn-import').onclick = importJson;
    document.getElementById('btn-clear').onclick = () => {
      if (confirm('确定清空观察池？')) { PS.watchlist._save([]); render(); }
    };
  }

  document.addEventListener('DOMContentLoaded', init);
})();
