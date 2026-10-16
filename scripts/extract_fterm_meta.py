r"""fterm 生产工件元数据提取（离线）——写到 NEW ECO 看板 sidecar。

来历（2026-10-08 正式化）：门户卡片展示 fterm 因子清单/训练档用的是
``D:\\NEW ECO\\data\\fterm_model_meta.json`` sidecar——NEW ECO 服务进程刻意
不引 torch（其 3.13 venv 被 SAC 拦 torch），所以提取必须用 **fterm 的 .venv312**
跑本脚本（只读工件一次，写 JSON）。**fterm 工件重训/换版后必须重跑本脚本**，
否则看板显示旧训练档（2026-10-06 门户批登记的运维义务）。

用法（任意 cwd）::

    D:\\Final\\.venv312\\Scripts\\python.exe D:\\NEW ECO\\scripts\\extract_fterm_meta.py
"""
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ART = Path(r"D:\Final\data\models\dlv2_production.pt")
OUT = Path(r"D:\NEW ECO\data\fterm_model_meta.json")


def main():
    import torch  # noqa: F401  # 仅为了 torch.load 反序列化（本脚本必须在 .venv312 下跑）

    payload = torch.load(ART, weights_only=False, map_location="cpu")
    if payload.get("kind") != "dlv2_production":
        raise SystemExit(f"工件类型不符: {payload.get('kind')}")
    sd = payload["state_dicts"]
    h = hashlib.sha256(ART.read_bytes()).hexdigest()
    out = {
        "trained_at": payload.get("trained_at"),
        "feature_cols": list(payload.get("feature_cols") or []),
        "n_embed_features": payload.get("n_embed_features"),
        "n_nets": len(sd),
        "n_samples": payload.get("n_samples"),
        "seeds": [s.get("seed") for s in sd],
        "val_loss_range": [min(s["val_loss"] for s in sd), max(s["val_loss"] for s in sd)]
        if sd else None,
        "sha256": h,
        "loss": "mse+ranknet(lam=1)" if len(sd) else None,
        "source_mtime": datetime.fromtimestamp(ART.stat().st_mtime).isoformat(timespec="seconds"),
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "source_artifact": str(ART),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"sidecar → {OUT}（{out['n_nets']} 网, sha256 {h[:12]}, trained_at {out['trained_at']}）")


if __name__ == "__main__":
    sys.exit(main())
