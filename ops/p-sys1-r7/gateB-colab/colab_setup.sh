#!/usr/bin/env bash
# Gate B Colab setup (idempotent; safe to re-run after a disconnect).
#  1. llama.cpp llama-server, CUDA build for THIS GPU's compute capability, pinned commit. The binary is cached in
#     $GATEB_CACHE so a new session copies it instead of rebuilding (~10-20 min on Colab's 2 vCPUs).
#  2. jaredpalmer/kev at a pinned commit + its own uv venv (torch 2.8 from Kev's uv.lock; Colab's system torch is untouched).
# Nothing here talks to the user's servers; only GitHub / PyPI / the Python downloads uv needs.
set -euo pipefail
GATEB_CACHE=${GATEB_CACHE:-/content/drive/MyDrive/gateB/cache}
LLAMA_COMMIT=${LLAMA_COMMIT:-ff5a23376725e5a889fa0bfb987b7541dc304da0}   # llama.cpp master 2026-10-19 (gemma4 arch present)
KEV_COMMIT=${KEV_COMMIT:-1f04cfa28e6a708485b28a9585010b9e436b776e}       # jaredpalmer/kev main 2026-10-19 (Kev 1.0 serving code)
LLAMA_BIN=${LLAMA_BIN:-/content/llama-bin}
KEV_DIR=${KEV_DIR:-/content/kev}
mkdir -p "$GATEB_CACHE" "$LLAMA_BIN"

CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d ' .')
GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
echo "[setup] GPU=$GPU sm_$CC  cache=$GATEB_CACHE"

# ---------------------------------------------------------------- 1. llama.cpp
# LLAMA_CUDA_FA=ON (default) compiles the FlashAttention kernels (~80 of ~190 CUDA files). OFF roughly halves a fresh build;
# llama-server then runs without FA (`-fa auto` falls back), which only shifts absolute Gemma latency.
LLAMA_CUDA_FA=${LLAMA_CUDA_FA:-ON}
BIN_CACHE="$GATEB_CACHE/llama-${LLAMA_COMMIT:0:12}-sm$CC-fa$LLAMA_CUDA_FA"
SRC=/content/llama.cpp
OLD_CACHE="$GATEB_CACHE/llama-${LLAMA_COMMIT:0:12}-sm$CC"           # name used by the first version of this script (FA=ON)
if [ "$LLAMA_CUDA_FA" = ON ] && [ -x "$OLD_CACHE/llama-server" ] && [ ! -x "$BIN_CACHE/llama-server" ]; then
  mkdir -p "$BIN_CACHE" && cp "$OLD_CACHE/llama-server" "$BIN_CACHE/"
fi
if [ -x "$BIN_CACHE/llama-server" ]; then
  echo "[setup] llama-server: cached build $BIN_CACHE"
  cp "$BIN_CACHE/llama-server" "$LLAMA_BIN/llama-server"
else
  echo "[setup] llama-server: building llama.cpp ${LLAMA_COMMIT:0:12} for sm_$CC, FA=$LLAMA_CUDA_FA — FIRST TIME ONLY, 30-60 min on Colab's $(nproc) vCPUs."
  echo "        Progress is printed every minute. If interrupted, re-running continues the same build (same runtime) instead of restarting."
  if [ "$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)" != "$LLAMA_COMMIT" ]; then   # keep a partial build of the same commit
    rm -rf "$SRC" && git init -q "$SRC"
    git -C "$SRC" remote add origin https://github.com/ggml-org/llama.cpp
    git -C "$SRC" fetch -q --depth 1 origin "$LLAMA_COMMIT"
    git -C "$SRC" checkout -q FETCH_HEAD
  fi
  cmake -S "$SRC" -B "$SRC/build" -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$CC" -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA_FA="$LLAMA_CUDA_FA" \
        -DGGML_NATIVE=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
        -DCMAKE_BUILD_TYPE=Release > /content/llama-cmake.log 2>&1 || { tail -40 /content/llama-cmake.log; exit 1; }
  cmake --build "$SRC/build" -j"$(nproc)" --target llama-server > /content/llama-build.log 2>&1 &
  BPID=$!; T0=$(date +%s)
  while kill -0 $BPID 2>/dev/null; do
    sleep 60
    echo "[setup]   $(( ($(date +%s) - T0) / 60 )) min · $(grep -ao '\[ *[0-9]*%\]' /content/llama-build.log | tail -1) $(tail -c 300 /content/llama-build.log | tr '\n' ' ' | grep -ao 'Building [A-Z]* object [^ ]*' | tail -1 | sed 's#.*/##')"
  done
  wait $BPID || { tail -60 /content/llama-build.log; exit 1; }
  cp "$SRC/build/bin/llama-server" "$LLAMA_BIN/llama-server"
  mkdir -p "$BIN_CACHE" && cp "$LLAMA_BIN/llama-server" "$BIN_CACHE/" && echo "$LLAMA_COMMIT" > "$BIN_CACHE/COMMIT"
  echo "[setup] llama-server built in $(( ($(date +%s) - T0) / 60 )) min and cached to $BIN_CACHE (next sessions skip this)"
fi
chmod +x "$LLAMA_BIN/llama-server"     # files copied from the Drive mount can lose the exec bit
"$LLAMA_BIN/llama-server" --version 2>&1 | tail -2

# ---------------------------------------------------------------- 2. Kev
if [ "$(git -C "$KEV_DIR" rev-parse HEAD 2>/dev/null || true)" != "$KEV_COMMIT" ]; then
  echo "[setup] kev: fetching ${KEV_COMMIT:0:12}"
  rm -rf "$KEV_DIR" && git init -q "$KEV_DIR"
  git -C "$KEV_DIR" remote add origin https://github.com/jaredpalmer/kev
  GIT_LFS_SKIP_SMUDGE=1 git -C "$KEV_DIR" fetch -q --depth 1 origin "$KEV_COMMIT"
  GIT_LFS_SKIP_SMUDGE=1 git -C "$KEV_DIR" checkout -q FETCH_HEAD
fi
command -v uv >/dev/null 2>&1 || pip -q install uv
if [ ! -x "$KEV_DIR/.venv/bin/python" ] || ! "$KEV_DIR/.venv/bin/python" -c "import kev.serve, fastapi, uvicorn" 2>/dev/null; then
  echo "[setup] kev: uv sync --extra serve (torch 2.8 per uv.lock, ~2-4 min)"
  (cd "$KEV_DIR" && UV_LINK_MODE=copy uv sync -q --frozen --extra serve --no-dev)
fi
"$KEV_DIR/.venv/bin/python" -c "import torch, transformers; print('[setup] kev venv: torch', torch.__version__, 'cuda', torch.version.cuda, 'available', torch.cuda.is_available(), '| transformers', transformers.__version__)"
echo "[setup] done"
