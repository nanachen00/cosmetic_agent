"""Graph RAG retrieval orchestration for Knowledge answers."""

import time

from core.observability import observe_pipeline_stage


class KnowledgeRetrievalService:
    """Collect graph evidence without owning answer-generation concerns."""

    def __init__(
        self,
        *,
        graph_repository,
        ingredient_extractor,
        ingredient_lookup,
        skin_tool,
        risk_tool,
        observe_stage=observe_pipeline_stage,
    ):
        self.graph_repository = graph_repository
        self.ingredient_extractor = ingredient_extractor
        self.ingredient_lookup = ingredient_lookup
        self.skin_tool = skin_tool
        self.risk_tool = risk_tool
        self.observe_stage = observe_stage

    @staticmethod
    def empty_context() -> dict:
        return {
            "graph_raw_data": None,
            "ingredient_info": None,
            "skin_concern_info": None,
            "warnings": [],
        }

    @staticmethod
    def order_collections(collections) -> list:
        ordered = list(collections)
        if "risk_rules" in ordered:
            ordered = [
                collection
                for collection in ordered
                if collection != "risk_rules"
            ] + ["risk_rules"]
        return ordered

    def retrieve(
        self,
        *,
        query: str,
        ordered_collections,
        intent_result,
        user_skin_type: str,
    ) -> dict:
        final_context = self.empty_context()
        collections = self.order_collections(ordered_collections)

        with self.graph_repository.session() as session:
            for collection in collections:
                print(
                    "[Knowledge Agent] 執行圖譜檢索："
                    f"{collection}"
                )

                if collection == "ingredients":
                    stage_started = time.perf_counter()
                    extracted_ingredient = (
                        self.ingredient_extractor.extract_ingredient(query)
                    )
                    self.observe_stage(
                        "knowledge",
                        "ingredient_extract",
                        stage_started,
                    )

                    if extracted_ingredient:
                        search_keyword = extracted_ingredient
                    elif intent_result and intent_result.query_expansion:
                        search_keyword = intent_result.query_expansion[0]
                    else:
                        search_keyword = query

                    stage_started = time.perf_counter()
                    ingredient_data = self.ingredient_lookup.get_node_data(
                        session=session,
                        keyword=search_keyword,
                    )
                    self.observe_stage(
                        "knowledge",
                        "neo4j_ingredient",
                        stage_started,
                    )
                    if ingredient_data:
                        final_context["ingredient_info"] = ingredient_data

                elif collection == "skin_concerns":
                    stage_started = time.perf_counter()
                    skin_data = self.skin_tool.search_concern(query)
                    self.observe_stage(
                        "knowledge",
                        "neo4j_skin_concern",
                        stage_started,
                    )
                    if skin_data:
                        final_context["skin_concern_info"] = skin_data

                elif collection == "risk_rules":
                    ingredient_info = final_context.get("ingredient_info")
                    current_ingredient = (
                        ingredient_info.get("name_zh")
                        if ingredient_info
                        else None
                    )
                    if current_ingredient:
                        stage_started = time.perf_counter()
                        warnings_found = self.risk_tool.check_ingredient_risk(
                            session,
                            current_ingredient,
                            user_skin_type,
                        )
                        self.observe_stage(
                            "knowledge",
                            "neo4j_risk_rules",
                            stage_started,
                        )
                        if warnings_found:
                            final_context["warnings"].extend(warnings_found)
                        final_context["graph_raw_data"] = {
                            "status": "risk_triggered",
                            "warnings": warnings_found,
                        }

        return final_context
