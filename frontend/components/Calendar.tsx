"use client";

import { DayPicker } from "react-day-picker";
import type { DateRange } from "react-day-picker";
import { zhTW } from "date-fns/locale";

import "react-day-picker/dist/style.css";

type CalendarProps = {
  selectedRange: DateRange | undefined;
  recordDates: Date[];
  onRangeChange: (range: DateRange | undefined) => void;
};

export default function Calendar({
  selectedRange,
  recordDates,
  onRangeChange
}: CalendarProps) {
  return (
    <div className="skin-calendar">
      <div className="skin-calendar-heading">
        <strong>選擇日期區段</strong>
        <span>最少 1 天，最多 5 天</span>
      </div>

      <DayPicker
        mode="range"
        locale={zhTW}
        selected={selectedRange}
        onSelect={onRangeChange}

        /*
          DayPicker 的 max 是「夜數」：
          0 夜 = 同一天
          4 夜 = 共 5 個日期
        */
        max={4}
        resetOnSelect

        /*
          暫時禁止選未來日期。
          之後後端若允許查看預約資料，可以移除。
        */
        disabled={{ after: new Date() }}

        /*
          recordDates 內的日期會加上 recorded modifier，
          CSS 再用愛心標示。
        */
        modifiers={{
          recorded: recordDates
        }}
        modifiersClassNames={{
          recorded: "calendar-recorded-day"
        }}
        className="blink-day-picker"
      />

      <div className="skin-calendar-legend">
        <span>
          <i className="calendar-range-dot" />
          已選日期
        </span>

        <span>
          <i className="calendar-heart">♡</i>
          有膚況紀錄
        </span>
      </div>
    </div>
  );
}