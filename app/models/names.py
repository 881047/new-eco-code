"""时点名称查找 (2026-09-06 第十轮 N10-4): 全链 PIT 名称统一

trainer/回测/builder/paper/monitor 共用的历史名称链查询, 消除"当前名/建单冻结名"的
口径分叉 —— ST/退 的 5% 限幅与候选过滤统一按执行日/评分日时点名称判定。
"""
import pandas as pd

from app import db


def name_at_hist(rows: list, d: str, fallback: str = "") -> str:
    """时点名称查找(纯函数, D1): rows=[(start,end,name),...] 按 start 升序;
    返回 d(YYYYMMDD) 当日生效的名称, 无匹配回退 fallback(当前名称)"""
    for s, e, nm in reversed(rows):  # 从最近倒查
        if s <= d < e:
            return nm
    return fallback


class NameLookup:
    """namechange 历史名称链 + 当前名称一次性加载; 复用方每次执行期构造一个实例
    (名称日更, 跨天缓存会漏掉新戴帽)"""

    def __init__(self, conn=None):
        own = conn is None
        conn = conn or db.get_conn()
        try:
            # 缺表/缺列时降级为空(新装机或测试临时库): 名称回退为空串 → 不判 ST
            nc = pd.DataFrame()
            try:
                if db.table_exists(conn, "namechange"):
                    nc = pd.read_sql(
                        "SELECT ts_code, start_date, end_date, name FROM namechange "
                        "ORDER BY start_date", conn)
            except Exception:
                nc = pd.DataFrame()
            cur = {}
            try:
                basic = pd.read_sql("SELECT ts_code, name FROM stock_basic", conn)
                cur = basic.set_index("ts_code")["name"].to_dict()
            except Exception:
                cur = {}
        finally:
            if own:
                conn.close()
        self._rows = {}
        if len(nc) and "ts_code" in nc.columns:
            for code, g in nc.groupby("ts_code"):
                self._rows[code] = [(str(r["start_date"]),
                                     str(r["end_date"]) if pd.notna(r["end_date"]) else "99999999",
                                     str(r["name"])) for _, r in g.iterrows()]
        self._cur = cur

    def name_at(self, code: str, dstr: str) -> str:
        """dstr(YYYYMMDD) 时点的股票名称; 无改名记录回退当前名称"""
        rows = self._rows.get(code)
        if not rows:
            return self._cur.get(code, "")
        return name_at_hist(rows, dstr, self._cur.get(code, ""))

    def is_st(self, code: str, dstr: str) -> bool:
        nm = self.name_at(code, dstr)
        return bool(nm) and ("ST" in nm or "退" in nm)

    def is_st_only(self, code: str, dstr: str) -> bool:
        """风险警示(ST/*ST)判定, 不含退市整理期("退") —— 限幅规则用
        (2026-09-07 全市场扩展: 退市整理期按板块普通限幅, 不再并入 5% 档;
        样本剔除照旧用 is_st 排除 ST 与退)"""
        nm = self.name_at(code, dstr)
        return bool(nm) and "ST" in nm
