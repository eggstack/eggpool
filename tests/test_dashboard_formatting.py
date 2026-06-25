from eggpool.dashboard.escape import format_bytes, format_scaled_count, format_tokens


def test_format_tokens_keeps_sub_million_counts_exact() -> None:
    assert format_tokens(0) == "0"
    assert format_tokens(47_000) == "47,000"
    assert format_tokens(999_999) == "999,999"


def test_format_tokens_scales_large_counts() -> None:
    assert format_tokens(21_230_000) == "21.23 M"
    assert format_tokens(3_120_000_000) == "3.12 B"
    assert format_tokens(1_110_000_000_000) == "1.11 T"


def test_format_scaled_count_handles_none_and_negative_values() -> None:
    assert format_scaled_count(None) == "0"
    assert format_scaled_count(-47_000) == "-47,000"
    assert format_scaled_count(-2_500_000) == "-2.50 M"


def test_format_bytes_scales_from_bytes_to_exabytes() -> None:
    assert format_bytes(999) == "999 B"
    assert format_bytes(1_500_000) == "1.5 MB"
    assert format_bytes(3_200_000_000) == "3.2 GB"
    assert format_bytes(4_700_000_000_000) == "4.7 TB"
    assert format_bytes(8_900_000_000_000_000) == "8.9 PB"
    assert format_bytes(1_200_000_000_000_000_000) == "1.2 EB"
