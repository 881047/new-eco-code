"""D06/D07 —— L3 臂 B(推导限价) vs 臂 C(真值限价) 的**逐笔差异归因**。

**这是诊断, 不是判据。** 判据 P1~P4 一字未动, 本脚本不写任何判决。

背景(2026-09-12): L3 跑出 **真值臂比修复后臂低 1.07pp/年(prod)**、0.74pp/年(full),
而 L1 同时说「分歧 99.98% 可归因, 四个桶都标着对策略零影响」。两条读数不能同时为真
而不解释 —— 本脚本回答**到底哪些 (股票, 方向, 日) 在两条臂里一个挡、一个放**,
再把它们回库查是哪个归因桶。

⚠️ 纪律: 它**只解释读数, 不改变读数**。即使查出「残余来自某个已归因桶、且该桶并非零影响」,
那也只是把 L1 里那句「对策略零影响」标注为**未经组合层验证的断言**, 而不是去改桶。

用法: ./.venv/Scripts/python.exe scripts/oneoff/diag_d67_truth_diff.py
输出: D:/claude-home/tmp/diag_d67_truth_diff.log
"""
from __future__ import annotations

import importlib.util
import io
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

from app import db                                             # noqa: E402
from app.logger import setup_logger                            # noqa: E402
from app.models.names import NameLookup                        # noqa: E402
from app.portfolio.board import board_of, limit_ratio, round2  # noqa: E402

LOG = Path("D:/claude-home/tmp/diag_d67_truth_diff.log")
D67 = BASE / "experiments" / "01_data" / "D06_D07_limit_fill.py"
START = "20190722"


def load_d67():
    spec = importlib.util.spec_from_file_location("d67", D67)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    setup_logger()
    db.init_db()
    mod = load_d67()
    nl = NameLookup()

    say("=" * 96)
    say("D06/D07 L3 臂 B vs 臂 C 逐笔差异归因（诊断；不改任何判据）")
    say("窗口 %s~  |  脚本 sha16 = %s" % (START, mod.script_sha()))
    say("=" * 96)

    say("\n--- 臂 B 修复后（推导限价） ---")
    mod.l3_arm(say, "model_score", "diag_d67_B", START, None)
    blk_b = list(mod.BLOCKED)
    why_b = list(mod.ORDER)

    # ⚠️ l3_arm 结尾会 close 掉传进去的 truth["conn"]（:555-556）—— 所以这条连接
    # 只借给臂 C 用，后面查明细必须另开一条，否则撞 "Cannot operate on a closed database"。
    say("\n--- 臂 C 真值（stk_limit 公布限价） ---")
    mod.l3_arm(say, "model_score", "diag_d67_C", START, {"conn": db.get_conn()})
    blk_c = list(mod.BLOCKED)
    why_c = list(mod.ORDER)
    conn = db.get_conn()

    kb = {(o["code"], o["side"], o["d"]) for o in blk_b}
    kc = {(o["code"], o["side"], o["d"]) for o in blk_c}
    say("\n被限价挡过的 (股票,方向,日) 集合:  B=%d  C=%d  交=%d" % (len(kb), len(kc), len(kb & kc)))
    say("  只被 B 挡: %d   只被 C 挡: %d" % (len(kb - kc), len(kc - kb)))

    say("\n--- 最终去向差异（按 (股票, 方向, 去向) 做**多重集**比较） ---")
    # ⚠️ 不能按 (code, side, 成交日) 配对: 顺延几天就会让配对键不同, 于是每一笔都被
    # 报成"只在一边出现"。多重集比较对「成交日平移」免疫, 只抓真正的去向改变。
    cb = Counter((o["code"], o["side"], o["why"]) for o in why_b)
    cc = Counter((o["code"], o["side"], o["why"]) for o in why_c)
    say("  B: %s" % " ".join("%s=%d" % kv for kv in sorted(
        Counter(o["why"] for o in why_b).items())))
    say("  C: %s" % " ".join("%s=%d" % kv for kv in sorted(
        Counter(o["why"] for o in why_c).items())))
    say("  多重集差异 (C−B): %s" % (" ".join("%s %+d" % (k, v) for k, v in
                                             sorted((cc - cb).items())) or "无"))
    say("  多重集差异 (B−C): %s" % (" ".join("%s %+d" % (k, v) for k, v in
                                             sorted((cb - cc).items())) or "无"))
    keys = sorted({(k[0], k[1]) for k in (set(cb) | set(cc)) if cb.get(k) != cc.get(k)})
    say("  去向改变的 (股票,方向) 对: %d" % len(keys))
    for code, side in keys:
        say("    %-11s %-5s" % (code, side))
        for tag, arr in (("B", why_b), ("C", why_c)):
            hits = [(o.get("d"), o["why"], round(o["px"], 3) if o.get("px") else None,
                     o.get("delayed")) for o in arr
                    if o["code"] == code and o["side"] == side]
            say("      %s: %s" % (tag, hits))

    # ---- 把差异格回库查: 它是哪个桶? ----
    diff = sorted(kc - kb) + sorted(kb - kc)
    say("\n--- 差异格明细（归因桶只能人工判读, 这里给全部原始值） ---")
    say("%-9s %-11s %-6s %-8s %-8s %7s %7s %8s %8s %8s %8s %s"
        % ("day", "code", "side", "board", "name", "pc", "close", "推导up", "公布up",
           "推导dn", "公布dn", "谁挡"))
    hist = {}
    for code, side, d in diff:
        x = conn.execute("SELECT pre_close, close FROM daily WHERE trade_date=? AND ts_code=?",
                         (d, code)).fetchone()
        if not x:
            say("  %s %-11s %-6s  (无 daily 行)" % (d, code, side))
            continue
        pc, cv = x
        st = nl.is_st_only(code, d)
        nm = nl.name_at(code, d)
        rr = limit_ratio(code, st, d, nm, nl.name_at)
        du, dd_ = round2(pc * (1 + rr)), round2(pc * (1 - rr))
        t = conn.execute("SELECT up_limit, down_limit FROM stk_limit "
                         "WHERE trade_date=? AND ts_code=?", (d, code)).fetchone()
        tu, td = (float(t[0]), float(t[1])) if t else (float("nan"), float("nan"))
        tag = ("B挡C放" if (code, side, d) in kb else "C挡B放")
        key = (board_of(code), tag, "退" in str(nm), st)
        hist[key] = hist.get(key, 0) + 1
        say("  %-9s %-11s %-6s %-8s %-8s %7.2f %7.2f %8.2f %8.2f %8.2f %8.2f %s"
            % (d, code, side, board_of(code), nm, pc, cv, du, tu, dd_, td, tag))

    say("\n--- 差异格按 (板块, 谁挡, 名含退, 是否ST) 汇总 ---")
    for k, n in sorted(hist.items(), key=lambda x: -x[1]):
        say("  %-10s %-8s 退=%-5s ST=%-5s  %4d" % (k[0], k[1], k[2], k[3], n))

    say("\n[说明] 本脚本不改判据。若差异格全部落在已归因桶里, 则 L1 那句「对策略零影响」")
    say("       在**组合层**是未经检验的断言 —— 需要另开一轮预注册去量, 不是就地改桶。")
    conn.close()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines), encoding="utf-8")
    print("\n[日志] %s" % LOG)


if __name__ == "__main__":
    main()
