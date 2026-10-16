"""一次性修正: 财务兜底数据披露日改为法定披露截止日(年报前视修复)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.logger import setup_logger
from app import db
from app.datasource.akshare_client import _est_ann_date


def main():
    from _guard import require_confirm
    require_confirm(__file__, "UPDATE 生产 fina_indicator.ann_date（改全市场披露日，不可逆）")
    setup_logger()
    conn = db.get_conn()
    rows = conn.execute("SELECT DISTINCT ts_code, end_date FROM fina_indicator").fetchall()
    updates = [(_est_ann_date(e), c, e) for c, e in rows]
    conn.executemany(
        "UPDATE fina_indicator SET ann_date=? WHERE ts_code=? AND end_date=?", updates)
    conn.commit()
    conn.close()
    print(f"已修正 {len(updates)} 条披露日")


if __name__ == "__main__":
    main()
