/* 金字塔选股 PyramidSeek —— 产物加载层（M4-2）
 *
 * 唯一数据入口：fetch 静态产物（`data/*.json`，《产物契约》V1.5）+ 内存缓存 + 提示条。
 * 约定（技术说明书 §7.2 / §7.6）：
 * - 断产物 / 加载失败 → 顶部红条提示，页面不白屏；
 * - `meta.degraded = true` → 顶部黄条提示「数据降级」；
 * - 产物契约版本 ≠ 前端预期 → 顶部黄条提示「版本不一致」。
 */
(function () {
  const PS = (window.PS = window.PS || {});
  const BASE = 'data/';
  const EXPECTED_CONTRACT = '1.2';   // 与后端 config.CONTRACT_VERSION 对齐（契约 V1.5）
  const cache = new Map();

  async function load(path) {
    if (cache.has(path)) return cache.get(path);
    let res;
    try {
      res = await fetch(BASE + path, { cache: 'no-cache' });
    } catch (err) {
      throw new Error(`${path}：无法加载（${err.message}）—— 产物缺失或服务未正确启动`);
    }
    if (!res.ok) throw new Error(`${path}：HTTP ${res.status}`);
    const data = await res.json();
    cache.set(path, data);
    return data;
  }

  /** 顶部提示条（warn / error / info） */
  function banner(level, text) {
    let bar = document.getElementById('ps-banner');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'ps-banner';
      document.body.insertBefore(bar, document.body.firstChild);
    }
    bar.className = 'ps-banner ' + level;
    bar.textContent = text;
    bar.style.display = 'block';
  }

  PS.api = {
    banner,
    load,
    meta: () => load('meta.json'),
    industries: () => load('industries.json'),
    searchIndex: () => load('search-index.json'),
    ranking: (market) => load(`ranking/${market}.json`),
    stock: (code) => load(`stock/${code}.json`),

    /** 页面启动：加载 meta + industries，做降级 / 版本提示；挂到 `PS.meta` / `PS.industries`。 */
    async boot() {
      const [meta, industries] = await Promise.all([this.meta(), this.industries()]);
      PS.meta = meta;
      PS.industries = industries;
      if (String(meta.contractVersion) !== EXPECTED_CONTRACT) {
        banner(
          'warn',
          `产物契约版本 ${meta.contractVersion} 与前端预期 ${EXPECTED_CONTRACT} 不一致，部分字段可能显示异常`
        );
      } else if (meta.degraded) {
        banner('warn', `本次快照为降级数据（部分接口未走主源）；${meta.notes || ''}`);
      }
      return { meta, industries };
    },

    /** 全部市场榜单分片（市场列表取自 industries.markets）。 */
    async rankings() {
      const industries = PS.industries || (await this.industries());
      const markets = industries.markets && industries.markets.length ? industries.markets : ['A', 'HK'];
      return Promise.all(markets.map((market) => this.ranking(market)));
    },

    /** 全部榜单行（主榜 + 单列）——搜索 / 观察池补信息用。 */
    async allRows() {
      const shards = await this.rankings();
      return shards.flatMap((shard) => [...(shard.rows || []), ...(shard.excluded || [])]);
    },
  };
})();
