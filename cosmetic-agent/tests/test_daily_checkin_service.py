from datetime import datetime, timezone

from app.services.daily_checkin_service import DailyCheckinService


class Cursor(list):
    def sort(self, *_args, **_kwargs):
        return self


class FakeRepository:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def find(self, query, _projection):
        return Cursor(row.copy() for row in self.rows if row["user_id"] == query["user_id"])

    def find_one(self, query):
        return next((row.copy() for row in self.rows if row["user_id"] == query["user_id"] and row["date"] == query["date"]), None)

    def insert_one(self, document):
        self.rows.append(document.copy())


def fixed_now():
    return datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def test_empty_status_and_first_checkin():
    repo = FakeRepository()
    service = DailyCheckinService(repo, fixed_now)
    status = service.build_status("user-1")
    assert status["today"] == "2026-09-25"
    assert status["next_day"] == 1
    assert status["beauty_unlocked"] is True
    assert status["doctor_unlocked"] is True
    assert status["beauty_remaining"] == 0
    assert status["doctor_remaining"] == 0
    result = service.check_in("user-1")
    assert result["already"] is False
    assert result["checkin"]["day_number"] == 1
    assert result["status"]["points"] == 10


def test_duplicate_and_history_order():
    repo = FakeRepository([{
        "user_id": "user-1",
        "date": "2026-09-25",
        "created_at": "2026-09-25T08:00:00+00:00",
        "checked_in_at": "2026-09-25T16:00:00+08:00",
        "timezone": "Asia/Taipei",
        "points_earned": 10,
    }])
    service = DailyCheckinService(repo, fixed_now)
    result = service.check_in("user-1")
    assert result["already"] is True
    assert len(repo.rows) == 1
    history = service.history("user-1")
    assert history["count"] == 1
    assert history["history"][0]["day_number"] == 1
