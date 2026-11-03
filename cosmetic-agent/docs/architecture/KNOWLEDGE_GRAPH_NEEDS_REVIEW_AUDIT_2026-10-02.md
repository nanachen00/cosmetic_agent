# Knowledge Graph needs-review audit — 2026-10-02

This review was performed independently by three agents. It records data-quality decisions; it is not a medical recommendation.

## Current result

- Dataset blink-knowledge-v1 version 1.1.0-draft.
- 428 nodes and 1,203 edges; seed validation passes with no orphan or missing endpoint.
- 266 edges remain needs-review and must stay hidden or explicitly labelled in public UI.
- Remaining review queue: RELATED_CONCERN 98, APPLIES_TO_RULE 67, SUITABLE_FOR_CONCERN 50, WARNS_CONCERN 39, MAY_INCREASE_IRRITATION_WITH 12.

## Decisions already applied

1. Removed all 25 legacy CONFLICTS_WITH edges. They were 12 duplicated direction pairs plus one glycolic-acid self-loop, with no edge-level citation.
2. Vitamin C + vitamin E and vitamin C + niacinamide were removed instead of being presented as conflicts.
3. The remaining legacy pairs were deduplicated and reframed as MAY_INCREASE_IRRITATION_WITH. They remain needs-review and describe routine/formulation-dependent irritation, never an absolute incompatibility.
4. Added 皮爽 as an ambiguous query term with four non-cancelled TFDA product candidates. It is not an alias of betamethasone.
5. Product names remain DrugProduct nodes; active ingredients remain separate. TFDA license fields are official-regulatory. Market popularity, dose, regimen, comparative potency and stock availability are not inferred.
6. Added 12 CareNeed nodes. Need-to-ingredient edges are deterministic navigation derived from function links, not treatment claims. Traversal is bidirectional without duplicating facts.

## Public safety rules

- Rosacea, eczema and acne stay in a medical-condition path and must not be converted into ordinary cosmetic recommendations.
- A symptom such as redness never establishes a diagnosis.
- Drug paths show official licence data and require physician or pharmacist context; they do not provide dosage or treatment duration.
- A酸 is ambiguous in colloquial use. The exact resolver keeps Tretinoin compatibility, while the UI also exposes the topical-retinoid class.
- CosIng classifications describe cosmetic functions; they do not prove product-level safety or clinical efficacy.

## Next evidence upgrades

1. Replace broad RELATED_CONCERN with specific relations such as HAS_SUBTYPE, HAS_SIGN, MAY_LEAD_TO and DIFFERENTIAL_OF.
2. Upgrade support claims only when an AAD, FDA, TFDA or CIR source supports the same scope, concentration, formulation and jurisdiction.
3. Move generic patch-test and AHA sun-sensitivity advice to global conditional rules instead of duplicating it per ingredient.
4. Remove or rewrite risk rules that confuse a cosmetic ingredient with a drug, diagnosis or finished formulation.

## Primary sources

- TFDA all drug licences: https://data.gov.tw/dataset/9122
- AAD dry-skin care: https://www.aad.org/public/everyday-care/skin-care-basics/dry/dermatologists-tips-relieve-dry-skin
- AAD acne guideline: https://www.aad.org/member/clinical-quality/guidelines/acne
- AAD rosacea care: https://www.aad.org/public/diseases/rosacea/triggers/tips
- FDA OTC acne monograph M006 and skin-protectant monograph M016 are registered in the seed source catalogue.
