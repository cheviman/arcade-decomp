/**
 *    Name:        kvideo_053246.h
 *    Description: reference video of The Simpsons' board: MAME's K052109, K053246/K053247 and K053251 drawing the
 *                 board model's frames, as MAME's simpsons.cpp draws them
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    05-11-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    A development tool, never part of the game: the frames the game's own video has to match. The board model
 *    (simpsons/machine.h) is the only state: at each of the screen's VBLANK edges the chips' memories and
 *    registers are copied from simpsons_board into MAME's devices, and the edge reaches them as MAME's screen
 *    sends it. The screen updates after VBLANK (simpsons.cpp: VIDEO_UPDATE_AFTER_VBLANK): a frame is drawn when
 *    VBLANK ends (line 16), after the K052109 takes its scroll registers, so it shows the sprites the DMA copied
 *    as VBLANK started.
 */
#ifndef KONAMI_REFERENCE_VIDEO_053246_H
#define KONAMI_REFERENCE_VIDEO_053246_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KV_WIDTH   320
#define KV_HEIGHT  224

/* The video chips, their graphics decoded from game/data.h's tile and sprite ROMs (after the ROM set is read) */
void kv_attach(void);

/* The screen's VBLANK ends (line 16): the frame is drawn when 'draw' says so */
void kv_vblank_end(bool draw);

/* The screen's VBLANK starts (line 240) */
void kv_vblank_start(void);

/* The last frame drawn: KV_WIDTH x KV_HEIGHT pixels, 0x00RRGGBB */
const uint32_t *kv_frame(void);

/* FNV-1a hash of the last frame's pixels (red, green, blue of each) */
uint64_t kv_frame_hash(void);

#ifdef __cplusplus
}
#endif

#endif
