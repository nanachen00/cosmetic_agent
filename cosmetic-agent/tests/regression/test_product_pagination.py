"""Regression tests for product pagination and cross-page de-duplication.

The production orchestrator is exercised with a stub product catalogue, so the
suite never connects to Neo4j, GetZep, OpenAI, or the embedding model.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch

# The pagination contract does not use GetZep.  Stub its import surface before
# loading the orchestrator so this regression suite stays dependency-free.
class _ZepClientStub:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


class _ZepMessageStub:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.__dict__.update(kwargs)


_zep_package = ModuleType("zep_cloud")
_zep_package.__path__ = []
_zep_client = ModuleType("zep_cloud.client")
_zep_client.Zep = _ZepClientStub
_zep_types = ModuleType("zep_cloud.types")
_zep_types.Message = _ZepMessageStub
_zep_package.client = _zep_client
_zep_package.types = _zep_types
sys.modules["zep_cloud"] = _zep_package
sys.modules["zep_cloud.client"] = _zep_client
sys.modules["zep_cloud.types"] = _zep_types


import agents.root.orchestrator as orchestrator_module
from agents.root.orchestrator import ConversationState, RootAgentOrchestrator


PAGE_SIZE = 5


@dataclass
class FakeIntent:
    category: str = "精華液"
    skin: str = "sensitive"
    concerns: tuple[str, ...] = ("保濕", "舒敏")
    budget: str = "1500"
    brands: tuple[str, ...] = ("品牌A",)

    def __post_init__(self) -> None:
        self.product_category = SimpleNamespace(value=self.category)
        self.skin_type = SimpleNamespace(value=self.skin)
        self.concern = list(self.concerns)
        self.budget_range = self.budget
        self.brand_preference = list(self.brands)

    def model_dump(self) -> dict:
        return {
            "product_category": self.category,
            "skin_type": self.skin,
            "concern": list(self.concerns),
            "budget_range": self.budget,
            "brand_preference": list(self.brands),
        }

    def snapshot(self) -> tuple:
        return (
            self.category,
            self.skin,
            self.concerns,
            self.budget,
            self.brands,
        )


class StubCatalog:
    """Return the first unseen page and record the orchestrator call contract."""

    def __init__(self, product_ids: list[str]) -> None:
        self.product_ids = product_ids
        self.calls: list[dict] = []

    @staticmethod
    def _excluded_from_call(args: tuple, kwargs: dict) -> set[str]:
        excluded: set[str] = set()
        for key, value in kwargs.items():
            if "exclude" not in key and "seen" not in key:
                continue
            if isinstance(value, (set, list, tuple, frozenset)):
                excluded.update(str(item) for item in value)
        # Also accept a positional exclusion collection so the test does not
        # prescribe a keyword name for the production implementation.
        for value in args:
            if isinstance(value, (set, list, tuple, frozenset)):
                excluded.update(str(item) for item in value)
        return excluded

    def run(self, intent: FakeIntent, *args, **kwargs) -> dict:
        excluded = self._excluded_from_call(args, kwargs)
        self.calls.append(
            {
                "intent": intent,
                "filters": intent.snapshot(),
                "excluded": frozenset(excluded),
            }
        )
        remaining = [
            product_id
            for product_id in self.product_ids
            if product_id not in excluded
        ]
        page = remaining[:PAGE_SIZE]
        return {
            "products": [
                {
                    "id": product_id,
                    "name": f"商品 {product_id}",
                    "brand": "測試品牌",
                    "category": intent.category,
                    "price": 500,
                }
                for product_id in page
            ],
            "warnings": [],
            "total_candidates": len(remaining),
        }


def format_stub_page(result: dict, *_args, **_kwargs) -> str:
    ids = [str(product["id"]) for product in result.get("products", [])]
    return "PAGE:" + ",".join(ids)


def page_ids(reply: str) -> list[str]:
    marker = "PAGE:"
    if marker not in reply:
        return []
    payload = reply.split(marker, 1)[1].splitlines()[0].strip()
    return [item for item in payload.split(",") if item]


def quick_reply_values(result: dict) -> set[str]:
    return {
        str(reply.get("value"))
        for reply in result.get("quick_replies", [])
        if isinstance(reply, dict)
    }


class ProductPaginationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.intent = FakeIntent()
        self.catalog = StubCatalog(list("ABCDEFGHIJKL"))
        self.agent = object.__new__(RootAgentOrchestrator)
        self.agent.state = ConversationState()
        self.agent.state.intent = self.intent
        self.agent.state.pain_gain = {}
        self.agent._log = lambda *_args, **_kwargs: None
        self.agent._run_product_agent = self.catalog.run
        self.format_patch = patch.object(
            orchestrator_module,
            "_format_ranked_products",
            side_effect=format_stub_page,
        )
        self.format_patch.start()
        self.addCleanup(self.format_patch.stop)

    def initial_page(self) -> list[str]:
        reply = self.agent._handle_find_product(self.intent)
        ids = page_ids(reply)
        self.assertEqual(ids, list("ABCDE"))
        return ids

    def test_initial_then_more_requeries_without_duplicates(self) -> None:
        first_page = self.initial_page()

        more = self.agent._handle_cycle_action("cycle_more")
        second_page = page_ids(more["reply"])

        self.assertEqual(second_page, list("FGHIJ"))
        self.assertTrue(set(first_page).isdisjoint(second_page))
        self.assertEqual(len(self.catalog.calls), 2)
        self.assertEqual(
            self.catalog.calls[1]["excluded"],
            frozenset(first_page),
        )

    def test_consecutive_more_advances_seen_product_ids(self) -> None:
        first_page = self.initial_page()
        second_page = page_ids(
            self.agent._handle_cycle_action("cycle_more")["reply"]
        )
        third_page = page_ids(
            self.agent._handle_cycle_action("cycle_more")["reply"]
        )

        self.assertEqual(second_page, list("FGHIJ"))
        self.assertEqual(third_page, list("KL"))
        self.assertEqual(
            self.catalog.calls[2]["excluded"],
            frozenset(first_page + second_page),
        )
        all_ids = first_page + second_page + third_page
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_exhaustion_returns_no_more_page_and_removes_more_action(self) -> None:
        self.catalog = StubCatalog(list("ABCDEFG"))
        self.agent._run_product_agent = self.catalog.run
        self.initial_page()
        last_page = self.agent._handle_cycle_action("cycle_more")
        self.assertEqual(page_ids(last_page["reply"]), list("FG"))

        exhausted = self.agent._handle_cycle_action("cycle_more")

        self.assertEqual(page_ids(exhausted["reply"]), [])
        self.assertNotIn("cycle_more", quick_reply_values(exhausted))
        self.assertEqual(len(self.catalog.calls), 3)

    def test_more_preserves_the_exact_intent_and_all_filters(self) -> None:
        self.initial_page()
        original_snapshot = self.intent.snapshot()

        self.agent._handle_cycle_action("cycle_more")

        more_call = self.catalog.calls[1]
        self.assertIs(more_call["intent"], self.intent)
        self.assertEqual(more_call["filters"], original_snapshot)
        self.assertEqual(self.intent.snapshot(), original_snapshot)

    def test_legacy_dynamic_category_label_fetches_more_without_changing_intent(self) -> None:
        self.intent = FakeIntent(category="面膜")
        self.agent.state.intent = self.intent
        first_page = self.initial_page()
        original_snapshot = self.intent.snapshot()

        action = self.agent._normalize_cycle_action("換個面膜")
        self.assertEqual(action, "cycle_more")
        result = self.agent._handle_cycle_action(action)
        second_page = page_ids(result["reply"])

        self.assertEqual(first_page, list("ABCDE"))
        self.assertEqual(second_page, list("FGHIJ"))
        self.assertTrue(set(first_page).isdisjoint(second_page))
        self.assertIs(self.agent.state.intent, self.intent)
        self.assertEqual(self.agent.state.intent.snapshot(), original_snapshot)
        self.assertEqual(self.catalog.calls[1]["filters"], original_snapshot)

    def test_change_condition_resets_pagination(self) -> None:
        self.initial_page()
        self.agent._handle_cycle_action("cycle_more")

        reset_result = self.agent._handle_cycle_action(
            "cycle_change_condition"
        )
        self.assertEqual(reset_result.get("stage"), "cycle_reset")
        self.assertIsNone(self.agent.state.intent)

        new_intent = FakeIntent(
            category="化妝水",
            skin="oily",
            concerns=("控油",),
            budget="800",
            brands=(),
        )
        self.agent.state.intent = new_intent
        restarted = page_ids(
            self.agent._handle_find_product(new_intent)
        )

        self.assertEqual(restarted, list("ABCDE"))
        self.assertEqual(self.catalog.calls[-1]["excluded"], frozenset())
        self.assertEqual(
            self.catalog.calls[-1]["filters"],
            new_intent.snapshot(),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
