"""Audio format conversion for calls.

Telephone audio (Twilio media streams) is G.711 μ-law at 8 kHz: one byte per
sample. The speech model works with 16-bit PCM at higher rates. These helpers
convert between the two. They are plain Python (the standard library's
`audioop` module is gone from Python 3.13), fast enough for the 20 ms frames a
call sends, and covered by round-trip tests.
"""

from array import array

_BIAS = 0x84
_CLIP = 32635


def _decode_byte(byte: int) -> int:
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


_ULAW_TO_LINEAR = [_decode_byte(b) for b in range(256)]


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample), _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not magnitude & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


# 16-bit samples are looked up by their top 14 bits; μ-law cannot tell the
# lowest two bits apart anyway.
_LINEAR_TO_ULAW = bytes(_encode_sample((i << 2) - 32768) for i in range(1 << 14))


def ulaw_to_pcm16(data: bytes) -> bytes:
    return array("h", (_ULAW_TO_LINEAR[b] for b in data)).tobytes()


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    return bytes(_LINEAR_TO_ULAW[(s + 32768) >> 2] for s in samples)


def resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Change the sample rate of 16-bit mono PCM by linear interpolation.

    Good enough for speech on a phone line. When the rate drops, neighbouring
    samples are averaged first, so high frequencies do not fold back as noise.
    """
    if from_rate == to_rate or not pcm:
        return pcm
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if to_rate < from_rate:
        samples = _moving_average(samples, round(from_rate / to_rate))
    count = len(samples) * to_rate // from_rate
    step = from_rate / to_rate
    out = array("h")
    last = len(samples) - 1
    for i in range(count):
        position = i * step
        left = int(position)
        right = min(left + 1, last)
        fraction = position - left
        out.append(round(samples[left] * (1 - fraction) + samples[right] * fraction))
    return out.tobytes()


def _moving_average(samples: array[int], width: int) -> array[int]:
    out = array("h")
    total = 0
    for i, sample in enumerate(samples):
        total += sample
        if i >= width:
            total -= samples[i - width]
        out.append(total // min(i + 1, width))
    return out
