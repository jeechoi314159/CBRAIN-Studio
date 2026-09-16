/*
 * RHD2216 Intan bio-amplifier — SPI driver (nRF52832).
 * Configures the amplifier, verifies SPI comms ("INTAN"), runs ADC self-cal,
 * and reads blocks of samples via CONVERT commands.
 */
#ifndef CB_RHD_H
#define CB_RHD_H

#include <stdint.h>

enum cb_rhd_error {
	CB_RHD_BUS = 1, CB_RHD_SPI = 2, CB_RHD_ROM = 3,
	CB_RHD_MODEL = 4, CB_RHD_CONFIG = 5,
};

/* Returns 0 if the RHD responded with "INTAN" and was configured/calibrated,
 * negative on failure (SPI not ready / no chip). */
int cb_rhd_init(void);

/* Read ONE time-sample: `nch` channels into out[0..nch-1], int16 two's-complement.
 * `order` = physical RHD channel per output (NULL = 0..nch-1). Self-contained
 * (issues the 2-deep CONVERT pipeline flush). This is the sample-accurate primitive
 * driven by the fs timer (A1). */
int cb_rhd_read_sample(int16_t *out, const uint8_t *order, uint8_t nch);

/* Read `spc` samples for `ch_count` channels into dst, laid out [ch][spc]
 * (channel-major), int16 two's-complement — matching cb_encode_data.
 * `order` lists the physical RHD channel to convert for each row (NULL = 0..n-1). */
int cb_rhd_read_block(int16_t *dst, const uint8_t *order, uint8_t ch_count, uint8_t spc);

#endif /* CB_RHD_H */
