/**
 *    Name:        kvideo_053246.cpp
 *    Description: reference video of The Simpsons' board: MAME's K052109, K053246/K053247 and K053251 drawing the
 *                 board model's frames, as MAME's simpsons.cpp draws them
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    05-11-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    MAME's files are compiled here, unchanged, inside a namespace of their own (kv), over the stand-ins of
 *    this folder (emu.h, screen.h, memarray.h, logmacro.h): util/palette and util/bitmap, emu/drawgfx and
 *    emu/tilemap, the K052109, the K053246/K053247 and the K053251, and konami_helper. What simpsons.cpp adds to
 *    them is written below as simpsons.cpp has it (MAME 0.289, Ernesto Corvi, BSD-3-Clause): its tile and sprite
 *    callbacks, its palette (2048 colors, xBGR 555, shadows and highlights at MAME's default factors), the
 *    sprites' offsets (set_config) and its screen update.
 */
#include "kvideo_053246.h"

#include "game/data.h"
#include "simpsons/machine.h"

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
#include "../mame/konami/konami_helper.cpp"
#include "../mame/konami/k053246_k053247_k055673.cpp"
#include "../mame/konami/k053251.cpp"
}

namespace
{

constexpr uint32_t kVideoClock = 24000000;
constexpr int kScreenWidth = 384;        // the screen's raw size and visible area (simpsons.cpp: set_raw)
constexpr int kScreenHeight = 264;
constexpr int kVisibleTop = SIMPSONS_VBLANK_END;
constexpr int kColors = 2048;

struct video
{
	kv::device_t owner{ 0, "simpsons", "simpsons", nullptr, kVideoClock };
	kv::device_palette_interface palette{ owner, kColors };
	kv::k052109_device k052109{ 0, "k052109", &owner, kVideoClock };
	kv::k053247_device k053246{ 0, "k053246", &owner, kVideoClock };
	kv::k053251_device k053251{ 0, "k053251", &owner, 0 };
	kv::bitmap_ind16 bitmap{ kScreenWidth, kScreenHeight };
	// simpsons.cpp's: the layers' and the sprites' color bases and the layers' priorities, from the K053251
	int layer_colorbase[3] = { 0, 0, 0 };
	int sprite_colorbase = 0;
	int layerpri[3] = { 0, 0, 0 };
};

video *g_video = nullptr;
std::vector<uint32_t> g_frame(KV_WIDTH * KV_HEIGHT);

// The board's memories and registers into MAME's devices: what the program wrote since the last copy
void load_chips()
{
	kv::k052109_device &k052109 = g_video->k052109;
	for (kv::offs_t offset = 0; offset < SIMPSONS_TILE_RAM_SIZE; offset++)
		if (k052109.read(offset) != simpsons_board.tile_ram[offset])
			k052109.write(offset, simpsons_board.tile_ram[offset]);

	// the K053247's RAM, as the DMA left it (big-endian words), and the K053246's registers
	kv::k053247_device &k053246 = g_video->k053246;
	kv::u16 *ram = nullptr;
	k053246.k053247_get_ram(&ram);
	for (int word = 0; word < SIMPSONS_SPRITE_RAM_SIZE / 2; word++)
		ram[word] = kv::u16(simpsons_board.object_ram[word * 2] << 8 | simpsons_board.object_ram[word * 2 + 1]);
	for (int reg = 0; reg < 8; reg++)
		k053246.k053246_w(kv::offs_t(reg), simpsons_board.sprite_registers[reg]);

	for (int reg = 0; reg < 16; reg++)
		g_video->k053251.write(kv::offs_t(reg), simpsons_board.priority[reg]);
}

// The palette RAM's colors: 2048 xBGR 555 entries, big endian (the 053248's bus)
void load_palette()
{
	for (int entry = 0; entry < kColors; entry++)
	{
		const uint16_t raw = uint16_t(simpsons_board.palette[entry * 2] << 8 | simpsons_board.palette[entry * 2 + 1]);
		g_video->palette.set_pen_color(kv::pen_t(entry),
			kv::rgb_t(kv::pal5bit(uint8_t(raw)), kv::pal5bit(uint8_t(raw >> 5)), kv::pal5bit(uint8_t(raw >> 10))));
	}
}

// simpsons.cpp's screen_update
void draw()
{
	static const int k053251_ci[3] = { kv::k053251_device::CI2, kv::k053251_device::CI3, kv::k053251_device::CI4 };
	kv::screen_device &screen = kv::the_screen();
	const kv::rectangle &cliprect = screen.visible_area();
	kv::bitmap_ind16 &bitmap = g_video->bitmap;
	kv::k052109_device &k052109 = g_video->k052109;
	kv::k053251_device &k053251 = g_video->k053251;

	const int bg_colorbase = k053251.get_palette_index(kv::k053251_device::CI0);
	g_video->sprite_colorbase = k053251.get_palette_index(kv::k053251_device::CI1);
	for (int i = 0; i < 3; i++)
	{
		const int prev_colorbase = g_video->layer_colorbase[i];
		g_video->layer_colorbase[i] = k053251.get_palette_index(kv::u8(k053251_ci[i]));
		if (g_video->layer_colorbase[i] != prev_colorbase)
			k052109.mark_tilemap_dirty(kv::u8(i));
	}

	int layer[3]{};
	for (int i = 0; i < 3; i++)
	{
		layer[i] = i;
		g_video->layerpri[i] = k053251.get_priority(kv::u8(k053251_ci[i]));
	}
	kv::konami_sortlayers3(layer, g_video->layerpri);

	screen.priority().fill(0, cliprect);
	bitmap.fill(kv::u16(16 * bg_colorbase), cliprect);
	k052109.tilemap_draw(screen, bitmap, cliprect, layer[0], 0, 1);
	k052109.tilemap_draw(screen, bitmap, cliprect, layer[1], 0, 2);
	k052109.tilemap_draw(screen, bitmap, cliprect, layer[2], 0, 4);
	g_video->k053246.k053247_sprites_draw(bitmap, cliprect);

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
	// simpsons.cpp's tile_callback
	k052109.set_tile_callback([](int layer, int bank, int &code, int &color, int &flags, int &priority) {
		(void)flags; (void)priority;
		code |= ((color & 0x3f) << 8) | (bank << 14);
		color = g_video->layer_colorbase[layer] + ((color & 0xc0) >> 6);
	});

	kv::k053247_device &k053246 = g_video->k053246;
	k053246.set_palette_interface(g_video->palette);
	k053246.m_gfxrom.set_target(game_sprites, GAME_SPRITES_SIZE);
	k053246.set_config(NORMAL_PLANE_ORDER, -43, 23);
	// simpsons.cpp's sprite_callback
	k053246.set_sprite_callback([](int &code, int &color, int &priority_mask) {
		(void)code;
		const int pri = (color & 0x0f80) >> 6;
		const int *layerpri = g_video->layerpri;
		if (pri <= layerpri[2])
			priority_mask = 0;
		else if (pri > layerpri[2] && pri <= layerpri[1])
			priority_mask = 0xf0;
		else if (pri > layerpri[1] && pri <= layerpri[0])
			priority_mask = 0xf0 | 0xcc;
		else
			priority_mask = 0xf0 | 0xcc | 0xaa;
		color = g_video->sprite_colorbase + (color & 0x001f);
	});

	k052109.start();
	k053246.start();
	g_video->k053251.start();
	k052109.reset();
	k053246.reset();
	g_video->k053251.reset();
}

extern "C" void kv_vblank_end(bool draw_frame)
{
	load_chips();
	kv::the_screen().vblank(false);
	if (draw_frame)
	{
		load_palette();
		draw();
	}
}

extern "C" void kv_vblank_start(void)
{
	load_chips();
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
