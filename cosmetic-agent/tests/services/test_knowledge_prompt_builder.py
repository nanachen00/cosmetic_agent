import hashlib

from agents.knowledge.knowledge_sub_agent import KnowledgeSubAgent
from app.services.knowledge_prompt_builder import KnowledgePromptBuilder


def sample_context():
    return {
        "ingredient_info": {
            "name_zh": "玻尿酸",
            "name_en": "Hyaluronic Acid",
            "ingredient_type": "保濕",
            "benefits": ["補水", "保濕"],
        },
        "skin_concern_info": {
            "name": "敏感肌",
            "description": "容易泛紅",
        },
        "warnings": ["請先局部測試"],
    }


def test_prompt_builder_preserves_the_pre_refactor_output_exactly():
    builder = KnowledgePromptBuilder()

    evidence = builder.build_evidence(sample_context())
    prompt = builder.build_system_prompt(evidence, "使用者偏好無香料")

    assert len(evidence) == 85
    assert hashlib.sha256(evidence.encode()).hexdigest() == (
        "5ccb407bc74ff71ec383c231a07eafd6005dba8c39df770c7e6bc32baebf7ead"
    )
    assert len(prompt) == 1957
    assert hashlib.sha256(prompt.encode()).hexdigest() == (
        "4ef3d0a77dbcdb0c27486b9b562c4af06f352bc3c1601d8c5cbda1d15b8ce025"
    )


def test_agent_compatibility_methods_delegate_to_prompt_builder():
    agent = KnowledgeSubAgent.__new__(KnowledgeSubAgent)
    builder = KnowledgePromptBuilder()

    evidence = agent._build_evidence(sample_context())

    assert evidence == builder.build_evidence(sample_context())
    assert agent._build_system_prompt(evidence, "記憶") == (
        builder.build_system_prompt(evidence, "記憶")
    )
    assert agent.prompt_builder.__class__ is KnowledgePromptBuilder


def test_memory_context_remains_limited_to_six_thousand_characters():
    builder = KnowledgePromptBuilder()

    prompt = builder.build_system_prompt("證據", "A" * 7000)

    assert "A" * 6000 in prompt
    assert "A" * 6001 not in prompt


def test_external_product_evidence_is_rendered_for_terra():
    builder = KnowledgePromptBuilder()
    evidence = builder.build_evidence(
        {
            "external_evidence": {
                "provider": "tavily",
                "status": "ok",
                "requested_fields": ["skin_types"],
                "items": [
                    {
                        "name": "AKIMIA 藍銅胜肽微電流面膜",
                        "concerns": ["乾燥"],
                        "sources": [
                            {
                                "source_type": "official",
                                "title": "官方商品頁",
                                "domain": "www.akimia.com",
                                "url": "https://www.akimia.com/product",
                            }
                        ],
                    }
                ],
            }
        }
    )

    assert "【指定商品證據】" in evidence
    assert "AKIMIA 藍銅胜肽微電流面膜" in evidence
    assert "https://www.akimia.com/product" in evidence
    assert "若指定欄位沒有證據，必須明說資料不足" in evidence


def test_empty_external_product_evidence_tells_terra_not_to_guess():
    builder = KnowledgePromptBuilder()
    evidence = builder.build_evidence(
        {
            "external_evidence": {
                "provider": "tavily",
                "status": "not_found",
                "requested_fields": ["ingredients"],
                "items": [],
            }
        }
    )

    assert "工具查無可用商品資料" in evidence
    assert "不可自行推測" in evidence

