from strategy import config

def test_adverse_filter_threshold_defined():
    # The threshold must be a positive float and less than 1.0 (reasonable range)
    assert isinstance(config.ADVERSE_FILTER_THRESHOLD, float)
    assert 0.0 < config.ADVERSE_FILTER_THRESHOLD < 1.0
