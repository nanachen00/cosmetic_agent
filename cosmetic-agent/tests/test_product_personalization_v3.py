from copy import deepcopy
import math
import pytest
from app.services.product_personalization import (
    DEFAULT_WEIGHTS, apply_scores, normalize_context, review_evidence,
    texture_labels, verified_attribute,
)
from agents.product.product_agent import _product_rank_key


def products():
    base = dict(concern_hits=6, efficacy_score=6, score=.7, price=500, currency='TWD',
        skin_types=['dry','oily'], ingredients_inci=['Water','Glycerin'], inci_status='full',
        skin_types_source_url='https://example.invalid/skin-types', skin_types_verification_status='verified',
        ingredient_evidence_level='full_inci', inci_source_url='https://example.invalid/formula',
        formula_market='TW', recommendation_scope='full_formula', data_quality='verified')
    return [dict(base,id='light',name='公開合成清爽凝膠',brand='BrandA',texture='清爽凝膠',
                 texture_source_url='https://example.invalid/light',texture_verification_status='verified'),
            dict(base,id='rich',name='公開合成滋潤配方',brand='BrandB',texture='滋潤配方',
                 texture_source_url='https://example.invalid/rich',texture_verification_status='verified')]


def ordered(rows, context):
    assert apply_scores(rows,context)
    return sorted(rows,key=lambda p:_product_rank_key(p,set(),personalization_applied=True))


def test_declared_default_is_75_percent_personal_inputs_and_no_fame_or_KOL():
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1)
    assert sum(DEFAULT_WEIGHTS[k] for k in ['need_fit','texture_fit','budget_fit','brand_preference']) == pytest.approx(.75)
    assert set(DEFAULT_WEIGHTS) == {'need_fit','ingredient_evidence','texture_fit','budget_fit','brand_preference','review_evidence'}


def test_same_query_and_catalogue_reverse_with_explicit_texture_preferences():
    a=ordered(products(),{'current_concerns':['保濕'],'preferred_textures':['清爽']})
    b=ordered(products(),{'current_concerns':['保濕'],'preferred_textures':['滋潤']})
    assert a[0]['id']=='light' and b[0]['id']=='rich'
    assert '質地' in a[0]['personalization_reasons'][0]
    assert a[0]['personalization_policy_version']=='personal-v3'


def test_current_texture_overrides_stored_preference():
    rows=ordered(products(),{'current_texture':'清爽','preferred_textures':['滋潤']})
    assert rows[0]['id']=='light'


def test_unknown_current_skin_type_does_not_erase_confirmed_profile():
    assert normalize_context({'current_skin_type':'unknown','skinType':'dry'})['skin_type']=='dry'
    assert normalize_context({'current_skin_type':'oily','skinType':'dry'})['skin_type']=='oily'


def test_personalization_is_primary_not_just_a_concern_hit_tiebreaker():
    rows=products();rows[0]['concern_hits']=7;rows[1]['concern_hits']=6
    ranked=ordered(rows,{'preferred_brands':['BrandB'],'ranking_policy_version':'personal-v3','ranking_weights':{'brand_score':1}})
    assert ranked[0]['id']=='rich'


def test_budget_preferences_can_change_order_and_no_budget_does_not_reward_cheapest():
    rows=products();rows[0]['price']=800;rows[1]['price']=300
    ranked=ordered(rows,{'current_budget_max':1000})
    assert ranked[0]['id']=='rich'
    assert ranked[0]['personalization_weights']['budget_fit']>0
    unlimited=normalize_context({'current_budget_max':None,'budget_max':500})
    assert unlimited['budget_max'] is None


def test_hard_warning_and_budget_tiers_still_cannot_be_bought_by_preference():
    rows=ordered(products(),{'preferred_brands':['BrandB']})
    rich=next(p for p in rows if p['id']=='rich');rich['price']=2000
    light=next(p for p in rows if p['id']=='light')
    assert _product_rank_key(rich,{'rich'},1000,True)>_product_rank_key(light,set(),1000,True)


def test_marketing_cannot_override_known_personal_fit():
    rows=products();rows[1].update(popularity_score=1e10,is_partner=True,KOL_followers=100000000,new_release=True)
    assert ordered(rows,{'preferred_textures':['清爽']})[0]['id']=='light'


@pytest.mark.parametrize('bad',[None,0,float('nan'),float('inf'),-3,True])
def test_unknown_or_invalid_review_count_never_becomes_social_proof(bad):
    assert review_evidence({'rating':5,'review_count':bad,'rating_source_url':'https://example.invalid/reviews'})==0


def test_review_count_is_shrunk_and_sponsored_content_not_independent_evidence():
    small=review_evidence({'rating':5,'review_count':1,'rating_source_url':'https://example.invalid/reviews'})
    large=review_evidence({'rating':4.5,'review_count':100,'rating_source_url':'https://example.invalid/reviews'})
    assert 0<small<large<=1
    assert review_evidence({'rating':5,'review_count':1000,'rating_source_url':'https://example.invalid/reviews','review_sponsored':True})==0


def test_missing_product_data_is_explained_not_invented_and_persona_alone_not_applied():
    assert not apply_scores(products(),{'customer_type':'cautious'})
    rows=[{'id':'unknown','name':'公開合成未知資料','price':None}]
    assert apply_scores(rows,{'current_skin_type':'sensitive','preferred_textures':['清爽']})
    p=rows[0]
    assert not p['personalization_reasons']
    assert any('缺少' in x for x in p['personalization_missing'])
    assert math.isfinite(p['personalization_score'])


def test_no_cross_profile_state_or_renormalization_from_candidate_pool():
    original=products();rows=deepcopy(original)
    apply_scores(rows,{'preferred_textures':['清爽'],'current_budget_max':1000})
    score=rows[0]['personalization_score'];weights=deepcopy(rows[0]['personalization_weights'])
    larger=deepcopy(original)+[dict(original[0],id='outlier',price=999999,concern_hits=1000000)]
    apply_scores(larger,{'preferred_textures':['清爽'],'current_budget_max':1000})
    assert larger[0]['personalization_score']==score
    assert larger[0]['personalization_weights']==weights
    assert original==products()


def test_nonfinite_and_huge_supplied_weights_cannot_produce_nan():
    rows=products()
    assert apply_scores(rows,{'current_concerns':['保濕'],'ranking_policy_version':'personal-v3','ranking_weights':{'need_fit':1e308,'review_evidence':float('nan')}})
    assert all(math.isfinite(p['personalization_score']) for p in rows)


def test_unversioned_inferred_weights_cannot_erase_actual_texture_preference():
    rows=ordered(products(),{'preferred_textures':['滋潤'], 'ranking_weights':{'brand_score':1}})
    assert rows[0]['id']=='rich'
    assert rows[0]['personalization_weights']['texture_fit']>0
    assert not apply_scores(products(),{'customer_type':'brand_based','ranking_weights':{'brand_score':1}})


def test_texture_requires_a_label_not_a_substring_or_ingredient_claim():
    assert not texture_labels('Angelica essence with enriched ingredients')
    assert not texture_labels('Rich in vitamin C')
    assert not texture_labels('不夠清爽、不太滋潤')
    assert texture_labels('Lightweight gel moisturizer') == {'light'}
    assert texture_labels('Rich cream with a soft texture') == {'rich'}


def test_unsourced_copy_and_legacy_skin_types_never_become_verified_fit():
    rows = [{
        'id': 'unsupported',
        'name': '清爽敏感肌精華',
        'description': '水感輕盈，適合敏感肌與乾性肌膚',
        'texture': '清爽',
        'skin_types': ['dry'],
        'suitable_skin_types': ['sensitive'],
    }]
    assert apply_scores(rows, {'current_skin_type': 'dry', 'preferred_textures': ['清爽']})
    product = rows[0]
    assert product['personalization_components']['texture_fit'] == 0
    assert product['personalization_components']['need_fit'] == 0
    assert not product['personalization_reasons']
    assert '缺少可核對的質地標示' in product['personalization_missing']
    assert '缺少可核對的適用膚質資料' in product['personalization_missing']

    legacy = [{
        'id': 'legacy-only',
        'suitable_skin_types': ['dry'],
        'skin_types_source_url': 'https://example.invalid/product',
        'skin_types_verification_status': 'verified',
    }]
    assert apply_scores(legacy, {'current_skin_type': 'dry'})
    assert legacy[0]['personalization_components']['need_fit'] == 0
    assert '缺少可核對的適用膚質資料' in legacy[0]['personalization_missing']


@pytest.mark.parametrize('field', ['texture', 'skin_types'])
def test_invalid_source_or_unverified_status_cannot_activate_attribute(field):
    product = {
        'texture': '清爽',
        'texture_source_url': 'https://user:secret@example.invalid/product',
        'texture_verification_status': 'verified',
        'skin_types': ['dry'],
        'skin_types_source_url': 'https://example.invalid/product',
        'skin_types_verification_status': 'machine_inferred',
    }
    assert verified_attribute(product, field) is False
