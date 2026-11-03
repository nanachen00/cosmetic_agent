OPENAI_API_KEY = "x"; OPENAI_MODEL = "x"
CALLS = []
def parse_intent(**kw):        # stands in for the LLM parser; records that it was (not) called
    CALLS.append(kw["user_query"])
    from schemas.intent import IntentParseResult, IntentType
    return IntentParseResult(intent_type=IntentType.FIND_PRODUCT, raw_query=kw["user_query"])
