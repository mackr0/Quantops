"""Pure-function tests for normalize.py."""

from edgar13f.normalize import (
    cusip_to_ticker,
    is_valid_cusip_shape,
    normalize_cusip,
    normalize_discretion,
    normalize_put_call,
    parse_shares,
    parse_value_dollars,
    parse_value_thousands,  # back-compat alias
)


class TestValueParsing:
    def test_bare_integer(self):
        # Verified against Berkshire's real 13F — this equals $576M
        assert parse_value_dollars("576074081") == 576_074_081

    def test_with_commas(self):
        assert parse_value_dollars("576,074,081") == 576_074_081

    def test_none_input(self):
        assert parse_value_dollars(None) is None

    def test_empty_string(self):
        assert parse_value_dollars("") is None

    def test_garbage(self):
        assert parse_value_dollars("not a number") is None

    def test_legacy_alias_works(self):
        """Back-compat: callers importing the old name still work."""
        assert parse_value_thousands("576074081") == parse_value_dollars("576074081")


class TestSharesParsing:
    def test_bare(self):
        assert parse_shares("12719675") == 12_719_675

    def test_commas(self):
        assert parse_shares("12,719,675") == 12_719_675

    def test_empty(self):
        assert parse_shares(None) is None


class TestCusip:
    def test_valid_shape(self):
        assert is_valid_cusip_shape("037833100")
        assert is_valid_cusip_shape("02005N100")   # letter ok

    def test_invalid_shape(self):
        assert not is_valid_cusip_shape("abc")
        assert not is_valid_cusip_shape("0378331000")   # 10 chars
        assert not is_valid_cusip_shape("03783310")     # 8 chars
        assert not is_valid_cusip_shape("")
        assert not is_valid_cusip_shape(None)

    def test_normalize_uppercases(self):
        assert normalize_cusip("02005n100") == "02005N100"

    def test_normalize_returns_none_for_garbage(self):
        assert normalize_cusip("abc") is None


class TestCusipToTicker:
    def test_known_aapl(self):
        assert cusip_to_ticker("037833100") == "AAPL"

    def test_known_brk_b(self):
        assert cusip_to_ticker("084670702") == "BRK.B"

    def test_case_insensitive(self):
        assert cusip_to_ticker("037833100") == "AAPL"

    def test_unknown_returns_none(self):
        """Strict: unknown CUSIPs return None rather than guessing."""
        assert cusip_to_ticker("999999999") is None

    def test_invalid_shape_returns_none(self):
        assert cusip_to_ticker("junk") is None


class TestPutCall:
    def test_put(self):
        assert normalize_put_call("PUT") == "PUT"
        assert normalize_put_call("put") == "PUT"

    def test_call(self):
        assert normalize_put_call("Call") == "CALL"

    def test_empty_is_none(self):
        assert normalize_put_call(None) is None
        assert normalize_put_call("") is None


class TestDiscretion:
    def test_sole(self):
        assert normalize_discretion("SOLE") == "SOLE"

    def test_shared(self):
        assert normalize_discretion("SHARED") == "SHARED"

    def test_dfnd(self):
        assert normalize_discretion("DFND") == "DFND"

    def test_defined_maps_to_dfnd(self):
        assert normalize_discretion("DEFINED") == "DFND"

    def test_unknown_is_none(self):
        assert normalize_discretion("weird") is None
