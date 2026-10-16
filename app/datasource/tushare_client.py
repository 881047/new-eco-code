"""Tushare Pro 客户端: 限速、限流重试、接口可用性探测"""
import datetime
import json
import logging
import os
import tempfile
import threading
import time

import pandas as pd
import tushare as ts

from app import config
from app import timeutil

log = logging.getLogger(__name__)

# 命中即视为限流(瞬时, 可退避重试)。2026-09-05 审查修复: 去掉"抱歉" —— 权限错误文案
# 恰是"抱歉, 您没有访问该接口的权限", 原会先命中限流分类: 权限错误被重试 6 次(约81秒)后
# RuntimeError 炸断管线, 探测缓存永远记不了 False, AkShare 兜底永不触发。
# 2026-09-05 晚补充: tushare 限流文案实为"频率超限(200次/分钟)", "频率"必须入限流类
# (实测 namechange 全量拉取时误入权限类, 靠失败计数续跑侥幸完成)
_RATE_MSG = ("每分钟最多", "频率")
# 命中即视为积分/权限不足(永久, 重试无意义, 直接放弃; P2-9)
_PERM_MSG = ("积分", "权限", "抱歉")

PROBE_CACHE = config.DATA_DIR / ".probe_cache.json"   # 探测结果落盘, 跨任务复用 (P2-9)
PROBE_TTL_DAYS = 7


class TushareClient:
    # 2026-09-12 审查 P2-20: 限速门改**类级**。原 `self._last` 是实例级的, 而限流是
    # **账号级**的 —— 一个进程里 updater / regime 各自 new 一个 client, 各自都以为
    # "我还没发过请求", 并发时实际频率就是 实例数 × 187 次/分, 一起撞 200/分 的墙。
    # 锁保证「取下一个发送时隙」是原子的: 任意 60s 窗口内的请求数都不超过 1/min_interval。
    _rate_lock = threading.Lock()
    _last_call = 0.0

    def __init__(self):
        if not config.TUSHARE_TOKEN:
            raise RuntimeError("缺少 TUSHARE_TOKEN，请检查 .env")
        self.pro = ts.pro_api(config.TUSHARE_TOKEN)
        # 2026-09-07 全面检查: 免费档限流为"频率超限(200次/分钟)", 0.21s≈285 次/分必触发,
        # 全量拉取靠退避重试侥幸完成(0905 namechange 实测) —— 改 0.32s≈187 次/分留余量
        self.min_interval = 0.32
        self.last_status = "ok"   # call() 每次更新: ok/empty/perm/error
        self._probe = {}
        self._load_probe_cache()

    def _wait(self) -> None:
        """等到下一个发送时隙并**预留**它。持锁睡是刻意的: 把等待串行化, 后来者排在前一个
        时隙之后 —— 这正是限速器该有的语义(而不是各自算各自的, 然后同时发出去)。
        时隙在**发送前**预留(原实现是收到响应后才记时间), 保证间隔 >= min_interval;
        响应比 min_interval 还慢时自然更慢, 不会更密。"""
        with TushareClient._rate_lock:
            wait = TushareClient._last_call + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            TushareClient._last_call = time.time()

    def _load_probe_cache(self) -> None:
        try:
            if not PROBE_CACHE.exists():
                return
            data = json.loads(PROBE_CACHE.read_text(encoding="utf-8"))
            # 2026-09-15：TTL 基准日与写入口同源（东八区），否则换时区部署时
            # "7 天"会在午夜附近被算成 6 或 8 天
            cutoff = (timeutil.today() - datetime.timedelta(days=PROBE_TTL_DAYS)).isoformat()
            for k, v in data.items():
                if isinstance(v, list) and len(v) == 2 and v[1] >= cutoff:
                    self._probe[k] = bool(v[0])
        except Exception:
            # 2026-09-15（外部审查 P1-4）：原为静默 `pass` —— 磁盘满/JSON 截断/权限错
            # 全都被吞掉，表现为"缓存莫名其妙不生效、每次都重新探测"，而这是**数据服务
            # 状态异常**的信号。留痕但不阻断：探测缓存只是加速项，读不到就重新探测。
            log.warning("接口探测缓存读取失败, 本次将重新探测可用性: %s", PROBE_CACHE, exc_info=True)

    def _save_probe_cache(self, key: str | None = None) -> None:
        """key 指定时只更新该条, 其余条目保留原日期 —— 防止任意一次保存给全部条目续期"""
        try:
            data = {}
            if PROBE_CACHE.exists():
                try:
                    data = json.loads(PROBE_CACHE.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            today = timeutil.today().isoformat()
            if key is not None:
                data[key] = [bool(self._probe.get(key, False)), today]
            else:
                for k, v in self._probe.items():
                    data[k] = [bool(v), today]
            # 2026-09-05 审查修复: 原 write_text 直接落盘, 与不走任务锁的脚本进程并发时
            # 整文件读改写会互相覆盖(陈旧 True 复活, 权限降级最长 7 天不被发现);
            # 改为临时文件 + os.replace 原子替换(与 config.save_config 同模式)
            fd, tmp = tempfile.mkstemp(dir=str(PROBE_CACHE.parent), prefix="probe.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, PROBE_CACHE)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except Exception:
            # 同上（P1-4 的另一半）：保存失败必须留痕 —— 否则"权限降级 7 天不被发现"
            # 的复发路径就是从这里静默打开的（见本文件 2026-09-05 修复注释）
            log.warning("接口探测缓存保存失败(不影响本次调用): %s", PROBE_CACHE, exc_info=True)

    def call(self, api_name: str, **params) -> pd.DataFrame:
        """带限速与限流重试的接口调用; 积分不足等永久错误直接放弃, 返回空 DataFrame。
        2026-09-04 审查 P1-10: 返回空表时用 self.last_status 区分三种情形,
        调用方据此决定「计入空数据 streak / 中止接口 / 标记待补下次续跑」:
          'empty' 确定无数据(接口成功返回空)  'perm' 积分/权限不足(永久)
          'error' 网络/服务异常(已有限重试, 下次续跑)  'ok' 有数据"""
        self.last_status = "ok"
        for attempt in range(6):
            self._wait()
            try:
                df = self.pro.query(api_name, **params)
                if df is None:
                    self.last_status = "empty"
                    return pd.DataFrame()
                self.last_status = "ok" if not df.empty else "empty"
                return df
            except Exception as e:
                msg = str(e)
                # 限流文案优先判定: 避免"积分不足将每分钟限流"这类混合文案被误判为永久错误
                if any(k in msg for k in _RATE_MSG):
                    sleep = min(65, 3 * (attempt + 2))
                    log.warning("接口 %s 限流，%ds 后重试: %s", api_name, sleep, msg)
                    time.sleep(sleep)
                    continue
                if any(k in msg for k in _PERM_MSG):
                    log.error("接口 %s 权限/积分不足, 直接放弃(不重试): %s", api_name, msg)
                    # 永久错误记入探测缓存为不可用(2026-09-04 修复): 原先不落盘也不失效缓存,
                    # 7 天探测缓存会掩盖权限降级, AkShare 兜底被跳过、数据静默停更
                    self._probe[api_name] = False
                    self._save_probe_cache(api_name)
                    self.last_status = "perm"
                    return pd.DataFrame()
                # 网络/服务异常: 有限重试(较短退避, 区别于限流), 仍未恢复标记 error 待补
                if attempt < 2:
                    log.warning("接口 %s 异常, 3s 后重试(%d/2): %s", api_name, attempt + 1, msg)
                    time.sleep(3)
                    continue
                log.error("接口 %s 异常(重试后仍失败), 标记待补(下次续跑): %s", api_name, msg)
                self.last_status = "error"
                return pd.DataFrame()
        raise RuntimeError(f"{api_name} 重试耗尽")

    def available(self, api_name: str, **probe_params) -> bool:
        """探测接口是否可用(是否有足够积分); 成功结果落盘缓存 7 天跨任务复用 (P2-9)。
        瞬时失败不落盘(配额文案会误伤), 只在进程内缓存到本次任务结束;
        永久权限错误由 call() 落盘为不可用。"""
        if api_name in self._probe:
            return self._probe[api_name]
        df = self.call(api_name, **probe_params)
        ok = df is not None and not df.empty
        self._probe[api_name] = ok
        if ok:
            self._save_probe_cache(api_name)
        log.info("接口 %s 可用性: %s", api_name, ok)
        return ok
