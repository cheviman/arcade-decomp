/**
 *    Name:        kvideo.cpp
 *    Description: reference video of Konami's 68000 boards: MAME's K052109 and K051960 drawing the board
 *                 model's frames, as MAME's tmnt.cpp draws TMNT's
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    MAME's files are compiled here, unchanged, inside a namespace of their own (kv), over the stand-ins of
 *    this folder (emu.h, screen.h, memarray.h, logmacro.h): util/palette and util/bitmap, emu/drawgfx and
 *    emu/tilemap, and the K052109 and the K051960. What tmnt.cpp adds to them is written below as tmnt.cpp
 *    has it (MAME 0.289, Nicola Salmoria, BSD-3-Clause): its tile and sprite callbacks, its colors, its
 *    shadows and its screen update.
 */
#include "kvideo.h"

#include "game/data.h"
#include "tmnt/machine.h"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <list>
#include <memory>
#include <new>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

namespace kv
{
#include "emu.h"
#include "../mame/util/palette.cpp"
#include "../mame/util/bitmap.cpp"
#include "../mame/emu/drawgfx.cpp"
#include "../mame/emu/tilemap.cpp"
#include "../mame/konami/k052109.cpp"
#include "../mame/konami/k051960.cpp"
}

namespace
{

constexpr int kScreenWidth = 384;        // the screen's raw size and visible area (tmnt.cpp: set_raw)
constexpr int kScreenHeight = 264;
constexpr int kVisibleTop = TMNT_VBLANK_END;

struct video
{
	kv::device_t owner{ 0, "tmnt", "tmnt", nullptr, TMNT_VIDEO_CLOCK };
	kv::device_palette_interface palette{ owner, 1024 };
	kv::k052109_device k052109{ 0, "k052109", &owner, TMNT_VIDEO_CLOCK };
	kv::k051960_device k051960{ 0, "k051960", &owner, TMNT_VIDEO_CLOCK };
	kv::bitmap_ind16 bitmap{ kScreenWidth, kScreenHeight };
	// tmnt.cpp's video_start: the layers' and the sprites' colors
	const int layer_colorbase[3] = { 0, 32, 40 };
	const int sprite_colorbase = 16;
};

video *g_video = nullptr;
std::vector<uint32_t> g_frame(KV_WIDTH * KV_HEIGHT);

// The board's memories and registers into MAME's devices: what the program wrote since the last copy
void load_chips()
{
	kv::k052109_device &k052109 = g_video->k052109;
	for (kv::offs_t offset = 0; offset < TMNT_TILE_RAM_SIZE; offset++)
		if (k052109.read(offset) != tmnt_board.tile_ram[offset])
			k052109.write(offset, tmnt_board.tile_ram[offset]);

	kv::k051960_device &k051960 = g_video->k051960;
	for (kv::offs_t offset = 0; offset < TMNT_SPRITE_RAM_SIZE; offset++)
		k051960.k051960_w(offset, tmnt_board.sprite_ram[offset]);
	k051960.k051937_w(0, tmnt_board.sprite_control);
	k051960.k051937_w(1, tmnt_board.shadow_config);
	for (int bank = 0; bank < 3; bank++)
		k051960.k051937_w(kv::offs_t(2 + bank), tmnt_board.sprite_rom_bank[bank]);
}

// The palette RAM's colors: 1024 xBGR 555 entries, a byte of each in two bytes (the palette device's
// 8 memory bits, big endian as the 68000's bus)
void load_palette()
{
	for (int entry = 0; entry < 1024; entry++)
	{
		const uint16_t raw = uint16_t(tmnt_board.palette[entry * 2] << 8 | tmnt_board.palette[entry * 2 + 1]);
		g_video->palette.set_pen_color(kv::pen_t(entry),
			kv::rgb_t(kv::pal5bit(uint8_t(raw)), kv::pal5bit(uint8_t(raw >> 5)), kv::pal5bit(uint8_t(raw >> 10))));
	}
}

// tmnt.cpp's screen_update
void draw()
{
	kv::screen_device &screen = kv::the_screen();
	const kv::rectangle &cliprect = screen.visible_area();
	kv::bitmap_ind16 &bitmap = g_video->bitmap;
	kv::k052109_device &k052109 = g_video->k052109;
	kv::k051960_device &k051960 = g_video->k051960;
	const int priority = tmnt_board.priority;

	k052109.tilemap_draw(screen, bitmap, cliprect, 2, kv::TILEMAP_DRAW_OPAQUE, 0);
	if ((priority & 1) == 1)
		k051960.k051960_sprites_draw(bitmap, cliprect, screen.priority(), 0, 0);
	k052109.tilemap_draw(screen, bitmap, cliprect, 1, 0, 0);
	if ((priority & 1) == 0)
		k051960.k051960_sprites_draw(bitmap, cliprect, screen.priority(), 0, 0);
	k052109.tilemap_draw(screen, bitmap, cliprect, 0, 0, 0);

	const kv::pen_t *pens = g_video->palette.pens();
	for (int y = 0; y < KV_HEIGHT; y++)
		for (int x = 0; x < KV_WIDTH; x++)
			g_frame[size_t(y) * KV_WIDTH + x] = pens[bitmap.pix(kVisibleTop + y, x)] & 0x00FFFFFFu;
}

} // namespace

extern "C" void kv_attach(void)
{
	g_video = new video();
	kv::screen_device &screen = kv::the_screen();
	screen.set_visible_area(0, KV_WIDTH - 1, kVisibleTop, kVisibleTop + KV_HEIGHT - 1);
	screen.priority().allocate(kScreenWidth, kScreenHeight);

	kv::k052109_device &k052109 = g_video->k052109;
	k052109.set_palette_interface(g_video->palette);
	k052109.set_gfx_region(game_tiles, GAME_TILES_SIZE);
	// tmnt.cpp's tmnt_tile_callback
	k052109.set_tile_callback([](int layer, int bank, int &code, int &color, int &flags, int &priority) {
		(void)flags; (void)priority;
		code |= ((color & 0x03) << 8) | ((color & 0x10) << 6) | ((color & 0x0c) << 9) | (bank << 13);
		color = g_video->layer_colorbase[layer] + ((color & 0xe0) >> 5);
	});

	kv::k051960_device &k051960 = g_video->k051960;
	k051960.set_palette_interface(g_video->palette);
	k051960.set_gfx_region(game_sprites, GAME_SPRITES_SIZE);
	k051960.set_plane_order(kv::K051960_PLANEORDER_MIA);
	// tmnt.cpp's tmnt_sprite_callback
	k051960.set_sprite_callback([](int &code, int &color, int &priority, bool &shadow) {
		(void)priority; (void)shadow;
		code |= (color & 0x10) << 9;
		color = g_video->sprite_colorbase + (color & 0x0f);
	});

	// tmnt.cpp's video_start: shadows at 0.75
	g_video->palette.set_shadow_factor(0.75);

	k052109.start();
	k051960.start();
	k052109.reset();
	k051960.reset();
}

extern "C" void kv_vblank_end(void)
{
	load_chips();
	kv::the_screen().vblank(false);
}

extern "C" void kv_vblank_start(bool draw_frame)
{
	load_chips();
	if (draw_frame)
	{
		load_palette();
		draw();
	}
	kv::the_screen().vblank(true);
}

extern "C" const uint32_t *kv_frame(void)
{
	return g_frame.data();
}

extern "C" uint64_t kv_frame_hash(void)
{
	uint64_t hash = 0xcbf29ce484222325ull;
	for (uint32_t pixel : g_frame)
		for (int shift = 16; shift >= 0; shift -= 8)
			hash = (hash ^ ((pixel >> shift) & 0xff)) * 0x100000001b3ull;
	return hash;
}
