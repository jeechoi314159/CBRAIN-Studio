/*
 * cb_dsp — CMSIS-DSP 실수 FFT 기반 대역 파워. detection.py band_power() 와 바이트 아닌
 * "수치" 호환(같은 창·같은 합)이라야 GUI 임계가 기기로 전달된다.
 */
#include <math.h>
#include <arm_math.h>

#include "cb_dsp.h"

static arm_rfft_fast_instance_f32 s_rfft;
static uint16_t s_n;
static float32_t s_hann[CB_DSP_MAXN];
static float32_t s_in[CB_DSP_MAXN];
static float32_t s_out[CB_DSP_MAXN];

uint16_t cb_dsp_pow2_floor(uint16_t w)
{
	uint16_t p = 32;
	while ((p << 1) <= w && (p << 1) <= CB_DSP_MAXN) {
		p <<= 1;
	}
	return p;
}

int cb_dsp_init(uint16_t n)
{
	if (n < 32 || n > CB_DSP_MAXN || (n & (n - 1)) != 0) {
		return -1;
	}
	if (arm_rfft_fast_init_f32(&s_rfft, n) != ARM_MATH_SUCCESS) {
		return -2;
	}
	s_n = n;
	const float two_pi = 6.283185307179586f;
	for (uint16_t i = 0; i < n; i++) {                 /* numpy hanning(n) */
		s_hann[i] = 0.5f - 0.5f * cosf(two_pi * i / (float)(n - 1));
	}
	return 0;
}

float cb_dsp_band_power(const int16_t *x, uint16_t n, int sr_hz, float f_lo, float f_hi)
{
	if (n != s_n) {
		return 0.0f;
	}
	float mean = 0.0f;
	for (uint16_t i = 0; i < n; i++) {
		mean += (float)x[i];
	}
	mean /= (float)n;
	for (uint16_t i = 0; i < n; i++) {
		s_in[i] = ((float)x[i] - mean) * s_hann[i];
	}
	arm_rfft_fast_f32(&s_rfft, s_in, s_out, 0);        /* forward, packed */

	/* 패킹: out[0]=DC.re, out[1]=Nyq.re, out[2k]=re, out[2k+1]=im (k=1..n/2-1) */
	const float df = (float)sr_hz / (float)n;
	float power = 0.0f;
	for (uint16_t k = 1; k < n / 2; k++) {
		float f = k * df;
		if (f >= f_lo && f < f_hi) {
			float re = s_out[2 * k], im = s_out[2 * k + 1];
			power += re * re + im * im;
		}
	}
	float fnyq = 0.5f * sr_hz;
	if (fnyq >= f_lo && fnyq < f_hi) {
		power += s_out[1] * s_out[1];
	}
	return power;
}
