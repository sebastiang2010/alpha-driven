import pytest
from strategy import market_maker, config

def test_inventory_penalty_limits():
    # Positive inventory far beyond limit should be capped at MAX_SKU_SKEW
    large_inv = (config.MAX_SKU_SKEW / config.INVENTORY_GAMMA) * 2
    skew = market_maker.MarketMaker.apply_inventory_penalty(large_inv)
    assert skew == config.MAX_SKU_SKEW

    # Negative inventory far beyond limit should be capped at -MAX_SKU_SKEW
    large_neg_inv = -(config.MAX_SKU_SKEW / config.INVENTORY_GAMMA) * 2
    skew = market_maker.MarketMaker.apply_inventory_penalty(large_neg_inv)
    assert skew == -config.MAX_SKU_SKEW

    # Within limits, skew follows linear rule
    inv = 10.0
    expected = inv * config.INVENTORY_GAMMA
    skew = market_maker.MarketMaker.apply_inventory_penalty(inv)
    assert pytest.approx(skew, rel=1e-6) == expected
