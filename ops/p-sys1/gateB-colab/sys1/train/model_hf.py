#!/usr/bin/env python3
"""hf 後端：Hugging Face 編碼器（mmBERT / mDeBERTa / XLM-R / EuroBERT …）＋ catalog.HEADS 定義的頭。torch + transformers。

- 所有候選同一組頭、同一個 budget.json（handoff §4 規則 4）。
- 句子頭讀 masked mean pooling；pair 頭讀（句子, 片段）成對編碼的 pooling。推論時句子與 pair 序列併成一個 batch，一次前向。
- 支援 CPU 與 ROCm（torch 在 ROCm 上同樣用 torch.cuda）；--attn sdpa|eager（ROCm 上 ModernBERT/mmBERT 的 flash-attn 未驗證，預設 sdpa）。
- `tiny-random-bert`：本機以設定檔建立的隨機初始化小 BERT（字元詞表取自訓練資料），不需下載，只用來在 VM 上自測 torch 路徑。
"""
from __future__ import annotations
import inspect, json, math, os, random
import torch
from torch import nn
import catalog as C

TINY = "tiny-random-bert"
import transformers as _tf
if int(_tf.__version__.split(".")[0]) not in (4, 5): raise SystemExit(f"[hf] untested transformers {_tf.__version__} (expected 4.x/5.x)")


class Heads(nn.Module):
    def __init__(self, hidden, dropout):
        super().__init__(); H = C.HEADS; self.drop = nn.Dropout(dropout)
        self.sent = nn.Linear(hidden, H["safety"] + H["emergency"] + H["intent"] + H["concern"])
        self.role = nn.Linear(hidden, H["role"]); self.avoid = nn.Linear(hidden, H["avoid"])


def _pool(h, mask):
    m = mask.unsqueeze(-1).to(h.dtype); return (h * m).sum(1) / m.sum(1).clamp(min=1.0)


def _tiny(out_dir, texts):
    from transformers import BertConfig, BertModel
    try: from transformers import BertTokenizerFast as BT
    except ImportError: from transformers import BertTokenizer as BT
    os.makedirs(out_dir, exist_ok=True)
    chars = sorted({ch for t in texts for ch in t if not ch.isspace()})
    cont = sorted({"##" + ch for t in texts for ch in t if ch.isascii() and ch.isalnum()})   # wordpieces so "100元"/"NT$" are not all [UNK]
    with open(os.path.join(out_dir, "vocab.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"] + chars + cont) + "\n")
    tok = BT(vocab_file=os.path.join(out_dir, "vocab.txt"), do_lower_case=False, tokenize_chinese_chars=True)
    cfg = BertConfig(vocab_size=len(tok.vocab), hidden_size=64, num_hidden_layers=2, num_attention_heads=2, intermediate_size=128, max_position_embeddings=256)
    return BertModel(cfg), tok


class HfBackend:
    name = "hf"

    def __init__(self, enc, tok, heads, max_len, device, dtype=None):
        self.enc, self.tok, self.heads, self.max_len, self.device, self.dtype = enc, tok, heads, max_len, device, dtype
        self._keys = set(inspect.signature(enc.forward).parameters)

    @staticmethod
    def device_auto(pref="auto"):
        if pref != "auto": return torch.device(pref)
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")

    @classmethod
    def create(cls, base, budget, attn, device, texts, workdir, trust_remote_code=False):
        from transformers import AutoModel, AutoTokenizer
        if base == TINY: enc, tok = _tiny(os.path.join(workdir, "_tiny_tok"), texts)
        else:
            tok = AutoTokenizer.from_pretrained(base, trust_remote_code=trust_remote_code)
            kw = {"attn_implementation": attn} if attn else {}
            kw["trust_remote_code"] = trust_remote_code
            try: enc = AutoModel.from_pretrained(base, **kw)
            except (ValueError, TypeError) as e:                       # some archs reject attn_implementation=sdpa → eager, recorded in meta
                print(f"[hf] attn_implementation={attn} rejected ({e}); falling back to eager"); enc = AutoModel.from_pretrained(base, attn_implementation="eager", trust_remote_code=trust_remote_code)
        enc = enc.float()                     # fp32 master weights (transformers 5 may load the checkpoint dtype, e.g. bf16); bf16 only via autocast
        heads = Heads(enc.config.hidden_size, budget["dropout"])
        dt = torch.bfloat16 if (budget.get("bf16_on_gpu") and device.type == "cuda") else None
        return cls(enc.to(device), tok, heads.to(device), budget["max_len"], device, dt)

    # ------------------------------------------------------------------ encoding
    def _encode(self, singles, pairs):
        """singles: [text]; pairs: [(text, arg)] → pooled [len(singles)+len(pairs), H]（一次前向）＋截斷數。"""
        feats, trunc = [], 0
        for t in singles:
            e = self.tok(t, truncation=True, max_length=self.max_len); feats.append(e)
            trunc += len(self.tok(t)["input_ids"]) > self.max_len
        for t, a in pairs:
            e = self.tok(t, a, truncation="only_first", max_length=self.max_len); feats.append(e)
            trunc += len(self.tok(t, a)["input_ids"]) > self.max_len
        b = self.tok.pad([{k: v for k, v in f.items() if k in ("input_ids", "attention_mask", "token_type_ids")} for f in feats], return_tensors="pt")
        mx = int(b["attention_mask"].sum(1).max())
        b = {k: v.to(self.device) for k, v in b.items() if k in self._keys}
        if self.dtype is not None and self.device.type == "cuda":
            with torch.autocast("cuda", dtype=self.dtype): h = self.enc(**b).last_hidden_state
        else: h = self.enc(**b).last_hidden_state
        return _pool(h.float(), b["attention_mask"]), trunc, mx

    # ------------------------------------------------------------------ training
    def fit(self, examples, budget, seed, log=print):
        random.seed(seed); torch.manual_seed(seed)
        H = C.HEADS; nb = 2 + H["intent"]; bs = budget["batch"]
        steps = budget["epochs"] * math.ceil(len(examples) / bs); warm = int(budget["warmup_frac"] * steps)
        opt = torch.optim.AdamW([{"params": self.enc.parameters(), "lr": budget["lr"]}, {"params": self.heads.parameters(), "lr": budget["head_lr"]}],
                                weight_decay=budget["weight_decay"])
        sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: (s + 1) / max(1, warm) if s < warm else max(0.0, (steps - s) / max(1, steps - warm)))
        bce, ce = nn.BCEWithLogitsLoss(), nn.CrossEntropyLoss(); self.enc.train(); self.heads.train(); step = 0
        for ep in range(budget["epochs"]):
            order = list(range(len(examples))); random.shuffle(order)
            for i in range(0, len(order), bs):
                ex = [examples[j] for j in order[i:i + bs]]
                pr = [(e["text"], a, h, l) for e in ex for h, a, l in e["pairs"]]
                pooled, _, _ = self._encode([e["text"] for e in ex], [(t, a) for t, a, _, _ in pr])
                zs = self.heads.sent(self.heads.drop(pooled[:len(ex)]))
                ys = torch.tensor([[e["y"]["safety"], e["y"]["emergency"]] + e["y"]["intent"] for e in ex], device=self.device)
                yc = torch.tensor([e["y"]["concern"] for e in ex], device=self.device)
                loss = bce(zs[:, :nb], ys) + ce(zs[:, nb:], yc)
                pp = pooled[len(ex):]
                ri = [k for k, p in enumerate(pr) if p[2] == "role"]; ai = [k for k, p in enumerate(pr) if p[2] == "avoid"]
                if ri: loss = loss + ce(self.heads.role(self.heads.drop(pp[ri])), torch.tensor([int(pr[k][3]) for k in ri], device=self.device))
                if ai: loss = loss + bce(self.heads.avoid(self.heads.drop(pp[ai])).squeeze(-1), torch.tensor([float(pr[k][3]) for k in ai], device=self.device))
                opt.zero_grad(); loss.backward()
                torch.nn.utils.clip_grad_norm_(list(self.enc.parameters()) + list(self.heads.parameters()), budget["grad_clip"])
                opt.step(); sched.step(); step += 1
                if step % max(1, steps // 20) == 0: log(f"[hf] epoch {ep + 1}/{budget['epochs']} step {step}/{steps} loss {loss.item():.4f}")
        self.enc.eval(); self.heads.eval()

    # ------------------------------------------------------------------ inference
    @torch.no_grad()
    def logits(self, items):
        self.enc.eval(); self.heads.eval(); H = C.HEADS; nb = 2 + H["intent"]
        singles = [t for t, _ in items]; pairs = [(t, h, a) for t, ps in items for h, a in ps]
        pooled, trunc, mx = self._encode(singles, [(t, a) for t, _, a in pairs])
        zs = self.heads.sent(pooled[:len(items)]).cpu().tolist(); pp = pooled[len(items):]; out, k = [], 0
        for i, (t, ps) in enumerate(items):
            pl = []
            for h, _ in ps:
                pl.append(getattr(self.heads, h)(pp[k:k + 1])[0].cpu().tolist()); k += 1
            z = zs[i]; out.append({"safety": z[0], "emergency": z[1], "intent": z[2:nb], "concern": z[nb:], "pairs": pl})
        return out, {"truncated": trunc, "max_tokens": mx}

    def save(self, out):
        d = os.path.join(out, "encoder"); self.enc.save_pretrained(d); self.tok.save_pretrained(d)
        torch.save(self.heads.state_dict(), os.path.join(out, "heads.pt"))

    @classmethod
    def load(cls, ckpt, meta, device="auto"):
        from transformers import AutoModel, AutoTokenizer
        dev = cls.device_auto(device); d = os.path.join(ckpt, "encoder")
        trc = bool(meta.get("trust_remote_code"))
        tok = AutoTokenizer.from_pretrained(d, trust_remote_code=trc)
        attn = meta.get("attn_used")
        enc = AutoModel.from_pretrained(d, trust_remote_code=trc, **({"attn_implementation": attn} if attn else {}))
        enc = enc.float()
        heads = Heads(enc.config.hidden_size, 0.0); heads.load_state_dict(torch.load(os.path.join(ckpt, "heads.pt"), map_location="cpu"))
        dt = torch.bfloat16 if (meta["budget"].get("bf16_on_gpu") and dev.type == "cuda") else None
        return cls(enc.to(dev).eval(), tok, heads.to(dev).eval(), meta["budget"]["max_len"], dev, dt)

    def attn_used(self):
        return getattr(self.enc.config, "_attn_implementation", None)

    @staticmethod
    def versions():
        import transformers
        v = {"torch": torch.__version__, "transformers": transformers.__version__, "cuda_available": torch.cuda.is_available(),
             "hip": getattr(torch.version, "hip", None), "cuda": torch.version.cuda}
        if torch.cuda.is_available(): v["device_name"] = torch.cuda.get_device_name(0)
        return v
