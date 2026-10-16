"""工程化落地审计（2026-09-13）：一份**可重复跑**的清单，不是一次性的口头结论。

为什么要有它：这个项目里真正咬人的从来不是"算法不够好"，而是"看着装好了其实不工作"——
自愈没接线、备份没人验过、测试往生产日志里写、配置改了标签没改。这类问题**单看代码看不出来**，
只能靠一条固定的清单反复去戳。审计的每条都必须打印**证据**（数字/路径/输出），不许只给 ✓。

用法：
    D:\\Python313\\python.exe -X utf8 scripts\\audit_engineering.py            # 快速（不跑测试）
    D:\\Python313\\python.exe -X utf8 scripts\\audit_engineering.py --tests     # 连全量测试一起跑

退出码 = 失败条数（0 = 全过）。只读：不写任何业务文件。
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod                                  # noqa: E402

OK, BAD, INFO, SKIP = "✓", "✗", "·", "○"      # SKIP = 该项本轮未跑（不算通过、也不算失败）


def _decode(raw: bytes) -> str:
    """子进程输出解码（2026-09-18 审查 Q-10）。

    原来 `subprocess.run(text=True)` 不带 encoding ⇒ 走**首选编码**。审计自己是用
    `-X utf8` 跑的（PEP 540 UTF-8 模式），于是首选编码被强制成 utf-8，而中文 Windows
    上 `schtasks` 发的是 **GBK/cp936** ⇒ 读线程抛 `UnicodeDecodeError: 0xce`、
    `p.stdout` 变成 None ⇒ 输出为空。实测复现过。

    顺序不能反：gbk 对几乎任意字节序列都能"成功"解码（它太宽容），放前面会把
    git 的 UTF-8 中文输出解成乱码。所以先 utf-8，失败再退 gbk。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _run(cmd: list, timeout: int = 60, env: dict = None) -> tuple:
    try:
        # text=False：自己拿到字节再按 _decode 的次序解，避开 -X utf8 与 GBK 的冲突
        p = subprocess.run(cmd, cwd=str(BASE), capture_output=True, timeout=timeout, env=env)
        return p.returncode, _decode(p.stdout).strip(), _decode(p.stderr).strip()
    except Exception as e:
        return 99, "", str(e)


def _py_env() -> dict:
    """给 **python 子进程**用的环境（2026-09-13 修；2026-09-19 注释更新——行为不变）。

    `.venv` 已是完整环境（3.13.15 迁移 + lock 依赖装齐，2026-09-18 实点全量 0 跳过），
    下方 PYTHONPATH 注入**保留**：让审计不依赖调用者 shell 的环境变量。历史背景（KB 14
    三补十九/三补二十二 §E）：venv 曾是 stub 时代，`D:\\Python313\\python.exe` 只有基础
    安装、直接起 `pytest` 会在 collection 阶段 ModuleNotFoundError ⇒ 当时靠这段注入续命。
    """
    env = dict(os.environ)
    cands = [BASE / ".venv" / "Lib" / "site-packages"]
    cands += sorted((BASE / ".venv" / "lib").glob("python*/site-packages"))
    parts = [str(p) for p in cands if p.exists()] + [str(BASE)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def check_repo(row: list) -> bool:
    rc, out, _ = _run(["git", "status", "--porcelain"])
    _, log, _ = _run(["git", "log", "--oneline", "-1"])
    ok = rc == 0 and out == ""
    row.append((ok, "仓库干净", f"{log}" if ok else f"{len(out.splitlines())} 个改动未提交"))
    return ok


def check_secrets(row: list) -> bool:
    """密钥不许进库：.env 被忽略 + config.yaml 的 auth_code 必须为空。"""
    rc1, ign, _ = _run(["git", "check-ignore", "-v", ".env"])
    try:
        raw = (BASE / "config.yaml").read_text(encoding="utf-8")
    except OSError as e:
        # 2026-09-14（外部复核 O13）：原来这里没有 try/except ⇒ config.yaml 读不到时
        # 审计在**打印任何一行之前**就 traceback 了（门禁自己不可信比不审计更糟）。
        row.append((False, "密钥不进库", f"读不到 config.yaml: {e}"))
        return False
    has_code = False
    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("auth_code:"):
            val = s.split(":", 1)[1].split("#")[0].strip().strip("'\"")
            has_code = bool(val)
    ok = rc1 == 0 and not has_code
    row.append((ok, "密钥不进库",
                f".env 由 {ign.split(':')[0] if ign else '?'} 忽略；config.yaml 的 auth_code 为空"))
    return ok


def check_tests(row: list, run_tests: bool) -> bool:
    if not run_tests:
        # 2026-09-14（外部复核 O12）：原来记成 `True` ⇒ 汇总会打印 **"9/9 项通过"而测试根本没跑**。
        # 现在记成 `None`（"未跑"），汇总里单列、不计入通过数（退出码仍为 0：快速审计是合法用法）。
        row.append((None, "全量测试", f"{INFO} 未跑（加 --tests 才算这一项）"))
        return True
    py = sys.executable
    # 外部事实守卫（2026-09-13, 07 号 #21）：**跑测试不许改写生产 config.yaml**。
    # 原实现里 tests/test_local_guard.py 真的打 /api/settings 端点 ⇒ 每次全量测试都把
    # `fees.transfer_fee` 的类型写坏（float → 字符串）。测试自己说"我没写"不算，看文件字节。
    import hashlib
    cfg_path = BASE / "config.yaml"
    sha_before = hashlib.sha256(cfg_path.read_bytes()).hexdigest() if cfg_path.exists() else None
    # 全量测试实测 60~90 秒（2026-09-13）—— 默认 60 秒的 _run 超时会把"跑得慢"误报成"挂"，
    # 审计工具自己误报比不审计更糟，所以这里给足 15 分钟。
    rc, out, err = _run([py, "-X", "utf8", "-m", "pytest", "-q"], timeout=900, env=_py_env())
    tail = (out or err).strip().splitlines()[-1] if (out or err).strip() else ""
    ok = rc == 0 and " passed" in tail
    sha_after = hashlib.sha256(cfg_path.read_bytes()).hexdigest() if cfg_path.exists() else None
    if sha_after != sha_before:
        ok = False
        tail = (tail + " ⚠ 测试改写了 config.yaml（有对外副作用）").strip()
    row.append((ok, "全量测试", tail or f"rc={rc}"))
    return ok


#: `schtasks /query /tn <名> /v /fo CSV /nh` 的列序（2026-09-18 审查 Q-10）。
#: 用 **/v + CSV + 按位置取值**，因为 `Last Result` / `Task To Run` 这类**标签名是本地化的**
#: （中文 Windows 实测输出 `上次结果` / `要运行的任务`）—— 原来按英文标签 startswith
#: 匹配，永远取到 "?"，该项因此**恒红**。/nh 去掉表头、位置不随语言变，才是语言无关的。
#:
#: ⚠️ 三个 flag 一个都不能省：`/fo CSV /nh` **不带 /v** 时只有 3 列
#:    （TaskName / Next Run Time / Status）—— 没有"上次结果"也没有"要运行的任务"，
#:    审计会**静默失去牙齿**，看起来绿其实什么都没查。列数不够时下面直接报解析失败。
_SCH_NEXT_RUN, _SCH_LAST_RESULT, _SCH_TASK_TO_RUN = 2, 6, 8
_SCH_MIN_COLS = _SCH_TASK_TO_RUN + 1


def _schtasks_row(task: str) -> tuple:
    """取该任务的详细 CSV 行。返回 (row 或 None, 人话说明)。"""
    rc, out, err = _run(["schtasks", "/query", "/tn", task, "/v", "/fo", "CSV", "/nh"])
    if rc != 0:
        return None, f"schtasks 查询失败 rc={rc}: {(err or out)[:120]}"
    line = next((l for l in out.splitlines() if l.strip()), "")
    if not line:
        return None, "schtasks 输出为空（解码或任务不存在？）"
    try:
        row = next(csv.reader([line]))
    except Exception as e:
        return None, f"CSV 解析失败: {e}"
    if len(row) < _SCH_MIN_COLS:
        return None, (f"列数只有 {len(row)} < {_SCH_MIN_COLS} —— 是不是把 /v 丢了？"
                      "（不带 /v 的 CSV 没有上次结果/要运行的任务，该项会假绿）")
    return row, None


def check_service(row: list) -> bool:
    # 探活是**主判据**：服务到底在不在，HTTP 说了算，不受 schtasks 本地化影响。
    alive = False
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cfg_mod.server_port()}/api/overview", timeout=4) as r:
            alive = 200 <= int(r.status) < 300
    except Exception:
        alive = False
    trow, why = _schtasks_row("NEW ECO Watchdog")
    last = trow[_SCH_LAST_RESULT].strip() if trow else "?"
    nxt = trow[_SCH_NEXT_RUN].strip() if trow else "?"
    # 2026-09-13（用户实拍"时不时弹窗又消失"）：任务是 Interactive only，**直接跑 .cmd 会每 10 分钟
    # 闪一个控制台窗口** ⇒ 必须走隐藏启动器（wscript //B + Run(..., 0, ...)）。这里把它当审计证据之一。
    action = trow[_SCH_TASK_TO_RUN].strip() if trow else "?"
    hidden = "watchdog_task_hidden.vbs" in action
    ok = alive and trow is not None and last == "0" and hidden
    detail = (f"探活 {'200' if alive else 'DOWN'}；看门狗上次结果 {last}，下次 {nxt}；"
              f"入口 {'隐藏启动器' if hidden else '⚠ 未走隐藏启动器(会闪窗): ' + action}")
    row.append((ok, "服务+看门狗", detail if trow is not None else f"{detail}｜{why}"))
    return ok


def check_doctor(row: list) -> bool:
    """开跑前体检（内嵌 `weekly doctor` 的 **18 项**，2026-09-22 起）。

    2026-09-15（外部独立审查 E3/F5）：**时序性红项不算失败**。
    `doctor` 有两项红是"到点自然会绿"的：①「打分存档覆盖最新锚点」②「方案文件存在」——
    它们锚定的是**下周五**的锚点，周一~周四跑必然为 ✗（保鲜档 19:30 才重训、方案周五后才建）。
    原来的写法把它们计成失败 ⇒ **审计在该时段退出码恒为 2**，任何按退出码判健康的
    外部自动化（CI / 看门狗脚本）每周有 4 天误报。

    现在的口径：这两项红 ⇒ 记成 `None`（`○` 未决/未跑，不进 fails 计数），**但明细照打**，
    并在行尾显式写"时序性（等到点自动绿）"—— 既不误报，也不假装通过。
    """
    import re as _re
    timed_names = ("打分存档覆盖最新锚点", "方案文件")
    # 2026-09-21 新增「模型新鲜度」: 它的 ✗ 是**查出来的真实缺陷**(生产 `model_score`
    # 无 `data_asof` 记录 ⇒ 模型年龄不可知), 历史无法回溯补记、不编造假值 ⇒
    # 解除时点是 **2026-10-01 月频全量重训**跑通后自动补齐并转绿。
    # 在那之前它是**如实**的红, 但"体检坏了"和"有个已知项待到点"是两回事 ⇒ 按
    # 时序项同口径记 None(不进 fails), 但明细照打并显式标注解除时点。
    # 过了 10-01 仍未解除 ⇒ 上面 `_excusable` 返回 False ⇒ 照旧算失败(不给长期豁免)。
    known_until = {"模型新鲜度": "20261001"}
    try:
        from app.weekly import doctor
        items = doctor.checks()
        bad = [i for i in items if not i.get("ok")]
        detail = (f"{len(items) - len(bad)}/{len(items)} 通过"
                  + ("" if not bad else "；失败: " + ", ".join(i["name"] for i in bad)))
        if not bad:
            row.append((True, "开跑前体检", detail))
            return True
        # 判"是不是只差时间"：① 名字命中时序项；② 并且下个执行档在**未来**（锚点还没到）
        next_exec = next((str(i.get("detail") or "") for i in items
                          if "下个执行档" in str(i.get("name", ""))), "")
        m = _re.search(r"(\d{8})", next_exec)
        future_exec = False
        if m:
            try:
                future_exec = m.group(1) > datetime.date.today().strftime("%Y%m%d")
            except Exception:
                future_exec = False
        def _excusable(name: str) -> bool:
            """这条 ✗ 是否属于"到点自然会绿"，而不是体检本身坏了。"""
            if any(t in name for t in timed_names):
                return future_exec
            for k, until in known_until.items():
                if k in name:
                    try:
                        return datetime.date.today().strftime("%Y%m%d") < until
                    except Exception:
                        return False
            return False

        only_timed = bool(bad) and all(_excusable(str(i.get("name", ""))) for i in bad)
        known = [i["name"] for i in bad if any(k in str(i.get("name", ""))
                                               for k in known_until)]
        row.append((None if only_timed else False, "开跑前体检",
                    detail
                    + ("（时序性: 锚点在下个执行档之后，等到点自动绿）"
                       if only_timed and not known else "")
                    + (f"（已知项: {', '.join(known)} —— 有明确解除时点，不是故障）"
                       if known else "")))
        # 只有"纯时序红"才算通过（记 None ⇒ 不进 fails）；其余红照旧算失败
        return bool(only_timed)
    except Exception as e:
        row.append((False, "开跑前体检", f"调用失败: {e}"))
        return False


def check_backup(row: list) -> bool:
    d = BASE / "data" / "backup"
    dbs = sorted(d.glob("neweco_????????.db"))
    if not dbs:
        row.append((False, "备份可读且新鲜", "没有任何备份文件"))
        return False
    latest = dbs[-1]
    age = (datetime.date.today() -
           datetime.date.fromtimestamp(latest.stat().st_mtime)).days
    try:
        con = sqlite3.connect(latest.as_uri() + "?mode=ro", uri=True)
        sv = con.execute("PRAGMA schema_version").fetchone()[0]
        n = con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        con.close()
        ok = age <= 8 and sv and n
        row.append((ok, "备份可读且新鲜",
                    f"{latest.name}（{age} 天前，{latest.stat().st_size / 1e9:.2f} GB，"
                    f"schema v{sv}，{n} 个对象）"))
    except Exception as e:
        row.append((False, "备份可读且新鲜", f"{latest.name} 读不出: {e}"))
        return False
    return ok


def check_artifacts(row: list) -> bool:
    """产物版本链（§6.1-2「任一产物可反查其输入版本」）—— 不是看字段在不在, 而是**验它真不真**：

      1) 方案带 meta（代码版本 / 打分指纹 / 配置指纹）；
      2) **重算打分存档的 sha256[:16]，必须与方案里记的一致** ⇒ 这份方案确实出自那份打分；
      3) 方案里的敞口与当前配置一致 ⇒ 改了配置没重算方案会被当场抓住（配置漂移）。

    2026-09-13 审计第一版把键名写错了（写成 meta.git_sha/scores_fp，实际是
    meta.code/scores_sha16）—— 报了个假警报。改成按真实 schema 取键。
    """
    try:
        import hashlib
        from app import config as cfg_mod
        from app.weekly import plan as wk_plan
        cfg = cfg_mod.load_config()
        plans = sorted(wk_plan.PLAN_DIR.glob("plan_????????.json"))
        if not plans:
            row.append((False, "产物版本链", "没有方案文件"))
            return False
        p = json.loads(plans[-1].read_text(encoding="utf-8"))
        meta = p.get("meta") or {}
        w_plan = p.get("equity_weight")
        w_cfg = wk_plan.equity_weight(cfg)
        same_w = w_plan is None or abs(float(w_plan) - float(w_cfg)) < 1e-9
        code = str(meta.get("code") or "")
        fp_plan = str(meta.get("scores_sha16") or "")
        sfile = p.get("scores_file") or ""
        spath = Path(sfile) if sfile else (wk_plan.SCORE_DIR / str(meta.get("scores_file") or ""))
        fp_real = ""
        fp_note = "打分文件取不到"
        if spath.exists():
            h = hashlib.sha256()
            with spath.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            fp_real = h.hexdigest()[:16]
            fp_note = ("指纹一致" if fp_real == fp_plan else
                       f"⚠ 指纹不符: 方案记 {fp_plan}, 文件实为 {fp_real}")
        ok = bool(code) and bool(fp_plan) and bool(fp_real) and fp_real == fp_plan and same_w
        row.append((ok, "产物版本链",
                    f"{plans[-1].name}：代码 {code} | 打分 {meta.get('scores_file')} "
                    f"{fp_note} | 敞口 方案 {w_plan} / 配置 {w_cfg}"
                    + ("" if same_w else " ⚠ 敞口不一致(改配置后必须重算方案)")))
        return ok
    except Exception as e:
        row.append((False, "产物版本链", f"检查失败: {e}"))
        return False


def check_ledger(row: list, path=None) -> bool:
    """账本（若已开始）：JSON 可解析 + **净值下限** + 资金恒等式 cash + 持仓市值 + 利息 ≈ 净值。

    2026-09-14（外部复核 O13 残余）：原来只查恒等式 `cash+interest+invested = nav` ——
    **全 0 也满足**（账户被清空/文件被写坏成 0 时会判"通过"）。现在补两道下限：
    净值必须 > 0；三个分量不能同时为 0。`path` 可注入（测试用）。
    """
    p = Path(path) if path is not None else (BASE / "data" / "weekly" / "ledger.json")
    if not p.exists():
        row.append((True, "账本完整性", f"{INFO} 还没有账本（首个执行档会新建）"))
        return True
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        hist = st.get("nav_history") or []
        if not hist:
            row.append((True, "账本完整性", f"账本在，但还没有净值点（成交 {len(st.get('trades') or [])} 笔）"))
            return True
        last = hist[-1]
        cash = float(st.get("cash") or 0.0)
        interest = float(st.get("interest") or 0.0)
        invested = float(last.get("invested") or 0.0)
        nav = float(last.get("nav") or 0.0)
        gap = cash + interest + invested - nav
        floor_ok = nav > 0.0 and (cash + interest + invested) > 0.0
        ok = abs(gap) <= 1.0 and floor_ok
        why = "" if floor_ok else "；⚠ 净值/分量不合法（全 0？被清空？）"
        row.append((ok, "账本完整性",
                    f"{len(hist)} 个净值点 | 现金 {cash:,.0f} + 利息 {interest:,.2f} + "
                    f"市值 {invested:,.0f} vs 净值 {nav:,.0f}（差 {gap:+.2f}）{why}"))
        return ok
    except Exception as e:
        row.append((False, "账本完整性", f"账本读不出: {e}"))
        return False


def check_ledger_14(row: list) -> bool:
    """14 号台账自己的可复核性（2026-09-20 结清轮加）：指针存在 / 措辞合规 / 计数与现算一致。

    为什么值得占一项：当晚审核查出 6 条"与实证冲突的判决或守卫"、20+ 条躲过
    "这张表就是全部开放项"这句话的活、以及一批过期计数（"13 键""11 项""171 条"）。
    手工清扫只能管好一轮 ⇒ 把三件事变成红灯。判据实现在 `scripts/audit_ledger_14.py`
    （单一真源，测试与这里都调它），那份脚本自带四条**阴性对照**：坏指针/坏措辞/坏计数
    注入后必须被抓到，否则本项直接红（防的是"恒绿的自我检查"，同 07 #53、三补二十六 §②）。
    """
    import subprocess
    p = subprocess.run([sys.executable, "-X", "utf8", str(BASE / "scripts" / "audit_ledger_14.py")],
                       capture_output=True, text=True, cwd=str(BASE))
    out = (p.stdout or "").strip().splitlines()
    tail = out[-1] if out else (p.stderr or "")[:120]
    ok = p.returncode == 0
    row.append((ok, "台账 14 可复核", tail if ok else f"rc={p.returncode}｜{tail}｜{'; '.join(out[2:6])}"))
    return ok


def check_knowledge_base(row: list) -> bool:
    """知识库 `D:\\know` + Codex skill 与仓库镜像必须存在且一致（2026-09-13）。

    为什么单列一项：知识库写的是**口径与纪律**，最不该丢；而它建在仓库外 ⇒
    "仓库里看不见 + 没有历史"（用户当天就以为它不见了）。镜像随提交走，这里守一致性。
    同一理由适用于讲"哪件事读哪一份"的 Codex skill（也在仓库外）。
    """
    sys.path.insert(0, str(BASE / "scripts"))
    from sync_know import MIRRORS, _files, diff_one   # 单一实现：与同步脚本/测试同一判据

    ok, parts = True, []
    for name, src, mirror, globs in MIRRORS:
        if not src.exists():
            row.append((False, "知识库与镜像", f"{name}源不存在: {src}"))
            return False
        d = diff_one(src, mirror, globs)
        bad = d["missing"] or d["stale"] or d["extra"]
        ok = ok and not bad
        parts.append(f"{name} {len(_files(src, globs))} 个 → {mirror.relative_to(BASE).as_posix()}"
                     + ("" if not bad else f"（缺 {d['missing']} 旧 {d['stale']} 多 {d['extra']}）"))
    row.append((ok, "知识库与镜像",
                "；".join(parts) + ("" if ok else " ⇒ 跑 scripts/sync_know.py")))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", action="store_true", help="连全量测试一起跑（约 1 分钟）")
    a = ap.parse_args()
    row: list = []
    checks = (check_repo, check_secrets, check_service, check_doctor, check_backup,
              check_artifacts, check_ledger, check_ledger_14, check_knowledge_base)
    fails = 0
    for fn in checks:
        try:
            if not fn(row):
                fails += 1
        except Exception as e:
            # 2026-09-14（外部复核 O13）：任何 check 自己抛异常 ⇒ 如实记一行 ✗，
            # 而不是让整个审计在打印前崩掉（那时"没看到 ✗"会被误读成"通过"）。
            fails += 1
            row.append((False, fn.__name__, f"检查本身抛异常: {type(e).__name__}: {e}"))
    if not check_tests(row, a.tests):
        fails += 1
    print(f"工程化审计 {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    for ok, name, detail in row:
        icon = SKIP if ok is None else (OK if ok else BAD)
        print(f"  {icon} {name}: {detail}")
    passed = sum(1 for ok, _n, _d in row if ok is not None and ok)
    skipped = sum(1 for ok, _n, _d in row if ok is None)
    tail = ("（未跑: " + "、".join(n for ok, n, _d in row if ok is None) + "）") if skipped else ""
    print(f"— {passed}/{len(row)} 项通过{tail}；退出码={fails}")
    return fails


if __name__ == "__main__":
    sys.exit(main())
