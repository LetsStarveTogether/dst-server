_KLEI_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
_PATH_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUV"
_KLEI_ID_LENGTH = 11
_ENCODED_LENGTH = 12


def encode_klei_id(klei_id: str) -> str:
    """Encode a Klei ID as its uppercase player-save directory name."""
    if (
        len(klei_id) != _KLEI_ID_LENGTH
        or not klei_id.startswith("KU_")
        or any(character not in _KLEI_ALPHABET for character in klei_id[3:])
    ):
        msg = "Klei ID must match KU_[0-9A-Za-z_-]{8}"
        raise ValueError(msg)

    bits = "".join(
        f"{_KLEI_ALPHABET.index(character):06b}"
        for character in klei_id[:2] + klei_id[3:]
    )
    return "".join(
        _PATH_ALPHABET[int(bits[offset : offset + 5], 2)]
        for offset in range(0, len(bits), 5)
    )


def decode_klei_id(encoded: str) -> str:
    """Decode an uppercase player-save directory name to its Klei ID."""
    if len(encoded) != _ENCODED_LENGTH or any(
        character not in _PATH_ALPHABET for character in encoded
    ):
        msg = "encoded Klei ID must contain 12 characters from 0-9 and A-V"
        raise ValueError(msg)

    bits = "".join(f"{_PATH_ALPHABET.index(character):05b}" for character in encoded)
    value = "".join(
        _KLEI_ALPHABET[int(bits[offset : offset + 6], 2)]
        for offset in range(0, len(bits), 6)
    )
    if not value.startswith("KU"):
        msg = "encoded path does not contain a Klei ID"
        raise ValueError(msg)
    return f"KU_{value[2:]}"


__all__ = ["decode_klei_id", "encode_klei_id"]
