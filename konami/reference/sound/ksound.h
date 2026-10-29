/**
 *    Name:        ksound.h
 *    Description: reference sound board of TMNT: MAME's Z80, K007232 and uPD7759, ymfm's YM2151 and the
 *                 title music, on the board model
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    A development tool, never part of the game: the sound the game's own sound board has to make.
 *    ks_attach() hooks it to the board (tmnt_board's sound chips and the Z80's IRQ): the Z80 runs the sound
 *    program cycle by cycle against the board's Z80 side (runtime/src/sound.c) whenever the board's time
 *    moves on (ks_run_until), and each chip makes its samples, at its own rate, up to every moment something
 *    changes it.
 *
 *    Time is the board's (68000 cycles, TMNT_CPU_CLOCK), kept from the Z80's own cycle count (3.579545 MHz)
 *    so that nothing drifts. The YM2151 makes a sample every 64 of its clocks, the K007232 every 128 (both
 *    on the Z80's clock), the uPD7759 every 4 of its own (640 kHz) and the title music every 32 of them:
 *    sample n of a chip is the one it makes from time n x period on.
 */
#ifndef KONAMI_REFERENCE_SOUND_H
#define KONAMI_REFERENCE_SOUND_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Hooks the sound board to tmnt_board (after tmnt_reset and the ROM set) */
void ks_attach(void);

/* Runs the sound board up to board time 'time' */
void ks_run_until(uint64_t time);

/* Runs it up to 'time' and closes the frame's samples */
void ks_end_frame(uint64_t time);

/* FNV-1a hashes of the frame just closed: the samples of the four sources, and the Z80's RAM */
uint64_t ks_frame_samples_hash(void);
uint64_t ks_state_hash(void);

/* The frame's samples mixed as MAME's routes do (the YM2151's two outputs at 0.40, the K007232's at 0.15,
   the uPD7759 at 0.30, the title music at 0.25, all to one speaker) at the YM2151's rate, the others held
   between their samples */
const int16_t *ks_frame_mix(size_t *count);
uint32_t ks_mix_rate(void);

/* The Z80's progress: the cycles it ran, and the addresses it executed (8 KB, address >> 3, bit address & 7) */
uint64_t ks_z80_cycles(void);
const uint8_t *ks_executed(void);

#ifdef __cplusplus
}
#endif

#endif
