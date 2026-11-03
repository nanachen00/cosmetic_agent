from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes.quiz import create_quiz_router
from tools.user.quiz_analysis_tool import (
    get_latest_quiz_result,
    get_quiz_questions,
    record_quiz_result,
)


def _answers():
    return {
        question["question_id"]: question["answers"][0]["answer_id"]
        for question in get_quiz_questions()
    }


def test_guest_quiz_is_scored_without_member_database_write():
    result = record_quiz_result(
        user_id="guest-11111111-1111-4111-8111-111111111111",
        selected_answers=_answers(),
    )

    assert result["ok"] is True
    assert result["persisted"] is False
    assert result["storage_scope"] == "session"
    assert result["quiz_result_id"] is None
    assert result["primary_type"]


def test_guest_latest_result_is_empty_instead_of_database_error():
    result = get_latest_quiz_result(
        "guest-11111111-1111-4111-8111-111111111111"
    )

    assert result == {
        "ok": True,
        "has_completed": False,
        "result": None,
        "persisted": False,
        "storage_scope": "session",
    }


def _unauthenticated(_request):
    raise HTTPException(status_code=401, detail="Authentication required")


def _client():
    app = FastAPI()
    app.include_router(
        create_quiz_router(
            require_auth_session=_unauthenticated,
            get_questions=get_quiz_questions,
            record_result=record_quiz_result,
            get_latest_result=get_latest_quiz_result,
        )
    )
    return TestClient(app)


def test_valid_guest_submit_succeeds_and_remains_session_only():
    response = _client().post(
        "/api/quiz/submit",
        json={
            "user_id": "guest-22222222-2222-4222-8222-222222222222",
            "selected_answers": _answers(),
        },
    )

    assert response.status_code == 200
    assert response.json()["persisted"] is False


def test_invalid_guest_id_stays_unauthorized_instead_of_becoming_500():
    response = _client().get(
        "/api/quiz/latest",
        params={"user_id": "guest-not-a-uuid"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"
