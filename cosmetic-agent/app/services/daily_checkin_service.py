from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from database.postgres.repositories.checkin import postgres_daily_checkin_collection

CHECKIN_TOTAL_DAYS = 14
CHECKIN_POINTS_PER_DAY = 10
BEAUTY_UNLOCK_DAY = 5
DOCTOR_UNLOCK_DAY = 7
CHECKIN_TIMEZONE_NAME = "Asia/Taipei"
CHECKIN_TIMEZONE = ZoneInfo(CHECKIN_TIMEZONE_NAME)


class DailyCheckinService:
    """Apply daily check-in rules using the PostgreSQL repository."""

    def __init__(self, repository=postgres_daily_checkin_collection, now_utc: Callable[[], datetime] | None = None):
        self._repository = repository
        self._now_utc = now_utc or (lambda: datetime.now(timezone.utc))

    def _checkin_time(self):
        now_utc = self._now_utc()
        return now_utc, now_utc.astimezone(CHECKIN_TIMEZONE)

    def _records(self, user_id: str):
        return list(
            self._repository.find({"user_id": user_id}, {"_id": 0}).sort(
                [("date", 1), ("created_at", 1)]
            )
        )

    def build_status(self, user_id: str):
        records = self._records(user_id)
        _, now_taipei = self._checkin_time()
        today = now_taipei.strftime("%Y-%m-%d")
        total_count = len(records)
        total_points = sum(
            int(record.get("points_earned", CHECKIN_POINTS_PER_DAY))
            for record in records
        )
        today_checked_in = any(str(record.get("date", "")) == today for record in records)
        full_cycles = total_count // CHECKIN_TOTAL_DAYS
        remainder = total_count % CHECKIN_TOTAL_DAYS

        if total_count == 0:
            current_cycle = 1
            current_cycle_records = []
            cycle_completed = False
        elif remainder == 0:
            last_date = str(records[-1].get("date", ""))
            if last_date >= today:
                current_cycle = max(full_cycles, 1)
                current_cycle_records = records[-CHECKIN_TOTAL_DAYS:]
                cycle_completed = True
            else:
                current_cycle = full_cycles + 1
                current_cycle_records = []
                cycle_completed = False
        else:
            current_cycle = full_cycles + 1
            current_cycle_records = records[-remainder:]
            cycle_completed = False

        completed_count = len(current_cycle_records)
        completed_days = list(range(1, completed_count + 1))
        next_day = None if completed_count >= CHECKIN_TOTAL_DAYS else completed_count + 1
        # Legacy unlock fields remain in the response, but content is always readable.
        beauty_unlocked = True
        doctor_unlocked = True
        current_cycle_checkins = []
        for index, record in enumerate(current_cycle_records, start=1):
            current_cycle_checkins.append({
                "day_number": index,
                "date": record.get("date"),
                "checked_in_at": record.get("checked_in_at") or record.get("created_at"),
                "points_earned": int(record.get("points_earned", CHECKIN_POINTS_PER_DAY)),
            })
        last_checkin_at = None
        if records:
            last_checkin_at = records[-1].get("checked_in_at") or records[-1].get("created_at")

        return {
            "ok": True,
            "user_id": user_id,
            "timezone": CHECKIN_TIMEZONE_NAME,
            "today": today,
            "points": total_points,
            "total_checkins": total_count,
            "current_cycle": current_cycle,
            "completed_days": completed_days,
            "completed_count": completed_count,
            "current_cycle_checkins": current_cycle_checkins,
            "today_checked_in": today_checked_in,
            "can_check_in": not today_checked_in and not cycle_completed,
            "cycle_completed": cycle_completed,
            "next_day": next_day,
            "beauty_unlocked": beauty_unlocked,
            "doctor_unlocked": doctor_unlocked,
            "beauty_remaining": 0 if beauty_unlocked else max(BEAUTY_UNLOCK_DAY - total_count, 0),
            "doctor_remaining": 0 if doctor_unlocked else max(DOCTOR_UNLOCK_DAY - total_count, 0),
            "last_checkin_at": last_checkin_at,
        }

    def check_in(self, user_id: str):
        before_status = self.build_status(user_id)
        if before_status["today_checked_in"]:
            return {
                "ok": True,
                "already": True,
                "message": "今天已經簽到過囉，明天再回來累積下一天 ♡",
                "checkin": None,
                "status": before_status,
            }
        if not before_status["can_check_in"]:
            return {
                "ok": True,
                "already": True,
                "message": f"第 {before_status['current_cycle']} 輪已完成 ♡ 明天會自動開始下一輪。",
                "checkin": None,
                "status": before_status,
            }

        next_day = before_status["next_day"] or 1
        cycle_number = before_status["current_cycle"]
        now_utc, now_taipei = self._checkin_time()
        today = now_taipei.strftime("%Y-%m-%d")
        document = {
            "user_id": user_id,
            "date": today,
            "created_at": now_utc.isoformat(),
            "checked_in_at": now_taipei.isoformat(),
            "timezone": CHECKIN_TIMEZONE_NAME,
            "points_earned": CHECKIN_POINTS_PER_DAY,
            "cycle_number": cycle_number,
            "day_number": next_day,
        }
        if self._repository.find_one({"user_id": user_id, "date": today}):
            return {
                "ok": True,
                "already": True,
                "message": "今天已經簽到過囉，明天再回來累積下一天 ♡",
                "checkin": None,
                "status": self.build_status(user_id),
            }
        inserted = self._repository.insert_one(document)
        if inserted is False:
            return {
                "ok": True,
                "already": True,
                "message": "今天已經簽到過囉，明天再回來累積下一天 ♡",
                "checkin": None,
                "status": self.build_status(user_id),
            }
        after_status = self.build_status(user_id)
        beauty_just_unlocked = not before_status["beauty_unlocked"] and after_status["beauty_unlocked"]
        doctor_just_unlocked = not before_status["doctor_unlocked"] and after_status["doctor_unlocked"]
        if next_day == CHECKIN_TOTAL_DAYS:
            message = f"✦ 第 {next_day} 天簽到成功，獲得 {CHECKIN_POINTS_PER_DAY}P ♡"
        elif doctor_just_unlocked:
            message = f"✦ 第 {next_day} 天簽到成功，獲得 {CHECKIN_POINTS_PER_DAY}P，皮膚科建議已解鎖！"
        elif beauty_just_unlocked:
            message = f"✦ 第 {next_day} 天簽到成功，獲得 {CHECKIN_POINTS_PER_DAY}P，每日美妝小知識已解鎖！"
        else:
            message = f"✦ 第 {next_day} 天簽到成功，獲得 {CHECKIN_POINTS_PER_DAY}P ♡"
        return {
            "ok": True,
            "already": False,
            "message": message,
            "checkin": {key: value for key, value in document.items() if key != "_id"},
            "status": after_status,
        }

    def history(self, user_id: str):
        history = []
        for index, record in enumerate(self._records(user_id)):
            history.append({
                "cycle_number": index // CHECKIN_TOTAL_DAYS + 1,
                "day_number": index % CHECKIN_TOTAL_DAYS + 1,
                "date": record.get("date"),
                "checked_in_at": record.get("checked_in_at") or record.get("created_at"),
                "timezone": record.get("timezone", CHECKIN_TIMEZONE_NAME),
                "points_earned": int(record.get("points_earned", CHECKIN_POINTS_PER_DAY)),
            })
        history.reverse()
        return {"ok": True, "user_id": user_id, "count": len(history), "history": history}
