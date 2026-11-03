#!/usr/bin/env python3
"""train_head — 編碼器＋typed 頭（catalog.HEADS）在 make_train_data 的資料上微調；固定預算（budget.json）；輸出 checkpoint＋cal 上的 temperature。

  python3 sys1/train/train_head.py --base jhu-clsp/mmBERT-base --data sys1/train/data/v1 --seed 13 --out ckpt/mmbert-ft-s13 [--device auto|cpu|cuda] [--attn sdpa]
  python3 sys1/train/train_head.py --base np-tiny ...            # numpy 接線自測後端（不是候選）
  python3 sys1/train/train_head.py --base tiny-random-bert ...   # torch 路徑自測（隨機小 BERT，不下載）

規則：
- 不讀 gate_data test、extra_safety、router_gold(_v2)；只用 --data 的 train/dev，與 gate_data cal（只做 temperature scaling，單調轉換，不改排序）。
- 先驗證 --data 的 SHA256SUMS 與 provenance.json 的 exclusion.exact_hits == 0，否則拒絕訓練。
- 預算只能來自 budget.json（不提供覆寫參數）；meta.json 記錄 budget SHA-256、資料 SHA-256、套件版本、裝置、attn 實作。
"""
from __future__ import annotations
import argparse, hashlib, json, math, os, platform, sys, time

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import catalog as C   # noqa: E402

BUDGET = os.path.join(HERE, "budget.json")


def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()


def load_data(d):
    for line in open(os.path.join(d, "SHA256SUMS"), encoding="utf-8"):
        h, name = line.split()
        if sha(os.path.join(d, name)) != h: sys.exit(f"[train] {d}/{name} 與 SHA256SUMS 不符，拒絕訓練")
    prov = json.load(open(os.path.join(d, "provenance.json"), encoding="utf-8"))
    if prov.get("exclusion", {}).get("exact_hits") != 0: sys.exit("[train] provenance.json 沒有通過排除檢查的紀錄，拒絕訓練")
    rd = lambda n: [json.loads(l) for l in open(os.path.join(d, n), encoding="utf-8") if l.strip()]
    return rd("train.jsonl"), rd("dev.jsonl"), prov


def examples(rows):
    return [{"text": r["text"], "y": C.sentence_targets(r), "pairs": C.pair_examples(r)} for r in rows]


def items_of(ex): return [(e["text"], [(h, a) for h, a, _ in e["pairs"]]) for e in ex]


def _sig(x): return 1 / (1 + math.exp(-max(-60, min(60, x))))


def _softmax(z, T=1.0):
    m = max(z); e = [math.exp((v - m) / T) for v in z]; s = sum(e); return [v / s for v in e]


def collect_logits(be, ex, bs=64):
    L = []
    for i in range(0, len(ex), bs):
        out, _ = be.logits(items_of(ex[i:i + bs])); L += out
    return L


def nll_bin(z, y, T): p = min(max(_sig(z / T), 1e-7), 1 - 1e-7); return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def nll_cls(z, y, T): return -math.log(max(_softmax(z, T)[y], 1e-12))


def groups(ex, L):
    """每個頭群組的 (logit, label, kind) 清單；temperature 一個頭群組一個。"""
    G = {k: [] for k in C.HEADS}
    for e, l in zip(ex, L):
        G["safety"].append((l["safety"], e["y"]["safety"], "b")); G["emergency"].append((l["emergency"], e["y"]["emergency"], "b"))
        G["intent"] += [(z, y, "b") for z, y in zip(l["intent"], e["y"]["intent"])]; G["concern"].append((l["concern"], e["y"]["concern"], "c"))
        for (h, _, y), z in zip(e["pairs"], l["pairs"]):
            G[h].append((z[0], y, "b") if h == "avoid" else (z, int(y), "c"))
    return G


def fit_temperature(G):
    grid = [round(0.25 * 1.1 ** k, 4) for k in range(40)]; T, rep = {}, {}
    for h, rows in G.items():
        if not rows: T[h] = 1.0; rep[h] = {"n": 0}; continue
        f = lambda t: sum((nll_bin if k == "b" else nll_cls)(z, y, t) for z, y, k in rows) / len(rows)
        best = min(grid, key=f); T[h] = best; rep[h] = {"n": len(rows), "nll_T1": round(f(1.0), 4), "nll_T": round(f(best), 4), "T": best}
    return T, rep


def dev_metrics(ex, L, T):
    n = len(ex); ok = {"safety": 0, "emergency": 0, "intent_exact": 0, "concern": 0}
    for e, l in zip(ex, L):
        ok["safety"] += (_sig(l["safety"] / T["safety"]) >= .5) == bool(e["y"]["safety"])
        ok["emergency"] += (_sig(l["emergency"] / T["emergency"]) >= .5) == bool(e["y"]["emergency"])
        ok["intent_exact"] += all((_sig(z / T["intent"]) >= .5) == bool(y) for z, y in zip(l["intent"], e["y"]["intent"]))
        ok["concern"] += max(range(len(l["concern"])), key=lambda k: l["concern"][k]) == e["y"]["concern"]
    return {k: round(v / n, 4) for k, v in ok.items()} | {"n": n}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--base", required=True); ap.add_argument("--data", required=True); ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--device", default="auto"); ap.add_argument("--attn", default="sdpa")
    ap.add_argument("--trust-remote-code", action="store_true", help="只給 bases.json 標記需要的基座（例如 EuroBERT）；會執行模型庫的程式碼")
    a = ap.parse_args()
    if os.path.exists(os.path.join(a.out, "meta.json")): sys.exit(f"[train] {a.out} 已有 checkpoint；換新的 --out（不覆蓋）")
    os.makedirs(a.out, exist_ok=True); budget_all = json.load(open(BUDGET, encoding="utf-8")); t0 = time.time()
    tr, dv, prov = load_data(a.data); Etr, Edv = examples(tr), examples(dv); Ecal = examples(C.cal_cases())
    log = lambda *x: (print(*x, flush=True), open(os.path.join(a.out, "train.log"), "a", encoding="utf-8").write(" ".join(map(str, x)) + "\n"))
    if a.base == "np-tiny":
        import model_np as M; budget = budget_all["np"]; be = M.NpBackend(); vers = M.NpBackend.versions(); dev = "cpu"; attn_used = None
    else:
        import model_hf as M; budget = budget_all["hf"]; dev = M.HfBackend.device_auto(a.device)
        be = M.HfBackend.create(a.base, budget, a.attn, dev, [r["text"] for r in tr], a.out, a.trust_remote_code); vers = M.HfBackend.versions(); attn_used = be.attn_used(); dev = str(dev)
    log(f"[train] base={a.base} backend={be.name} seed={a.seed} device={dev} attn={attn_used} train={len(Etr)} dev={len(Edv)} cal={len(Ecal)} budget={budget}")
    be.fit(Etr, budget, a.seed, log)
    Lcal = collect_logits(be, Ecal); T, trep = fit_temperature(groups(Ecal, Lcal))
    Ldv = collect_logits(be, Edv); dm = dev_metrics(Edv, Ldv, T)
    be.save(a.out)
    meta = {"base": a.base, "backend": be.name, "seed": a.seed, "budget": budget, "budget_sha256": sha(BUDGET), "data_dir": os.path.abspath(a.data),
            "data_sha256sums": sha(os.path.join(a.data, "SHA256SUMS")), "data_provenance": prov, "heads": C.HEADS, "labels": {"intent": C.LABELS, "concern": C.CONCERN_LABELS, "role": C.ROLE_LABELS},
            "catalogue_signature": C.K.catalogue_signature(), "temperature": T, "temperature_fit": {"on": "gate_data cal (seed 7, n_per 24)", "per_head": trep},
            "dev_metrics": dm, "versions": vers, "python": platform.python_version(), "device": dev, "attn_requested": a.attn if be.name == "hf" else None, "trust_remote_code": bool(a.trust_remote_code), "attn_used": attn_used,
            "train_seconds": round(time.time() - t0, 1), "created": time.strftime("%F %T")}
    json.dump(meta, open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"[train] done in {meta['train_seconds']}s  T={T}  dev={dm}  → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
