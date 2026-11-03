#!/usr/bin/env python3
"""fetch_gguf — download one Gemma 4 GGUF (text weights only: no mmproj / MTP draft files) and print its local path + provenance as JSON.
  python fetch_gguf.py --quant q8_0 | q4_0 | 12b-q4_0 | e4b-q4_0 | e4b-q8_0   [--repo owner/name] [--dest /content/models]
The label (= result folder gemma/<label>/) is <size>-<quant>; no size prefix means E2B.
Sources are tried in order; the first repo that exists and holds a matching file wins (recorded in the JSON).
  q8_0      unsloth/gemma-4-E2B-it-GGUF                      (verified Oct 2026: Q8_0 ~5.05 GB, PTQ)
  q4_0      google/gemma-4-E2B-it-qat-q4_0-gguf              (verified Oct 2026: official QAT Q4_0 ~3.35 GB)
  12b-q4_0  google QAT repo if published, else bartowski/gemma-4-12b-it-GGUF (exists; PTQ) — ~6.7 GB, fits a T4
  e4b-*     google QAT repo if published, else unsloth/gemma-4-E4B-it-GGUF
Needs `huggingface_hub` (preinstalled on Colab). HF_TOKEN is used if set."""
import argparse, json, os, re, sys

SOURCES = {
    "q8_0": ["unsloth/gemma-4-E2B-it-GGUF"],
    "q4_0": ["google/gemma-4-E2B-it-qat-q4_0-gguf"],
    "12b-q4_0": ["google/gemma-4-12B-it-qat-q4_0-gguf", "google/gemma-4-12b-it-qat-q4_0-gguf", "unsloth/gemma-4-12B-it-GGUF", "bartowski/gemma-4-12b-it-GGUF"],
    "e4b-q4_0": ["google/gemma-4-E4B-it-qat-q4_0-gguf", "unsloth/gemma-4-E4B-it-GGUF"],
    "e4b-q8_0": ["unsloth/gemma-4-E4B-it-GGUF"],
}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--quant", required=True, choices=sorted(SOURCES)); ap.add_argument("--repo")
    ap.add_argument("--dest", default="/content/models"); ap.add_argument("--revision", default=None)
    A = ap.parse_args()
    from huggingface_hub import HfApi, hf_hub_download
    quant = A.quant.split("-")[-1]
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(quant).replace("_", "[_-]") + r"(?![a-z0-9_])", re.I)   # Q4_0 but not Q4_0_4_4 / IQ4_0
    tried = []
    for repo in ([A.repo] if A.repo else SOURCES[A.quant]):
        try:
            info = HfApi().model_info(repo, revision=A.revision)
        except Exception as e:
            tried.append(f"{repo}: {type(e).__name__}"); continue
        files = [s.rfilename for s in info.siblings]
        cands = [f for f in files if f.lower().endswith(".gguf") and not re.search(r"mmproj|mtp|assistant|-of-\d", f.lower()) and pat.search(os.path.basename(f))]
        if not cands:
            tried.append(f"{repo}: no matching *{quant}*.gguf"); continue
        cands.sort(key=lambda f: (f.count("/"), len(f))); fn = cands[0]
        os.makedirs(A.dest, exist_ok=True)
        path = hf_hub_download(repo, fn, revision=info.sha, local_dir=os.path.join(A.dest, repo.replace("/", "__")))
        print(json.dumps({"quant": A.quant, "repo": repo, "file": fn, "revision": info.sha, "path": path, "bytes": os.path.getsize(path),
                          "candidates": cands, "tried_before": tried}, ensure_ascii=False))
        return
    sys.exit(f"[fetch] no source worked for {A.quant}: {tried}")


if __name__ == "__main__":
    main()
