"""CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout.
Bit-serial on purpose: it is the same shift register the Verilog module
implements, so the two can be compared line by line."""

POLY = 0x1021
INIT = 0xFFFF


def crc16(data: bytes, crc: int = INIT) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ POLY) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


assert crc16(b"123456789") == 0x29B1, "CRC variant mismatch"
