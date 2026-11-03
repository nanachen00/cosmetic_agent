#!/usr/bin/env python3
"""np-tiny 後端：雜湊字元 n-gram + 線性頭（numpy/scipy；無 torch）。

用途只有一個：在沒有 torch 的環境把「產資料→訓練→serve→probe→collect→analyze」整條接線跑通（handoff T4 DoD 的 tiny 模型）。
它不是候選：不得進 registry 當替換候選，數字沒有意義。真實基座一律走 model_hf.py（同頭、同預算）。
"""
from __future__ import annotations
import json, os, re, zlib
import numpy as np
from scipy import sparse
import catalog as C

DIM = 1 << 14
_REV = {v: k for k, v in C.ING_ZH.items()}


def _grams(s, tag, n=(1, 2, 3)):
    return [f"{tag}{s[i:i + k]}" for k in n for i in range(len(s) - k + 1)]


def _idx(feats):
    return sorted({zlib.crc32(f.encode()) % DIM for f in feats})


def sent_feats(text):
    return _idx(_grams(text, "S:"))


def pair_feats(text, head, arg):
    f = _grams(arg, f"{head}A:")
    pos = text.find(arg); L = len(arg)
    if pos < 0 and arg in _REV:                                   # avoid: canonical name (A酸) may appear as a variant (視黃醇)
        m = re.search(C.P2.ING_PAT[_REV[arg]], text, re.I)
        if m: pos, L = m.start(), m.end() - m.start()
    if pos >= 0:
        f += _grams(text[max(0, pos - 6):pos], f"{head}L:") + _grams(text[pos + L:pos + L + 6], f"{head}R:")
    else:
        f += [f"{head}:notfound"]
    return _idx(f)


def _mat(rows):
    data, ind, ptr = [], [], [0]
    for r in rows: ind += r; data += [1.0 / max(1, len(r)) ** 0.5] * len(r); ptr.append(len(ind))
    return sparse.csr_matrix((np.array(data, np.float32), np.array(ind, np.int64), np.array(ptr, np.int64)), shape=(len(rows), DIM))


def _sig(x): return 1 / (1 + np.exp(-x))


def _softmax(x):
    e = np.exp(x - x.max(1, keepdims=True)); return e / e.sum(1, keepdims=True)


class NpBackend:
    name = "np-tiny"

    def __init__(self, W=None):
        H = C.HEADS
        self.W = W or {"sent": np.zeros((DIM, H["safety"] + H["emergency"] + H["intent"] + H["concern"]), np.float32),
                       "role": np.zeros((DIM, H["role"]), np.float32), "avoid": np.zeros((DIM, 1), np.float32)}

    # ------------------------------------------------------------------ training (full-batch Adam, fixed budget)
    def fit(self, examples, budget, seed, log=print):
        rng = np.random.default_rng(seed); H = C.HEADS
        Xs = _mat([sent_feats(e["text"]) for e in examples])
        ys = np.array([[e["y"]["safety"], e["y"]["emergency"]] + e["y"]["intent"] for e in examples], np.float32)
        yc = np.array([e["y"]["concern"] for e in examples])
        R = [(e["text"], a, l) for e in examples for h, a, l in e["pairs"] if h == "role"]
        A = [(e["text"], a, l) for e in examples for h, a, l in e["pairs"] if h == "avoid"]
        Xr = _mat([pair_feats(t, "role", a) for t, a, _ in R]) if R else None; yr = np.array([l for *_, l in R], int)
        Xa = _mat([pair_feats(t, "avoid", a) for t, a, _ in A]) if A else None; ya = np.array([[l] for *_, l in A], np.float32)
        for k in self.W: self.W[k] = (rng.standard_normal(self.W[k].shape) * 0.01).astype(np.float32)
        m = {k: np.zeros_like(v) for k, v in self.W.items()}; v2 = {k: np.zeros_like(v) for k, v in self.W.items()}
        lr, l2, b1, b2 = budget["lr"], budget["l2"], 0.9, 0.999; nb = 2 + H["intent"]
        for step in range(1, budget["steps"] + 1):
            Z = Xs @ self.W["sent"]; G = {}
            gb = (_sig(Z[:, :nb]) - ys) / len(examples)
            P = _softmax(Z[:, nb:]); P[np.arange(len(yc)), yc] -= 1; gc = P / len(examples)
            G["sent"] = np.asarray(Xs.T @ np.hstack([gb, gc]))
            if Xr is not None:
                Pr = _softmax(np.asarray(Xr @ self.W["role"])); Pr[np.arange(len(yr)), yr] -= 1; G["role"] = np.asarray(Xr.T @ (Pr / len(yr)))
            else: G["role"] = np.zeros_like(self.W["role"])
            if Xa is not None: G["avoid"] = np.asarray(Xa.T @ ((_sig(np.asarray(Xa @ self.W["avoid"])) - ya) / len(ya)))
            else: G["avoid"] = np.zeros_like(self.W["avoid"])
            for k in self.W:
                g = G[k] + l2 * self.W[k]; m[k] = b1 * m[k] + (1 - b1) * g; v2[k] = b2 * v2[k] + (1 - b2) * g * g
                self.W[k] -= lr * (m[k] / (1 - b1 ** step)) / (np.sqrt(v2[k] / (1 - b2 ** step)) + 1e-8)
            if step % max(1, budget["steps"] // 5) == 0: log(f"[np-tiny] step {step}/{budget['steps']}")

    # ------------------------------------------------------------------ inference
    def logits(self, items):
        """items: [(text, [(head, arg), ...])] → [{"safety","emergency","intent":[6],"concern":[14],"pairs":[[...]]}]"""
        H = C.HEADS; nb = 2 + H["intent"]; out = []
        Z = np.asarray(_mat([sent_feats(t) for t, _ in items]) @ self.W["sent"])
        for i, (t, pairs) in enumerate(items):
            z = Z[i]; pl = []
            for h, a in pairs:
                x = _mat([pair_feats(t, h, a)]); pl.append(np.asarray(x @ self.W[h])[0].tolist())
            out.append({"safety": float(z[0]), "emergency": float(z[1]), "intent": z[2:nb].tolist(), "concern": z[nb:].tolist(), "pairs": pl})
        return out, {"truncated": 0, "max_tokens": max(len(t) for t, _ in items)}

    def save(self, out):
        np.savez_compressed(os.path.join(out, "np_tiny.npz"), **self.W)

    @classmethod
    def load(cls, ckpt, meta):
        z = np.load(os.path.join(ckpt, "np_tiny.npz")); return cls({k: z[k] for k in z.files})

    @staticmethod
    def versions():
        import scipy; return {"numpy": np.__version__, "scipy": scipy.__version__}
