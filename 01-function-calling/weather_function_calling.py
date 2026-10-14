"""Minh hoạ FUNCTION CALLING thuần với Google Gemini SDK.

Tool `get_weather` được định nghĩa schema thủ công VÀ thực thi ngay trong
chính file app này. Model chỉ QUYẾT ĐỊNH gọi tool nào; app mới là nơi chạy.

Cách chạy:
    pip install -r ../requirements.txt
    export GEMINI_API_KEY=...
    python weather_function_calling.py
"""

from google import genai
from google.genai import types
from dotenv import load_dotenv

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()
client = genai.Client()

MODEL = "gemini-2.5-flash"

SYSTEM_INSTRUCTION = (
    "Bạn là trợ lý thời tiết thân thiện, trả lời bằng tiếng Việt tự nhiên. "
    "Dùng emoji phù hợp (🌧️ 🌤️ 💨 💧). "
    "Tóm tắt ngắn gọn, dễ hiểu, và đưa ra lời khuyên thực tế "
    "(ví dụ: mang ô, mặc áo mỏng, ...)."
)

# 1. App tự định nghĩa schema của tool
get_weather_declaration = types.FunctionDeclaration(
    name="get_weather",
    description="Lấy thời tiết hiện tại của một thành phố",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "city": types.Schema(
                type=types.Type.STRING, description="Tên thành phố"
            )
        },
        required=["city"],
    ),
)

get_forecast_declaration = types.FunctionDeclaration(
    name="get_forecast",
    description="Lấy dự báo thời tiết cho một thành phố từ 1 đến 7 ngày tới",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "city": types.Schema(
                type=types.Type.STRING, description="Tên thành phố"
            ),
            "days": types.Schema(
                type=types.Type.INTEGER,
                description="Số ngày dự báo (từ 1 đến 7, mặc định 3)",
            ),
        },
        required=["city"],
    ),
)

TOOLS = [
    types.Tool(
        function_declarations=[get_weather_declaration, get_forecast_declaration]
    )
]


# 2. App tự thực thi tool bằng Open-Meteo (API thật, không cần API key).
# ponytail: Open-Meteo REST trực tiếp không cache/geocode disambiguation; thêm local cache khi request volume tăng.
OPEN_METEO_GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"


def _get_json(url: str, params: dict[str, str]) -> dict:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "weather-function-calling/1.0"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def _location(city: str) -> dict:
    data = _get_json(OPEN_METEO_GEOCODING, {"name": city, "count": "1", "language": "en", "format": "json"})
    results = data.get("results", [])
    if not results:
        raise ValueError(f"Không tìm thấy thành phố: {city}")
    return results[0]


def _weather(city: str, days: int = 3) -> str:
    if not 1 <= days <= 7:
        raise ValueError("days phải nằm trong khoảng 1 đến 7")
    location = _location(city)
    data = _get_json(OPEN_METEO_FORECAST, {
        "latitude": str(location["latitude"]), "longitude": str(location["longitude"]),
        "forecast_days": str(days), "timezone": "auto",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
    })
    return json.dumps({"city": location["name"], "country": location.get("country"), "current": data.get("current"), "forecast": data.get("daily")}, ensure_ascii=False)


def get_weather(city: str) -> str:
    """Lấy thời tiết hiện tại của một thành phố từ Open-Meteo."""
    try:
        return _weather(city, 1)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False)


def get_forecast(city: str, days: int = 3) -> str:
    """Lấy dự báo thật từ Open-Meteo cho 1-7 ngày."""
    try:
        return _weather(city, days)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False)


TOOL_MAP = {
    "get_weather": get_weather,
    "get_forecast": get_forecast,
}


def run(prompt: str) -> str:
    """Gửi *prompt* tới Gemini, tự động xử lý function calling và trả về câu trả lời cuối."""
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    ]

    # 3. Gọi model — model quyết định có gọi tool hay không
    resp = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            tools=TOOLS,
            system_instruction=SYSTEM_INSTRUCTION,
        ),
    )

    # 4. Vòng lặp: nếu model yêu cầu tool, app TỰ THỰC THI rồi đưa kết quả trả lại
    while resp.function_calls:
        # Thêm phản hồi của model vào lịch sử hội thoại
        contents.append(resp.candidates[0].content)

        function_responses = []
        for fc in resp.function_calls:
            print(f"  [model yêu cầu] {fc.name}({fc.args})")
            tool_fn = TOOL_MAP.get(fc.name)
            result = tool_fn(**fc.args) if tool_fn else json.dumps({"error": f"Tool không hỗ trợ: {fc.name}"}, ensure_ascii=False)
            print(f"  [app thực thi]  -> {result}")
            function_responses.append(
                types.Part.from_function_response(
                    name=fc.name, response={"result": result}
                )
            )

        # Gửi kết quả tool trả về cho model
        contents.append(types.Content(role="user", parts=function_responses))
        resp = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                tools=TOOLS,
                system_instruction=SYSTEM_INSTRUCTION,
            ),
        )

    # 5. Model tổng hợp câu trả lời cuối
    return resp.text


def _self_check() -> None:
    data = json.loads(get_forecast("Hanoi", days=2))
    assert "forecast" in data and "time" in data["forecast"], f"Invalid forecast: {data}"
    assert len(data["forecast"]["time"]) == 2, f"Expected 2 days: {data}"


if __name__ == "__main__":
    _self_check()
    question = "Dự báo thời tiết Hà Nội và Đà Nẵng 3 ngày tới thế nào?"
    print(f"User: {question}\n")
    print("Trả lời:", run(question))
