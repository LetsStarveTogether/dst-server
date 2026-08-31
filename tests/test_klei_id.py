import pytest

from dst_server import decode_klei_id, encode_klei_id


@pytest.mark.parametrize(
    ("klei_id", "encoded"),
    [
        ("KU_00000000", "A7G000000000"),
        ("KU_ABCDEFGH", "A7H8MC6JHT0H"),
        ("KU_WRv6AVc8", "A7K1NP32JUC8"),
        ("KU_abcdefgh", "A7KIB6JQ56LB"),
    ],
)
def test_klei_id_codec_matches_game(klei_id: str, encoded: str) -> None:
    assert encode_klei_id(klei_id) == encoded
    assert decode_klei_id(encoded) == klei_id


def test_klei_id_codec_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="Klei ID must match"):
        encode_klei_id("KU_short")
    with pytest.raises(ValueError, match="must contain 12 characters"):
        decode_klei_id("A7K1NP32JUC!")
    with pytest.raises(ValueError, match="does not contain a Klei ID"):
        decode_klei_id("000000000000")
