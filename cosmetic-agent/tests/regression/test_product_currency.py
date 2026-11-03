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
