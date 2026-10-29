/**
 *    Name:        kvideo.h
 *    Description: reference video of Konami's 68000 boards: MAME's K052109 and K051960 drawing the board
 *                 model's frames, as MAME's tmnt.cpp draws TMNT's
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    A development tool, never part of the game: the frames the game's own video has to match. The board
 *    model (tmnt/machine.h) is the only state: at each of the screen's VBLANK edges the chips' memories and
 *    registers are copied from tmnt_board into MAME's devices, and the edge reaches them as MAME's screen
 *    sends it. As in MAME, a frame is drawn when VBLANK starts, before the chips' own VBLANK work (the
 *    K051960's sprite DMA: the frame shows the sprites of the DMA before); the K052109 takes its scroll
 *    registers when VBLANK ends.
 */
#ifndef KONAMI_REFERENCE_VIDEO_H
#define KONAMI_REFERENCE_VIDEO_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KV_WIDTH   320
#define KV_HEIGHT  224

/* The video chips, their graphics decoded from game/data.h's tile and sprite ROMs (after the ROM set is read) */
void kv_attach(void);

/* The screen's VBLANK ends (line 16) */
void kv_vblank_end(void);

/* The screen's VBLANK starts (line 240): the frame is drawn when 'draw' says so */
void kv_vblank_start(bool draw);

/* The last frame drawn: KV_WIDTH x KV_HEIGHT pixels, 0x00RRGGBB */
const uint32_t *kv_frame(void);

/* FNV-1a hash of the last frame's pixels (red, green, blue of each) */
uint64_t kv_frame_hash(void);

#ifdef __cplusplus
}
#endif

#endif
