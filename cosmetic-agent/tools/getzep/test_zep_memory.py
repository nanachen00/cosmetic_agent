import os
import sys
import uuid

base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)

from tools.getzep.zep_connection import get_zep_client
from zep_cloud.types import Message


def test_zep_ingest_sdk():
    client = get_zep_client()

    test_user_id = "user_mengchi_test"
    test_thread_id = f"thread_{uuid.uuid4().hex[:8]}"

    print(f"📡 正在透過官方 SDK 測試 Zep 寫入 | Thread ID: {test_thread_id} ...")

    # Step 1. 建立/同步 User（若已存在會丟出例外,屬正常情況,用 try/except 包起來即可）
    try:
        client.user.add(
            user_id=test_user_id,
            email=f"{test_user_id}@example.com",
            first_name="MengChi",
            last_name="Test",
        )
        print(f"👤 已建立使用者: {test_user_id}")
    except Exception as e:
        print(f"ℹ️ User 可能已存在，略過建立: {e}")

    # Step 2. 建立 Thread（v3 術語，取代原本的 Session）
    try:
        client.thread.create(
            thread_id=test_thread_id,
            user_id=test_user_id,
        )
        print(f"🏠 已建立 Thread: {test_thread_id}")
    except Exception as e:
        print(f"❌ 建立 Thread 失敗: {e}")
        return

    # Step 3. 寫入訊息
    try:
        result = client.thread.add_messages(
            thread_id=test_thread_id,
            messages=[
                Message(
                    role="user",
                    name="MengChi",
                    content="你好，我是敏感肌，平常選保養品很怕成分含有酒精或水楊酸。",
                ),
                Message(
                    role="assistant",
                    name="Assistant",
                    content="好的，收到！我會為您鎖定溫柔修護、無酒精與水楊酸的產品。",
                ),
            ],
        )
        print("✅ [Zep Test] 🎉 恭喜！成功寫入 Zep 雲端圖譜記憶體！")
        print("👉 現在請前往 Zep Dashboard (https://app.getzep.com) 點擊 Users 或 Graphs 查看結果！")
    except Exception as e:
        print(f"❌ [Zep Test] 寫入失敗: {e}")


if __name__ == "__main__":
    test_zep_ingest_sdk()