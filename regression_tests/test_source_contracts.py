"""BLINK AI 離線回歸測試。

只讀取 Python 原始碼，不 import 專案、不連線資料庫、GetZep 或模型。
目的：在重啟 API 前，先攔截語法錯誤、端點消失及 Root Agent
關鍵分流順序遭到意外修改等問題。
"""

from __future__ import annotations

import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = (
    WORKSPACE_ROOT / "cosmetic-agent"
    if (WORKSPACE_ROOT / "cosmetic-agent").is_dir()
    else WORKSPACE_ROOT
)
API_FILE = PROJECT_ROOT / "api" / "api_server.py"
ROUTE_ROOT = PROJECT_ROOT / "app" / "api" / "routes"
HEALTH_ROUTE_FILE = ROUTE_ROOT / "health.py"
AUTH_ROUTE_FILE = ROUTE_ROOT / "auth.py"
CHAT_ROUTE_FILE = ROUTE_ROOT / "chat.py"
CONVERSATIONS_ROUTE_FILE = ROUTE_ROOT / "conversations.py"
ORCHESTRATOR_FILE = PROJECT_ROOT / "agents" / "root" / "orchestrator.py"
OBSERVABILITY_FILE = PROJECT_ROOT / "core" / "observability.py"


def read_source(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"找不到必要檔案：{path}")
    return path.read_text(encoding="utf-8")


class PythonSyntaxContracts(unittest.TestCase):
    def test_critical_files_compile(self) -> None:
        for path in (
            API_FILE,
            HEALTH_ROUTE_FILE,
            AUTH_ROUTE_FILE,
            CHAT_ROUTE_FILE,
            CONVERSATIONS_ROUTE_FILE,
            ORCHESTRATOR_FILE,
            OBSERVABILITY_FILE,
        ):
            with self.subTest(file=str(path.relative_to(PROJECT_ROOT))):
                source = read_source(path)
                compile(source, str(path), "exec")


class ApiContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = read_source(API_FILE)
        cls.health_source = read_source(HEALTH_ROUTE_FILE)
        cls.auth_source = read_source(AUTH_ROUTE_FILE)
        cls.chat_source = read_source(CHAT_ROUTE_FILE)
        cls.conversations_source = read_source(CONVERSATIONS_ROUTE_FILE)

    def test_existing_critical_routes_still_exist(self) -> None:
        expected_routes = (
            ('@router.get("/api/v1/health")', self.health_source),
            ('@router.post("/identity")', self.auth_source),
            ('@router.get("/api/chat/history")', self.conversations_source),
            ('@router.post("/api/chat/message")', self.chat_source),
            ('@router.post("/api/v1/session/clear")', self.chat_source),
        )
        for route, route_source in expected_routes:
            with self.subTest(route=route):
                self.assertIn(route, route_source)
        self.assertIn('prefix="/api/auth"', self.auth_source)
        self.assertIn("include_router", self.source)

    def test_observability_is_installed(self) -> None:
        self.assertIn(
            "from core.observability import install_observability",
            self.source,
        )
        self.assertIn("install_observability(", self.source)

    def test_chat_response_keeps_frontend_contract(self) -> None:
        self.assertIn('result["route"]', self.chat_source)
        self.assertIn('result["sessionId"]', self.chat_source)


class OrchestratorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = read_source(ORCHESTRATOR_FILE)

    def assert_ordered(self, *needles: str) -> None:
        positions = []
        start = self.source.index("def _process_current_turn")
        end = self.source.index("def _handle_new_input", start)
        block = self.source[start:end]
        cursor = -1
        for needle in needles:
            try:
                cursor = block.index(needle, cursor + 1)
                positions.append(cursor)
            except ValueError as error:
                raise AssertionError(f"Root 流程缺少關鍵步驟：{needle}") from error
        self.assertEqual(
            positions,
            sorted(positions),
            msg=f"Root Agent 關鍵分流順序被改動：{needles}",
        )

    def test_root_routing_priority_is_preserved(self) -> None:
        self.assert_ordered(
            "guard_input(user_input)",
            'user_input.startswith("cycle_")',
            "if self.state.is_waiting_skin_tracking:\n            return",
            "self._is_experience_topic_switch(user_input)",
            "looks_like_experience(user_input)",
            "self.state.is_waiting_rank_feedback",
            "self.state.is_waiting_confirm",
            "self.state.is_waiting_clarification",
            "# ── ⑧ 新輸入 ──",
        )

    def test_skin_tracking_stays_a_priority_flow(self) -> None:
        self.assertIn(
            'elif action == "cycle_skin_tracking"',
            self.source,
        )
        self.assertIn(
            'return self._handle_skin_tracking_input("開始記錄膚況")',
            self.source,
        )

    def test_experience_topic_switch_returns_to_intent_parser(self) -> None:
        self.assertIn("EXPERIENCE_TOPIC_SWITCH_MARKERS", self.source)
        self.assertIn("ignore_history=True", self.source)

    def test_knowledge_intent_calls_knowledge_agent(self) -> None:
        start = self.source.index("def _dispatch")
        end = self.source.index("def _handle_find_product", start)
        dispatch = self.source[start:end]
        self.assertIn("IntentType.KNOWLEDGE_QUERY", dispatch)
        self.assertIn("knowledge_agent_run(", dispatch)

    def test_agent_monitoring_decorator_remains_enabled(self) -> None:
        self.assertIn(
            "from core.observability import monitor_agent_turn",
            self.source,
        )
        self.assertIn("@monitor_agent_turn\n    def process", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
