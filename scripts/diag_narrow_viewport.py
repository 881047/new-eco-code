"""真视口取证器：按**启动窗口尺寸**渲染看板，量"有没有横向溢出"，并定位最宽元素。

## 为什么需要它（2026-09-14 实测教训，两次都被它救回来）

`msedge --headless --window-size=390,1400 --screenshot` **不能**给出 390px 的布局视口 ——
本机 Edge 的最小窗口宽度是 **504px**（实测 `--window-size=390` ⇒ `window.innerWidth == 504`）⇒
那张"右侧被裁"的截图其实是 **504px 布局被裁到 390 宽**。两条判定都用过：
  ① 页面里临时加 `@media(max-width:400px){body{outline:10px solid magenta}}`：390 与 480 两张截图
     **都没有紫色** ⇒ 媒体查询从未命中 ⇒ 视口 > 400（截图宽度 ≠ 布局视口）；
  ② 本脚本直接 `Runtime.evaluate` 读 `window.innerWidth` ⇒ 拿到真实值 504。
iframe 也不行：本服务发 `X-Frame-Options: DENY` + `frame-ancestors 'none'`（安全姿态正确，不动它）。
`Emulation.setDeviceMetricsOverride` 在本机 Edge 153 上**时灵时不灵**（同一条命令两次跑：一次 390、
一次 643）⇒ 本脚本**不用**它，只用"启动窗口尺寸 + 回读真实 innerWidth"这条可复现的路。

**判据**：`document.documentElement.scrollWidth == window.innerWidth`（页面自身不横向溢出）；
某元素 `right > innerWidth` 只有在**可滚动容器里**才正常（如 `#lhbTable.scroll-box` 内的宽表）。

## 用法（服务需在跑）

    D:\\Python313\\python.exe -X utf8 scripts\\diag_narrow_viewport.py
    D:\\Python313\\python.exe -X utf8 scripts\\diag_narrow_viewport.py --widths 504,768,1440

每个宽度起一个独立浏览器窗口 → 打开看板 → 等首屏 fetch 落位 → 回读真实视口 → 量溢出 → 存整页截图
`logs/_vp_<真实宽度>.png`。**任何宽度溢出 ⇒ 退出码 1**（可当门禁）；取证失败 ⇒ 退出码 2（显式失败，
绝不静默给旧数）。
"""
import argparse
import base64
import json
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
LOG_DIR = BASE / "logs"

# 端口单一口径（2026-09-22）：原先这里写死 8787，与 app/notify.py / 看门狗 / start.bat 各写一份。
import sys                                                          # noqa: E402
sys.path.insert(0, str(BASE))                                       # noqa: E402
from app import config as cfg_mod                                    # noqa: E402
_DEFAULT_URL = f"http://127.0.0.1:{cfg_mod.server_port()}/"

PROBE_JS = """
(() => {
  const vw = window.innerWidth;
  const de = document.documentElement;
  const bad = [];
  document.querySelectorAll("body *").forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > vw + 1 && el.tagName !== "HTML") {
      const cls = (typeof el.className === "string" && el.className.trim())
        ? "." + el.className.trim().split(/\\s+/).join(".") : "";
      bad.push({sel: el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + cls,
                right: Math.round(r.right), w: Math.round(r.width)});
    }
  });
  bad.sort((a, b) => b.right - a.right);
  const seen = {};
  const worst = bad.filter(x => (seen[x.sel] = (seen[x.sel] || 0) + 1) <= 2).slice(0, 6);
  return {vw, scrollWidth: de.scrollWidth, bodyW: document.body.scrollWidth,
          overflow: de.scrollWidth > vw + 1, worst};
})()
"""


def _free_port() -> int:
    """每次取一个空闲端口 —— 固定端口会被**上一次没退干净的 Edge** 占着，于是新连接挂到旧浏览器上、
    量出来的是别的窗口（工具自身出过一次这种错，所以现在必须回读 innerWidth 核对）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _call(ws, mid: int, method: str, params=None) -> dict:
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


def _measure(url: str, width: int, height: int, settle: float) -> tuple:
    """起一个窗口 → 打开看板 → 回读真实视口并量溢出。返回 (info, png_bytes)。"""
    from websockets.sync.client import connect
    port = _free_port()
    tmp = tempfile.mkdtemp(prefix="neweco_vp_")
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--window-size={width},{height}",
         f"--remote-debugging-port={port}", f"--user-data-dir={tmp}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # 不闪窗（07 号 #27 纪律）
    try:
        ws_url, mid = None, 1
        for _ in range(60):
            try:
                r = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2)
                pages = [t for t in r.json() if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001,S110 - 启动期轮询: 连不上就重试(故意静默)
                pass
            time.sleep(0.5)
        if not ws_url:
            raise RuntimeError("连不上 CDP（Edge 起不来?）")
        with connect(ws_url, open_timeout=10, close_timeout=5, max_size=None) as ws:
            _call(ws, mid, "Page.enable"); mid += 1
            _call(ws, mid, "Page.navigate", {"url": url}); mid += 1
            time.sleep(settle)
            res = _call(ws, mid, "Runtime.evaluate",
                        {"expression": PROBE_JS, "returnByValue": True}); mid += 1
            info = res.get("result", {}).get("value") or {}
            shot = _call(ws, mid, "Page.captureScreenshot",
                         {"format": "png", "captureBeyondViewport": True}); mid += 1
            return info, base64.b64decode(shot["data"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - 退出清理: 超时就硬杀
            proc.kill()
        # 2026-09-25 补：本 `finally` 原来**只杀浏览器进程、不删临时目录** ⇒
        # 本机 `%TEMP%` 已留 11 个 `neweco_vp_*`。同族第 8 处（07 号 #64）：
        # **`finally` 里清了进程就要连目录一起清** —— 半个 finally 等于没有 finally。
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=_DEFAULT_URL)
    ap.add_argument("--widths", default="390,560,768,1440")
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--settle", type=float, default=6.0, help="等首屏 fetch 落位的秒数")
    args = ap.parse_args()
    widths = [int(w) for w in args.widths.split(",") if w.strip()]
    if not EDGE.exists():
        print(f"[err] 找不到 Edge: {EDGE}")
        return 2
    LOG_DIR.mkdir(exist_ok=True)
    rc = 0
    for w in widths:
        try:
            info, png = _measure(args.url, w, args.height, args.settle)
        except Exception as e:  # noqa: BLE001 - 取证失败必须显式失败, 不许静默给旧数
            print(f"[请求 {w:>4}px] ✗ 取证失败: {type(e).__name__}: {e}")
            rc = 2
            continue
        real = int(info.get("vw") or 0)
        out = LOG_DIR / f"_vp_{real}.png"
        out.write_bytes(png)
        flag = "溢出!" if info.get("overflow") else "无溢出"
        note = "" if real == w else f"（请求 {w}px，实测视口 {real}px —— Edge 最小窗口宽度）"
        print(f"[请求 {w:>4}px] 实测 {real}px {flag}  scrollWidth={info.get('scrollWidth')} "
              f"截图={out.name}{note}")
        for x in info.get("worst", []):
            print(f"         └ {x['sel'][:80]}  right={x['right']} w={x['w']}")
        if info.get("overflow"):
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
