/* 今日行动中枢：把散在各卡片里的状态综合成带优先级的行动清单。
   全部规则确定性可追溯 —— 每条自带 source（数据来自哪个接口的哪个字段），不许"凭感觉"。 */
import { store } from '../store'
import { fmtPct, fmtNum, STATE_CN, daysBetween8 } from '../format'

const ROUTES = {
  today:  { name: "today" },
  signal: { name: "today", hash: "#signal" },
  lhb:    { name: "lhb" },
  paper:  { name: "paper" },
  weekly: { name: "sleeve" },
  /* 2026-10-08 批4：状态页与邮件页合并为 /ops（旧路径 /status、/notify 只是 redirect，
     没有路由 name —— 这里若继续写 { name: "status" } 会导航失败）。 */
  status: { name: "ops", hash: "#status" },
  notify: { name: "ops", hash: "#notify" },
  health: { name: "sleeve", hash: "#weekly-health" },
}

const LVL_ORDER = { P0: 0, P1: 1, P2: 2 }

/* 单条 = { level, text, sub, route, source } */
export function computeActions() {
  const out = [];
  const o = store.overview.data;
  const w = store.weekly.data;
  const p = store.paper.data;
  const st = store.status.data;
  const sg = store.signal.data;
  const lh = store.lhb.data;
  const h = store.weeklyHealth.data;
  const n = store.notify.data;

  /* ---- P0：影响资金安全 / 系统停摆 ---- */
  if (store.failed.length) {
    out.push({ level: "P0", text: `${store.failed.length} 个数据块取数失败（${store.failed.join(" / ")}）`,
      sub: "下面的清单可能不完整；30 秒后自动重试，也可刷新页面", route: null, source: "boot 横幅" });
  }
  if (p && p.paused) {
    out.push({ level: "P0", text: "交易已暂停（paper.paused=true）",
      sub: "不按方案/信号挂单、不成交，存量挂单已作废；恢复需改 config.yaml", route: ROUTES.paper, source: "/api/paper.paused" });
  }
  if (w && w.kpi && w.kpi.halt && w.kpi.halt.active) {
    out.push({ level: "P0", text: `周频风控停买中（自 ${w.kpi.halt.since}）：${w.kpi.halt.reason}`,
      sub: "只卖不买，人工 resume 才解除", route: ROUTES.weekly, source: "/api/weekly kpi.halt" });
  }
  if (w && w.ops && w.ops.next_exec === w.ops.today) {
    const ok = w.ops.exec_ok_today;
    if (ok === false) {
      out.push({ level: "P0", text: "周频执行档未落账（看门狗会告警）",
        sub: String(w.ops.exec_detail || "").replace(/\*\*/g, ""), route: ROUTES.weekly, source: "/api/weekly ops" });
    }
  }
  if (h && h.level === "dead") {
    out.push({ level: "P0", text: "周频健康度：dead", sub: (h.reasons || []).join("；"), route: ROUTES.health, source: "/api/weekly/health" });
  }

  /* ---- P1：需要今天处理 / 会误导判断 ---- */
  if (o && o.backtest_pin_missed) {
    out.push({ level: "P1", text: "指定回测基线缺失，当前展示最新 run",
      sub: "config.yaml 的 backtest_run 指向的 run 不在库里", route: { name: "plans", hash: "#nav" }, source: "/api/overview.backtest_pin_missed" });
  }
  if (sg && sg.rows && sg.rows.length) {
    const sells = sg.rows.filter(r => r.action === "sell" || r.action === "sell_weak" || r.action === "reduce");
    if (sells.length) {
      out.push({ level: "P1", text: `${sells.length} 个卖出/减仓信号（模拟盘次日开盘自动执行）`,
        sub: sells.map(r => `${r.name}(${r.ts_code})`).join("、"), route: ROUTES.signal, source: "/api/signal" });
    }
  }
  if (st && st.rows) {
    const daily = st.rows.find(r => r.name === "daily");
    /* 滞后口径 = **开市日**（/api/status 的 open_days_since_daily，COR-02 根治）：
       长假该值为 0 ⇒ 永不在长假误报；日历缺失时回退日历日估算（阈值放大 1 倍容错） */
    const cal = st.open_days_since_daily;
    const gapTrading = typeof cal === "number" ? cal : null;
    const gap = gapTrading !== null ? gapTrading
      : (daily ? daysBetween8(daily.last_date) : null);
    const unit = gapTrading !== null ? "开市日" : "天";
    const HOLIDAY = gapTrading === null ? "；春节/国庆等长假属正常，可忽略" : "";
    if (daily && gap !== null) {
      /* 2026-09-28 审查修复: 开市日口径下阈值对齐晨间待办邮件(notify late>=2 即 warn
         "数据不新, 今天的信号与挂单都不可信") —— 原前端 P1 阈值放宽一倍(gap>=4),
         且 P2 的"周末/短假属正常"在开市日口径下失真(连续 2 个开市日没更新绝非假期) */
      const p1 = gapTrading !== null ? gap >= 3 : gap >= 10;
      const p2 = gapTrading !== null ? gap >= 2 : gap >= 4;
      if (p1) {
        out.push({ level: "P1", text: `日线数据截至 ${daily.last_date}，已 ${gap} 个${unit}未更新`,
          sub: "先点「立即更新数据」；今天的信号与挂单都不可信（与晨间待办邮件同口径）" + HOLIDAY,
          route: ROUTES.status, source: "/api/status daily.last_date" });
      } else if (p2) {
        out.push({ level: "P2", text: `日线数据截至 ${daily.last_date}（${gap} 个${unit}前）`,
          sub: gapTrading !== null ? "已连续 2 个开市日未更新, 请检查日链" : "周末/短假属正常",
          route: ROUTES.status, source: "/api/status daily.last_date" });
      }
    }
  }
  if (h && (h.level === "warning" || h.level === "degrading")) {
    out.push({ level: "P1", text: `周频健康度：${h.level}`,
      sub: `滚动 IC ${h.rolling_ic ?? "--"}（冻结基线 ${h.frozen_ic ?? "--"}）${(h.reasons || []).length ? "；" + h.reasons.join("；") : ""}`,
      route: ROUTES.health, source: "/api/weekly/health" });
  }

  /* ---- P2：值得扫一眼 ---- */
  if (lh && lh.rows && lh.rows.length) {
    const mine = lh.rows.filter(r => r.in_plan);
    if (mine.length) {
      out.push({ level: "P2", text: `${mine.length} 只持仓上榜龙虎榜`,
        sub: mine.map(r => `${r.name} ${fmtPct(r.change, 1)}`).join("、"), route: ROUTES.lhb, source: "/api/lhb in_plan" });
    }
  }
  if (w && w.plan && w.plan.expectation_multiplicity) {
    out.push({ level: "P2", text: "周频方案的多重比较校正后不显著",
      sub: `DSR N=7 ${w.plan.expectation_multiplicity.dsr_n7.join("/")}，N=25 ${w.plan.expectation_multiplicity.dsr_n25.join("/")}（门槛 0.95）——预期别按点估计读`,
      route: ROUTES.weekly, source: "/api/weekly plan.expectation_multiplicity" });
  }
  if (o && o.state) {
    /* 状态本身不是行动项，但档位变化值得知道（放 P2 尾部） */
    out.push({ level: "P2", text: `市场状态 ${STATE_CN[o.state] || o.state}（评分 ${o.state_score ?? "--"}）`,
      sub: "月频 regime 建议仓位 " + fmtPct(o.equity_weight, 0), route: ROUTES.today, source: "/api/overview" });
  }
  if (n && n.rows && n.rows.length) {
    const bad = n.rows.filter(r => !r.ok).length;
    if (bad) {
      out.push({ level: "P2", text: `近期 ${bad} 封邮件发送失败`, sub: "告警可能没送达，去系统与邮件页看错误详情", route: ROUTES.notify, source: "/api/notify" });
    }
  }

  out.sort((a, b) => LVL_ORDER[a.level] - LVL_ORDER[b.level]);
  return out;
}

/* 一键体检：把行动中枢依赖的域全部强制刷一遍 */
export async function runCheckup() {
  const { refresh } = await import('../store');
  await refresh("overview", "signal", "lhb", "paper", "weekly", "status", "weeklyHealth", "notify");
}
