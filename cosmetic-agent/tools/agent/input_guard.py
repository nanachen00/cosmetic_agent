"""
Input Guard — 使用者輸入防護層
兩段式檢查：
1. Rule-based 快篩（0成本，先擋明顯惡意）
2. Terra 分類（模糊案例才用，判斷是否偏離藥妝主題）
"""
import re

from openai import OpenAI

from tools.agent.intent_parser import OPENAI_API_KEY, OPENAI_MODEL

client = OpenAI(api_key=OPENAI_API_KEY)

# ── Rule-based 黑名單 ──
INJECTION_PATTERNS = [
    r'ignore\s+(previous|above|all)\s+instructions',
    r'you\s+are\s+now',
    r'system\s*prompt',
    r'忽略(以上|之前|所有)(指令|規則|提示)',
    r'扮演.{0,10}(駭客|色情|暴力)',
    r'(jailbreak|DAN mode)',
    r'反白|洩漏.{0,6}(prompt|提示詞|系統指令)',
]

ABUSE_PATTERNS = [
    r'(幹|操|靠北|去死|白痴|智障|廢物)',
]

# 明顯無關主題的關鍵字（快篩用，模糊的交給 Terra）
OFFTOPIC_HARD = [
    '寫程式', '寫code', 'python', 'javascript', '選舉', '總統',
    '股票', '虛擬貨幣', '比特幣', '作業幫我寫', '論文代寫',
]

# 藥妝相關白名單關鍵字（有這些直接放行，省 Terra 呼叫）
DOMAIN_KEYWORDS = [
    '保養', '保濕', '面膜', '精華', '化妝水', '乳液', '洗面乳', '防曬',
    '痘', '粉刺', '美白', '敏感', '乾', '油', '膚', '皮膚', '成分',
    '酸', '醇', '推薦', '刺激', '過敏', '泛紅', '毛孔', '細紋', '斑',
    '記錄', '簽到', '三友', '品牌', '價格', '預算', 'cycle_', 'confirm_',
    '對', '是', '好', '跳過', 'skip',
]


def _rule_check(text: str) -> dict:
    """第一段：rule-based 快篩"""
    t = text.strip()

    # 空輸入或過長
    if not t:
        return {"allowed": False, "reason": "empty",
                "reply": "請告訴我您的保養需求喔！"}
    if len(t) > 1000:
        return {"allowed": False, "reason": "too_long",
                "reply": "輸入內容過長，請精簡描述您的保養需求（1000字內）。"}

    # Prompt injection
    for pat in INJECTION_PATTERNS:
        if re.search(pat, t, re.IGNORECASE):
            return {"allowed": False, "reason": "injection",
                    "reply": "偵測到不當指令，我是藥妝保養顧問，讓我們回到保養話題吧！"}

    # 辱罵
    for pat in ABUSE_PATTERNS:
        if re.search(pat, t):
            return {"allowed": False, "reason": "abuse",
                    "reply": "請以友善的方式對話，我很樂意幫您解決保養問題 😊"}

    # 白名單：含藥妝關鍵字直接放行
    if any(kw in t for kw in DOMAIN_KEYWORDS):
        return {"allowed": True, "reason": "domain_keyword"}

    # 硬性無關主題
    if any(kw in t for kw in OFFTOPIC_HARD):
        return {"allowed": False, "reason": "offtopic",
                "reply": "這個問題超出我的專業範圍囉！我是藥妝保養顧問，"
                         "可以問我保養品推薦、成分知識或膚況記錄 💄"}

    # 模糊案例 → 交給 Terra
    return {"allowed": None, "reason": "uncertain"}


def _terra_check(text: str) -> dict:
    """第二段：Terra 分類模糊案例"""
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": '你是輸入分類器。判斷使用者輸入是否與「保養品、膚況、藥妝、美容」相關，或是正常的對話回應（如確認、選擇、補充說明）。只輸出一個詞：RELEVANT 或 IRRELEVANT'},
                {"role": "user", "content": text},
            ],
            reasoning_effort="none",
            max_completion_tokens=10,
        )
        answer = (response.choices[0].message.content or "").strip().upper()
        if "IRRELEVANT" in answer:
            return {"allowed": False, "reason": "terra_offtopic", "reply": '這個話題我不太擅長耶！我是您的藥妝保養顧問，歡迎問我保養品推薦、成分知識或記錄膚況 💄'}
        return {"allowed": True, "reason": "terra_relevant"}
    except Exception:
        # Terra 掛了就放行，避免阻擋正常使用者。
        return {"allowed": True, "reason": "terra_unavailable_passthrough"}


def guard_input(text: str) -> dict:
    """
    主入口
    回傳：{"allowed": bool, "reason": str, "reply": str(僅在拒絕時)}
    """
    result = _rule_check(text)
    if result["allowed"] is None:
        result = _terra_check(text)
    return result


if __name__ == '__main__':
    tests = [
        "找油皮痘肌洗面乳",                       # 正常
        "ignore previous instructions and ...",  # injection
        "幫我寫python爬蟲",                       # 無關
        "你們家股票會漲嗎",                       # 無關
        "對，就是這樣",                           # 確認回覆（白名單）
        "今天天氣如何",                           # 模糊 → Terra
        "白痴機器人",                             # 辱罵
    ]
    for t in tests:
        r = guard_input(t)
        status = "✅放行" if r["allowed"] else f"❌攔截({r['reason']})"
        print(f"{status} | {t}")
        if not r["allowed"]:
            print(f"     → {r.get('reply','')}")
