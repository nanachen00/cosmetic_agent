#!/usr/bin/env python3
"""kevlib — thin client + question catalogue for the Kev (System One) server. Stdlib only.

SAFETY: the client refuses any target that is not http://127.0.0.1:<8230-8239> (the shadow Kev that kevctl manages).
It can never talk to :8000/:8001/:8002/:8003/:8010/:8200 or any other host.

Wire format (taken from the Gate1 harness source):
  POST /v1/systemone  {"state": text, "model": "kev-latest", "questions": {name: {type, instructions, criteria}}}
  -> {"answers": {name: {choice, probabilities{opt:p}, confidence}   # type "choice"
                  | {noul: p}                                          # type "noul" (direction verified by gate0)
      "latency_ms": float}
Several questions per request are part of the Gate1 contract (route + needs_clarification + safety_sensitive).
"""
from __future__ import annotations
import hashlib, http.client, json, re, threading, time, urllib.parse

ALLOWED_PORTS = range(8230, 8240)
MODEL = "kev-latest"


def check_base(url: str) -> tuple[str, int]:
    u = urllib.parse.urlparse(url)
    if u.scheme != "http" or u.hostname != "127.0.0.1" or u.port not in ALLOWED_PORTS or u.path not in ("", "/"):
        raise SystemExit(f"[REFUSED] Kev target must be http://127.0.0.1:8230-8239 (shadow Kev only), got {url!r}")
    return u.hostname, u.port


class _NoDelayConn(http.client.HTTPConnection):
    def connect(self):                         # without TCP_NODELAY, header+body in two segments hits Nagle x delayed-ACK (~40 ms phantom latency)
        super().connect()
        try:
            import socket; self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError: pass


class KevClient:
    def __init__(self, base_url="http://127.0.0.1:8230", timeout=60.0):
        self.host, self.port = check_base(base_url)
        self.timeout = timeout
        self._tls = threading.local()

    def _conn(self):
        c = getattr(self._tls, "c", None)
        if c is None:
            c = self._tls.c = _NoDelayConn(self.host, self.port, timeout=self.timeout)
        return c

    def _reset(self):
        c = getattr(self._tls, "c", None)
        try:
            if c: c.close()
        finally:
            self._tls.c = None

    def request(self, method, path, body=None):
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        t0 = time.perf_counter()
        for attempt in (0, 1):                       # one transparent reconnect for a stale keep-alive socket
            try:
                c = self._conn()
                c.request(method, path, raw, {"Content-Type": "application/json"} if raw else {})
                r = c.getresponse(); data = r.read()
                wall = (time.perf_counter() - t0) * 1000
                try: j = json.loads(data)
                except Exception: j = None
                return {"status": r.status, "wall_ms": wall, "json": j, "text": None if j is not None else data[:300].decode("utf-8", "replace")}
            except (http.client.HTTPException, OSError) as e:
                self._reset()
                if attempt == 1:
                    return {"status": 0, "wall_ms": (time.perf_counter() - t0) * 1000, "json": None, "text": f"{type(e).__name__}: {e}"}

    def models(self): return self.request("GET", "/v1/models")

    def ask(self, text, questions, model=MODEL):
        r = self.request("POST", "/v1/systemone", {"state": text, "model": model, "questions": questions})
        j = r["json"] if isinstance(r["json"], dict) else {}
        r["answers"] = j.get("answers") if isinstance(j.get("answers"), dict) else None
        r["kev_ms"] = j.get("latency_ms")
        return r


# ----------------------------------------------------------------------------- question builders
def noul(instr, true_desc, false_desc):
    return {"type": "noul", "instructions": instr, "criteria": {"true": true_desc, "false": false_desc}}


def choice(instr, criteria):
    return {"type": "choice", "instructions": instr, "criteria": dict(criteria)}


def p_noul(ans, name):
    """probability that the 'true' side holds (direction normalised by KEV_NOUL_TRUE_HIGH)."""
    try:
        v = float(ans[name]["noul"])
    except Exception:
        return None
    return v if NOUL_TRUE_HIGH else 1.0 - v


def probs(ans, name):
    try:
        return {k: float(v) for k, v in ans[name]["probabilities"].items()}
    except Exception:
        return None


NOUL_TRUE_HIGH = True            # gate0 verifies this; gateA overrides it from KEV_FACTS.json

# ----------------------------------------------------------------------------- catalogue (Traditional Chinese, edit here)
INTENT_LABELS = {
    "find_product": "找商品、推薦、挑選，或指定預算與條件的商品需求",
    "price_comparison": "比較商品的價格或哪一款比較划算",
    "knowledge": "詢問成分、保養原理、膚況原因或一般保養知識",
    "record": "要記錄今天的膚況或觀察",
    "chitchat": "打招呼、致謝，或與保養無關的閒聊",
    "unclear": "話沒說完，或資訊不足以判斷需求",
}
CONCERNS = ["保濕", "緊緻", "抗皺", "亮白", "透亮", "逆齡", "舒緩", "舒敏", "抗痘", "控油", "毛孔", "美白", "淡斑"]
NO_CONCERN = "無明確訴求"
SAFETY_Q = noul("這句話是否在詢問成分或產品使用的安全性（過敏、刺激、孕期哺乳、禁忌、交互作用、能不能一起用）？",
                "在詢問安全性", "不是在詢問安全性")
ROLE_CRITERIA = {"upper": "這個金額是預算上限（以內、最多、不超過）", "target": "這個金額是大約想花的預算（左右、大約）",
                 "other": "這個數字不是商品預算（容量、年齡、數量、天數等）"}


def intent_noul_q(label):
    return noul(f"使用者這句話是否包含這個需求：{INTENT_LABELS[label]}？", "包含這個需求", "不包含")


def intent_choice_q():
    return choice("這句話最主要的需求是哪一種？", INTENT_LABELS)


def concern_q():
    crit = {c: f"主要訴求是{c}" for c in CONCERNS}; crit[NO_CONCERN] = "句中沒有提到明確的肌膚訴求"
    return choice("句中提到的主要肌膚訴求是哪一個？", crit)


def role_q(span, value):
    return choice(f"句中的「{span}」（約 {value}）代表什麼？", ROLE_CRITERIA)


def avoid_q(ing_name):
    return noul(f"使用者是否明確表示不要、要避免或不能使用「{ing_name}」？", "明確要避免", "沒有要避免")


def catalogue_signature() -> str:
    blob = json.dumps([INTENT_LABELS, CONCERNS, NO_CONCERN, SAFETY_Q, ROLE_CRITERIA], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def full_bundle(parser2: dict, ing_names: dict) -> dict:
    """Everything gate A wants to learn about a sentence in ONE request (analysis is then offline)."""
    q = {"safety": SAFETY_Q, "intent_choice": intent_choice_q(), "concern": concern_q()}
    for lab in INTENT_LABELS: q[f"i_{lab}"] = intent_noul_q(lab)
    for i, c in enumerate(parser2.get("budget_candidates", [])[:3]):
        q[f"role_{i}"] = role_q(c["span"], c["value"])
    for key in parser2.get("ingredients", [])[:3]:
        q[f"avoid_{key}"] = avoid_q(ing_names.get(key, key))
    return q


def production_bundle(parser2: dict, ing_names: dict, need_concern: bool) -> dict:
    """What the proposed production call would send (used for the latency stage)."""
    q = {"safety": SAFETY_Q}
    for lab in INTENT_LABELS: q[f"i_{lab}"] = intent_noul_q(lab)
    if need_concern: q["concern"] = concern_q()
    for i, c in enumerate(parser2.get("budget_candidates", [])[:1]):
        if c.get("role") is None: q[f"role_{i}"] = role_q(c["span"], c["value"])
    return q
