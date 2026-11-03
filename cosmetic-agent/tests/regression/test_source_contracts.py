"""BLINK AI 離線回歸測試。

只讀取 Python 原始碼，不 import 專案、不連線資料庫、GetZep 或模型。
目的：在重啟 API 前，先攔截語法錯誤、端點消失及 Root Agent
關鍵分流順序遭到意外修改等問題。
"""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_FILE = PROJECT_ROOT / "api" / "api_server.py"
ORCHESTRATOR_FILE = PROJECT_ROOT / "agents" / "root" / "orchestrator.py"
OBSERVABILITY_FILE = PROJECT_ROOT / "core" / "observability.py"
CHAT_GUARD_FILE = PROJECT_ROOT / "app" / "security" / "chat_guard.py"
MEMORY_SERVICE_FILE = PROJECT_ROOT / "tools" / "getzep" / "conversation_memory_service.py"
GUEST_SESSION_ROUTE_FILE = PROJECT_ROOT / "app" / "api" / "routes" / "guest_session.py"
CONVERSATION_ROUTE_FILE = PROJECT_ROOT / "app" / "api" / "routes" / "conversations.py"
CHAT_ROUTE_FILE = PROJECT_ROOT / "app" / "api" / "routes" / "chat.py"


def read_source(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"找不到必要檔案：{path}")
    return path.read_text(encoding="utf-8")


class PythonSyntaxContracts(unittest.TestCase):
    def test_critical_files_compile(self) -> None:
        for path in (
            API_FILE,
            ORCHESTRATOR_FILE,
            OBSERVABILITY_FILE,
            CHAT_GUARD_FILE,
            MEMORY_SERVICE_FILE,
            GUEST_SESSION_ROUTE_FILE,
            CONVERSATION_ROUTE_FILE,
            CHAT_ROUTE_FILE,
        ):
            with self.subTest(file=str(path.relative_to(PROJECT_ROOT))):
                source = read_source(path)
                compile(source, str(path), "exec")


class ApiContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = read_source(API_FILE)
        cls.guest_session_source = read_source(GUEST_SESSION_ROUTE_FILE)
        cls.conversation_route_source = read_source(CONVERSATION_ROUTE_FILE)
        cls.chat_route_source = read_source(CHAT_ROUTE_FILE)

    def test_existing_critical_routes_still_exist(self) -> None:
        expected_main_contracts = (
            ("health", ("/api/v1/health", "create_health_router")),
            ("identity", ("/api/auth/identity", "app.include_router(auth_router)")),
        )
        for route_name, markers in expected_main_contracts:
            with self.subTest(route=route_name):
                self.assertTrue(any(marker in self.source for marker in markers))
        self.assertIn("create_conversation_router(", self.source)
        self.assertIn('@router.get("/api/chat/history")', self.conversation_route_source)
        self.assertIn('@router.post("/api/chat/message")', self.chat_route_source)
        self.assertIn('@router.post("/api/v1/session/clear")', self.chat_route_source)
        self.assertIn("create_chat_router(", self.source)

    def test_observability_is_installed(self) -> None:
        self.assertIn(
            "from core.observability import install_observability",
            self.source,
        )
        self.assertIn("install_observability(", self.source)

    def test_chat_response_keeps_frontend_contract(self) -> None:
        self.assertIn('result["route"]', self.chat_route_source)
        self.assertIn('result["sessionId"]', self.chat_route_source)

    def test_chat_security_is_applied_to_every_paid_route(self) -> None:
        self.assertEqual(self.chat_route_source.count("await deps.begin_chat_security(request, req, user_id)"), 3)
        self.assertEqual(
            self.chat_route_source.count("await deps.guard_conversation_creation(request, user_id)"),
            3,
        )
        self.assertIn('request.headers.get("idempotency-key"', self.source)
        self.assertIn("await deps.finish_chat_security(guard_ticket)", self.chat_route_source)
        self.assertIn("finish_chat_security=_CHAT_GUARD.finish", self.source)
        self.assertIn("CHAT_SECURITY_LOG_HMAC_SECRET", self.source)
        self.assertIn("hmac.new(", self.source)

    def test_guest_identity_comes_from_signed_cookie(self) -> None:
        self.assertIn("create_guest_session_router(", self.source)
        self.assertIn('@router.post("/api/auth/guest-session")', self.guest_session_source)
        self.assertIn("signer.verify(cookie)", self.guest_session_source)
        self.assertIn("httponly=True", self.guest_session_source)
        self.assertIn('samesite="lax"', self.guest_session_source)


class OrchestratorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = read_source(ORCHESTRATOR_FILE)

    def assert_ordered(self, *needles: str) -> None:
        positions = []
        start = self.source.index("def _process_current_turn")
        end = self.source.index("def _handle_new_input", start)
        block = self.source[start:end]
        for needle in needles:
            try:
                positions.append(block.index(needle))
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
            "if self.state.is_waiting_clarification:\n            normalized",
            "return self._do_dispatch(self.state.intent)",
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


class ProductEmbeddingContracts(unittest.TestCase):
    def test_product_worker_never_loads_embedding_model_in_process(self) -> None:
        product_agent = PROJECT_ROOT / "agents" / "product" / "product_agent.py"
        source = read_source(product_agent)
        self.assertNotIn("from FlagEmbedding import BGEM3FlagModel", source)
        self.assertNotIn("BGEM3FlagModel(", source)
        self.assertIn("product embedding service unavailable", source)




if __name__ == "__main__":
    unittest.main(verbosity=2)
