#!/usr/bin/env python3
"""laya_ctx — 估算 P+Sys1 題組放進 Laya context 的 token 數（handoff T3：ModernBERT 512 / mmBERT 1024；放不下就報告，不截斷）。

Laya 把題目（instructions + 選項描述）和句子一起編碼；確切的渲染格式在 Laya 的程式裡（model card 只提到 noul 以 `false:`/`true:` 呈現）。
本工具用近似渲染「句子 + 題目 + 每個選項 key: 描述」計 token，只用來判斷『有沒有明顯放不下』；實際以 contract_probe.py 的回應為準。
句子來源：make_train_data 的模板句（不碰 gate_data/hold-out）。

  python3 sys1/laya_ctx.py --tokenizer jhu-clsp/mmBERT-base --ctx 1024
  python3 sys1/laya_ctx.py --tokenizer answerdotai/ModernBERT-large --ctx 512    # typed-decisions 檢查點是 1024，根英文檢查點 512
"""
import argparse, os, random, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(HERE, "train"))
import catalog as C   # noqa: E402
import gateb as GB    # noqa: E402
import make_train_data as MD   # noqa: E402


def render(state, q):
    lines = [state, q["instructions"]] + [f"{k}: {v}" for k, v in q["criteria"].items()]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--tokenizer", required=True); ap.add_argument("--ctx", type=int, required=True); ap.add_argument("--n", type=int, default=200)
    a = ap.parse_args()
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.tokenizer); rows = MD.build(11, 60); random.Random(0).shuffle(rows)
    per_q, bundle_sum, over = {}, [], []
    for r in rows[:a.n]:
        q = GB.pk_bundle(C.P2.parse2(r["text"])); tot = 0
        for name, qq in q.items():
            n = len(tok(render(r["text"], qq))["input_ids"]); key = name.split("_")[0] if name.startswith(("role_", "avoid_")) else name
            per_q[key] = max(per_q.get(key, 0), n); tot += n
            if n > a.ctx: over.append((name, n, r["text"]))
        bundle_sum.append(tot)
    print(f"tokenizer {a.tokenizer} · ctx {a.ctx} · {min(a.n, len(rows))} 句模板句")
    print("每題（句子+題目+選項）最大 token 數：")
    for k, v in sorted(per_q.items(), key=lambda kv: -kv[1]): print(f"  {k:<18} {v:>5} {'超過 ctx ✗' if v > a.ctx else ''}")
    bs = sorted(bundle_sum); print(f"整組題目逐題相加：中位 {bs[len(bs) // 2]} / 最大 {bs[-1]}（若伺服器把整組塞進同一個 context，這是需要的量）")
    print("結論：" + ("每一題單獨都放得進 ctx" if not over else f"{len(over)} 題超過 ctx（不要截斷題組；回報負責人）"))
    return 0 if not over else 6


if __name__ == "__main__":
    sys.exit(main())
