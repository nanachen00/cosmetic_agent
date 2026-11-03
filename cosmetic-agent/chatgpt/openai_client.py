from dotenv import load_dotenv

load_dotenv()
import os
from openai import OpenAI

# OpenAI API Key
API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# 建立 OpenAI Client
if not API_KEY:
    raise RuntimeError("缺少 OPENAI_API_KEY 環境變數")


client = OpenAI(
    api_key=API_KEY
)

# 模型名稱
MODEL_NAME = "gpt-5.6-terra"


def ask_model(user_query: str):
    """
    傳送使用者問題給 OpenAI 模型
    """

    response = client.responses.create(
        model=MODEL_NAME,
        input=user_query
    )

    return response.output_text


# 測試
if __name__ == "__main__":
    result = ask_model("我是油性肌，最近容易長痘痘，適合使用維他命C嗎？")
    print("AI 回覆：")
    print(result)