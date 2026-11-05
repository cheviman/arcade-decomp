/**
 *    Name:        ksound_053260.h
 *    Description: reference sound board of The Simpsons: MAME's Z80 and K053260 and ymfm's YM2151, on the board
 *                 model
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    05-11-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    A development tool, never part of the game: the sound the game's own sound board has to make, and the Z80
 *    the 053248's program talks to (its RAM and ROM test asks the Z80 for its own). ks_attach() hooks it to the
 *    board (simpsons_board's sound chips, the Z80's IRQ and its NMI's arming): the Z80 runs the sound program
 *    cycle by cycle against the board's Z80 side (runtime/src/sound.c) whenever the board's time moves on
 *    (ks_run_until), and each chip makes its samples up to every moment something changes it.
 *
 *    Time is the board's (053248 cycles, SIMPSONS_CPU_CLOCK), kept from the Z80's own cycle count (3.579545
 *    MHz) so that nothing drifts. The YM2151 and the K053260 make a sample every 64 Z80 clocks; the K053260's
 *    timer steps every 16 and pulses SH1 every 64, which raises the Z80's NMI unless the program armed it in the
 *    last 4 cycles (simpsons.cpp's z80_nmi_w and z80_arm_nmi_w).
 */
#ifndef KONAMI_REFERENCE_SOUND_053260_H
#define KONAMI_REFERENCE_SOUND_053260_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Hooks the sound board to simpsons_board (after simpsons_reset and the ROM set) */
void ks_attach(void);

/* Runs the sound board up to board time 'time' */
void ks_run_until(uint64_t time);

/* Runs it up to 'time' and closes the frame's samples */
void ks_end_frame(uint64_t time);

/* FNV-1a hashes of the frame just closed: the samples of the two chips, and the Z80's RAM */
uint64_t ks_frame_samples_hash(void);
uint64_t ks_state_hash(void);

/* The frame's samples mixed as MAME's routes do, in stereo (the YM2151's first output at 0.5 to both sides,
   its second not connected; the K053260's two at 0.5 to their side), at the chips' rate, left and right
   interleaved */
const int16_t *ks_frame_mix(size_t *count);
uint32_t ks_mix_rate(void);

/* The Z80's progress: the cycles it ran, and the places it executed: the sound ROM's region offsets (0000-7FFF
   fixed, 10000-27FFF the banks'), 30000 plus the address for code above BFFF; one byte a place, 1 = executed */
uint64_t ks_z80_cycles(void);
const uint8_t *ks_executed(size_t *count);

/* The sound program ROM's bytes the Z80 reads as data (not its opcode and argument fetches), noted from
   ks_record_rom_reads on: ks_rom_reads gives them sorted, each (instruction place << 32 | region offset) */
void ks_record_rom_reads(void);
const uint64_t *ks_rom_reads(size_t *count);

#ifdef __cplusplus
}
#endif

#endif
