"""
Skin Report Tool 真實連線測試腳本
執行方式：
    python3 test_report_tool_real.py
"""
import os
import sys
from datetime import date

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from models.taide_client import TaideClient
from schemas.tracking_schema import Issue, Zone
from tools.agent.tracking.skin_report_tool import build_report, render_report_text


def test_report_tool_real():
    print("=" * 60)
    print("🧪 開始測試 Skin Report Tool (真實 TAIDE 連線版)")
    print("=" * 60)

    # 1. 初始化 TaideClient (連線至本機服務)
    try:
        real_llm = TaideClient(base_url="http://localhost:8004")
        print("✅ 成功初始化 TaideClient\n")
    except Exception as e:
        print(f"❌ 初始化 TaideClient 失敗: {e}")
        return

    # 2. 測試資料
    mock_records = [
        {
            "date": "2026-07-20",
            "zone": Zone.chin.value,
            "issue": Issue.acne.value,
            "severity": 4,
            "products": ["抗痘精華"],
        },
        {
            "date": "2026-07-25",
            "zone": Zone.chin.value,
            "issue": Issue.acne.value,
            "severity": 2,
            "products": ["修護霜"],
        },
        {
            "date": "2026-07-20",
            "zone": Zone.t_zone.value,
            "issue": Issue.oiliness.value,
            "severity": 2,
            "products": ["控油化妝水"],
        },
        {
            "date": "2026-07-25",
            "zone": Zone.t_zone.value,
            "issue": Issue.oiliness.value,
            "severity": 4,
            "products": ["控油化妝水"],
        },
    ]

    # 3. 執行報表構建（傳入真實的 real_llm）
    report = build_report(
        user_id="test_user_001",
        records=mock_records,
        date_from=date(2026, 7, 20),
        date_to=date(2026, 7, 25),
        consecutive_days=5,
        llm_client=real_llm,  # 👈 傳入真實 TaideClient 實例
    )

    # 4. 輸出文字報表結果
    text_output = render_report_text(report)

    print("=" * 60)
    print("📄 渲染文字報表輸出結果：")
    print("=" * 60)
    print(text_output)
    print("=" * 60)


if __name__ == "__main__":
    test_report_tool_real()
