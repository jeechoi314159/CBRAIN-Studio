"""Acquisition provenance shared by the three experiment applications."""
import numpy as np

# CB_INTAN >= 0.3.0: valid source report, RHD failure, reason in bits 4..6.
SOURCE_VALID = 1 << 14
SENSOR_FAULT = 1 << 13
RHD_UV_PER_LSB = 0.195
FAULTS = {
    1: 'SPI 버스 준비 실패',
    2: 'SPI 전송 실패',
    3: 'INTAN 응답 불일치',
    4: 'RHD2216 모델 확인 실패',
    5: '증폭기 설정 확인 실패',
}

def legacy_test_pattern(block):
    """Exact old firmware fallback, not a heuristic for periodic biological data.

    Old firmware uses output row (not physical channel) and the absolute counter.
    Eight consecutive samples across every row avoid matching a single value.
    """
    if block.enc != 0 or block.n_samples < 8:
        return False
    n = np.arange(block.n_samples, dtype=np.int64) + block.first_counter
    factors = 7 + 3 * np.arange(block.channel_count, dtype=np.int64)
    expected = (((factors[:, None] * n) & 1023) - 512) * 16
    return np.array_equal(block.channels, expected)
