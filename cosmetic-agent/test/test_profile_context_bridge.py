from copy import deepcopy

from tools.user.profile_context_bridge import ProfileContextBridge


class FakeProfileRepository:
    def __init__(self, profile):
        self.profile = deepcopy(profile)
        self.update_calls = []

    def get_profile(self, user_id):
        return deepcopy(self.profile)

    def save_profile(self, user_id, profile):
        self.profile = deepcopy(profile)
        return deepcopy(self.profile)

    def update_skin_profile(self, user_id, skin_type=None, allergens=None):
        self.update_calls.append((user_id, skin_type, allergens))
        if skin_type is not None:
            self.profile["skinType"] = skin_type
        if allergens is not None:
            self.profile["allergens"] = list(allergens)
        return deepcopy(self.profile)

    def update_customer_type(self, user_id, customer_type):
        self.profile["customerType"] = customer_type
        return deepcopy(self.profile)


class FakeMemory:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value


def sample_profile():
    return {
        "user_id": "user-1",
        "name": "小美",
        "gender": "女",
        "birthday": "2000-01-02",
        "email": "mei@example.com",
        "skinType": "油性肌",
        "allergens": ["酒精"],
        "customerType": "ingredient_researcher",
        "wellnessStatus": "",
        "periodStartDate": "",
        "periodEndDate": "",
    }


def test_profile_question_is_deterministic():
    bridge = ProfileContextBridge("user-1", FakeProfileRepository(sample_profile()))
    profile = bridge.load_profile()

    assert bridge.answer_profile_question("我是什麼膚質？", profile) == (
        "您在基本資料中登記的膚質是「油性肌」。"
    )
    assert "酒精" in bridge.answer_profile_question("我對什麼過敏？", profile)
    assert "成分研究型" in bridge.answer_profile_question(
        "我是什麼消費人格？", profile
    )


def test_verified_context_excludes_email_but_includes_recommendation_facts():
    bridge = ProfileContextBridge("user-1", FakeProfileRepository(sample_profile()))
    context = bridge.build_prompt_context(bridge.load_profile())

    assert "膚質：油性肌" in context
    assert "酒精" in context
    assert "成分研究型" in context
    assert "mei@example.com" not in context


def test_explicit_skin_and_allergen_statement_updates_repository_and_memory():
    repository = FakeProfileRepository(sample_profile())
    memory = FakeMemory()
    bridge = ProfileContextBridge("user-1", repository)

    result = bridge.sync_explicit_facts(
        "我是乾性肌，而且我對香料和水楊酸過敏，請推薦乳液",
        current_profile=bridge.load_profile(),
        memory=memory,
    )

    assert result.changed
    assert not result.update_only
    assert result.profile["skinType"] == "乾性肌"
    assert result.profile["allergens"] == ["酒精", "香料", "水楊酸"]
    assert memory.values["skin_type"] == "dry"
    assert memory.values["disliked_ingredients"] == ["酒精", "香料", "水楊酸"]


def test_uncertain_or_question_form_does_not_change_profile():
    repository = FakeProfileRepository(sample_profile())
    bridge = ProfileContextBridge("user-1", repository)

    first = bridge.sync_explicit_facts("我可能是乾性肌嗎？")
    second = bridge.sync_explicit_facts("我對香料過敏嗎？")

    assert not first.changed_fields
    assert not second.changed_fields
    assert repository.update_calls == []


def test_explicit_allergen_removal_is_persisted():
    profile = sample_profile()
    profile["allergens"] = ["酒精", "香料"]
    repository = FakeProfileRepository(profile)
    bridge = ProfileContextBridge("user-1", repository)

    result = bridge.sync_explicit_facts("我對酒精不過敏了")

    assert result.changed
    assert result.profile["allergens"] == ["香料"]


def test_pure_profile_statement_returns_acknowledgement():
    repository = FakeProfileRepository(sample_profile())
    bridge = ProfileContextBridge("user-1", repository)

    result = bridge.sync_explicit_facts("我是乾性肌")

    assert result.changed
    assert result.update_only
    assert "乾性肌" in result.acknowledgement


def test_legacy_quiz_result_is_mirrored_when_profile_has_no_customer_type():
    profile = sample_profile()
    profile["customerType"] = ""
    repository = FakeProfileRepository(profile)

    class LegacyQuizBridge(ProfileContextBridge):
        def _latest_quiz_customer_type(self):
            return "price_sensitive"

    bridge = LegacyQuizBridge("user-1", repository)
    loaded = bridge.load_profile()

    assert loaded["customerType"] == "price_sensitive"
    assert "價格敏感型" in bridge.answer_profile_question(
        "我是什麼消費人格？",
        loaded,
    )
