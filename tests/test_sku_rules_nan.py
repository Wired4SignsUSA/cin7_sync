import math
from engine.sku_rules import parse_corner_bom_rule


def test_parse_corner_bom_rule_tolerates_nan_and_none():
    assert parse_corner_bom_rule(math.nan, math.nan) is None
    assert parse_corner_bom_rule(None, None) is None


def test_parse_corner_bom_rule_known_code_with_nan_attr1():
    rule = parse_corner_bom_rule(math.nan, " sr200 ")
    assert rule is not None
    assert rule["RuleCode"] == "SR200"
    assert rule["Attr1Name"] is None
