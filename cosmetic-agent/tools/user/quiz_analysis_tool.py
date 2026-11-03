from datetime import datetime, timezone
from uuid import uuid4


# =========================================================
# Constants
# =========================================================

CUSTOMER_TYPES = [
    "hesitant_decision_maker",
    "ingredient_researcher",
    "price_sensitive",
    "review_dependent",
    "brand_oriented",
    "explorer",
    "function_oriented",
    "impulse_buyer"
]


QUIZ_VERSION = "v3"


QUIZ_QUESTIONS = [
    {
        "question_id": "q1",
        "question": (
            "出發旅行前一週，你看到一款沒用過的保養品，"
            "但家裡原本的產品還剩半瓶。你比較可能怎麼做？"
        ),
        "answers": [
            {
                "answer_id": "q1_save_for_later",
                "label": "先把資訊存起來",
                "description": "旅行結束後，再回頭決定",
                "scores": {
                    "hesitant_decision_maker": 0.8,
                    "review_dependent": 0.3
                }
            },
            {
                "answer_id": "q1_check_routine_fit",
                "label": "確認是否適合目前的保養步驟",
                "description": "先釐清能不能和手邊產品一起使用",
                "scores": {
                    "ingredient_researcher": 0.7,
                    "function_oriented": 0.5
                }
            },
            {
                "answer_id": "q1_bundle_with_plan",
                "label": "看看能否和原本要買的東西一起購入",
                "description": "整體安排合適才加入",
                "scores": {
                    "price_sensitive": 0.8,
                    "hesitant_decision_maker": 0.3
                }
            },
            {
                "answer_id": "q1_trip_surprise",
                "label": "讓旅行多一點新鮮感",
                "description": "覺得對味，就想帶著一起出發",
                "scores": {
                    "impulse_buyer": 0.7,
                    "explorer": 0.6
                }
            }
        ]
    },

    {
        "question_id": "q2",
        "question": (
            "兩位膚質不同的朋友，對同一款產品給了完全相反的心得。"
            "你通常會怎麼處理？"
        ),
        "answers": [
            {
                "answer_id": "q2_understand_context",
                "label": "先了解彼此怎麼使用",
                "description": "包括搭配方式與當時的肌膚狀況",
                "scores": {
                    "ingredient_researcher": 0.6,
                    "function_oriented": 0.7
                }
            },
            {
                "answer_id": "q2_observe_more_cases",
                "label": "暫時不急著下結論",
                "description": "再觀察幾種不同使用情況",
                "scores": {
                    "review_dependent": 0.8,
                    "hesitant_decision_maker": 0.4
                }
            },
            {
                "answer_id": "q2_return_to_need",
                "label": "回到自己現在最想改善的問題",
                "description": "看看它和目前需求是否吻合",
                "scores": {
                    "function_oriented": 0.8,
                    "price_sensitive": 0.3
                }
            },
            {
                "answer_id": "q2_follow_similar_person",
                "label": "參考和自己習慣較接近的那一位",
                "description": "從較熟悉的經驗開始判斷",
                "scores": {
                    "brand_oriented": 0.7,
                    "review_dependent": 0.5
                }
            }
        ]
    },

    {
        "question_id": "q3",
        "question": (
            "你在逛購物網站時，系統推薦了一款原本不在購物清單裡的產品。"
            "什麼情況最可能讓你繼續了解它？"
        ),
        "answers": [
            {
                "answer_id": "q3_familiar_routine",
                "label": "使用方式和我平常的習慣很接近",
                "description": "感覺不需要花太多時間重新適應",
                "scores": {
                    "brand_oriented": 0.8,
                    "hesitant_decision_maker": 0.3
                }
            },
            {
                "answer_id": "q3_complete_explanation",
                "label": "頁面把適用情況說得很完整",
                "description": "能理解它在不同步驟中扮演的角色",
                "scores": {
                    "ingredient_researcher": 0.7,
                    "function_oriented": 0.5
                }
            },
            {
                "answer_id": "q3_low_entry_barrier",
                "label": "嘗試它不需要付出太高的成本",
                "description": "即使不適合，也不會造成太大負擔",
                "scores": {
                    "price_sensitive": 0.7,
                    "hesitant_decision_maker": 0.3
                }
            },
            {
                "answer_id": "q3_different_and_curious",
                "label": "它和平常看到的產品不太一樣",
                "description": "新鮮的呈現方式讓我有點好奇",
                "scores": {
                    "explorer": 0.8,
                    "impulse_buyer": 0.4
                }
            }
        ]
    },

    {
        "question_id": "q4",
        "question": (
            "一款新產品使用兩週後沒有不舒服，但效果也不明顯。"
            "你比較可能怎麼做？"
        ),
        "answers": [
            {
                "answer_id": "q4_continue_as_planned",
                "label": "照原本預計的時間繼續使用",
                "description": "等完整使用一段時間後再判斷",
                "scores": {
                    "function_oriented": 0.7,
                    "brand_oriented": 0.4
                }
            },
            {
                "answer_id": "q4_adjust_usage",
                "label": "調整使用頻率或搭配方式",
                "description": "觀察不同用法會不會帶來差異",
                "scores": {
                    "ingredient_researcher": 0.6,
                    "explorer": 0.5
                }
            },
            {
                "answer_id": "q4_check_expected_timing",
                "label": "再看看相似情況通常多久有感",
                "description": "確認現在的反應是否仍在合理範圍",
                "scores": {
                    "review_dependent": 0.7,
                    "hesitant_decision_maker": 0.4
                }
            },
            {
                "answer_id": "q4_switch_if_attracted",
                "label": "遇到更吸引我的選擇就換一款",
                "description": "不一定要把目前這款用到底",
                "scores": {
                    "impulse_buyer": 0.8,
                    "explorer": 0.4
                }
            }
        ]
    },

    {
        "question_id": "q5",
        "question": (
            "你長期使用的產品突然更換了包裝、配方或售價，"
            "你通常會怎麼反應？"
        ),
        "answers": [
            {
                "answer_id": "q5_rebuy_and_compare",
                "label": "先買一次新版，親自比較差異",
                "description": "熟悉的選擇值得再確認一次",
                "scores": {
                    "brand_oriented": 0.8,
                    "function_oriented": 0.3
                }
            },
            {
                "answer_id": "q5_find_similar_alternative",
                "label": "找使用感受接近的替代選擇",
                "description": "重新整理幾個可能適合的候選",
                "scores": {
                    "ingredient_researcher": 0.5,
                    "explorer": 0.4
                }
            },
            {
                "answer_id": "q5_wait_for_clarity",
                "label": "等資訊和購買時機更明確",
                "description": "先不急著決定是否繼續回購",
                "scores": {
                    "price_sensitive": 0.7,
                    "review_dependent": 0.5
                }
            },
            {
                "answer_id": "q5_reselect_freely",
                "label": "把這次變動當成重新挑選的機會",
                "description": "也許會遇到更符合現在心情的產品",
                "scores": {
                    "explorer": 0.7,
                    "impulse_buyer": 0.6
                }
            }
        ]
    }
]


# =========================================================
# Frontend Questions
# =========================================================

def get_quiz_questions():
    """
    提供前端顯示 Quiz 題目。

    scores 不會傳給前端，
    避免前端直接看到人格計分規則。
    """

    result = []

    for question in QUIZ_QUESTIONS:

        result.append(
            {
                "question_id": question[
                    "question_id"
                ],

                "question": question[
                    "question"
                ],

                "answers": [
                    {
                        "answer_id": answer[
                            "answer_id"
                        ],

                        "label": answer[
                            "label"
                        ],

                        "description": answer[
                            "description"
                        ]
                    }
                    for answer in question[
                        "answers"
                    ]
                ]
            }
        )

    return result


# =========================================================
# Quiz Validation
# =========================================================

def get_question_lookup():
    """
    建立 question_id 對應題目資料。
    """

    return {
        question["question_id"]: question
        for question in QUIZ_QUESTIONS
    }


def validate_selected_answers(
    selected_answers
):
    """
    驗證 Quiz 答案。

    前端傳入格式：

    {
        "q1": "q1_save_for_later",
        "q2": "q2_observe_more_cases",
        "q3": "q3_familiar_routine",
        "q4": "q4_check_expected_timing",
        "q5": "q5_wait_for_clarity"
    }
    """

    if not isinstance(
        selected_answers,
        dict
    ):
        raise ValueError(
            "selected_answers 必須是 dict"
        )

    if not selected_answers:
        raise ValueError(
            "selected_answers 不可為空"
        )

    question_lookup = (
        get_question_lookup()
    )

    valid_question_ids = set(
        question_lookup.keys()
    )

    submitted_question_ids = set(
        selected_answers.keys()
    )

    missing_question_ids = (
        valid_question_ids
        - submitted_question_ids
    )

    unknown_question_ids = (
        submitted_question_ids
        - valid_question_ids
    )

    if missing_question_ids:
        raise ValueError(
            "尚未完成所有 Quiz 題目，缺少："
            + ", ".join(
                sorted(missing_question_ids)
            )
        )

    if unknown_question_ids:
        raise ValueError(
            "包含不存在的 question_id："
            + ", ".join(
                sorted(unknown_question_ids)
            )
        )

    for question_id, answer_id in (
        selected_answers.items()
    ):

        if not isinstance(answer_id, str):
            raise ValueError(
                f"{question_id} 的 answer_id 必須是字串"
            )

        valid_answer_ids = {
            answer["answer_id"]
            for answer in question_lookup[
                question_id
            ]["answers"]
        }

        if answer_id not in valid_answer_ids:
            raise ValueError(
                f"{answer_id} 不屬於 {question_id}"
            )

    return True


# =========================================================
# Quiz Score Calculation
# =========================================================

def calculate_quiz_scores(
    selected_answers
):
    """
    根據選項分數計算各 Customer Type 傾向。

    Quiz 僅作為冷啟動資料，
    不等同於真實對話或購買行為。
    """

    validate_selected_answers(
        selected_answers
    )

    question_lookup = (
        get_question_lookup()
    )

    scores = {
        customer_type: 0.0
        for customer_type in CUSTOMER_TYPES
    }

    for question_id, answer_id in (
        selected_answers.items()
    ):

        selected_answer = next(
            answer
            for answer in question_lookup[
                question_id
            ]["answers"]
            if answer["answer_id"] == answer_id
        )

        for customer_type, score in (
            selected_answer["scores"].items()
        ):

            if customer_type in scores:

                scores[
                    customer_type
                ] += float(score)

    question_count = len(
        selected_answers
    )

    normalized_scores = {
        customer_type: round(
            min(
                score / question_count,
                1.0
            ),
            3
        )
        for customer_type, score
        in scores.items()
    }

    return normalized_scores


def calculate_quiz_confidence(
    sorted_scores
):
    """
    計算 Quiz 判斷可信度。

    Quiz 屬於冷啟動資料，
    因此最高可信度限制為 0.7。
    """

    if len(sorted_scores) < 2:
        return 0.0

    primary_score = sorted_scores[0][1]
    secondary_score = sorted_scores[1][1]

    score_gap = max(
        primary_score
        - secondary_score,
        0.0
    )

    gap_score = min(
        score_gap / 0.5,
        1.0
    )

    confidence = (
        gap_score * 0.6
        + 0.4
    )

    return round(
        min(confidence, 0.7),
        3
    )


def analyze_quiz_result(
    selected_answers
):
    """
    回傳主要類型、次要類型、完整分數與可信度。
    """

    scores = calculate_quiz_scores(
        selected_answers
    )

    sorted_scores = sorted(
        scores.items(),
        key=lambda item: item[1],
        reverse=True
    )

    primary_type = sorted_scores[0][0]
    secondary_type = sorted_scores[1][0]

    confidence = calculate_quiz_confidence(
        sorted_scores
    )

    return {
        "primary_type": primary_type,
        "secondary_type": secondary_type,
        "scores": scores,
        "confidence": confidence
    }


# =========================================================
# Quiz Behavior Event
# =========================================================

def build_quiz_behavior_event(
    user_id,
    selected_answers,
    quiz_result,
    session_id=None,
    message_id=None
):
    """
    建立 quiz_completed 行為事件。

    Behavior Analysis Tool 會從 metadata.quiz_scores
    讀取問卷結果，並以較低權重使用。
    """

    now = datetime.now(
        timezone.utc
    )

    event = {
        "event_id": str(
            uuid4()
        ),

        "event_type": (
            "quiz_completed"
        ),

        "event_source": (
            "quiz"
        ),

        "signal_type": (
            "explicit"
        ),

        "confidence": quiz_result[
            "confidence"
        ],

        "session_id": session_id,

        "message_id": message_id,

        "agent_name": (
            "user_analysis_agent"
        ),

        "intent": (
            "customer_type_quiz"
        ),

        "context_scope": (
            "long_term"
        ),

        "target_person": (
            "self"
        ),

        "metadata": {
            "quiz_version": (
                QUIZ_VERSION
            ),

            "selected_answers": (
                selected_answers
            ),

            "quiz_scores": quiz_result[
                "scores"
            ],

            "quiz_primary_type": (
                quiz_result[
                    "primary_type"
                ]
            ),

            "quiz_secondary_type": (
                quiz_result[
                    "secondary_type"
                ]
            ),

            "quiz_confidence": (
                quiz_result[
                    "confidence"
                ]
            ),

            "answered_count": (
                len(selected_answers)
            ),

            "total_question_count": (
                len(QUIZ_QUESTIONS)
            )
        },

        "occurred_at": now
    }

    normalized_event = (
        normalize_behavior_event(
            event
        )
    )

    normalized_event["user_id"] = (
        user_id
    )

    normalized_event["created_at"] = (
        now
    )

    return normalized_event


# =========================================================
# Save Quiz Result
# =========================================================

def build_quiz_result_document(
    user_id,
    selected_answers,
    quiz_result,
    behavior_event_id,
    session_id=None
):
    """
    建立 user_quiz_results 文件。
    """

    now = datetime.now(
        timezone.utc
    )

    return {
        "quiz_result_id": str(
            uuid4()
        ),

        "user_id": user_id,

        "quiz_version": (
            QUIZ_VERSION
        ),

        "session_id": session_id,

        "selected_answers": (
            selected_answers
        ),

        "primary_type": (
            quiz_result[
                "primary_type"
            ]
        ),

        "secondary_type": (
            quiz_result[
                "secondary_type"
            ]
        ),

        "scores": (
            quiz_result[
                "scores"
            ]
        ),

        "confidence": (
            quiz_result[
                "confidence"
            ]
        ),

        "behavior_event_id": (
            behavior_event_id
        ),

        "completed_at": now,

        "created_at": now
    }


def record_quiz_result(
    user_id,
    selected_answers,
    session_id=None,
    message_id=None,
):
    """Analyze and persist a completed quiz in PostgreSQL."""
    if not user_id:
        raise ValueError("user_id is required")

    quiz_result = analyze_quiz_result(selected_answers=selected_answers)

    # Guests can complete the quiz, but their result must not be written to the
    # member-only PostgreSQL tables.  The browser keeps the result for this
    # session and a signed-in member gets the durable database path below.
    if str(user_id).startswith("guest-"):
        return {
            "ok": True,
            "persisted": False,
            "storage_scope": "session",
            "event_id": None,
            "event_mongo_id": None,
            "quiz_result_id": None,
            "quiz_result_mongo_id": None,
            "quiz_version": QUIZ_VERSION,
            **quiz_result,
        }

    from database.postgres.repositories.quiz import postgres_quiz_repository

    return postgres_quiz_repository.record(
        user_id=user_id,
        quiz_version=QUIZ_VERSION,
        selected_answers=selected_answers,
        quiz_result=quiz_result,
        session_id=session_id,
        message_id=message_id,
    )


def get_latest_quiz_result(user_id):
    """Return the latest completed quiz for a user from PostgreSQL."""
    if not user_id:
        raise ValueError("user_id is required")

    if str(user_id).startswith("guest-"):
        return {
            "ok": True,
            "has_completed": False,
            "result": None,
            "persisted": False,
            "storage_scope": "session",
        }

    from database.postgres.repositories.quiz import postgres_quiz_repository

    return postgres_quiz_repository.latest(user_id=user_id)
