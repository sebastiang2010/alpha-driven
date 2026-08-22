from strategy import config

def test_base_size_calculation():
    # With exposure level 0 (dry‑run) the effective multiplier is SIMULATION_QUOTE_MULTIPLIER
    effective = config.effective_exposure_multiplier()
    base = config.BASE_ORDER_SIZE_XRP * effective
    # SIMULATION_QUOTE_MULTIPLIER is 1.0 by default, so base should equal BASE_ORDER_SIZE_XRP
    assert base == config.BASE_ORDER_SIZE_XRP
