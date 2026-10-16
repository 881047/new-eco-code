/* 单股事件流懒加载（行级展开用）——轻量 fetch 封装 */
export function fetchStory(code, days = 90) {
  /* 2026-09-18 审查 Q-02：服务端已用 Query(ge=1, le=3660) 钳住，这里再夹一遍是
     为了不白跑一个必然 422 的请求（同一参数面有两个客户端，见 api.js kgPositionsStory）。 */
  const want = Math.min(3660, Math.max(1, parseInt(days, 10) || 90));
  return fetch(`/api/kg/positions_story?days=${want}`, { cache: "no-store" })
    .then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(d => {
      const pos = (d.positions || []).find(x => x.ts_code === code);
      /* 接口返回的列表里没有该股 ≠ 该股没有事件（可能是接口上限/该股不在持仓集）⇒ 标记 missing，
         不许退化成"无事件"（失败与空必须同形可辨，见 07 号事故台账） */
      return pos || { ts_code: code, events: [], summary: "", missing: true };
    });
}
