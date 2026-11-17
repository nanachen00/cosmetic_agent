from agents.product.product_agent import _normalize_catalog_price


def test_oliveyoung_usd_price_is_converted_to_twd(monkeypatch):
    monkeypatch.setenv("PRODUCT_USD_TWD_RATE", "31.855")
    product = _normalize_catalog_price({"source": "oliveyoung", "price": 17})

    assert product["price"] == 542
    assert product["currency"] == "TWD"
    assert product["original_price"] == 17
    assert product["original_currency"] == "USD"


def test_taiwan_catalog_price_stays_in_twd():
    product = _normalize_catalog_price({"source": "watsons", "price": 189})

    assert product["price"] == 189
    assert product["currency"] == "TWD"
    assert product["original_currency"] == "TWD"


def test_explicit_currency_is_not_relabelled_or_converted_twice():
    known = _normalize_catalog_price({'source':'oliveyoung','price':542,'currency':'TWD'})
    assert known['price']==542
    unknown = _normalize_catalog_price({'source':'product_review','price':20,'currency':'EUR'})
    assert unknown['price'] is None and unknown['currency'] is None
    foreign = _normalize_catalog_price({'source':'product_review','price':20,'currency':'USD'})
    assert foreign['original_currency']=='USD' and foreign['price_is_estimate'] is True


def test_missing_or_nonfinite_price_stays_unknown():
    for value in [None, float('nan'),float('inf'),True,-1,0]:
        p=_normalize_catalog_price({'source':'product_review','price':value})
        assert p['price'] is None and p['currency'] is None


def test_unknown_source_does_not_invent_local_currency():
    product = _normalize_catalog_price({'source': 'product_review', 'price': 299})
    assert product['price'] is None and product['currency'] is None
    assert product['price_is_estimate'] is True


def test_recorded_taiwan_currency_aliases_remain_usable_without_conversion():
    for currency in ['NTD', 'NT$']:
        product = _normalize_catalog_price({'source':'product_review','price':299,'currency':currency})
        assert product['price']==299 and product['currency']=='TWD'
        assert product['price_is_estimate'] is False
