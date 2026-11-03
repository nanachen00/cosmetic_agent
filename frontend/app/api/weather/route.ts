import { NextRequest, NextResponse } from "next/server";

export const revalidate = 600;

type OpenMeteoResponse = {
  current: {
    time: string;
    temperature_2m: number;
    relative_humidity_2m: number;
    weather_code: number;
  };
  hourly: {
    time: string[];
    uv_index: number[];
    precipitation_probability: number[];
  };
};

function weatherIcon(code: number) {
  if (code <= 1) return "☀︎";
  if (code <= 3) return "◒";
  if (code === 45 || code === 48) return "≋";
  if (code >= 51 && code <= 82) return "☂";
  if (code >= 95) return "ϟ";
  return "☁︎";
}

function weatherCondition(code: number) {
  if (code === 0) return "晴朗";
  if (code === 1) return "大致晴朗";
  if (code === 2) return "晴時多雲";
  if (code === 3) return "陰天";
  if (code === 45 || code === 48) return "有霧";
  if (code >= 51 && code <= 57) return "毛毛雨";
  if (code >= 61 && code <= 67) return "有雨";
  if (code >= 71 && code <= 77) return "降雪";
  if (code >= 80 && code <= 82) return "陣雨";
  if (code >= 85 && code <= 86) return "陣雪";
  if (code >= 95) return "雷雨";
  return "多雲";
}

function fallbackAdvice(
  temperature: number,
  humidity: number,
  uv: number,
  rain: number,
) {
  const guidance = [
    humidity >= 75
      ? "保濕以清爽質地為主"
      : humidity <= 45
        ? "加強補水與鎖水"
        : "維持日常保濕",
  ];
  if (temperature >= 30) guidance.push("減少厚重疊擦");
  if (temperature <= 18) guidance.push("可提高乳霜滋潤度");
  if (uv >= 3) guidance.push("外出使用廣譜防曬");
  if (rain >= 60) guidance.push("戶外可選耐水型防曬");
  return guidance.slice(0, 3).join("，") + "。";
}

function coordinate(value: string | null, minimum: number, maximum: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= minimum && parsed <= maximum
    ? parsed
    : null;
}

function safeLocation(value: string | null) {
  const normalized = (value || "").trim().replace(/[\r\n]/g, " ");
  return normalized.slice(0, 80) || "目前位置";
}

export async function GET(request: NextRequest) {
  const latitude = coordinate(
    request.nextUrl.searchParams.get("latitude"),
    -90,
    90,
  );
  const longitude = coordinate(
    request.nextUrl.searchParams.get("longitude"),
    -180,
    180,
  );
  if (latitude === null || longitude === null) {
    return NextResponse.json({ error: "INVALID_LOCATION" }, { status: 400 });
  }
  const location = safeLocation(request.nextUrl.searchParams.get("location"));
  const endpoint = new URL("https://api.open-meteo.com/v1/forecast");
  endpoint.searchParams.set("latitude", String(latitude));
  endpoint.searchParams.set("longitude", String(longitude));
  endpoint.searchParams.set(
    "current",
    "temperature_2m,relative_humidity_2m,weather_code",
  );
  endpoint.searchParams.set("hourly", "uv_index,precipitation_probability");
  endpoint.searchParams.set("timezone", "auto");
  endpoint.searchParams.set("forecast_days", "1");

  try {
    const response = await fetch(endpoint, {
      next: { revalidate: 600 },
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) throw new Error("Open-Meteo " + response.status);

    const data = (await response.json()) as OpenMeteoResponse;
    const currentTime = new Date(data.current.time).getTime();
    const hourlyIndex = data.hourly.time.reduce((nearest, time, index) => {
      const distance = Math.abs(new Date(time).getTime() - currentTime);
      const nearestDistance = Math.abs(
        new Date(data.hourly.time[nearest]).getTime() - currentTime,
      );
      return distance < nearestDistance ? index : nearest;
    }, 0);

    const temperature = Math.round(data.current.temperature_2m);
    const humidity = Math.round(data.current.relative_humidity_2m);
    const uv = Math.round(data.hourly.uv_index[hourlyIndex] ?? 0);
    const rain = Math.round(
      data.hourly.precipitation_probability[hourlyIndex] ?? 0,
    );
    const condition = weatherCondition(data.current.weather_code);
    const advice = fallbackAdvice(temperature, humidity, uv, rain);

    return NextResponse.json(
      {
        location,
        temperature,
        humidity,
        uv,
        rain,
        icon: weatherIcon(data.current.weather_code),
        condition,
        advice,
      },
      { headers: { "Cache-Control": "public, s-maxage=600" } },
    );
  } catch (error) {
    console.error("weather fetch failed", error);
    return NextResponse.json(
      { error: "WEATHER_UNAVAILABLE" },
      { status: 503 },
    );
  }
}
