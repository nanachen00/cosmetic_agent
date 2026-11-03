#!/usr/bin/env python3
"""contract_check — 只驗證 Sys1 伺服器的『契約』（handoff §3），不產生任何評比資料、不寫檔、不經 policy（不收集 gate_data / hold-out）。
用固定的探測句（gateb.TRUE_S / FALSE_S 與 probe 句），檢查：/v1/models、每題都有回答、機率範圍、choice 選項一致、noul 方向（是＝高）、延遲 p50/p95。

  python3 sys1/train/contract_check.py --url http://127.0.0.1:8232 --name mmbert-ft [--repeat 20]
exit 0 = 契約 OK；4 = 有問題（逐條列出）。
"""
import argparse, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import catalog as C   # noqa: E402
import gateb as GB    # noqa: E402

PROBE = "孕婦可以用水楊酸嗎，預算八百"


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--url", required=True); ap.add_argument("--name", required=True); ap.add_argument("--repeat", type=int, default=20)
    a = ap.parse_args(); K = C.K; K.check_base(a.url); cl = K.KevClient(a.url, timeout=60); bad = []
    m = cl.models(); print("models:", m["status"], str(m["json"])[:200])
    if m["status"] != 200: bad.append("/v1/models 不是 200")
    q = GB.pk_bundle(C.P2.parse2(PROBE)); walls = []; srv = []
    for i in range(a.repeat):
        r = cl.ask(PROBE, q, model=a.name); walls.append(r["wall_ms"]); srv.append(r["kev_ms"])
        if r["status"] != 200: bad.append(f"POST status {r['status']}: {(r['text'] or '')[:200]}"); break
    ans = r.get("answers") or {}
    for n, qq in q.items():
        v = ans.get(n)
        if not isinstance(v, dict): bad.append(f"{n}: 沒有回答"); continue
        if qq["type"] == "noul":
            try: assert 0 <= float(v["noul"]) <= 1
            except Exception: bad.append(f"{n}: noul 不是 0–1 機率 {v}")
        else:
            pr = v.get("probabilities") or {}
            if set(pr) != set(qq["criteria"]): bad.append(f"{n}: probabilities 的選項與題目不一致")
            elif abs(sum(pr.values()) - 1) > 1e-3: bad.append(f"{n}: probabilities 總和 {sum(pr.values()):.4f}")
            if v.get("choice") not in qq["criteria"]: bad.append(f"{n}: choice {v.get('choice')!r} 不在選項內")
    ps = lambda S: [float((cl.ask(s, {"safety": K.SAFETY_Q}, model=a.name).get("answers") or {}).get("safety", {}).get("noul", "nan")) for s in S]
    t, f = ps(GB.TRUE_S), ps(GB.FALSE_S); mt, mf = sum(t) / len(t), sum(f) / len(f)
    print(f"noul 方向：安全句平均 {mt:.3f} vs 非安全句 {mf:.3f} → {'是＝高 ✓' if mt > mf else '✗ 反向或無訊號'}")
    if not mt > mf: bad.append("noul 方向不是『是＝高』")
    ws = sorted(walls); p = lambda q_: ws[min(len(ws) - 1, int(q_ * (len(ws) - 1) + .5))]
    print(f"題數 {len(q)} · 牆鐘 p50 {p(.5):.0f} ms / p95 {p(.95):.0f} ms（{len(ws)} 次，單執行緒）· 伺服器自報 {srv[-1]}")
    print("契約問題：" + ("無" if not bad else "")); [print("  -", b) for b in bad]
    return 0 if not bad else 4


if __name__ == "__main__":
    sys.exit(main())
