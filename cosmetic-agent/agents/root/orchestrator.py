"""
Root Agent Orchestrator v2
1. Input Guard 防護（惡意/無關輸入攔截）
2. 支援使用者長文字輸入
3. Pain & Gain 分析 → 需求摘要確認對話
4. End-to-end output 回應使用者痛點
5. 排序不符時繼續對話了解原因
6. Cycle Loop + Global Memory
"""
import sys
sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')


import os
import json
import re
import time
from core.observability import observe_pipeline_stage
from typing import Optional
from dotenv import load_dotenv
from openai import OpenAI
from app.clients.terra_completion_client import TerraCompletionClient
from tools.agent.intent_parser import OPENAI_API_KEY, OPENAI_MODEL, parse_intent
from tools.agent.response_composer import ResponseComposer
from tools.agent.experience_parser import (
    looks_like_experience,
    parse_product_experience,
)
from tools.getzep.zep_memory_adapter import ZepMemoryAdapter
from tools.getzep.conversation_memory_service import (
    ConversationMemoryService,
    ConversationMemoryContext,
)
from tools.getzep.conversation_title_service import (
    ConversationTitleService,
)
from tools.user.profile_context_bridge import (
    ProfileContextBridge,
    ProfileSyncResult,
)
from tools.user.user_analysis_context_bridge import (
    UserAnalysisContextBridge,
)
from tools.agent.clarification_tool import (
    get_clarification,
    QuickReply,
    SKIN_TYPE_REPLIES,
    CONCERN_REPLIES,
    CATEGORY_REPLIES,
)
from schemas.intent import (
    IntentParseResult,
    IntentType,
    calculate_ambiguity_score,
    calculate_routing_confidence,
    calculate_task_completeness,
)
try:
    from tools.a2a.productknowledge_orchestrator import run as product_agent_run
    print("[INIT] 使用 A2A 包裝層（Gate + 知識庫補強）")
except Exception as _e:
    print(f"[WARNING] A2A 包裝層載入失敗，直呼 product_agent：{_e}")
    from agents.product.product_agent import run as product_agent_run
from agents.product.product_agent import clean_ingredient_display, product_stable_key
from tools.agent.input_guard import guard_input
from tools.search.product_web_search import (
    build_product_evidence_packet,
    infer_product_detail_fields,
    should_search_named_product,
)
from tools.agent.dialogue_router import (
    classify_dialogue_route,
    conversation_recall_reply,
)
from app.services.temporal_freshness import (
    build_freshness_guard_reply,
    classify_freshness_query,
)
from app.services.weather_context_service import build_weather_reply
from tools.agent.product_turn_resolver import resolve_product_turn
from core.observability import monitor_agent_turn


try:
    from agents.knowledge.knowledge_sub_agent import run as knowledge_agent_run
except Exception as e:
    print(f"[WARNING] knowledge_agent 載入失敗：{e}")
    def knowledge_agent_run(**kwargs):
        return {"reply": "知識庫暫時維護中，請稍後再試。"}


try:
    from agents.skin_tracking.tracking_agent import run as skin_tracking_run
except Exception as e:
    print(f"[WARNING] skin_tracking_agent 載入失敗：{e}")
    def skin_tracking_run(**kwargs):
        return {"reply": "好的！請問您現在有什麼膚況困擾？"}


try:
    from tools.a2a.skin_product_bridge import get_skin_context
except Exception as _e:
    print(f"[WARNING] skin_product_bridge 載入失敗：{_e}")
    def get_skin_context(*a, **k):
        return None


load_dotenv()


def _explicit_skin_consult_intent(query: str) -> IntentParseResult:
    # The API already chose skin consultation, so a second model classification is redundant.
    return IntentParseResult(
        intent_type=IntentType.SYMPTOM_SOLVING,
        raw_query=query,
        ambiguity_score=0.0,
        routing_confidence=1.0,
        task_completeness=1.0,
    )


OPENAI_CLIENT = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=max(10.0, float(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "60"))),
    max_retries=max(0, min(2, int(os.getenv("OPENAI_MAX_RETRIES", "1")))),
)
TERRA_MODEL = os.getenv("TERRA_MODEL", "gpt-5.6-terra")
_terra_client = TerraCompletionClient.from_env()


AMBIGUITY_THRESHOLD = 0.7
_MAX_SESSION_HISTORY_MESSAGES = max(
    6,
    int(os.getenv("CHAT_MAX_SESSION_HISTORY_MESSAGES", "20")),
)
_MAX_SESSION_HISTORY_CHARS = max(
    4000,
    int(os.getenv("CHAT_MAX_SESSION_HISTORY_CHARS", "12000")),
)


SAFETY_KEYWORDS = [
    '適合嗎', '能用嗎', '安全嗎', '會怎樣', '適合我嗎',
    '可以用嗎', '會刺激嗎', '敏感肌能', '敏感肌可以',
    '能一起用嗎',
    '會不會', '有沒有問題', '有影響嗎',
]


# 使用者正在回答商品經驗追問時，如果又明確提出另一個請求，
# 必須先離開商品經驗等待狀態，再交由 intent_parser 重新路由。
# 這些字只負責辨識「換話題」，不負責決定要呼叫哪一個 Agent。
EXPERIENCE_TOPIC_SWITCH_MARKERS = (
    "我想問",
    "請問",
    "想知道",
    "想了解",
    "幫我查",
    "幫我找",
    "幫我推薦",
    "推薦我",
    "為什麼",
    "怎麼",
    "如何",
    "是什麼",
    "有什麼功用",
    "有什麼作用",
    "有什麼功效",
)




def _trim_to_sentence(text: str, limit: int = 80) -> str:
    """截斷到 limit 內最後一個完整句子；找不到句尾標點就整句保留到 limit"""
    if not text or len(text) <= limit:
        return text or ""
    cut = text[:limit]
    for punct in ('。', '！', '？', '～'):
        idx = cut.rfind(punct)
        if idx > 20:          # 至少留 20 字才截，避免只剩一小段
            return cut[:idx + 1]
    return cut + "…"




# ──────────────────────────────────────────
# ConversationState
# ──────────────────────────────────────────


class ConversationState:
    def __init__(self):
        self.intent: Optional[IntentParseResult] = None
        self.clarify_dimension: str = ""
        self.is_waiting_clarification: bool = False
        self.is_waiting_confirm: bool = False       # 等待使用者確認需求摘要
        self.is_waiting_rank_feedback: bool = False # 等待使用者對排序的回饋
        self.is_waiting_experience_product: bool = False
        self.is_waiting_experience_detail: bool = False
        self.pending_product_name: str = ""
        self.is_waiting_skin_tracking: bool = False
        self.skin_session: Optional[dict] = None
        self.skin_type_skipped: bool = False
        self.history: list = []
        self.turn_count: int = 0
        self.pain_gain: dict = {}       # Pain & Gain 分析結果
        self.last_products: list = []
        self.seen_product_ids: set[str] = set()   # 上次推薦的商品列表
        self.active_flow: str = "auto"
        self.last_action: str = ""


    def add_turn(self, role: str, content: str):
        normalized_content = str(content or "")[:6000]
        self.history.append({"role": role, "content": normalized_content})
        self.history = self.history[-_MAX_SESSION_HISTORY_MESSAGES:]
        while (
            len(self.history) > 2
            and sum(len(str(turn.get("content", ""))) for turn in self.history)
            > _MAX_SESSION_HISTORY_CHARS
        ):
            self.history.pop(0)
        if role == "user":
            self.turn_count += 1


    def reset(self):
        self.__init__()




# ──────────────────────────────────────────
# Pain & Gain 分析（Terra）
# ──────────────────────────────────────────


def _analyze_pain_gain(user_input: str) -> dict:
    """用 Terra 從使用者長文字中萃取 pain/gain/skin_summary/key_concerns"""
    system_prompt = """你是台灣藥妝保養品小幫手。
請從使用者的描述中，萃取以下資訊，用繁體中文輸出 JSON：
{
  "pain": "痛點描述，以「您」為主詞，禁止出現「使用者」三個字（1-2句）",
  "gain": "期待描述，以「您」為主詞，禁止出現「使用者」三個字（1-2句）",
  "skin_summary": "膚質與膚況摘要（1句）",
  "key_concerns": ["主要訴求1", "主要訴求2"],
  "budget_hint": "預算線索（若有提到，否則為null）",
  "special_notes": "其他特殊需求或限制（若有）"
}
只輸出 JSON，不要其他說明。"""


    try:
        response = OPENAI_CLIENT.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ],
            reasoning_effort="none",
            max_completion_tokens=300,
            response_format={"type": "json_object"},
        )
        text = (response.choices[0].message.content or "").strip()

        start = text.find('{')
        end   = text.rfind('}') + 1
        if start != -1 and end > start:
            parsed = json.loads(text[start:end])
            for k in ('pain', 'gain', 'skin_summary', 'special_notes'):
                if isinstance(parsed.get(k), str):
                    parsed[k] = parsed[k].replace('使用者', '您')
            return parsed
    except Exception as e:
        print(f"[Pain&Gain] 分析失敗：{e}")


    return {
        "pain":          "您提到了一些肌膚困擾",
        "gain":          "希望找到適合的保養品",
        "skin_summary":  "待確認",
        "key_concerns":  [],
        "budget_hint":   None,
        "special_notes": None,
    }




def _build_confirm_message(pain_gain: dict, intent: IntentParseResult) -> str:
    """根據 Pain & Gain 分析結果，產出需求確認訊息"""
    skin_map = {
        "oily": "油性肌", "dry": "乾性肌",
        "sensitive": "敏感肌", "combination": "混合肌",
        "normal": "中性肌", "unknown": "尚待確認",
    }
    skin_label   = skin_map.get(intent.skin_type.value, "尚待確認")
    concerns_str = "、".join(intent.concern) if intent.concern else pain_gain.get('key_concerns', [])
    if isinstance(concerns_str, list):
        concerns_str = "、".join(concerns_str)


    msg = "讓我確認一下您的需求 🌸\n\n"
    if pain_gain.get('pain'):
        msg += f"😟 您的困擾：{pain_gain['pain']}\n"
    if pain_gain.get('gain'):
        msg += f"✨ 您希望達到：{pain_gain['gain']}\n"
    msg += f"💆 膚質：{skin_label}\n"
    if concerns_str:
        msg += f"🎯 主要訴求：{concerns_str}\n"
    if pain_gain.get('budget_hint'):
        msg += f"💰 預算：{pain_gain['budget_hint']}\n"
    if pain_gain.get('special_notes'):
        msg += f"📝 特別注意：{pain_gain['special_notes']}\n"


    msg += "\n這樣理解正確嗎？\n還是有什麼需要補充或調整的？"
    return msg


# 需求確認只有「整句都是肯定詞」才算同意；「不是」「沒有，我是乾肌」等否定、修正或補充
# 都不會直接分派，避免用使用者已經否定的需求推薦。
_CONFIRM_FILLER_TOKENS = ("啊", "喔", "哦", "呀", "吧", "耶", "了", "的", "嗯", "啦", "呢", "唷")
_CONFIRM_AFFIRMATIVE_TOKENS = tuple(sorted((
    "對", "對的", "對啊", "是", "是的", "好", "好的", "好啊", "確認", "沒錯", "沒問題", "正確",
    "可以", "就是", "就是這樣", "這樣", "就這樣", "下一步", "繼續", "謝謝", "謝", "麻煩了", "麻煩你",
    "ok", "okay", "yes", "yep", *_CONFIRM_FILLER_TOKENS,
), key=len, reverse=True))
# 只有否定、沒有說要改什麼：不能拿這句重新解析需求（會蓋掉原本的 intent）。
_CONFIRM_REJECT_TOKENS = tuple(sorted((
    "不是", "不對", "不好", "不用", "不要", "錯", "錯了", "否", "no", "nope", "需要調整", "要調整",
    "調整", "修改", "重來", *_CONFIRM_FILLER_TOKENS,
), key=len, reverse=True))


def _normalize_confirm_reply(reply: str) -> str:
    return re.sub(r"[\W_]+", "", (reply or "").lower())


def _consists_of(text: str, tokens: tuple[str, ...]) -> bool:
    if not text:
        return False
    while text:
        token = next((t for t in tokens if text.startswith(t)), None)
        if token is None:
            return False
        text = text[len(token):]
    return True


def _is_confirmation(reply: str) -> bool:
    text = _normalize_confirm_reply(reply)
    if text in ("confirmyes", "next"):
        return True
    return not text.startswith("confirm") and _consists_of(text, _CONFIRM_AFFIRMATIVE_TOKENS)


def _is_bare_rejection(reply: str) -> bool:
    text = _normalize_confirm_reply(reply)
    return text == "confirmno" or _consists_of(text, _CONFIRM_REJECT_TOKENS)


def _build_product_decision_summary(
    intent: IntentParseResult,
    applied_context: list[str],
) -> str:
    """用個人資料說明「為何這次要這樣篩」，而不是重述查詢條件。"""
    def join_zh(values: list[str]) -> str:
        if len(values) < 2:
            return "".join(values)
        return "、".join(values[:-1]) + "與" + values[-1]

    context: dict[str, str] = {}
    for item in applied_context:
        label, separator, value = item.partition("：")
        if not separator:
            label, separator, value = item.partition(":")
        if separator and label.strip() and value.strip():
            context[label.strip()] = value.strip()

    raw_query = str(getattr(intent, "raw_query", "") or "")
    requested_category = str(
        getattr(getattr(intent, "product_category", None), "value", "") or ""
    ).strip()
    if requested_category == "不明":
        requested_category = ""

    concern_order = (
        "保濕", "抗痘", "控油", "舒緩", "舒敏", "毛孔",
        "亮白", "美白", "淡斑", "抗皺", "緊緻",
    )
    explicit_concerns = [value for value in concern_order if value in raw_query]
    recent_concerns = [
        value.strip()
        for value in re.split(r"[、,，/／]", context.get("近期膚況", ""))
        if value.strip()
    ]
    secondary_concerns = [
        value for value in recent_concerns if value not in explicit_concerns
    ]

    avoid = context.get("避開", "")
    texture = context.get("質地偏好", "")
    skin_type = context.get("膚質", "")
    all_concerns = list(dict.fromkeys(explicit_concerns + secondary_concerns))
    daytime = any(token in raw_query for token in ("白天", "日間"))

    # 第一句只講個人資料帶來的取捨，避免把使用者剛說的話換句話說。
    if "保濕" in all_concerns and any(
        value in all_concerns for value in ("抗痘", "控油", "毛孔")
    ):
        balancing = [
            value for value in ("抗痘", "控油", "毛孔")
            if value in all_concerns
        ]
        first_sentence = (
            f"你近期一邊需要補水，一邊也在處理{join_zh(balancing[:2])}；"
            + (
                "白天若選得太厚重，可能增加悶黏感。"
                if daytime
                else "若只追求滋潤度，可能不符合你想要的清爽使用感。"
            )
        )
    elif any(value in all_concerns for value in ("舒緩", "舒敏")):
        first_sentence = (
            "你近期更在意肌膚舒適度，因此這次不能只看熱門度或單一功效，"
            "配方耐受性也會影響排序。"
        )
    elif "保濕" in all_concerns and any(value in skin_type for value in ("乾", "敏感")):
        first_sentence = (
            f"你的{skin_type or '近期膚況'}需要補水，但日常使用還要兼顧舒適度；"
            "因此不會只用滋潤度判斷。"
        )
    elif all_concerns:
        first_sentence = (
            f"你近期最需要處理的是{join_zh(all_concerns[:2])}，"
            "所以這次排序會看商品能否同時回應這些需求，而不是只看單一成分。"
        )
    else:
        first_sentence = (
            "這次會把你的日常使用情境放在熱門度之前，"
            "避免選到看似符合品類、實際使用感卻不合適的商品。"
        )

    benefit_map = {
        "保濕": "補水",
        "抗痘": "痘痘照護",
        "控油": "油脂調理",
        "舒緩": "穩定膚況",
        "舒敏": "溫和照護",
        "毛孔": "毛孔調理",
        "亮白": "提亮膚色",
        "美白": "提亮膚色",
        "淡斑": "改善暗沉斑點",
        "抗皺": "細紋照護",
        "緊緻": "彈力照護",
    }
    benefits: list[str] = []
    for concern in all_concerns:
        benefit = benefit_map.get(concern)
        if benefit and benefit not in benefits:
            benefits.append(benefit)

    decision_parts: list[str] = []
    if texture:
        decision_parts.append(f"保留{texture}的使用感")
    if benefits:
        decision_parts.append(f"兼顧{join_zh(benefits[:3])}")
    if not decision_parts:
        decision_parts.append("依需求吻合度與可核對的商品資料排序")

    second_sentence = "因此我會優先" + "、".join(decision_parts)
    if avoid:
        second_sentence += f"，並先排除{avoid}，避免和你的耐受條件衝突"
    second_sentence += "。"

    return first_sentence + second_sentence




# ──────────────────────────────────────────
# 商品推薦格式化（含排序理由 & 比較）
# ──────────────────────────────────────────
def _format_ranked_products(
    result: dict,
    pain_gain: dict,
    intent: IntentParseResult,
) -> str:
    """
    將 Product Agent 已排序的商品整理成前端顯示文字。

    注意：
    1. 這裡不會重新排序商品。
    2. 商品順序由 Product Agent 決定。
    3. 手機回覆最多顯示三項商品。
    4. 只顯示影響決策的重點，不重複商品長文案。
    """

    products = (
        result.get("products")
        or []
    )
    seen_product_keys: set[str] = set()
    deduped_products = []
    for product in products:
        if not isinstance(product, dict):
            continue
        product_key = str(
            product.get("name")
            or product.get("id")
            or ""
        ).strip().casefold()
        if not product_key or product_key in seen_product_keys:
            continue
        seen_product_keys.add(product_key)
        deduped_products.append(product)
    products = deduped_products

    warnings = (
        result.get("warnings")
        or []
    )

    if not products:
        message = result.get("message")
        if isinstance(message, dict):
            detailed_message = str(message.get("main") or "").strip()
            sub_message = str(message.get("sub") or "").strip()
            if sub_message:
                detailed_message += "\n- " + sub_message
        else:
            detailed_message = str(message or "").strip()
        if detailed_message:
            return detailed_message
        return (
            "目前沒有找到很符合的商品。"
            "你可以再告訴我預算、偏好的品牌，"
            "或喜歡清爽還是滋潤的質地，我再幫你找。"
        )

    result_count = min(3, len(products))
    reply = f"我已把條件一次合併，先縮小到 {result_count} 款：\n"
    personalization = result.get("personalization_context") or {}
    applied_context = [
        str(item).strip()
        for item in (personalization.get("applied") or [])
        if str(item).strip()
    ]
    if applied_context:
        reply += "\n**已自動套用你的資料**\n"
        reply += "- " + "｜".join(applied_context[:5]) + "\n"

    decision_summary = _build_product_decision_summary(intent, applied_context)
    if decision_summary:
        reply += "\n**適合你的原因**\n"
        reply += decision_summary + "\n"

    # =====================================================
    # 商品內容
    # =====================================================

    for index, product in enumerate(
        products[:3]
    ):
        # 只顯示數字排名。
        label = (
            f"{index + 1}."
        )

        name = str(
            product.get("name")
            or "未提供商品名稱"
        ).strip()

        brand = str(
            product.get("brand")
            or ""
        ).strip()
        title = name
        if brand and name.casefold().startswith(brand.casefold()):
            title = name[len(brand):].lstrip(" ｜|—-・") or name

        category = str(
            product.get("category")
            or ""
        ).strip()
        requested_category = str(
            getattr(getattr(intent, "product_category", None), "value", "") or ""
        ).strip()
        if (
            category in {"", "臉部保養", "身體保養", "其他", "unknown"}
            and requested_category not in {"", "不明"}
        ):
            category = requested_category

        price = product.get(
            "price"
        )

        rank_reason = str(
            product.get("rank_reason")
            or product.get("recommendation_reason")
            or ""
        ).strip()

        over_budget = bool(
            product.get("over_budget")
        )

        budget_tag = (
            "（略超預算）"
            if over_budget
            else ""
        )

        reply += f"\n### {label} {title}\n"
        if product.get("is_partner"):
            reply += "- **通路：** 三友藥妝商品\n"
        if brand:
            reply += f"- **品牌：** {brand}\n"
        if category:
            reply += f"- **類型：** {category}\n"

        # 推薦原因
        if rank_reason:
            natural_reason = (
                rank_reason
                .replace(
                    "使用者",
                    "你",
                )
                .replace(
                    "您的",
                    "你的",
                )
            )

            reply += f"- **適合你的原因：** {_trim_to_sentence(natural_reason, 92)}\n"

        ingredients = (
            product.get("key_ingredients")
            or product.get("ingredients")
            or []
        )
        ingredient_text = "、".join(
            str(value).strip()
            for value in ingredients[:3]
            if str(value).strip()
            and len(str(value).strip()) <= 45
            and not any(
                marker in str(value)
                for marker in ("適合", "有效", "使用後", "肌膚", "質地", "主打", "解決", "呈現")
            )
        )
        if ingredient_text:
            reply += f"- **重點成分：** {_trim_to_sentence(ingredient_text, 72)}\n"

        if price not in {None, ""}:
            reply += f"- **價格：** NT${price}{budget_tag}\n"

        sources = product.get("sources") or []
        for source in sources[:1]:
            if not isinstance(source, dict):
                continue
            source_url = str(source.get("url") or "").strip()
            if source_url.startswith(("https://", "http://")):
                reply += f"- [查看商品資料]({source_url})\n"

    # =====================================================
    # 注意事項
    # =====================================================

    if warnings:
        warning_text = "；".join(
            str(item)
            for item in warnings[:2]
            if item
        )

        if warning_text:
            reply += (
                f"\n**共同留意：** {warning_text}\n"
            )

    reply += "\n想改價格、質地或品牌，直接說要改哪一項就好。"


    

    protected_terms = [
        str(product.get(key) or "").strip()
        for product in products
        if isinstance(product, dict)
        for key in ("name", "brand")
    ]
    return clean_ingredient_display(reply, protected_terms)






# ──────────────────────────────────────────
# 排序回饋處理
# ──────────────────────────────────────────


def _handle_rank_feedback(user_feedback: str, products: list, intent: IntentParseResult) -> str:
    """使用者對排序不滿意時，用 TAIDE 分析回饋並回應"""
    system_prompt = """你是台灣藥妝保養小幫手。
使用者對商品排序有意見，請：
1. 理解使用者的回饋重點
2. 解釋調整後的排序邏輯
3. 給出簡短的回應（不超過100字）
【鐵則】只能提及「目前推薦排序」清單內的商品，嚴禁提及、編造清單外任何商品名稱或價格
語氣親切，用繁體中文回覆。"""


    product_names = "、".join([f"{p['name'][:20]}(NT${p.get('price','?')})" for p in products[:5]])
    user_prompt   = f"""
目前推薦排序：{product_names}
使用者回饋：{user_feedback}
使用者膚質：{intent.skin_type.value if intent else 'unknown'}
使用者訴求：{'、'.join(intent.concern) if intent and intent.concern else '未指定'}
請回應使用者的回饋，並說明如何調整。"""


    try:
        reply = _terra_client.complete(
            model=TERRA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=200,
            temperature=0.5,
            timeout_seconds=20,
        )
        reply += "\n\n請問您希望優先考慮哪個方向？\n"
        reply += "（例如：更注重價格 / 更在意某個成分 / 偏好某個品牌）"
        return reply
    except Exception:
        return (
            "感謝您的回饋！\n"
            "了解您認為目前的排序不太符合您的需求。\n"
            "請問您最在意的是哪個面向？\n"
            "（價格、成分、品牌、功效、我的膚質）\n"
            "我來根據您的優先考量為您打造最適合的推薦😊"
        )




# ──────────────────────────────────────────
# RootAgentOrchestrator
# ──────────────────────────────────────────



def _clean_internal_user_message(value: str) -> str:
    """
    移除只供 Root Agent 分流使用的內部標記。
    這個標記不能寫入 GetZep、對話標題或前端紀錄。
    """
    message = str(value or "").strip()

    internal_prefixes = (
        "__knowledge_consult__:",
    )

    for prefix in internal_prefixes:
        if message.startswith(prefix):
            return message[len(prefix):].strip()

    return message


def _is_product_refinement_query(value: str) -> bool:
    """Recognise a follow-up that refines an existing product result set."""
    text = str(value or "").strip().lower()
    brand_refinement = (
        any(term in text for term in ("品牌", "牌子", "大牌"))
        and any(
            term in text
            for term in (
                "比較有名", "比較知名", "更有名", "更知名",
                "知名一點", "大牌一點", "有名的品牌", "有名的牌子",
                "更多", "其他", "換", "再給我",
            )
        )
    )
    constraint_refinement = any(
        term in text
        for term in (
            "再便宜", "便宜一點", "清爽一點", "不要太厚重",
            "不要厚重", "不黏", "其他款", "更多款", "再給我",
            "換一款", "換一批", "換成", "改成",
        )
    )
    return brand_refinement or constraint_refinement


def _wants_new_product_batch(value: str) -> bool:
    text = str(value or "").strip().lower()
    return any(term in text for term in ("更多", "其他", "換", "再給我", "另一"))


def _merge_product_refinement_intent(
    previous: IntentParseResult,
    current: IntentParseResult,
    user_input: str,
) -> IntentParseResult:
    """Carry hard constraints forward without carrying an old brand restriction."""
    previous_data = previous.model_dump()
    current_data = current.model_dump()
    raw_query = str(user_input or "")
    unlimited_budget = any(
        marker in raw_query
        for marker in (
            "價格不限", "價錢不限", "預算不限", "不限預算",
            "沒有預算限制", "不設預算",
        )
    )

    if current_data.get("skin_type") in {None, "", "unknown"}:
        current_data["skin_type"] = previous_data.get("skin_type", "unknown")
    if not current_data.get("concern"):
        current_data["concern"] = list(previous_data.get("concern") or [])

    current_category = current_data.get("product_category")
    category_inherited = current_category in {None, "", "不明"}
    if category_inherited:
        current_data["product_category"] = previous_data.get("product_category", "不明")

    if not unlimited_budget and not current_data.get("budget_range"):
        current_data["budget_range"] = previous_data.get("budget_range")
    for field in ("texture_preference",):
        if field in current_data and current_data.get(field) in {None, "", "unknown", "不明"}:
            current_data[field] = previous_data.get(field, current_data.get(field))

    query_expansion = list(current_data.get("query_expansion") or [])
    if category_inherited:
        query_expansion = list(previous_data.get("query_expansion") or []) + query_expansion
    query_expansion.append(raw_query)
    current_data["query_expansion"] = list(
        dict.fromkeys(str(item).strip() for item in query_expansion if str(item).strip())
    )
    current_data["intent_type"] = "find_product"
    current_data["raw_query"] = raw_query
    return IntentParseResult(**current_data)


class RootAgentOrchestrator:


    def __init__(
        self,
        model: str = "gpt-5.6-terra",
        user_id: str = "anonymous",
        conversation_id: str = "default",
    ):
        """
        建立 Root Agent 對話實例。

        user_id：
            登入帳號 ID。
            同一帳號的 Profile 與長期偏好共用。

        conversation_id：
            目前獨立對話 ID。
            每個 conversation_id 使用不同 GetZep Thread。

        未傳 conversation_id 時使用 default，
        保持舊程式相容。
        """

        self.model = model

        self.user_id = (
            user_id
            or "anonymous"
        ).strip()

        self.conversation_id = (
            conversation_id
            or "default"
        ).strip()

        if not self.user_id:
            self.user_id = "anonymous"

        if not self.conversation_id:
            self.conversation_id = "default"

        self.state = ConversationState()

        self.memory = ZepMemoryAdapter(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
        )

        self.memory_service = ConversationMemoryService(
            self.memory
        )

        self.title_service = ConversationTitleService()
        self.response_composer = ResponseComposer()

        self._turn_memory: Optional[
            ConversationMemoryContext
        ] = None

        self.profile_bridge = ProfileContextBridge(
            user_id=self.user_id,
        )

        # User Analysis 只提供補充背景與商品排序權重，不參與 Root 路由。
        # Bridge 內所有資料庫與分析錯誤都會 fail-open，不中斷聊天。
        self.user_analysis_bridge = UserAnalysisContextBridge(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
        )

        self.user_analysis_context: dict = {}

        self.basic_profile: dict = {}

        self._profile_load_error: str = ""

        self._pending_profile_sync: Optional[
            ProfileSyncResult
        ] = None


    def _memory_prompt(self) -> str:
        return self._turn_memory.prompt if self._turn_memory else ""


    def _intent_memory_context(self) -> str:
        if not self._turn_memory:
            return self.memory.to_summary() or ""
        parts = []
        if self._turn_memory.profile_summary:
            parts.append(self._turn_memory.profile_summary)
        if self._turn_memory.zep_context:
            parts.append(self._turn_memory.zep_context)
        return "\n\n".join(parts)


    def _current_dialogue_step(self) -> str:
        if self.state.is_waiting_skin_tracking:
            phase = str((self.state.skin_session or {}).get("phase") or "collect")
            return f"skin_tracking:{phase}"
        if self.state.is_waiting_clarification:
            return f"clarification:{self.state.clarify_dimension or 'routing'}"
        if self.state.is_waiting_confirm:
            return "intent_confirmation"
        if self.state.is_waiting_rank_feedback:
            return "product_rank_feedback"
        if self.state.last_products:
            return "product_results"
        return "idle"


    def _routing_context(self) -> dict:
        intent = self.state.intent
        return {
            "active_flow": self.state.active_flow or "auto",
            "current_step": self._current_dialogue_step(),
            "intent_type": (
                intent.intent_type.value
                if intent is not None
                else None
            ),
            "product_category": (
                intent.product_category.value
                if intent is not None
                else None
            ),
            "has_products": bool(self.state.last_products),
            "seen_product_count": len(self.state.seen_product_ids),
            "is_waiting_confirm": bool(self.state.is_waiting_confirm),
            "skin_session_active": bool(
                self.state.is_waiting_skin_tracking
                or self.state.skin_session
            ),
        }
    def _handle_resolved_product_turn(
        self,
        user_input: str,
        resolution: dict,
    ) -> dict:
        """Dispatch a product turn that is already unambiguous without an LLM call."""
        self._log(
            "PRODUCT_ROUTE",
            f"local reason={resolution.get('reason')} route={resolution.get('route')}",
        )
        action = resolution.get("action")
        if action:
            return self._handle_structured_action(
                action,
                _clean_internal_user_message(user_input),
            )

        if resolution.get("route") == "skin_consult":
            intent = IntentParseResult(
                intent_type=IntentType.KNOWLEDGE_QUERY,
                raw_query=user_input,
            )
            self.state.intent = intent
            self._refresh_intent_scores()
            return self._do_dispatch(self.state.intent)

        previous = self.state.intent
        current = IntentParseResult(
            intent_type=IntentType.FIND_PRODUCT,
            raw_query=user_input,
        )
        if (
            resolution.get("inherit_context")
            and previous is not None
            and previous.intent_type in {
                IntentType.FIND_PRODUCT,
                IntentType.RECOMMENDATION,
                IntentType.PRICE_COMPARISON,
            }
        ):
            current = _merge_product_refinement_intent(
                previous,
                current,
                user_input,
            )

        current_data = current.model_dump()
        patch = dict(resolution.get("intent_patch") or {})
        if patch.get("concern") and current_data.get("concern"):
            patch["concern"] = list(
                dict.fromkeys(current_data["concern"] + patch["concern"])
            )
        current_data.update(patch)
        current_data["intent_type"] = IntentType.FIND_PRODUCT.value
        current_data["raw_query"] = user_input
        query_expansion = list(current_data.get("query_expansion") or [])
        query_expansion.append(user_input)
        current_data["query_expansion"] = list(dict.fromkeys(query_expansion))
        self.state.intent = IntentParseResult(**current_data)
        self._refresh_intent_scores()

        if resolution.get("needs_clarification"):
            return self._ask_clarification("product_category")
        return self._do_dispatch(self.state.intent)



    @staticmethod
    def _annotate_skin_quick_replies(replies: list | None) -> list:
        annotated = []
        for item in replies or []:
            if not isinstance(item, dict):
                annotated.append(item)
                continue
            entry = dict(item)
            value = str(entry.get("value") or "").strip().lower()
            if value in {"confirm_yes", "確認", "確定", "是", "對", "好", "ok", "yes"}:
                entry["action"] = "record_confirm"
            elif value in {"edit_skin_record", "修改", "我想改一下", "重新修改", "重新填寫"}:
                entry["action"] = "record_correction"
            elif value in {"取消", "算了", "停止", "離開", "cancel"}:
                entry["action"] = "cancel"
            elif value in {"1", "2", "3", "4", "5"}:
                entry["action"] = "record_severity"
            annotated.append(entry)
        return annotated


    def refresh_profile_context(self) -> dict:
        """Reload the canonical member profile and mirror safe fields to Zep."""
        self.basic_profile = self.profile_bridge.refresh(
            memory=self.memory,
        )
        self._profile_load_error = ""
        return self.basic_profile


    def _profile_summary_for_prompt(self) -> str:
        parts = []
        verified_profile = self.profile_bridge.build_prompt_context(
            self.basic_profile,
        )
        consumer_profile = self.user_analysis_bridge.build_prompt_context(
            self.user_analysis_context,
        )
        zep_summary = self.memory.to_summary() or ""

        if verified_profile:
            parts.append(verified_profile)
        if consumer_profile:
            parts.append(consumer_profile)
        if zep_summary:
            parts.append("【長期對話偏好】\n" + zep_summary)

        return "\n\n".join(parts)


    def _prepare_profile_context(
        self,user_input: str,
    ) -> None:
        """
        每輪重新載入正式基本資料。
    
        聊天內容只作為本輪推理參考，
        不直接修改使用者正式基本資料。
        正式資料只能從基本資料頁儲存。
        """
    
        # 明確清除舊的聊天同步結果，
        # 避免上一輪的更新狀態被重複使用。
        self._pending_profile_sync = None
    
        try:
            self.refresh_profile_context()
        except Exception as error:
            self.basic_profile = {}
            self._profile_load_error = str(error)
            self._log(
                "PROFILE",
                f"載入基本資料失敗：{error}",
    )


    def _handle_profile_interaction(
        self,
        user_input: str,
    ) -> Optional[dict]:
        """
        回答使用者對自己基本資料的查詢。
    
        此函式只讀取資料，不進行任何資料庫寫入。
        """
    
        profile_reply = self.profile_bridge.answer_profile_question(
            message=user_input,
            profile=self.basic_profile,
        )
    
        if not profile_reply:
            return None
    
        if self._profile_load_error:
            profile_reply = "目前無法讀取您的基本資料，請稍後再試。"
    
        self.state.add_turn(
            "assistant",
            profile_reply,
        )
    
        return {
            "reply": profile_reply,
            "quick_replies": [],
            "intent": {},
            "stage": "profile_query",
        }


    def _run_product_agent(
        self,
        intent: IntentParseResult,
        excluded_product_ids: Optional[set[str]] = None,
    ) -> dict:
        disliked = list(self.memory.get("disliked_ingredients", []) or [])
        profile_allergens = list(self.basic_profile.get("allergens") or [])
        other_allergen = str(self.basic_profile.get("otherAllergen") or "").strip()
        if other_allergen:
            profile_allergens.extend(
                item.strip()
                for item in re.split(r"[、,，/／;；]+", other_allergen)
                if item.strip()
            )
        for ingredient in profile_allergens:
            if ingredient not in disliked:
                disliked.append(ingredient)
        for ingredient in self.user_analysis_context.get("avoid_ingredients", []):
            if ingredient not in disliked:
                disliked.append(ingredient)
        memory_parts = [self._memory_prompt()]
        personalization_context = self._product_personalization_context(
            intent,
            disliked_ingredients=disliked,
        )
        if personalization_context["applied"]:
            memory_parts.append(
                "【本次已驗證的個人化條件】\n- "
                + "\n- ".join(personalization_context["applied"])
            )
        consumer_prompt = self.user_analysis_bridge.build_prompt_context(
            self.user_analysis_context,
        )
        if consumer_prompt and consumer_prompt not in memory_parts[0]:
            memory_parts.append(consumer_prompt)

        if excluded_product_ids is None:
            self.state.seen_product_ids.clear()

        result = product_agent_run(
            intent,
            disliked_ingredients=disliked,
            memory_context="\n".join(part for part in memory_parts if part),
            preference_context=self.user_analysis_context,
            excluded_product_ids=sorted(excluded_product_ids or set()),
        )
        result["personalization_context"] = personalization_context
        products = result.get("products") or []
        self.state.last_products = products
        self.state.seen_product_ids.update(
            product_stable_key(product)
            for product in products
            if product_stable_key(product)
        )
        return result

    def _product_personalization_context(
        self,
        intent: IntentParseResult,
        *,
        disliked_ingredients: list[str],
    ) -> dict:
        """Return only the recommendation signals actually applied this turn."""
        applied: list[str] = []
        skin_value = str(getattr(intent.skin_type, "value", intent.skin_type) or "")
        if skin_value and skin_value != "unknown":
            applied.append(f"膚質：{self._skin_label(skin_value)}")

        avoid = list(dict.fromkeys(str(item).strip() for item in disliked_ingredients if str(item).strip()))
        if avoid:
            applied.append("避開：" + "、".join(avoid[:3]))

        history_concerns = [
            str(item).strip()
            for item in self.user_analysis_context.get("skin_history_concerns", [])
            if str(item).strip()
        ]
        if history_concerns:
            applied.append("近期膚況：" + "、".join(history_concerns[:3]))

        textures = [
            str(item).strip()
            for item in self.user_analysis_context.get("preferred_textures", [])
            if str(item).strip()
        ]
        if textures:
            applied.append("質地偏好：" + "、".join(textures[:2]))

        budget = getattr(intent, "budget_range", None) or self.user_analysis_context.get("budget_max")
        if budget:
            budget_text = str(budget).strip()
            if budget_text.replace(".", "", 1).isdigit():
                budget_text = f"NT${budget_text} 以內"
            applied.append("預算：" + budget_text)

        return {"applied": applied}

    def _apply_skin_product_context(self, skin_context: dict | None) -> None:
        """Merge deterministic history signals without overwriting saved preferences."""
        if not skin_context:
            return
        self.user_analysis_context["skin_history_concerns"] = list(
            skin_context.get("concern_hints") or []
        )
        self.user_analysis_context["skin_history_risk_flags"] = list(
            skin_context.get("risk_flags") or []
        )
        if (
            not self.user_analysis_context.get("preferred_textures")
            and skin_context.get("preference_hints")
        ):
            self.user_analysis_context["preferred_textures"] = list(
                skin_context["preference_hints"]
            )
        if (
            not self.user_analysis_context.get("budget_max")
            and skin_context.get("budget_max_hint")
        ):
            self.user_analysis_context["budget_max"] = skin_context["budget_max_hint"]

    def _log(self, stage: str, message: str):
        print(f"[{stage}] {message}")


    def _skin_label(self, skin: str) -> str:
        mapping = {
            "oily": "油性肌", "dry": "乾性肌",
            "combination": "混合肌", "sensitive": "敏感肌",
            "normal": "中性肌", "unknown": "各膚質",
        }
        return mapping.get(skin, skin)


    def _normalize_clarification_answer(
        self,
        dimension: str,
        user_input: str,
    ) -> Optional[str]:
        text = (user_input or "").strip()
        if text in ("skip", "跳過", "⏭️ 跳過"):
            return "skip"


        aliases = {
            "skin_type": {
                "油性肌": "oily", "油肌": "oily", "oily": "oily",
                "乾性肌": "dry", "乾肌": "dry", "dry": "dry",
                "混合肌": "combination", "混合性肌膚": "combination",
                "combination": "combination",
                "敏感肌": "sensitive", "敏感性肌膚": "sensitive",
                "sensitive": "sensitive",
                "中性肌": "normal", "一般膚質": "normal", "normal": "normal",
            },
            "concern": {
                "痘痘": "抗痘", "粉刺": "抗痘", "抗痘": "抗痘",
                "毛孔": "毛孔", "乾燥": "保濕", "保濕": "保濕",
                "泛紅": "舒敏", "敏感": "舒敏", "舒緩": "舒緩",
                "出油": "控油", "控油": "控油", "暗沉": "透亮",
                "美白": "美白", "淡斑": "淡斑", "皺紋": "抗皺",
            },
            "product_category": {
                "洗面乳": "洗面乳", "化妝水": "化妝水", "面膜": "面膜",
                "精華": "精華液", "精華液": "精華液", "乳液": "乳液",
                "乳液/乳霜": "乳液",
                "乳霜": "乳霜", "保濕棉片": "保濕棉片", "防曬": "防曬乳",
                "防曬乳": "防曬乳",
            },
        }


        topic_switch_markers = (
            "換個問題", "另外想問", "順便問", "是什麼", "有什麼作用",
            "如何使用", "怎麼使用", "幫我比較", "哪個比較", "多少錢",
            "分享使用心得", "記錄膚況", "膚況紀錄",
        )
        if any(marker in text for marker in topic_switch_markers):
            return None

        dimension_aliases = aliases.get(dimension, {})
        if dimension == "concern" and text:
            return text
        if text in dimension_aliases:
            return dimension_aliases[text]
        for label, value in dimension_aliases.items():
            if label in text:
                return value
        return None


    def _clear_experience_state(self):
        self.state.is_waiting_experience_product = False
        self.state.is_waiting_experience_detail = False
        self.state.pending_product_name = ""


    def _clear_skin_tracking_state(self):
        self.state.is_waiting_skin_tracking = False
        self.state.skin_session = None
        if self.state.active_flow == "skin_record":
            self.state.active_flow = "auto"


    def _handle_skin_tracking_input(self, user_input: str, action: str | None = None) -> dict:
        """續接 Skin Tracking 多輪對話，並保存 Agent 回傳的 session。"""
        try:
            result = skin_tracking_run(
                user_message=user_input,
                user_id=self.memory.user_id,
                session=self.state.skin_session,
                action=action,
            )
        except Exception as e:
            self._clear_skin_tracking_state()
            self._log("ERROR", f"skin_tracking_agent 執行失敗：{e}")
            reply = "抱歉，膚況紀錄暫時無法使用，請稍後再試一次。"
            self.state.add_turn("assistant", reply)
            return {
                "reply": reply,
                "quick_replies": [],
                "intent": self.state.intent.model_dump() if self.state.intent else {},
                "stage": "skin_tracking_error",
            }


        if not isinstance(result, dict):
            self._clear_skin_tracking_state()
            reply = "抱歉，膚況紀錄服務回傳格式異常，請稍後再試一次。"
            self._log("ERROR", "skin_tracking_agent 未回傳 dict")
            self.state.add_turn("assistant", reply)
            return {
                "reply": reply,
                "quick_replies": [],
                "intent": self.state.intent.model_dump() if self.state.intent else {},
                "stage": "skin_tracking_error",
            }


        done = bool(result.get("done", False))
        next_session = result.get("session")


        if not done and not isinstance(next_session, dict):
            self._clear_skin_tracking_state()
            reply = "抱歉，膚況紀錄無法保存目前進度，請稍後再試一次。"
            self._log("ERROR", "skin_tracking_agent 未回傳可續接的 session")
            self.state.add_turn("assistant", reply)
            return {
                "reply": reply,
                "quick_replies": [],
                "intent": self.state.intent.model_dump() if self.state.intent else {},
                "stage": "skin_tracking_session_error",
            }


        if done:
            self._clear_skin_tracking_state()
        else:
            self.state.skin_session = next_session
            self.state.is_waiting_skin_tracking = True


        reply = result.get("reply", "好的，請繼續描述今天的膚況。")
        self.state.add_turn("assistant", reply)
        self._log(
            "SKIN_TRACKING",
            f"stage={result.get('stage', 'unknown')}, done={done}",
        )


        return {
            "reply": reply,
            "quick_replies": self._annotate_skin_quick_replies(
                result.get("quick_replies", [])
            ),
            "intent": self.state.intent.model_dump() if self.state.intent else {},
            "stage": result.get(
                "stage",
                "skin_tracking_done" if done else "skin_tracking_collect",
            ),
            "route": "skin_record",
        }


    def _is_experience_topic_switch(self, user_input: str) -> bool:
        """判斷等待商品資料期間，使用者是否明確提出另一個新請求。"""
        text = (user_input or "").strip()
        if not text:
            return False
        return any(marker in text for marker in EXPERIENCE_TOPIC_SWITCH_MARKERS)


    def _merge_product_identity_answer(
        self,
        user_input: str,
    ) -> tuple[str, str]:
        """
        合併等待中的泛稱商品與使用者補充的品牌／完整名稱。

        例：
        pending="美白面膜", answer="未來美" -> "未來美 美白面膜"
        pending="美白面膜", answer="未來美美白面膜" -> "未來美美白面膜"
        """
        answer = (user_input or "").strip(" ，,。！!？?")
        pending = (self.state.pending_product_name or "").strip()

        for prefix in ("品牌是", "牌子是", "商品是", "名稱是", "叫做", "是"):
            if answer.startswith(prefix):
                answer = answer[len(prefix):].strip(" ：:，,。")
                break

        if not answer:
            return pending, ""

        if pending and pending in answer:
            product_name = answer
            brand_name = answer.split(pending, 1)[0].strip(" 的-－：:")
        elif pending and answer in pending:
            product_name = pending
            brand_name = ""
        elif pending:
            product_name = f"{answer} {pending}".strip()
            brand_name = answer
        else:
            product_name = answer
            brand_name = ""

        return product_name, brand_name


    def _handle_experience_input(self, user_input: str) -> Optional[dict]:
        if (
            self.state.is_waiting_experience_product
            and self.state.pending_product_name
        ):
            stage = "waiting_product_identity"
        elif self.state.is_waiting_experience_product:
            stage = "waiting_product_name"
        elif self.state.is_waiting_experience_detail:
            stage = "waiting_experience_detail"
        else:
            stage = ""


        last_product_names = [
            product.get("name", "")
            for product in self.state.last_products
            if product.get("name")
        ]


        parsed = parse_product_experience(
            user_input=user_input,
            pending_product_name=self.state.pending_product_name,
            conversation_stage=stage,
            session_history=self.state.history[:-1],
            memory_context=self._memory_prompt(),
            last_product_names=last_product_names,
            model=self.model,
        )


        # 已經明確追問品牌／完整名稱後，只要使用者正常補充商品資訊，
        # 就視為完成商品識別，不再因「美白面膜」等通用詞重複追問。
        if stage == "waiting_product_identity":
            merged_product_name, inferred_brand_name = (
                self._merge_product_identity_answer(user_input)
            )
            if merged_product_name:
                parsed["is_experience"] = True
                parsed["product_name"] = merged_product_name
                parsed["needs_product_name"] = False
                parsed["needs_product_identity"] = False

                if inferred_brand_name and not parsed.get("brand_name"):
                    parsed["brand_name"] = inferred_brand_name

                if not parsed.get("attributes"):
                    parsed["needs_experience_detail"] = True
                    parsed["reply"] = (
                        f"了解，是「{merged_product_name}」。"
                        "使用後的感受如何呢？"
                    )


        if not parsed.get("is_experience"):
            self._clear_experience_state()
            return None


        product_name = (parsed.get("product_name") or "").strip()
        reply = (parsed.get("reply") or "").strip()


        if parsed.get("needs_product_name"):
            self.state.is_waiting_experience_product = True
            self.state.is_waiting_experience_detail = False
            self.state.pending_product_name = ""
            if not reply:
                reply = (
                    "了解，您之前有買過相關商品。"
                    "可以告訴我是什麼品牌，或包裝上的完整商品名稱嗎？"
                )
            stage_name = "experience_waiting_product"
        elif parsed.get("needs_product_identity"):
            self.state.is_waiting_experience_product = True
            self.state.is_waiting_experience_detail = False
            self.state.pending_product_name = product_name
            # 結構性追問由 Orchestrator 固定，避免模型跳過品牌問題。
            reply = (
                f"了解，您買過「{product_name}」。"
                "可以再告訴我是什麼品牌，或提供包裝上的完整商品名稱嗎？"
            )
            stage_name = "experience_waiting_identity"
        elif parsed.get("needs_experience_detail"):
            self.state.is_waiting_experience_product = False
            self.state.is_waiting_experience_detail = True
            self.state.pending_product_name = product_name
            if not reply:
                reply = (
                    f"了解，我記下您買過「{product_name}」。"
                    "使用後的感受如何呢？"
                )
            stage_name = "experience_waiting_detail"
        else:
            self.memory.record_product_experience(
                product_name=product_name,
                brand_name=parsed.get("brand_name", ""),
                sentiment=parsed.get("sentiment", "unknown"),
                attributes=parsed.get("attributes") or [],
                avoid_preferences=parsed.get("avoid_preferences") or [],
                preferred_features=parsed.get("preferred_features") or [],
            )
            self._clear_experience_state()
            if not reply:
                reply = "了解，我已記下這次使用經驗，之後推薦時會一起參考。"
            stage_name = "experience_recorded"
            self._log(
                "EXPERIENCE",
                json.dumps(parsed, ensure_ascii=False),
            )


        self.state.is_waiting_clarification = False
        self.state.clarify_dimension = ""
        self.state.add_turn("assistant", reply)


        return {
            "reply": reply,
            "quick_replies": [],
            "intent": self.state.intent.model_dump() if self.state.intent else {},
            "stage": stage_name,
        }


    def _update_intent(self, dimension: str, value: str):
        current = self.state.intent.model_dump()
        if dimension == "skin_type":
            current["skin_type"] = value
        elif dimension == "concern":
            if value not in current.get("concern", []):
                current.setdefault("concern", []).append(value)
        elif dimension == "product_category":
            current["product_category"] = value
        current["ambiguity_score"] = max(0.0, current.get("ambiguity_score", 1.0) - 0.4)
        try:
            self.state.intent = IntentParseResult(**current)
        except Exception as e:
            self._log("ERROR", f"更新 intent 失敗：{e}")


    def _refresh_intent_scores(self) -> None:
        """Refresh routing certainty and target Agent readiness after enrichment."""
        if self.state.intent is None:
            return
        current = self.state.intent.model_dump()
        current["routing_confidence"] = calculate_routing_confidence(self.state.intent)
        current["task_completeness"] = calculate_task_completeness(self.state.intent)
        try:
            self.state.intent = IntentParseResult(**current)
        except Exception as error:
            self._log("ERROR", f"更新 intent 分數失敗：{error}")

    def _fix_unclear_intent(self):
        if self.state.intent.intent_type != IntentType.UNCLEAR:
            return
        current = self.state.intent.model_dump()
        if self.state.intent.product_category.value != "不明":
            current["intent_type"] = "find_product"
        elif self.state.intent.concern:
            current["intent_type"] = "symptom_solving"
        else:
            current["intent_type"] = "recommendation"
        try:
            self.state.intent = IntentParseResult(**current)
        except Exception as e:
            self._log("ERROR", f"推斷 intent_type 失敗：{e}")


    def _next_missing_dimension(self) -> Optional[str]:
        intent = self.state.intent
        if intent.skin_type.value == "unknown" and not self.state.skin_type_skipped:
            return "skin_type"
        if not intent.concern:
            return "concern"
        if intent.product_category.value == "不明":
            return "product_category"
        return None


    def _ask_clarification(self, dimension: str) -> dict:
        questions = {
            "routing": (
                "想先確認一下：您現在主要想找產品、處理肌膚狀況、了解成分用法、比較產品，還是分享使用心得？",
                [
                    QuickReply(
                        label="🔎 找適合產品",
                        value="我想找適合的保養品",
                    ),
                    QuickReply(
                        label="🧴 改善肌膚狀況",
                        value="我想改善目前的肌膚問題",
                    ),
                    QuickReply(
                        label="💡 詢問成分用法",
                        value="我想了解保養成分或使用方式",
                    ),
                    QuickReply(label="⚖️ 比較產品", value="我想比較保養品"),
                    QuickReply(label="✍️ 分享使用心得", value="我想分享產品使用心得"),
                ],
            ),
            "skin_type": (
                "請問您是什麼膚質呢？選擇後我可以幫您推薦最適合的保養品！",
                SKIN_TYPE_REPLIES,
            ),
            "concern": (
                "您最想改善哪個肌膚問題呢？",
                CONCERN_REPLIES,
            ),
            "product_category": (
                "您想找哪種類型的保養品呢？",
                CATEGORY_REPLIES,
            ),
        }
        question_text, quick_replies = questions[dimension]
        self.state.is_waiting_clarification = True
        self.state.clarify_dimension = dimension
        self.state.add_turn("assistant", question_text)
        self._log("STEP", f"追問維度：{dimension}")
        return {
            "reply": question_text,
            "quick_replies": [{"label": qr.label, "value": qr.value} for qr in quick_replies],
            "intent": self.state.intent.model_dump(),
            "stage": f"clarification_{dimension}",
        }


    def _get_cycle_replies(
        self,
        intent: IntentParseResult,
        include_more: bool = True,
    ) -> list:
        return [
            {"label": "調整推薦條件", "value": "cycle_change_condition", "action": "change_condition"},
            {"label": "我要記錄膚況", "value": "cycle_skin_tracking", "action": "skin_tracking"},
        ]

    def _normalize_cycle_action(self, user_input: str) -> Optional[str]:
        value = (user_input or "").strip()
        if value == "cycle_rank_feedback":
            return "cycle_change_condition"
        if value.startswith("cycle_"):
            return value

        aliases = {
            "看更多": "cycle_more",
            "看更多商品": "cycle_more",
            "換一批": "cycle_replace_batch",
            "下一批": "cycle_replace_batch",
            "不要重複": "cycle_replace_batch",
            "再多給我幾個": "cycle_more",
            "再多幾個": "cycle_more",
            "更換商品類型": "cycle_change_category",
            "換品類": "cycle_change_category",
            "換個品類": "cycle_change_category",
            "換商品類別": "cycle_change_category",
            "調整推薦條件": "cycle_change_condition",
            "調整條件": "cycle_change_condition",
            "排序不對，幫我調整": "cycle_change_condition",
            "換搜尋條件": "cycle_change_condition",
            "重新搜尋": "cycle_change_condition",
        }
        if value in {"skip", "跳過"}:
            return "cycle_stale_skip"

        if self.state.intent:
            category = self.state.intent.product_category.value
            if category and category != "不明" and value == f"換個{category}":
                return "cycle_more"

        return aliases.get(value)

    def _enrich_intent_from_memory(self, intent: IntentParseResult) -> IntentParseResult:
        if intent is None:
            return intent
        current = intent.model_dump()
        changed = False
        if current['skin_type'] == 'unknown' and self.memory.get('skin_type'):
            memory_skin = str(self.memory.get('skin_type') or '').strip()

            skin_type_map = {
                '油性肌': 'oily',
                '油肌': 'oily',
                'oily': 'oily',
                '乾性肌': 'dry',
                '乾肌': 'dry',
                'dry': 'dry',
                '混合肌': 'combination',
                '混合性肌': 'combination',
                'combination': 'combination',
                '敏感肌': 'sensitive',
                'sensitive': 'sensitive',
                '中性肌': 'normal',
                'normal': 'normal',
            }

            normalized_skin = skin_type_map.get(
                memory_skin.lower(),
                skin_type_map.get(memory_skin),
            )

            if normalized_skin:
                current['skin_type'] = normalized_skin
                self._log(
                    "MEMORY",
                    f"補入膚質：{memory_skin} -> {normalized_skin}",
                )
                changed = True
        if not current['concern'] and self.memory.get('concerns'):
            current['concern'] = self.memory.get('concerns')[-2:]
            self._log("MEMORY", f"補入訴求：{current['concern']}")
            changed = True
        if not current['concern'] and self.user_analysis_context.get('preferred_effects'):
            current['concern'] = self.user_analysis_context.get('preferred_effects')[:2]
            self._log("偏好", f"補入長期功效偏好：{current['concern']}")
            changed = True
        raw_query = str(current.get("raw_query") or "")
        no_budget_markers = (
            "\u50f9\u683c\u4e0d\u9650",
            "\u50f9\u9322\u4e0d\u9650",
            "\u9810\u7b97\u4e0d\u9650",
            "\u4e0d\u9650\u9810\u7b97",
            "\u6c92\u6709\u9810\u7b97\u9650\u5236",
            "\u4e0d\u8a2d\u9810\u7b97",
        )
        explicitly_unlimited_budget = any(
            marker in raw_query for marker in no_budget_markers
        )
        if (
            not explicitly_unlimited_budget
            and not current.get('budget_range')
            and self.memory.get('budget_range')
        ):
            current['budget_range'] = self.memory.get('budget_range')
            self._log("MEMORY", f"補入預算：{current['budget_range']}")
            changed = True
        if (
            not explicitly_unlimited_budget
            and not current.get('budget_range')
            and self.user_analysis_context.get('budget_max')
        ):
            current['budget_range'] = f"{self.user_analysis_context['budget_max']}元以內"
            self._log("偏好", f"補入常用預算：{current['budget_range']}")
            changed = True
        if (
            str(current.get('texture_preference') or '') in {'', '不明'}
            and self.user_analysis_context.get('preferred_textures')
        ):
            known_textures = {'清爽', '滋潤', '凝膠', '乳霜', '油類'}
            preferred_texture = next(
                (
                    str(value).strip()
                    for value in self.user_analysis_context['preferred_textures']
                    if str(value).strip() in known_textures
                ),
                '',
            )
            if preferred_texture:
                current['texture_preference'] = preferred_texture
                self._log("偏好", f"補入質地偏好：{preferred_texture}")
                changed = True
        if changed:
            try:
                if current.get('intent_type') == 'unclear':
                    if current.get('product_category', '不明') != '不明':
                        current['intent_type'] = 'find_product'
                    elif current.get('concern'):
                        current['intent_type'] = 'symptom_solving'
                    else:
                        current['intent_type'] = 'recommendation'
                    self._log("MEMORY", f"修正 intent_type：{current['intent_type']}")
                new_intent = IntentParseResult(**current)
                new_intent.ambiguity_score = calculate_ambiguity_score(new_intent)
                self._log("MEMORY", f"補齊後 ambiguity_score：{new_intent.ambiguity_score}")
                return new_intent
            except Exception as e:
                self._log("ERROR", f"補齊 intent 失敗：{e}")
                return intent
        return intent


    def _handle_structured_action(self, action: str, user_input: str) -> dict:
        """Dispatch a trusted action without forcing it through generic intents."""
        self.state.last_action = action
        self._log("ACTION", f"action={action}, step={self._current_dialogue_step()}")

        if action in {"more_products", "replace_batch"}:
            cycle_action = (
                "cycle_more"
                if action == "more_products"
                else "cycle_replace_batch"
            )
            result = self._handle_cycle_action(cycle_action)
            result["action"] = action
            result["replace_products"] = action == "replace_batch"
            return result

        if action in {"record_location", "record_issue", "record_other_issue"}:
            self.state.active_flow = "skin_record"
            result = self._handle_skin_tracking_input(user_input, action=action)
            result["action"] = action
            return result
        if action in {"record_correction", "record_severity", "record_confirm"}:

            if action == "record_confirm" and not (
                self.state.is_waiting_skin_tracking
                or self.state.skin_session
            ):
                return {
                    "reply": "目前沒有待確認的膚況紀錄，請先描述今天的膚況。",
                    "quick_replies": [],
                    "intent": {},
                    "stage": "record_action_missing_session",
                    "action": action,
                }
            self.state.active_flow = "skin_record"
            tracking_input = "confirm_yes" if action == "record_confirm" else user_input
            result = self._handle_skin_tracking_input(tracking_input)
            result["action"] = action
            return result

        if action == "cancel":
            self._clear_experience_state()
            self._clear_skin_tracking_state()
            self.state.is_waiting_clarification = False
            self.state.clarify_dimension = ""
            self.state.is_waiting_confirm = False
            self.state.is_waiting_rank_feedback = False
            self.state.intent = None
            self.state.last_products = []
            self.state.seen_product_ids.clear()
            return {
                "reply": "已取消目前的操作。你可以重新描述需求，或選擇其他功能。",
                "quick_replies": [],
                "intent": {},
                "stage": "action_cancelled",
                "action": "cancel",
            }

        return {
            "reply": "這個操作目前無法執行，請重新選擇。",
            "quick_replies": [],
            "intent": {},
            "stage": "unknown_action",
            "action": action,
        }


    def _handle_cycle_action(self, action: str) -> dict:
        self._log("CYCLE", f"action={action}")


        if action in {"cycle_more", "cycle_replace_batch"}:
            is_replace_batch = action == "cycle_replace_batch"
            if not self.state.intent:
                return {
                    "reply": "目前沒有可延續的商品搜尋，請先告訴我想找的商品。",
                    "quick_replies": [],
                    "intent": {},
                    "stage": (
                        "cycle_replace_batch_missing_intent"
                        if is_replace_batch
                        else "cycle_more_missing_intent"
                    ),
                }

            product_result = self._run_product_agent(
                self.state.intent,
                excluded_product_ids=set(self.state.seen_product_ids),
            )
            products = product_result.get("products") or []
            self.state.last_products = products
            self.state.seen_product_ids.update(
                product_stable_key(product)
                for product in products
                if product_stable_key(product)
            )
            if not products:
                return {
                    "reply": "目前這組條件暫時找不到下一批商品。請點「調整推薦條件」縮放品類、需求或預算。",
                    "quick_replies": self._get_cycle_replies(
                        self.state.intent,
                        include_more=False,
                    ),
                    "intent": self.state.intent.model_dump(),
                    "stage": (
                        "cycle_replace_batch_exhausted"
                        if is_replace_batch
                        else "cycle_more_exhausted"
                    ),
                }

            return {
                "reply": _format_ranked_products(
                    product_result,
                    self.state.pain_gain,
                    self.state.intent,
                ),
                "quick_replies": self._get_cycle_replies(self.state.intent),
                "intent": self.state.intent.model_dump(),
                "products": products,
                "stage": (
                    "cycle_replace_batch"
                    if is_replace_batch
                    else "cycle_more"
                ),
            }
        elif action == "cycle_change_category":
            current = self.state.intent.model_dump()
            current["product_category"] = "不明"
            self.state.intent = IntentParseResult(**current)
            self.state.is_waiting_clarification = False
            self.state.skin_type_skipped = False
            self.state.seen_product_ids.clear()
            self.state.last_products = []
            return self._ask_clarification("product_category")
        elif action == "cycle_change_condition":
            self.state.reset()
            return {
                "reply": "沒問題！請告訴我想調整的地方，並詳細說明您的膚況與商品期待（例如：價格太高、想要某個成分、偏好某個品牌、質地或更換商品種類），我會重新搜尋並調整推薦順序。",
                "quick_replies": [],
                "intent": {},
                "stage": "cycle_reset",
            }
        elif action == "cycle_skin_tracking":
            self._clear_experience_state()
            self.state.is_waiting_clarification = False
            self.state.clarify_dimension = ""
            self.state.is_waiting_confirm = False
            self.state.is_waiting_rank_feedback = False
            self._clear_skin_tracking_state()
            return self._handle_skin_tracking_input("開始記錄膚況")
        elif action == "cycle_rank_feedback":
            self.state.is_waiting_rank_feedback = True
            return {
                "reply": "好的！請告訴我您覺得這樣的排序哪裡不滿意呢？\n（例如：價格太高、想要某個成分、偏好某個品牌...）",
                "quick_replies": [],
                "intent": self.state.intent.model_dump() if self.state.intent else {},
                "stage": "rank_feedback_waiting",
            }
        elif action == "cycle_stale_skip":
            return {
                "reply": "這個跳過選項已失效，請使用目前顯示的最新選項。",
                "quick_replies": self._get_cycle_replies(self.state.intent) if self.state.intent else [],
                "intent": self.state.intent.model_dump() if self.state.intent else {},
                "stage": "cycle_stale_skip",
            }
        return {
            "reply": "請選擇一個選項",
            "quick_replies": self._get_cycle_replies(self.state.intent),
            "intent": self.state.intent.model_dump(),
            "stage": "cycle_unknown",
        }


    def _dispatch(self, intent: IntentParseResult) -> str:
        self._log("DISPATCH", f"intent_type={intent.intent_type.value}")


        # 特判：安全性諮詢
        if (intent.intent_type == IntentType.FIND_PRODUCT and
                any(kw in (intent.raw_query or '') for kw in SAFETY_KEYWORDS)):
            self._log("DISPATCH", "偵測到安全性諮詢")
            return self._handle_safety_query(intent)


        if intent.intent_type == IntentType.FIND_PRODUCT:
            return self._handle_find_product(intent)
        elif intent.intent_type == IntentType.SYMPTOM_SOLVING:
            return self._handle_symptom_solving(intent)
        elif intent.intent_type == IntentType.RECOMMENDATION:
            return self._handle_recommendation(intent)
        elif intent.intent_type == IntentType.PRICE_COMPARISON:
            return self._handle_price_comparison(intent)
        elif intent.intent_type == IntentType.KNOWLEDGE_QUERY:
            if should_search_named_product(intent):
                # Product Agent is the local-first gate. Tavily is reached only when
                # the named product is absent from the reviewed local catalogue.
                product_result = self._run_product_agent(intent)
                products = product_result.get("products") or []
                is_web = product_result.get("source") == "tavily" or any(
                    product.get("is_web_result") for product in products
                )
                provider = "tavily" if is_web else "neo4j"
                evidence_packet = product_result.get("evidence_packet") or build_product_evidence_packet(
                    query=intent.raw_query,
                    products=products,
                    provider=provider,
                    requested_fields=infer_product_detail_fields(intent.raw_query),
                    fallback_reason=(
                        "local_catalogue_miss" if is_web or not products else "local_catalogue_hit"
                    ),
                )
                result = knowledge_agent_run(
                    user_query=intent.raw_query,
                    user_skin_type=intent.skin_type.value,
                    session_id=f"{self.user_id}:{self.conversation_id}",
                    memory_context=self._memory_prompt(),
                    parsed_intent=intent,
                    external_evidence=evidence_packet,
                )
                return result.get(
                    "reply",
                    "目前找到商品資料，但暫時無法整理回答，請稍後再試。",
                )
            result = knowledge_agent_run(
                user_query=intent.raw_query,
                user_skin_type=intent.skin_type.value,
                session_id=f"{self.user_id}:{self.conversation_id}",
                memory_context=self._memory_prompt(),
                parsed_intent=intent,
            )
            return result.get("reply", "抱歉，查詢知識庫時發生錯誤。")
        else:
            return "抱歉，我不太理解您的需求，可以再詳細說明一下嗎？"


    def _handle_find_product(self, intent: IntentParseResult) -> str:
        exclude_previous = bool(
            self.state.last_products
            and _is_product_refinement_query(intent.raw_query)
            and _wants_new_product_batch(intent.raw_query)
        )
        result = self._run_product_agent(
            intent,
            excluded_product_ids=(
                set(self.state.seen_product_ids) if exclude_previous else None
            ),
        )
        products = result.get("products") or []
        self.state.last_products = products
        self.state.seen_product_ids.update(
            product_stable_key(product)
            for product in products
            if product_stable_key(product)
        )
        return _format_ranked_products(result, self.state.pain_gain, intent)


    def _handle_symptom_solving(self, intent: IntentParseResult) -> str:
        result = knowledge_agent_run(
            user_query=intent.raw_query,
            user_skin_type=intent.skin_type.value,
            session_id=f"{self.user_id}:{self.conversation_id}",
            memory_context=self._memory_prompt(),
            parsed_intent=intent,
        )
        return result.get("reply", "目前無法取得相關膚況知識，請稍後再試。")


    def _handle_recommendation(self, intent: IntentParseResult) -> str:
        result = self._run_product_agent(intent)
        self.state.last_products = result.get('products', [])
        self._log("RECOMMENDATION", f"skin={intent.skin_type.value}")
        reply = _format_ranked_products(result, self.state.pain_gain, intent)
        reply += "\n（如需結合您的膚況紀錄做個人化調整，請選擇「記錄膚況」）"
        return reply


    def _handle_safety_query(self, intent: IntentParseResult) -> str:
        skin  = intent.skin_type.value
        reply = ""
        try:
            knowledge_result = knowledge_agent_run(
                user_query=intent.raw_query,
                user_skin_type=skin,
                session_id=f"{self.user_id}:{self.conversation_id}",
                memory_context=self._memory_prompt(),
                parsed_intent=intent,
            )
            k_reply = knowledge_result.get("reply", "")
            if k_reply:
                reply += f"【成分安全諮詢】\n{k_reply}\n\n"
        except Exception as e:
            self._log("WARNING", f"knowledge_agent 查詢失敗：{e}")


        product_result = self._run_product_agent(intent)
        self.state.last_products = product_result.get('products', [])


        if product_result.get('products'):
            reply += "【相關保養品推薦】\n"
            reply += _format_ranked_products(product_result, self.state.pain_gain, intent)
        return reply


    def _handle_price_comparison(self, intent: IntentParseResult) -> str:
        category = intent.product_category.value
        budget   = f"預算 {intent.budget_range} 元內" if intent.budget_range else "不限預算"
        self._log("PRICE_COMPARISON", f"category={category}, budget={budget}")


        result = self._run_product_agent(intent)
        self.state.last_products = result.get('products', [])


        if not result.get('products'):
            return f"目前找不到{category}的比較結果，請試試調整條件。"


        reply = f"幫您比較{category}的 CP 值（{budget}）：\n\n"
        for i, p in enumerate(result['products']):
            partner_tag = " ⭐三友" if p.get('is_partner') else ""
            budget_tag  = "（超預算）" if p.get('over_budget') else ""
            ings = "、".join((p.get('ingredients') or [])[:3]) or "—"
            reply += f"{i+1}. {p['name']}{partner_tag}\n"
            reply += f"   NT${p['price']}{budget_tag}｜主要成分：{ings}\n"
            if p.get('copy'):
                reply += f"   {_trim_to_sentence(p.get('copy',''), 120)}\n"
            reply += "\n"
        reply += "💡 排序已綜合考量價格、成分與您的膚質訴求。"
        return reply


    # ─── 主流程 ───


    def get_welcome(self) -> dict:
        """
        對話開場：尚未輸入前的歡迎訊息 + 引導按鈕
        按鈕對應三個 sub-agent 能力：商品推薦 / 藥妝知識 / 膚況記錄
        老用戶根據 Global Memory 個人化建議
        """
        skin    = self.memory.get('skin_type')
        cats    = self.memory.get('preferred_categories') or []
        skin_zh = self._skin_label(skin) if skin else None


        if skin_zh and cats:
            # 老用戶：個人化開場
            reply = (f"歡迎回來！上次您找過{cats[-1]}，"
                     f"今天想繼續為您的{skin_zh}找保養品，"
                     f"還是查成分知識、記錄膚況呢？")
            suggestions = [
                {"label": f"{skin_zh}找{cats[-1]}", "value": f"{skin_zh}找{cats[-1]}"},
                {"label": "換個品類逛逛",           "value": "推薦其他保養品"},
                {"label": "查成分知識",             "value": "我想了解保養品成分"},
                {"label": "記錄今天膚況",           "value": "cycle_skin_tracking"},
            ]
        else:
            # 新用戶：能力導覽開場
            reply = ("嗨！我是您的藥妝保養小幫手\n"
                     "可以幫您：\n"
                     "1️⃣ 依膚質推薦保養品（含成分安全把關）\n"
                     "2️⃣ 解答成分知識\n"
                     "3️⃣ 記錄追蹤您的膚況變化\n\n"
                     "請直接輸入需求，或點下方簡易範例：")
            suggestions = [
                {"label": "油肌找抗痘洗面乳",   "value": "油肌找抗痘洗面乳"},
                {"label": "乾肌找保濕面膜",     "value": "乾肌找保濕面膜"},
                {"label": "敏感肌找舒緩精華",   "value": "敏感肌找舒緩精華液"},
                {"label": "水楊酸適合每天用嗎", "value": "水楊酸可以每天用嗎"},
                {"label": "記錄我的膚況",       "value": "cycle_skin_tracking"},
            ]


        return {"reply": reply, "quick_replies": suggestions,
                "intent": {}, "stage": "welcome"}


    @monitor_agent_turn
    def process(
        self,
        user_input: str,
        conversation_flow: str | None = None,
        action: str | None = None,
        on_delta=None,
        freshness_context: dict | None = None,
    ) -> dict:
        """每輪統一載入 GetZep context，並在任何回覆路徑寫回 assistant 訊息。"""
        requested_flow = str(conversation_flow or "").strip().lower()
        if requested_flow in {"product_search", "skin_record", "skin_consult", "auto"}:
            self.state.active_flow = requested_flow
        effective_flow = self.state.active_flow or "auto"
        # 每一輪都重新讀取主資料庫，避免 Profile 頁面更新後，既有
        # Agent session 仍使用舊值；同時只同步本輪明確自述的穩定膚質資料。
        profile_started = time.perf_counter()
        self._prepare_profile_context(user_input)
        observe_pipeline_stage("root", "profile_context", profile_started)

        # 先讀取既有分析，讓本輪 intent parser 與其他 Agent 都能看到
        # 消費人格背景。此步驟不會觸發重新分析，也不改變 Root 路由。
        user_analysis_started = time.perf_counter()
        self.user_analysis_context = self.user_analysis_bridge.refresh(
            basic_profile=self.basic_profile,
        )
        observe_pipeline_stage(
            "root",
            "user_analysis_preload",
            user_analysis_started,
        )

        # 必須在 begin_turn 寫入本輪 user message 前執行，
        # 才能確認這是不是目前 conversation 的第一句真實訊息。
        # Terra 或標題寫入失敗時，不能中斷原本聊天流程。
        title_updated = False
        title_started = time.perf_counter()

        try:
            title_updated = bool(
                self.title_service.maybe_title_conversation(
                    adapter=self.memory,
                    first_message=_clean_internal_user_message(user_input),
                    flow=effective_flow,
                )
            )
        except Exception as e:
            self._log(
                "WARNING",
                f"ConversationTitleService 失敗：{e}",
            )
        observe_pipeline_stage("root", "conversation_title", title_started)


        memory_started = time.perf_counter()
        if str(self.user_id).startswith("guest-"):
            self._turn_memory = None
            self._log("MEMORY", "訪客工作階段略過遠端長期記憶")
        else:
            try:
                self._turn_memory = self.memory_service.begin_turn(
                    user_message=_clean_internal_user_message(user_input),
                    profile_summary=self._profile_summary_for_prompt(),
                )
            except Exception as e:
                self._turn_memory = None
                self._log("WARNING", f"ConversationMemoryService.begin_turn 失敗：{e}")
        observe_pipeline_stage("root", "memory_begin_turn", memory_started)


        try:
            result = self._process_current_turn(
                user_input,
                conversation_flow=effective_flow,
                explicit_action=action,
                on_delta=on_delta,
                freshness_context=freshness_context,
            )
        except Exception:
            self._pending_profile_sync = None
            self.memory_service.finish_error_turn(
                "抱歉，系統忙碌中，請稍後再試一次。"
            )
            raise


        sync_result = self._pending_profile_sync
        if (
            sync_result is not None
            and sync_result.changed_fields
            and not sync_result.update_only
            and isinstance(result.get("reply"), str)
        ):
            result["reply"] = sync_result.acknowledgement + "\n\n" + result["reply"]


        response_metadata = {


            "quick_replies": result.get("quick_replies", []),


            "cards": result.get("cards", []),


            "route": result.get("route"),


            "action": result.get("action"),


            "replace_products": result.get("replace_products", False),


            "stage": result.get("stage"),


        }


        memory_finish_started = time.perf_counter()
        self.memory_service.finish_turn(


            result.get("reply", ""),


            metadata=response_metadata,


        )
        observe_pipeline_stage(
            "root",
            "memory_finish_turn",
            memory_finish_started,
        )
        self._pending_profile_sync = None
        return result


    def _emergency_result(self, reason: str) -> dict:
        self._log("SAFETY", f"緊急症狀攔截：{reason}")
        return {
            "reply": (
                "這可能是嚴重過敏反應。請立即撥打 119，或請身旁的人協助叫救護車；"
                "不要自行駕車，也不要等待症狀自行消退。若您有醫師開立的緊急藥物，"
                "請依醫囑使用。"
            ),
            "quick_replies": [],
            "intent": {},
            "stage": "emergency",
            "route": "emergency",
        }

    def _process_current_turn(
        self,
        user_input: str,
        conversation_flow: str | None = None,
        explicit_action: str | None = None,
        on_delta=None,
        freshness_context: dict | None = None,
    ) -> dict:
        if conversation_flow in {"product_search", "skin_record", "skin_consult", "auto"}:
            self.state.active_flow = conversation_flow
        elif (
            self.state.active_flow == "skin_record"
            and not self.state.is_waiting_skin_tracking
            and self.state.skin_session is None
        ):
            # Recover conversations completed before skin-record flow release existed.
            self.state.active_flow = "auto"
        self.state.add_turn("user", _clean_internal_user_message(user_input))
        self._log("INPUT", f"turn={self.state.turn_count}, input={user_input!r}")

        # 緊急症狀只看本句文字，必須先於任何資料查詢與多輪狀態，
        # 避免「我是什麼膚質？…呼吸困難」被基本資料查詢先接走。
        emergency_route = classify_dialogue_route(user_input)
        if emergency_route and emergency_route["route"] == "emergency":
            return self._emergency_result(emergency_route["reason"])

        # 基本資料查詢與純資料更新不交給 LLM 猜測，也必須放在
        # Input Guard 與多輪狀態之前，避免「我是什麼膚質」被誤攔截。
        profile_result = self._handle_profile_interaction(user_input)
        if profile_result is not None:
            return profile_result

        routing_context = self._routing_context()
        dialogue_route = classify_dialogue_route(
            user_input,
            conversation_flow=self.state.active_flow,
            current_step=routing_context["current_step"],
            structured_state=routing_context,
            history=self.state.history[:-1],
            action=explicit_action,
        )
        if dialogue_route and dialogue_route["route"] == "emergency":
            return self._emergency_result(dialogue_route["reason"])

        if dialogue_route and dialogue_route["route"] == "action":
            return self._handle_structured_action(
                dialogue_route.get("action", ""),
                _clean_internal_user_message(user_input),
            )

        recall_reply = None
        if (
            self._turn_memory
            and self._turn_memory.context_loaded
        ):
            recall_reply = conversation_recall_reply(
                user_input,
                self._turn_memory.zep_context,
            )
        if recall_reply is not None:
            self._log(
                "MEMORY",
                "使用同一 conversation 的明確使用者陳述回答回憶問題",
            )
            return {
                "reply": recall_reply,
                "quick_replies": [],
                "intent": {
                    "intent_type": IntentType.KNOWLEDGE_QUERY.value,
                },
                "stage": "dispatch",
            }


        # ── Fresh external-data gate (no model call) ──
        # Weather is the first enabled domain.  Other volatile domains are
        # classified by the service but remain outside this skincare agent.
        if os.getenv("TEMPORAL_FRESHNESS_GUARD_ENABLED", "1").strip().lower() not in {
            "0", "false", "off", "no"
        }:
            freshness_decision = (freshness_context or {}).get(
                "decision"
            ) or classify_freshness_query(user_input)
            if (
                freshness_decision
                and freshness_decision["requires_external_data"]
            ):
                self._log(
                    "FRESHNESS_GUARD",
                    ",".join(freshness_decision["reason_codes"]),
                )
                grounded_reply = build_weather_reply(freshness_context or {})
                return {
                    "reply": grounded_reply or build_freshness_guard_reply(freshness_decision),
                    "quick_replies": [],
                    "intent": {"intent_type": IntentType.KNOWLEDGE_QUERY.value},
                    "stage": "freshness_grounded" if grounded_reply else "freshness_guard",
                    "route": "skin_consult" if grounded_reply else "clarify",
                    "freshness": freshness_decision,
                    "weather": (freshness_context or {}).get("observation"),
                }


        # ── Default question deterministic routing ──
        #
        # 這兩類入口不交給 LLM 自由判斷：
        #
        # 1. 「你最想改善哪個肌膚問題？」
        #    → 固定進 concern clarification
        #
        # 2. 「你的/我的膚質最適合哪個保養品？」
        #    → 有已知膚質：改寫成明確商品推薦 query
        #    → 無已知膚質：固定進 skin_type clarification
        #
        normalized_input = (
            user_input
            .strip()
            .replace("？", "")
            .replace("?", "")
        )

        concern_default_triggers = (
            "你最想改善哪個肌膚問題",
            "您最想改善哪個肌膚問題",
            "我最想改善哪個肌膚問題",
        )

        if any(
            trigger in normalized_input
            for trigger in concern_default_triggers
        ):
            self.state.intent = IntentParseResult(
                raw_query=user_input,
            )
            return self._ask_clarification(
                "concern"
            )

        skin_product_triggers = (
            "你的膚質最適合哪個保養品",
            "我的膚質最適合哪個保養品",
            "依我的膚質推薦保養品",
            "依膚質推薦保養品",
            "什麼保養品適合我的膚質",
            "什麼保養品最適合我的膚質",
        )

        if any(
            trigger in normalized_input
            for trigger in skin_product_triggers
        ):
            memory_skin = str(
                self.memory.get("skin_type")
                or ""
            ).strip()

            skin_type_map = {
                "油性肌": "oily",
                "油肌": "oily",
                "oily": "oily",
                "乾性肌": "dry",
                "乾肌": "dry",
                "dry": "dry",
                "混合肌": "combination",
                "混合性肌": "combination",
                "combination": "combination",
                "敏感肌": "sensitive",
                "sensitive": "sensitive",
                "中性肌": "normal",
                "normal": "normal",
            }

            normalized_skin = (
                skin_type_map.get(
                    memory_skin.lower()
                )
                or skin_type_map.get(
                    memory_skin
                )
            )

            if not normalized_skin:
                self.state.intent = IntentParseResult(
                    raw_query=user_input,
                )
                return self._ask_clarification(
                    "skin_type"
                )

            skin_zh_map = {
                "oily": "油性肌",
                "dry": "乾性肌",
                "combination": "混合肌",
                "sensitive": "敏感肌",
                "normal": "中性肌",
            }

            original_input = user_input

            user_input = (
                f"{skin_zh_map[normalized_skin]}"
                "找適合的保養品"
            )

            self._log(
                "DEFAULT_ROUTE",
                (
                    f"{original_input!r} -> "
                    f"{user_input!r}"
                ),
            )


        # ── Unsupported product category guard ──
        # 商品資料庫目前沒有粉底液。
        # 不可因 category 無法解析，就 fallback 成一般保養品推薦。
        normalized_product_input = (
            user_input
            .strip()
            .replace(" ", "")
        )

        unsupported_product_terms = (
            "粉底液",
        )

        matched_unsupported_product = next(
            (
                term
                for term in unsupported_product_terms
                if term in normalized_product_input
            ),
            None,
        )

        if matched_unsupported_product:
            self._log(
                "UNSUPPORTED_PRODUCT",
                f"目前資料庫未支援：{matched_unsupported_product}",
            )

            return {
                "reply": (
                    f"目前商品資料庫還沒有「{matched_unsupported_product}」的資料，"
                    "所以我不會用其他保養品代替推薦。"
                    "目前可以幫你找洗面乳、化妝水、精華液、乳液／乳霜、"
                    "面膜、防曬乳或保濕棉片。"
                ),
                "quick_replies": [
                    {"label": "精華液", "value": "精華液"},
                    {"label": "乳液／乳霜", "value": "乳液"},
                    {"label": "面膜", "value": "面膜"},
                    {"label": "防曬乳", "value": "防曬乳"},
                ],
                "intent": {},
                "stage": "clarification_unsupported_product",
            }


        # ── ① Input Guard（最前面）──
        # 按鈕指令（cycle_/confirm_/skip）不用檢查
        # Normalize quick-reply values and persisted display labels before Guard/LLM.
        if not self.state.is_waiting_clarification:
            cycle_action = self._normalize_cycle_action(user_input)
            if cycle_action:
                return self._handle_cycle_action(cycle_action)

        if not self.state.is_waiting_skin_tracking and \
           not user_input.startswith(("__", "cycle_", "confirm_")) and \
           user_input.strip() not in ["skip", "⏭️ 跳過", "跳過"]:
            guard = guard_input(user_input)
            if not guard["allowed"]:
                self._log("GUARD", f"攔截：{guard['reason']}")
                return {
                    "reply": guard.get("reply", "請輸入與保養相關的問題喔！"),
                    "quick_replies": [],
                    "intent": {},
                    "stage": f"guarded_{guard['reason']}",
                }


        # ── ② Cycle 指令 ──
        if user_input.startswith("cycle_"):
            return self._handle_cycle_action(user_input)

        # 膚況諮詢由 API 明確指定時，固定交給 Knowledge Agent。
        # 放在膚況追蹤狀態判斷之前，避免被追蹤流程或商品意圖攔截。
        if user_input.startswith("__knowledge_consult__:"):
            knowledge_query = user_input.split(":", 1)[1].strip()
            try:
                knowledge_result = knowledge_agent_run(
                    user_query=knowledge_query,
                    user_skin_type="unknown",
                    session_id=f"{self.user_id}:{self.conversation_id}",
                    memory_context=self._memory_prompt(),
                    parsed_intent=_explicit_skin_consult_intent(knowledge_query),
                    on_delta=on_delta,
                )
                knowledge_reply = knowledge_result.get(
                    "reply",
                    "目前無法取得相關肌膚知識，請稍後再試。",
                )
            except Exception as error:
                self._log("WARNING", f"knowledge_consult 查詢失敗：{error}")
                knowledge_reply = "目前無法取得相關肌膚知識，請稍後再試。"
            return {
                "reply": knowledge_reply,
                "intent": {
                    "intent_type": IntentType.KNOWLEDGE_QUERY.value,
                },
                "stage": "dispatch",
            }


        # ── ③ 膚況紀錄多輪流程 ──
        # 進入 Skin Tracking 後，下一句必須優先交回 Skin Tracking Agent，
        # 不再送進一般 intent_parser，直到 Agent 回傳 done=True。
        if self.state.is_waiting_skin_tracking:
            return self._handle_skin_tracking_input(user_input)


        # ── ④ 商品購買／使用經驗（優先於 clarification）──
        # 等待商品品牌／感受期間，使用者可能直接換話題。
        # 例如「我想問 A 酸功用是什麼」應離開商品經驗狀態，
        # 再由 intent_parser 判斷並交給 Knowledge Agent。
        if (
            (
                self.state.is_waiting_experience_product
                or self.state.is_waiting_experience_detail
            )
            and self._is_experience_topic_switch(user_input)
        ):
            self._clear_experience_state()
            self.state.is_waiting_clarification = False
            self.state.clarify_dimension = ""
            self._log("EXPERIENCE", "偵測到新話題，退出商品經驗等待狀態")
            return self._handle_new_input(
                user_input,
                ignore_history=True,
            )


        is_experience_input = looks_like_experience(user_input)
        if (
            is_experience_input
            and self.state.is_waiting_clarification
            and self.state.clarify_dimension == "routing"
        ):
            self.state.is_waiting_clarification = False
            self.state.clarify_dimension = ""

        if (
            self.state.is_waiting_experience_product
            or self.state.is_waiting_experience_detail
            or is_experience_input
        ):
            experience_result = self._handle_experience_input(user_input)
            if experience_result is not None:
                return experience_result


        # ── ⑤ 等待排序回饋 ──
        if self.state.is_waiting_rank_feedback:
            return self._handle_rank_feedback_reply(user_input)


        # ── ⑥ 等待需求確認 ──
        if self.state.is_waiting_confirm:
            return self._handle_confirm_reply(user_input)


        # ── ⑦ 等待 clarification ──
        if self.state.is_waiting_clarification:
            normalized = self._normalize_clarification_answer(
                self.state.clarify_dimension,
                user_input,
            )
            if normalized is not None:
                return self._handle_clarification_reply(normalized)


            # 使用者插入了另一段話，不可硬當成上一題的答案。
            self.state.is_waiting_clarification = False
            self.state.clarify_dimension = ""
            self._log("CLARIFICATION", "偵測到插話，改按新輸入重新理解")


        product_turn = resolve_product_turn(
            user_input,
            conversation_flow=self.state.active_flow,
            current_step=routing_context["current_step"],
            structured_state=routing_context,
            action=explicit_action,
        )
        guarded_route = dialogue_route.get("route") if dialogue_route else None
        if (
            not product_turn["requires_intent_parser"]
            and guarded_route not in {"small_talk", "skin_record", "multi_intent"}
        ):
            return self._handle_resolved_product_turn(user_input, product_turn)

        if dialogue_route:
            route = dialogue_route["route"]
            self._log("ROUTE_GUARD", f"{route}：{dialogue_route['reason']}")

            if route == "small_talk":
                return {
                    "reply": (
                        "嗨～很高興見到你！你可以直接告訴我想了解膚況、"
                        "記錄今天的狀況，或找／比較商品。"
                    ),
                    "quick_replies": [
                        {"label": "了解膚況", "value": "__knowledge_consult__:我想了解目前的膚況該怎麼處理"},
                        {"label": "記錄膚況", "value": "cycle_skin_tracking"},
                        {"label": "找保養品", "value": "我想找適合目前膚況的保養品"},
                    ],
                    "intent": {},
                    "stage": "small_talk",
                    "route": "clarify",
                }

            if route == "incomplete":
                return {
                    "reply": (
                        "我在聽，你可以把話說完～如果還不確定怎麼描述，"
                        "也可以先選一個方向。"
                    ),
                    "quick_replies": [
                        {"label": "了解膚況", "value": "__knowledge_consult__:我想了解目前的膚況該怎麼處理"},
                        {"label": "記錄膚況", "value": "cycle_skin_tracking"},
                        {"label": "找保養品", "value": "我想找適合目前膚況的保養品"},
                    ],
                    "intent": {},
                    "stage": "incomplete_utterance",
                    "route": "clarify",
                }

            if route == "product_search":
                return self._handle_new_input(
                    user_input,
                    routing_context=routing_context,
                    allow_reasoning_fallback=True,
                )

            if route == "skin_record":
                self._clear_experience_state()
                self.state.is_waiting_clarification = False
                self.state.clarify_dimension = ""
                self.state.is_waiting_confirm = False
                self.state.is_waiting_rank_feedback = False
                self._clear_skin_tracking_state()
                return self._handle_skin_tracking_input(user_input)

            if route == "skin_consult":
                self.state.intent = IntentParseResult(
                    intent_type=IntentType.KNOWLEDGE_QUERY,
                    raw_query=user_input,
                )
                self._refresh_intent_scores()
                return self._do_dispatch(self.state.intent)

            if route == "clarify":
                return {
                    "reply": "我先確認一下：您想了解膚況怎麼處理、記錄今天的膚況，還是找適合的商品？",
                    "quick_replies": [
                        {"label": "了解膚況", "value": "__knowledge_consult__:我想了解目前的膚況該怎麼處理"},
                        {"label": "記錄膚況", "value": "cycle_skin_tracking"},
                        {"label": "找保養品", "value": "我想找適合目前膚況的保養品"},
                    ],
                    "intent": {},
                    "stage": "route_clarification",
                }

            if route == "multi_intent":
                return {
                    "reply": (
                        "我辨識到您同時想記錄膚況與找商品。為避免誤存資料，"
                        "請先選一件開始；完成後再處理另一件。"
                    ),
                    "quick_replies": [
                        {"label": "先記錄膚況", "value": "cycle_skin_tracking"},
                        {"label": "先找商品", "value": "我想先找適合目前膚況的保養品"},
                    ],
                    "intent": {},
                    "stage": "multi_intent_plan",
                }

        # ── ⑧ 新輸入 ──
        return self._handle_new_input(
            user_input,
            routing_context=routing_context,
        )


    def _handle_new_input(
        self,
        user_input: str,
        ignore_history: bool = False,
        routing_context: dict | None = None,
        allow_reasoning_fallback: bool = True,
    ) -> dict:
        self._log("STEP", "先解析 intent；只有需要時才補做 Pain & Gain 分析")


        long_term = self._intent_memory_context()
        if long_term:
            self._log("MEMORY", f"已載入個人資料與 GetZep context，長度={len(long_term)}")


        # 商品意圖已包含膚質、需求、類別與預算；不要先做第二次重複的 LLM 分析。
        self.state.pain_gain = {}
        previous_product_intent = self.state.intent
        had_product_results = bool(self.state.last_products)


        # Intent 解析
        intent_started = time.perf_counter()
        intent = parse_intent(
            user_query=user_input,
            session_history=([] if ignore_history else self.state.history[:-1]),
            long_term_summary=long_term,
            model=self.model,
            routing_context=routing_context or self._routing_context(),
            allow_reasoning_fallback=allow_reasoning_fallback,
        )
        observe_pipeline_stage("root", "intent_parse", intent_started)
        if intent is None:
            self._log("ERROR", "intent_parser 回傳 None")
            intent = IntentParseResult(
                intent_type=IntentType.UNCLEAR,
                raw_query=user_input,
                ambiguity_score=1.0,
            )


        intent = self._enrich_intent_from_memory(intent)
        if intent.intent_type in {
            IntentType.FIND_PRODUCT,
            IntentType.RECOMMENDATION,
            IntentType.PRICE_COMPARISON,
        }:
            skin_context = get_skin_context(self.memory.user_id)
            self._apply_skin_product_context(skin_context)
            history_concerns = list((skin_context or {}).get("concern_hints") or [])
            if history_concerns and not intent.concern:
                current = intent.model_dump()
                current["concern"] = history_concerns[:2]
                intent = IntentParseResult(**current)
                self._log("歷史膚況", f"補入訴求：{history_concerns[:2]}")
            # 歷史紀錄中的明確質地／預算偏好也要在補齊後再套用一次。
            intent = self._enrich_intent_from_memory(intent)
        if (
            had_product_results
            and previous_product_intent is not None
            and previous_product_intent.intent_type in {
                IntentType.FIND_PRODUCT,
                IntentType.RECOMMENDATION,
                IntentType.PRICE_COMPARISON,
            }
            and _is_product_refinement_query(user_input)
        ):
            try:
                intent = _merge_product_refinement_intent(
                    previous_product_intent,
                    intent,
                    user_input,
                )
                self._log(
                    "PRODUCT_REFINEMENT",
                    "沿用上一輪膚質、需求、品類與預算；品牌限制依本輪重新判斷",
                )
            except Exception as error:
                self._log("PRODUCT_REFINEMENT", f"條件繼承失敗：{error}")
        self.state.intent = intent
        self._refresh_intent_scores()
        intent = self.state.intent

        if intent.intent_type in (IntentType.FIND_PRODUCT, IntentType.RECOMMENDATION):
            raw_concerns = intent.concern or []
            if not isinstance(raw_concerns, (list, tuple, set)):
                raw_concerns = [raw_concerns]
            concerns = [
                str(getattr(item, "value", item)).strip()
                for item in raw_concerns
                if str(getattr(item, "value", item)).strip()
            ]
            skin_type = str(getattr(intent.skin_type, "value", intent.skin_type) or "")
            category = str(
                getattr(intent.product_category, "value", intent.product_category) or "商品"
            )
            budget = getattr(intent, "budget_range", None)
            self.state.pain_gain = {
                "pain": "、".join(concerns),
                "gain": f"找到符合條件的{category if category and category != '不明' else '商品'}",
                "skin_summary": skin_type,
                "key_concerns": concerns,
                "budget_hint": str(budget) if budget else "",
                "special_notes": "",
            }
            self._log(
                "PAIN_GAIN_FAST_PATH",
                json.dumps(self.state.pain_gain, ensure_ascii=False),
            )
        elif len(user_input) > 20:
            pain_gain_started = time.perf_counter()
            self.state.pain_gain = _analyze_pain_gain(user_input)
            observe_pipeline_stage("root", "pain_gain_analysis", pain_gain_started)
            self._log(
                "PAIN_GAIN", json.dumps(self.state.pain_gain, ensure_ascii=False)
            )


        self._log("INTENT", json.dumps({
            "intent_type":      intent.intent_type.value,
            "skin_type":        intent.skin_type.value,
            "concern":          intent.concern,
            "product_category": intent.product_category.value,
            "ambiguity_score":  intent.ambiguity_score,
            "routing_confidence": intent.routing_confidence,
            "task_completeness": intent.task_completeness,
        }, ensure_ascii=False))

        if intent.routing_confidence < 0.5:
            self._log("STEP", "目標 Agent 不明，先確認使用者要做的事")
            return self._ask_clarification("routing")


        # 商品複合需求要能一次執行，不得只因文字較長就要求再確認。
        product_intents = {
            IntentType.FIND_PRODUCT,
            IntentType.RECOMMENDATION,
            IntentType.PRICE_COMPARISON,
        }
        if (
            len(user_input) > 40
            and intent.ambiguity_score < 0.7
            and intent.intent_type not in product_intents
        ):
            confirm_msg = _build_confirm_message(self.state.pain_gain, intent)
            self.state.is_waiting_confirm = True
            self.state.add_turn("assistant", confirm_msg)
            return {
                "reply": confirm_msg,
                "quick_replies": [
                    {"label": "✅ 對，就是這樣", "value": "confirm_yes"},
                    {"label": "❌ 需要調整",     "value": "confirm_no"},
                ],
                "intent": intent.model_dump(),
                "stage":  "pain_gain_confirm",
            }


        # Agent 已確定，但資料不足時，只追問能讓該 Agent 開始工作的必要資訊。
        if intent.task_completeness < 1.0 and intent.intent_type in (IntentType.FIND_PRODUCT, IntentType.RECOMMENDATION):
            self._log("STEP", "產品 Agent 已確定，但缺少需求；只追問主要肌膚問題")
            return self._ask_clarification("concern")


        self._log("STEP", "意圖明確，直接分派")
        return self._do_dispatch(intent)


    def _handle_confirm_reply(self, user_input: str) -> dict:
        """使用者確認/修正需求摘要"""
        self.state.is_waiting_confirm = False


        # 使用者確認
        if _is_confirmation(user_input):
            self._log("CONFIRM", "使用者確認需求，直接分派")
            return self._do_dispatch(self.state.intent)


        # 使用者只說「不是」或按「需要調整」→ 保留原本的需求，請使用者說要改哪裡
        if _is_bare_rejection(user_input):
            self._log("CONFIRM", "使用者否定需求摘要，詢問要調整的項目")
            reply = "好的，請直接告訴我哪裡需要調整，例如膚質、想解決的問題、品類或預算。"
            self.state.is_waiting_confirm = True
            self.state.add_turn("assistant", reply)
            return {
                "reply": reply,
                "quick_replies": [{"label": "✅ 原本的就可以", "value": "confirm_yes"}],
                "intent": self.state.intent.model_dump() if self.state.intent else {},
                "stage":  "pain_gain_confirm_adjust",
            }


        # 使用者要修正 → 重新收集
        self._log("CONFIRM", "使用者要修正需求")
        intent = parse_intent(
            user_query=user_input,
            session_history=self.state.history[:-1],
            long_term_summary=self._intent_memory_context(),
            model=self.model,
            routing_context=self._routing_context(),
            allow_reasoning_fallback=True,
        )
        if intent:
            self.state.intent = self._enrich_intent_from_memory(intent)


        confirm_msg = _build_confirm_message(self.state.pain_gain, self.state.intent)
        self.state.is_waiting_confirm = True
        self.state.add_turn("assistant", confirm_msg)
        return {
            "reply": f"了解！讓我重新整理一下：\n\n{confirm_msg}",
            "quick_replies": [
                {"label": "✅ 對，就是這樣", "value": "confirm_yes"},
                {"label": "❌ 需要調整",     "value": "confirm_no"},
            ],
            "intent": self.state.intent.model_dump(),
            "stage":  "pain_gain_confirm_retry",
        }


    def _handle_rank_feedback_reply(self, user_input: str) -> dict:
        """使用者對排序的回饋"""
        self.state.is_waiting_rank_feedback = False
        self.user_analysis_bridge.record_rank_feedback(
            user_input,
            self.state.last_products,
        )
        self.user_analysis_context = self.user_analysis_bridge.refresh(
            basic_profile=self.basic_profile,
            analyze_if_needed=True,
            force=True,
        )
        reply = _handle_rank_feedback(
            user_input,
            self.state.last_products,
            self.state.intent
        )
        self.state.add_turn("assistant", reply)
        return {
            "reply": reply,
            "quick_replies": [
                {"label": "重新推薦",     "value": "cycle_change_condition"},
                {"label": "換搜尋條件",   "value": "cycle_change_condition"},
                {"label": "我要記錄膚況", "value": "cycle_skin_tracking"},
            ],
            "intent": self.state.intent.model_dump() if self.state.intent else {},
            "stage":  "rank_feedback_done",
        }


    def _compose_agent_reply(
        self,
        intent: IntentParseResult,
        raw_reply: str,
    ) -> str:
        protected_terms: list[str] = []
        for product in self.state.last_products[:10]:
            if not isinstance(product, dict):
                continue
            for key in ("name", "brand"):
                value = str(product.get(key) or "").strip()
                if value and value not in protected_terms:
                    protected_terms.append(value)

        context = [
            {
                "role": str(turn.get("role") or ""),
                "content": str(turn.get("content") or ""),
            }
            for turn in self.state.history[-6:]
            if isinstance(turn, dict)
        ]
        result = self.response_composer.compose(
            user_query=intent.raw_query,
            intent_type=intent.intent_type.value,
            raw_reply=raw_reply,
            conversation_context=context,
            protected_terms=protected_terms,
        )
        if result.used_model:
            self._log("COMPOSER", "已完成自然語氣組裝")
        else:
            self._log(
                "COMPOSER",
                f"保留原始 Agent 回覆: {result.fallback_reason}",
            )
        if intent.intent_type in {
            IntentType.FIND_PRODUCT,
            IntentType.RECOMMENDATION,
            IntentType.PRICE_COMPARISON,
        }:
            return clean_ingredient_display(result.reply, protected_terms)
        return result.reply

    def _do_dispatch(self, intent: IntentParseResult) -> dict:
        """分派並回傳"""
        # A2A：膚況追蹤 context 併入訴求（有紀錄才生效）
        skin_context_started = time.perf_counter()
        skin_ctx = get_skin_context(self.memory.user_id)
        observe_pipeline_stage(
            "root",
            "skin_context",
            skin_context_started,
        )
        if skin_ctx and skin_ctx.get('concern_hints'):
            current = intent.model_dump()
            added = [c for c in skin_ctx['concern_hints'] if c not in current.get('concern', [])]
            if added:
                current.setdefault('concern', []).extend(added)
                try:
                    intent = IntentParseResult(**current)
                    self.state.intent = intent
                    self._log("SKIN_A2A", f"膚況紀錄補入訴求：{added}")
                except Exception as e:
                    self._log("SKIN_A2A", f"併入失敗：{e}")


        # Root 已完成原本的意圖判斷後，才把本輪選購訊號交給
        # UserAnalysisAgent。分析結果只作為推理背景與排序加權；
        # 不會反向改寫 intent，也不會改變要分派到哪個 Agent。
        user_analysis_dispatch_started = time.perf_counter()
        self.user_analysis_bridge.record_intent(
            intent,
            source_text=intent.raw_query,
        )
        self.user_analysis_context = self.user_analysis_bridge.refresh(
            basic_profile=self.basic_profile,
            analyze_if_needed=True,
        )
        self._apply_skin_product_context(skin_ctx)
        observe_pipeline_stage(
            "root",
            "user_analysis_dispatch",
            user_analysis_dispatch_started,
        )

        agent_dispatch_started = time.perf_counter()
        raw_reply = self._dispatch(intent)
        observe_pipeline_stage(
            "root",
            "agent_dispatch",
            agent_dispatch_started,
        )

        response_compose_started = time.perf_counter()
        reply = self._compose_agent_reply(intent, raw_reply)
        observe_pipeline_stage(
            "root",
            "response_compose",
            response_compose_started,
        )

        self.state.add_turn("assistant", reply)

        memory_save_started = time.perf_counter()
        self.memory.update_from_intent(intent)
        self.memory.save()
        observe_pipeline_stage(
            "root",
            "memory_update_save",
            memory_save_started,
        )
        self._log("MEMORY", f"已更新：{self.memory.to_summary()}")
        route = (
            "skin_consult"
            if intent.intent_type in (
                IntentType.SYMPTOM_SOLVING,
                IntentType.KNOWLEDGE_QUERY,
            )
            else "product_search"
        )
        return {
            "reply":         reply,
            "quick_replies": self._get_cycle_replies(intent),
            "intent":        intent.model_dump(),
            "stage":         "dispatch",
            "route":         route,
            "products":      (
                self.state.last_products
                if route == "product_search"
                else None
            ),
        }


    def _handle_clarification_reply(self, user_input: str) -> dict:
        self._log("STEP", f"處理追問回覆，維度：{self.state.clarify_dimension}")
        is_skip = user_input.strip() in ["skip", "⏭️ 跳過", "跳過"]
        self.state.is_waiting_clarification = False


        if not is_skip:
            self._update_intent(self.state.clarify_dimension, user_input.strip())
        else:
            self._log("CLARIFICATION", "使用者選擇跳過")
            if self.state.clarify_dimension == "skin_type":
                self.state.skin_type_skipped = True
            current = self.state.intent.model_dump()
            current["ambiguity_score"] = max(0.0, current.get("ambiguity_score", 1.0) - 0.4)
            try:
                self.state.intent = IntentParseResult(**current)
            except Exception as e:
                self._log("ERROR", f"跳過更新失敗：{e}")


        self._log("CLARIFICATION", (
            f"更新後 intent：skin={self.state.intent.skin_type.value}, "
            f"concern={self.state.intent.concern}, "
            f"category={self.state.intent.product_category.value}"
        ))


        self._fix_unclear_intent()
        self._refresh_intent_scores()
        intent = self.state.intent
        if intent.routing_confidence < 0.5:
            return self._ask_clarification("routing")
        if intent.task_completeness < 1.0 and intent.intent_type in (IntentType.FIND_PRODUCT, IntentType.RECOMMENDATION):
            return self._ask_clarification("concern")
        self._log("STEP", "追問完成，分派")
        return self._do_dispatch(self.state.intent)




# ──────────────────────────────────────────
# 測試
# ──────────────────────────────────────────


if __name__ == "__main__":
    print("\n【測試 5】GetZep 記憶情境 — 油性 + 敏感肌雙重膚質，第二輪推薦精華液是否記得")
    print("=" * 60)


    zep_user_5 = "zep_test_oily_sensitive_001"   # 全新 user_id，避免沿用舊測試殘留的記憶


    agent5 = RootAgentOrchestrator(user_id=zep_user_5)


    # ── 第一輪：使用者說明自己「油性肌 + 敏感肌」，並詢問適合的成分類型 ──
    print("\n【5a】第一輪：自述油性肌+敏感肌，詢問適合成分")
    r5a = agent5.process("我是油性膚質，同時也是敏感肌，請問我適合使用什麼類型的成分？")
    print(f"Stage: {r5a['stage']}")
    print(f"Reply:\n{r5a['reply'][:400]}")
    print(f"[intent] skin_type={r5a['intent'].get('skin_type')}, "
          f"concern={r5a['intent'].get('concern')}, "
          f"category={r5a['intent'].get('product_category')}")


    print(f"\n[Zep] to_summary() = {agent5.memory.to_summary()}")
    print(f"[Zep] get('skin_type') = {agent5.memory.get('skin_type')}")
    print(f"[Zep] get('concerns') = {agent5.memory.get('concerns')}")


    # skin_type 目前的 schema 是單一 enum，若使用者同時是「油性+敏感」，
    # 檢查系統實際記住的是哪一個、還是有沒有機制同時保留兩者
    skin_recorded = agent5.memory.get('skin_type')
    if skin_recorded in ("oily", "sensitive"):
        print(f"⚠️ 使用者自述同時是油性肌與敏感肌，但 Zep 只記住了單一膚質：{skin_recorded}"
              "（skin_type 目前 schema 是單一 enum，可能遺失了另一個膚質標籤）")
    elif skin_recorded:
        print(f"[Zep] 記住的膚質為：{skin_recorded}")
    else:
        print("⚠️ Zep 沒有記住任何膚質，第一輪的自述可能沒有被 intent_parser 正確解析出 skin_type")


    # ── 第二輪：同一使用者，改問精華液推薦，看是否同時反映油性+敏感肌 ──
    print("\n【5b】第二輪（同一人）：推薦精華液 — 檢查是否同時考量油性與敏感肌")
    r5b = agent5.process("推薦我精華液")
    print(f"Stage: {r5b['stage']}")
    print(f"Reply:\n{r5b['reply']}")
    print(f"[intent] skin_type={r5b['intent'].get('skin_type')}, "
          f"concern={r5b['intent'].get('concern')}, "
          f"category={r5b['intent'].get('product_category')}")


    reply_text = r5b['reply']
    oily_hit = any(kw in reply_text for kw in ["油性", "控油", "出油", "毛孔"])
    sensitive_hit = any(kw in reply_text for kw in ["敏感", "舒緩", "泛紅", "屏障", "溫和", "無刺激"])


    print(f"\n推薦文字中是否呼應「油性」相關字眼：{oily_hit}")
    print(f"推薦文字中是否呼應「敏感」相關字眼：{sensitive_hit}")


    if oily_hit and sensitive_hit:
        print("✅ 第二輪推薦同時呼應了油性與敏感兩個膚質特性")
    elif oily_hit or sensitive_hit:
        print("⚠️ 第二輪推薦只呼應了其中一個膚質特性，另一個可能在第一輪就沒被記住，"
              "或是 product_agent 推薦時沒有把兩個標籤都納入考量")
    else:
        print("⚠️ 第二輪推薦完全沒有呼應到油性或敏感肌，代表膚質記憶可能沒有生效，"
              "或是 _handle_recommendation / product_agent_run 沒有把 skin_type 落實到搜尋條件裡")
