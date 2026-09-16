/*
 * cb_dsp — 온-디바이스 대역 파워 (CMSIS-DSP 실수 FFT).
 *
 * 데스크톱 cbrain_studio/app/detection.py 의 band_power() 와 **동일 공식**이라야
 * GUI 캘리브레이션 절대 임계가 기기로 그대로 전달된다:
 *   x -= mean(x);  X = rfft(x * hanning(n));  power = Σ|X[k]|²  (f_lo ≤ f_k < f_hi)
 * hanning(n) = 0.5 - 0.5·cos(2πi/(n-1)) (numpy 와 동일, n-1 분모).
 *
 * n 은 2의 거듭제곱(32..512). window_ms×sr/1000 을 pow2 로 내림해 쓴다(기본 250ms +
 * pow2 샘플레이트 → 정확히 pow2).
 */
#ifndef CB_DSP_H
#define CB_DSP_H

#include <stdint.h>

#define CB_DSP_MAXN 512      /* 최대 FFT 길이(2의 거듭제곱) */

/* FFT 길이 n(2의 거듭제곱) 로 초기화. 성공 0. */
int cb_dsp_init(uint16_t n);

/* int16 윈도우 x[0..n-1] 의 [f_lo, f_hi) 대역 파워. cb_dsp_init 의 n 과 일치해야. */
float cb_dsp_band_power(const int16_t *x, uint16_t n, int sr_hz, float f_lo, float f_hi);

/* w 이하의 최대 2의 거듭제곱 (32..512). */
uint16_t cb_dsp_pow2_floor(uint16_t w);

#endif /* CB_DSP_H */
