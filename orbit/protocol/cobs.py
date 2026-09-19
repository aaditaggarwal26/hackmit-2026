"""COBS (Cheshire & Baker). encode() returns the stuffed bytes WITHOUT the
0x00 delimiter; decode() takes the stuffed bytes WITHOUT the delimiter.
Python handles the general 0xFF-run case; the RTL does not need to because
every Orbit frame is <= 254 bytes (params.MAX_FRAME)."""


class CobsError(ValueError):
    pass


def encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_idx = 0
    code = 1
    for b in data:
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1
    out[code_idx] = code
    return bytes(out)


def decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise CobsError("zero byte inside encoded data")
        i += 1
        end = i + code - 1
        if end > n:
            raise CobsError("code runs past end of frame")
        out += data[i:end]
        i = end
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)
