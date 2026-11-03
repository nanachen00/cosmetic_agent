"""Knowledge Agent：Graph RAG 檢索與可設定模型供應者的最終回答生成。"""
from dotenv import load_dotenv

load_dotenv()

import os
import sys
import time



BASE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../")
)
if BASE_PATH not in sys.path:
    sys.path.insert(0, BASE_PATH)

from app.clients.knowledge_response_client import KnowledgeResponseClient
from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from app.services.knowledge_history_service import KnowledgeConversationHistory
from app.services.knowledge_prompt_builder import KnowledgePromptBuilder
from app.services.knowledge_retrieval_service import KnowledgeRetrievalService
from core.knowledge_response_config import KnowledgeResponseConfig
from core.observability import observe_pipeline_stage
from tools.agent.intent_parser import parse_intent
from tools.search.ingredient_extractor import terra_extractor
from tools.search.ingredient_tool import ingredient_lookup_tool
from tools.search.knowledge_intent import knowledge_intent_parser
from tools.search.risk_rule_tool import risk_rule_tool
from tools.search.skinconcern_tool import skin_tool


class KnowledgeSubAgent:
    def __init__(self):
        self.graph_repository = KnowledgeGraphRepository.from_env()
        self.neo4j_uri = self.graph_repository.uri
        self.neo4j_user = self.graph_repository.user
        self.neo4j_password = self.graph_repository.password
        self.neo4j_driver = self.graph_repository.driver

        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not openai_api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY 環境變數")

        response_config = KnowledgeResponseConfig.from_env()
        self.response_provider = response_config.provider
        self.max_output_tokens = response_config.max_output_tokens
        self.model = response_config.model

        self.response_client = KnowledgeResponseClient.from_config(
            config=response_config,
            openai_api_key=openai_api_key,
        )
        self.client = self.response_client.sdk_client

        self.skin_tool = skin_tool
        self.risk_tool = risk_rule_tool
        self.retrieval_service = KnowledgeRetrievalService(
            graph_repository=self.graph_repository,
            ingredient_extractor=terra_extractor,
            ingredient_lookup=ingredient_lookup_tool,
            skin_tool=self.skin_tool,
            risk_tool=self.risk_tool,
            observe_stage=observe_pipeline_stage,
        )

        self.prompt_builder = KnowledgePromptBuilder()

        # 保留原屬性作為相容介面，實際保存規則交由 history service。
        self.history_service = KnowledgeConversationHistory(max_history_turns=20)
        self.conversation_history = self.history_service.conversations
        self.max_history_turns = self.history_service.max_history_turns

        print(f"[Knowledge Agent] 最終回答模型：{self.model}")

    # ------------------------------------------------------------------
    # 對話歷史管理
    # ------------------------------------------------------------------

    def _get_history_service(self) -> KnowledgeConversationHistory:
        """同步暫留相容欄位並回傳歷史服務。"""
        history_service = getattr(self, "history_service", None)
        if history_service is None:
            history_service = KnowledgeConversationHistory(
                max_history_turns=self.max_history_turns
            )
            self.history_service = history_service

        history_service.conversations = self.conversation_history
        history_service.max_history_turns = self.max_history_turns
        return history_service

    def _get_history(self, session_id: str) -> list:
        """取得或初始化指定 session 的對話歷史。"""
        history_service = self._get_history_service()
        history = history_service.get(session_id)
        self.conversation_history = history_service.conversations
        return history

    def _append_history(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        """加入一則訊息，並限制保留的歷史輪數。"""
        history_service = self._get_history_service()
        history_service.append(session_id, role, content)
        self.conversation_history = history_service.conversations

    def clear_history(self, session_id: str) -> None:
        """清除指定 session 的 Knowledge Agent 暫存歷史。"""
        history_service = self._get_history_service()
        history_service.clear(session_id)
        self.conversation_history = history_service.conversations

    def _get_last_user_query(self, session_id: str) -> str:
        """取得指定 session 上一則使用者訊息。"""
        return self._get_history_service().last_user_query(session_id)

    def _resolve_query_for_retrieval(
        self,
        user_query: str,
        session_id: str,
    ) -> str:
        """
        只為檢索補上追問的上一句內容。

        合併後的文字不會直接拿去回覆使用者，避免模型複誦系統補上的內容。
        """
        normalized_query = user_query.strip()
        follow_up_markers = ["那", "還有", "呢", "而且", "另外"]
        is_short = len(normalized_query) <= 12
        looks_like_follow_up = is_short or any(
            normalized_query.startswith(marker)
            for marker in follow_up_markers
        )

        if not looks_like_follow_up:
            return user_query

        last_query = self._get_last_user_query(session_id)
        if not last_query:
            return user_query

        combined = f"{last_query}（延伸追問：{user_query}）"
        print(f"[Knowledge Agent] 檢索追問合併：{combined}")
        return combined

    def _get_retrieval_service(self) -> KnowledgeRetrievalService:
        """同步相容欄位與可替換工具，供 Graph RAG 檢索使用。"""
        retrieval_service = getattr(self, "retrieval_service", None)
        if retrieval_service is None:
            retrieval_service = KnowledgeRetrievalService(
                graph_repository=self.graph_repository,
                ingredient_extractor=terra_extractor,
                ingredient_lookup=ingredient_lookup_tool,
                skin_tool=self.skin_tool,
                risk_tool=self.risk_tool,
                observe_stage=observe_pipeline_stage,
            )
            self.retrieval_service = retrieval_service

        retrieval_service.graph_repository = self.graph_repository
        retrieval_service.ingredient_extractor = terra_extractor
        retrieval_service.ingredient_lookup = ingredient_lookup_tool
        retrieval_service.skin_tool = self.skin_tool
        retrieval_service.risk_tool = self.risk_tool
        retrieval_service.observe_stage = observe_pipeline_stage
        return retrieval_service

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def handle_request(
        self,
        user_query: str,
        is_a2a: bool = False,
        user_skin_type: str = "normal",
        session_id: str = "default",
        memory_context: str = "",
        parsed_intent=None,
        external_evidence: dict | None = None,
        on_delta=None,
    ) -> dict:
        print(
            "[Knowledge Agent] 收到請求："
            f"query={user_query!r}, a2a={is_a2a}, "
            f"skin_type={user_skin_type}, session={session_id}"
        )

        retrieval_query = self._resolve_query_for_retrieval(
            user_query,
            session_id,
        )

        # Root Agent 已解析過時直接沿用，避免同一問題再呼叫一次 Terra。
        stage_started = time.perf_counter()
        if parsed_intent is None:
            intent_res = parse_intent(
                user_query=retrieval_query,
                model="gpt-5.6-terra",
            )
            parse_status = "success"
        else:
            intent_res = parsed_intent
            parse_status = "reused"
        observe_pipeline_stage(
            "knowledge",
            "intent_parse",
            stage_started,
            status=parse_status,
        )

        stage_started = time.perf_counter()
        ordered_collections = (
            knowledge_intent_parser.get_ordered_collections(
                retrieval_query
            )
        )
        observe_pipeline_stage(
            "knowledge",
            "collection_route",
            stage_started,
        )

        final_context = self._get_retrieval_service().retrieve(
            query=retrieval_query,
            ordered_collections=ordered_collections,
            intent_result=intent_res,
            user_skin_type=user_skin_type,
        )

        if external_evidence:
            final_context = dict(final_context or {})
            final_context["external_evidence"] = external_evidence

        if is_a2a:
            return {
                "status": "success",
                "payload": final_context,
            }

        reply_result = self._generate_final_pharmacist_reply(
            query=user_query,
            context=final_context,
            session_id=session_id,
            memory_context=memory_context,
            on_delta=on_delta,
        )
        reply_result["payload"] = final_context
        return reply_result

    # ------------------------------------------------------------------
    # 回覆生成
    # ------------------------------------------------------------------

    def _get_prompt_builder(self) -> KnowledgePromptBuilder:
        """取得可獨立測試的提示詞建構器。"""
        prompt_builder = getattr(self, "prompt_builder", None)
        if prompt_builder is None:
            prompt_builder = KnowledgePromptBuilder()
            self.prompt_builder = prompt_builder
        return prompt_builder

    def _build_evidence(self, context: dict) -> str:
        """相容入口：交由 prompt builder 整理圖譜證據。"""
        return self._get_prompt_builder().build_evidence(context)

    def _build_system_prompt(
        self,
        evidence: str,
        memory_context: str,
    ) -> str:
        """相容入口：交由 prompt builder 建立 system prompt。"""
        return self._get_prompt_builder().build_system_prompt(
            evidence,
            memory_context,
        )

    def _generate_final_pharmacist_reply(
        self,
        query: str,
        context: dict,
        session_id: str = "default",
        memory_context: str = "",
        on_delta=None,
    ) -> dict:
        evidence = self._build_evidence(context)
        history = self._get_history(session_id)
        recent_user_messages = [
            message
            for message in history
            if message.get("role") == "user"
        ][-4:]
        recent_context = "\n".join(
            "使用者曾說：" + str(message.get("content") or "")[:600]
            for message in recent_user_messages
        )
        combined_memory = "\n\n".join(
            part
            for part in (
                recent_context,
                (memory_context or "").strip(),
            )
            if part
        )
        system_prompt = self._build_system_prompt(
            evidence,
            combined_memory,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"【本輪唯一要回答的問題】\n{query}",
            },
        ]

        print("=" * 60)
        print(
            f"Session: {session_id} | "
            f"歷史輪數: {len(history) // 2} | "
            f"Prompt length: {len(system_prompt)} | "
            f"Model: {self.model}"
        )
        print("=" * 60)

        stage_started = time.perf_counter()
        try:
            reply_text = self.response_client.generate(
                system_prompt=system_prompt,
                messages=messages,
                on_delta=on_delta,
            )

            if not reply_text:
                raise RuntimeError(f"{self.model} 沒有回傳回答內容")

            observe_pipeline_stage(
                "knowledge",
                f"final_generation_{self.response_provider}",
                stage_started,
            )

            self._append_history(session_id, "user", query)
            self._append_history(
                session_id,
                "assistant",
                reply_text,
            )

            return {
                "status": "success",
                "reply": reply_text,
                "stage": "knowledge_agent_final_reply",
            }
        except Exception as error:
            observe_pipeline_stage(
                "knowledge",
                f"final_generation_{self.response_provider}",
                stage_started,
                status="error",
            )
            return {
                "status": "error",
                "reply": (
                    "目前暫時無法完成知識查詢，請稍後再試一次。"
                ),
                "error": str(error),
            }

    def __del__(self):
        try:
            self.graph_repository.close()
        except Exception:
            pass


# 供 Root Agent Orchestrator 呼叫的單例 wrapper。
_agent_instance = None


def run(
    user_query: str,
    is_a2a: bool = False,
    user_skin_type: str = "normal",
    session_id: str = "default",
    memory_context: str = "",
    parsed_intent=None,
    external_evidence: dict | None = None,
    on_delta=None,
) -> dict:
    global _agent_instance

    if _agent_instance is None:
        _agent_instance = KnowledgeSubAgent()

    return _agent_instance.handle_request(
        user_query=user_query,
        is_a2a=is_a2a,
        user_skin_type=user_skin_type,
        session_id=session_id,
        memory_context=memory_context,
        parsed_intent=parsed_intent,
        external_evidence=external_evidence,
        on_delta=on_delta,
    )
