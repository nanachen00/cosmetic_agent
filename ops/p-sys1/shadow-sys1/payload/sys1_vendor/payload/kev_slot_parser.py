
from __future__ import annotations
from dataclasses import dataclass, asdict
import re
from typing import Optional

SKIN = {
    "combination": [r"混合肌", r"混合性(?:肌膚|皮膚)?"],
    "dry": [r"乾肌", r"乾性(?:肌膚|皮膚)?", r"偏乾", r"乾燥"],
    "oily": [r"油肌", r"油性(?:肌膚|皮膚)?", r"容易出油"],
    "sensitive": [r"敏感肌", r"敏感性(?:肌膚|皮膚)?"],
}
CATEGORY = {
    "moisturizer": [r"乳液", r"乳霜", r"面霜", r"保濕霜", r"保濕產品"],
    "cleanser": [r"洗面乳", r"潔面", r"清潔產品"],
    "sunscreen": [r"防曬", r"防曬乳"],
    "serum": [r"精華", r"精華液"],
    "toner": [r"化妝水", r"爽膚水"],
    "mask": [r"面膜"],
}
INGREDIENTS = {
    "hyaluronic_acid": [r"玻尿酸", r"透明質酸", r"hyaluronic\s*acid"],
    "ceramide": [r"神經醯胺", r"ceramide"],
    "niacinamide": [r"菸鹼醯胺", r"煙醯胺", r"niacinamide"],
    "retinol": [r"視黃醇", r"\bA酸\b", r"retinol"],
    "salicylic_acid": [r"水楊酸", r"salicylic\s*acid"],
    "aha": [r"果酸", r"\bAHA\b"],
}

AFFIRMATIVE_SAFETY = [
    r"過敏", r"刺痛", r"紅腫", r"灼熱", r"破皮", r"副作用",
    r"禁忌", r"懷孕", r"孕婦", r"交互作用",
    r"一起(?:使用|擦|用).{0,8}(?:刺激|安全|可以|能不能|可不可以)",
    r"(?:眼周|眼睛周圍).{0,24}(?:刺痛|紅|腫|灼熱|不舒服|不適|異常)",
    r"(?:擦|用|使用).{0,12}(?:後|之後).{0,12}(?:臉|皮膚).{0,8}(?:一直|持續)?(?:紅|發紅)",
]

TRACKING = [
    r"(?:請|幫我|麻煩)?\s*(?:記錄|紀錄)\s*今天",
    r"(?:請|幫我|麻煩)?\s*(?:記下|記一下)\s*今天",
    r"(?:把|將)\s*今天.{0,30}(?:加入|放進|寫進).{0,12}(?:膚況|肌膚|皮膚)?(?:記錄|紀錄)",
    r"(?:更新|繼續追蹤|繼續記錄|繼續紀錄).{0,20}(?:膚況|肌膚|皮膚|狀況)",
    r"(?:膚況|肌膚|皮膚).{0,10}(?:記錄|紀錄|追蹤|打卡)",
    r"第\s*\d+\s*天.{0,12}(?:膚況|皮膚|肌膚)",
    r"(?:最近|這幾天|近幾天).{0,12}(?:膚況|皮膚|肌膚).{0,12}(?:記錄|紀錄|追蹤)",
]

KNOWLEDGE = [
    r"差別", r"差異", r"是什麼", r"是甚麼", r"有什麼作用", r"有甚麼作用", r"主要(?:是)?做什麼",
    r"原理", r"什麼是", r"甚麼是", r"白話解釋", r"解釋.{0,10}(?:作用|原理)",
    r"保養重點", r"用途", r"(?:只)?理解.{0,12}(?:其中|成分|玻尿酸|神經醯胺|菸鹼醯胺|水楊酸|視黃醇|果酸)", r"怎麼搭配", r"如何搭配", r"為什麼.{0,16}(?:保濕|保養)",
]
COMPARE = [
    r"哪個比較(?:適合|好|保濕|刺激|溫和)",
    r"(?:比較|對比)\s*.{0,15}(?:和|跟|與|vs\.?|versus)\s*.",
    r".{1,15}(?:和|跟|與|vs\.?|versus).{1,15}(?:差別|差異)",
]

# Important: there is no bare "推薦" pattern. It must be an imperative/action.
PRODUCT_ACTION = [
    r"(?:我)?只要.{0,12}(?:推薦|推建|商品建議)",
    r"(?:請|麻煩(?:你|一下)?|可以(?:幫我)?|能(?:幫我)?)\s*(?:直接)?\s*(?:推薦|推建)",
    r"直接\s*(?:推薦|推建)",
    r"幫我.{0,8}(?:找|挑|選|篩選|推薦|推建)",
    r"(?:想|要|打算|準備)\s*(?:買|找|挑)",
    r"有沒有.{0,16}(?:可以選|可選|推薦|適合).{0,8}(?:產品|商品|乳液|乳霜|精華|防曬|洗面乳|化妝水|面膜)",
    r"(?:找|挑|選).{0,10}(?:產品|商品|乳液|乳霜|精華|防曬|洗面乳|化妝水|面膜)",
]

NEGATED_PRODUCT = [
    r"(?:我)?不是在(?:選|挑|找|買)(?:產品|商品|東西)",
    r"(?:我)?不是(?:來)?(?:購物|買東西)",
    r"(?:先|目前|現在)?\s*(?:不要|不用|先不|不需要)\s*(?:再)?\s*(?:(?:幫|替)我\s*)?(?:推薦|買|找|挑|選|看)\s*(?:任何)?\s*(?:產品|商品|東西)?",
    r"(?:先|目前|現在)?\s*(?:不要|不用|先不|不需要)\s*(?:再)?\s*(?:幫忙\s*)?(?:推薦|買|找|挑|看)(?:產品|商品)?",
    r"(?:不需要|不用|不要)\s*(?:任何)?\s*(?:產品|商品)\s*(?:推薦|建議|搜尋|篩選)",
    r"不是(?:要|想)\s*(?:(?:幫|替)我\s*)?(?:買|找|挑|推薦)",
    r"(?:我)?不是(?:來|為了)\s*(?:找|買|挑|推薦)?\s*(?:產品|商品)",
    r"(?:我)?(?:沒有|沒)要\s*(?:買|找|挑|推薦)\s*(?:東西|產品|商品)?",
    r"不打算\s*(?:買|找|挑)",
    r"(?:商品|產品)\s*(?:先不用|先不看|晚點再看|之後再看)",
    r"(?:商品|產品)\s*(?:推薦|建議|搜尋|篩選)\s*(?:先)?\s*(?:不用|不要|不需要|暫停|晚點再說|之後再說)",
]

NEGATED_KNOWLEDGE = [
    r"(?:成分)?(?:原理|作用|用途|知識|差別|差異)\s*(?:今天|目前|現在)?\s*(?:先)?\s*(?:不談|不問|不看|不用|先不|暫停|晚點再說|之後再說)",
    r"(?:先|目前|現在|今天)?\s*(?:不要|不用|先不|不需要)\s*(?:再)?\s*(?:解釋|分析|說明)\s*(?:成分)?(?:原理|作用|用途|知識)?",
]

NEGATED_TRACKING = [
    r"(?:我)?不是(?:(?:要|想|來)(?:做|進行)?|來)\s*(?:膚況|肌膚|皮膚)?(?:記錄|紀錄|追蹤)",
    r"(?:先|目前|現在|今天)?\s*(?:不要|不用|先不|不需要|不做)\s*(?:再)?\s*(?:做|進行)?\s*(?:膚況|肌膚|皮膚)?(?:記錄|紀錄|追蹤)",
    r"(?:我)?沒有要\s*(?:做|進行)?\s*(?:膚況|肌膚|皮膚)?(?:記錄|紀錄|追蹤)",
    r"(?:膚況|肌膚|皮膚)?(?:記錄|紀錄|追蹤)\s*(?:先|暫時)?\s*(?:放一邊|放著|先不用|不用|晚點再看|晚點再做|晚點再說|晚點再處理|稍後再做|稍後再說|稍後再處理|之後再做|之後再說|之後再處理|改天(?:再)?做|改天(?:再)?說|改天(?:再)?處理|暫停|停止)",
    r"(?:膚況|肌膚|皮膚)?(?:記錄|紀錄|追蹤)\s*(?:今天|目前|現在)?\s*(?:先)?\s*(?:不做|不用|暫停|停止)",
    r"(?:今天|目前|現在)?\s*(?:先)?\s*(?:關掉|關閉|停用|停止)\s*(?:膚況|肌膚|皮膚)?(?:記錄|紀錄|追蹤)",
]

AMBIGUOUS = [
    r"(?:還)?不確定.{0,30}(?:推薦|商品|產品|知識|成分|追蹤|記錄|紀錄|買|了解|資訊)",
    r"不知道.{0,30}(?:買|商品|產品|了解|知識|成分|追蹤|記錄|紀錄|從哪)",
    r"可能.{0,30}(?:也可能|或者)",
    r"(?:問題|需求).{0,12}(?:還沒|沒有).{0,12}(?:整理|完整|說清楚)",
    r"資訊.{0,8}(?:不完整|不足|不夠)",
    r"模糊.{0,8}(?:需求|想法|問題)",
    r"不確定.{0,16}(?:從哪|方向|分類)",
    r"(?:還沒|尚未|沒有)確定.{0,30}(?:推薦|商品|產品|知識|成分|追蹤|記錄|紀錄|方向)",
    r"不知道.{0,20}(?:第一步|下一步|該怎麼做|怎麼開始)",
]

CONVERSATION = [
    r"^(?:嗨|哈囉|你好|早安|午安|晚安)[！!，,。\s]*(?:今天.*)?$",
    r"^(?:謝謝|感謝).*$",
    r"^好的?[，,。\s]*(?:我)?先(?:想想|考慮|休息).*$",
    r"^(?:先這樣|今天先到這裡|再見).*$",
    r"^你是什麼服務[？?。]?$",
    r"^你可以先告訴我你能幫我做什麼嗎[？?。]?$",
    r"^(?:商品|產品).{0,8}(?:先不用|先不看|晚點再看).{0,12}(?:謝謝|感謝|先這樣).*$",
    r"^不是要問保養.{0,12}(?:聊|聊天).*$",
    r"^我只是(?:想)?打招呼.*$",
    r".*(?:只是|純粹)想(?:聊|聊天)(?:一下)?[。.!！]?$",
    r".*(?:保養|產品|商品|成分)?(?:問題)?(?:改天|下次|晚點|之後)(?:再)?(?:問|說|處理)?.{0,12}(?:先)?(?:聊|聊天)(?:一下)?.*",
    r".*(?:先不用|不用|先不|不需要|不要).{0,12}(?:推薦|推建|找|買|挑|選|看|處理).{0,18}(?:謝謝|感謝).{0,18}(?:改天|晚點|之後)(?:再)?(?:說|看|繼續)?.*",
    r".*(?:商品|產品|成分|保養).{0,14}(?:都|也)?(?:先)?(?:不用|不處理|先不用).{0,18}(?:休息|先這樣|改天|晚安).*",
    r".*(?:先不要|不用|先不用|不需要)處理任何事.{0,12}(?:改天|晚點|之後).*",
    r".*(?:謝謝|感謝)(?:說明|回答)?.{0,12}(?:目前|現在)?(?:沒有|沒)(?:別的|其他)?(?:需求|問題).*",
    r".*(?:產品|商品).{0,12}(?:先不看|先不用|不用|晚點再看|改天再看).{0,12}(?:謝謝|感謝).*",
]

@dataclass
class ParseResult:
    raw_query: str
    budget_twd_max: Optional[int] = None
    budget_twd_target: Optional[int] = None
    skin_type: Optional[str] = None
    product_category: Optional[str] = None
    fragrance_free: Optional[bool] = None
    ingredients: list[str] = None
    explicit_compare: bool = False
    explicit_product_search: bool = False
    explicit_tracking: bool = False
    safety_marker: bool = False
    knowledge_marker: bool = False
    conversation_marker: bool = False
    product_negated: bool = False
    tracking_negated: bool = False
    ambiguous_marker: bool = False

    def to_dict(self):
        d = asdict(self)
        d["ingredients"] = d["ingredients"] or []
        return d

def _first_map(text, table):
    for key, pats in table.items():
        if any(re.search(p, text, re.I) for p in pats):
            return key
    return None

def _strip_patterns(text, patterns):
    t = text
    matched = False
    for p in patterns:
        t2, n = re.subn(p, " ", t, flags=re.I)
        if n:
            matched = True
            t = t2
    return t, matched

def _strip_negated_safety(text: str) -> str:
    t = text
    # Multi-symptom negation first.
    t = re.sub(
        r"(?:沒有|沒|並沒有|並無)(?:出現|發生|覺得|感到)?\s*"
        r"(?:過敏|刺痛|紅腫|灼熱|破皮)"
        r"(?:\s*(?:、|或|和|跟)\s*(?:過敏|刺痛|紅腫|灼熱|破皮))*",
        " ", t
    )
    t = re.sub(r"(?:不是|並非)\s*(?:過敏|刺痛|紅腫|灼熱|破皮|副作用)", " ", t)
    t = re.sub(r"(?:沒有|沒)\s*(?:任何)?\s*副作用", " ", t)
    return t

def parse(text: str) -> ParseResult:
    t = text.strip()

    budget_max = None
    for pat in [
        r"(?:預算\s*)?(?:不超過|最高|最多|上限(?:是|為)?)\s*(\d{2,6})\s*元",
        r"最高\s*預算\s*(\d{2,6})\s*元",
        r"(\d{2,6})\s*元\s*(?:以內|以下|內)",
        r"(?:NT\$?\s*)?(\d{2,6})\s*(?:元)?\s*(?:上限)",
    ]:
        m = re.search(pat, t, re.I)
        if m:
            budget_max = int(m.group(1))
            break

    budget_target = None
    m = re.search(r"(?:預算\s*)?(?:大約|約|差不多|抓)\s*(\d{2,6})\s*元", t, re.I)
    if m:
        budget_target = int(m.group(1))

    ingredients = [k for k, pats in INGREDIENTS.items()
                   if any(re.search(p, t, re.I) for p in pats)]

    safety_text = _strip_negated_safety(t)
    tracking_text, tracking_negated = _strip_patterns(t, NEGATED_TRACKING)
    product_text, product_negated = _strip_patterns(t, NEGATED_PRODUCT)
    knowledge_text, _knowledge_negated = _strip_patterns(t, NEGATED_KNOWLEDGE)

    safety = any(re.search(p, safety_text, re.I) for p in AFFIRMATIVE_SAFETY)
    tracking = any(re.search(p, tracking_text, re.I) for p in TRACKING)
    compare = any(re.search(p, knowledge_text, re.I) for p in COMPARE) or bool(re.search(r"(?:差別|差異)", knowledge_text))
    knowledge = compare or any(re.search(p, knowledge_text, re.I) for p in KNOWLEDGE)
    product_action = any(re.search(p, product_text, re.I) for p in PRODUCT_ACTION)
    conversation = any(re.search(p, t, re.I) for p in CONVERSATION)
    ambiguous = any(re.search(p, t, re.I) for p in AMBIGUOUS)

    return ParseResult(
        raw_query=t, budget_twd_max=budget_max, budget_twd_target=budget_target,
        skin_type=_first_map(t, SKIN), product_category=_first_map(t, CATEGORY),
        fragrance_free=True if re.search(r"(無香料|不含香料|不要香精|無香精)", t) else None,
        ingredients=ingredients, explicit_compare=compare,
        explicit_product_search=product_action, explicit_tracking=tracking,
        safety_marker=safety, knowledge_marker=knowledge,
        conversation_marker=conversation, product_negated=product_negated,
        tracking_negated=tracking_negated, ambiguous_marker=ambiguous,
    )

def deterministic_route_hint(p: ParseResult) -> Optional[str]:
    # High-risk/explicit operational actions first. These remain non-mappable
    # and therefore only short-circuit to the existing BLINK path.
    if p.safety_marker:
        return "safety"
    if p.explicit_tracking:
        return "skin_tracking"

    # Explicit ambiguity maps directly to BLINK `unclear`.
    if p.ambiguous_marker:
        return "clarify"

    if p.explicit_product_search:
        return "product"

    if p.knowledge_marker and (p.product_negated or not p.explicit_product_search):
        return "knowledge"
    if p.budget_twd_max is not None and (p.product_category or p.skin_type) and not p.product_negated:
        return "product"
    if p.knowledge_marker:
        return "knowledge"

    if p.conversation_marker:
        return "conversation"
    return None


