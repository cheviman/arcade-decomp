/**
 *    Name:        emu.h
 *    Description: minimal stand-in for MAME's emu.h, enough to build MAME's tilemap and drawgfx code and the
 *                 Konami video chips (K052109, K051960) alone
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    kvideo.cpp includes it, and MAME's files after it, inside a namespace of their own (kv), so that their
 *    names keep away from the 68000 core's stand-ins in the same program; every standard header they use is
 *    included before that. The rendering is MAME's own (util/bitmap, util/palette, emu/drawgfx, emu/tilemap):
 *    what stands in here is the device framework around it. A device's ROM region is the host's (set_target),
 *    graphics are decoded as MAME's device_gfx_interface::decode_gfx does (digfx.cpp), the palette is MAME's
 *    palette_t with a group for shadows and one for highlights (dipalette.cpp), and the screen is one screen
 *    whose VBLANK callbacks the host fires. Saving state, timers and device callbacks do nothing.
 *    The gfx layout definitions and macros below are MAME's digfx.h (BSD-3-Clause, Nicola Salmoria, Aaron
 *    Giles); MAME's terms are in ../mame/LICENSE.
 */
#pragma once

#define __EMU_H__

using u8 = uint8_t;
using u16 = uint16_t;
using u32 = uint32_t;
using u64 = uint64_t;
using s8 = int8_t;
using s16 = int16_t;
using s32 = int32_t;
using s64 = int64_t;
using offs_t = uint32_t;
using pen_t = uint32_t;
using indirect_pen_t = uint16_t;

#define ATTR_COLD
#define NAME(x) x, #x
#define FUNC(x) &x, #x
#define BIT(x, n) (((x) >> (n)) & 1)
#define DEVICE_SELF ""
#define DECLARE_DEVICE_TYPE(Type, Class) extern const device_type Type;
#define DEFINE_DEVICE_TYPE(Type, Class, ShortName, FullName) const device_type Type = ShortName;
#define TIMER_CALLBACK_MEMBER(name) void name(s32 param)
#define DISABLE_COPYING(TYPE) TYPE(const TYPE &) = delete; TYPE &operator=(const TYPE &) = delete

enum endianness_t { ENDIANNESS_LITTLE, ENDIANNESS_BIG };
constexpr endianness_t ENDIANNESS_NATIVE = ENDIANNESS_LITTLE;
enum { CLEAR_LINE = 0, ASSERT_LINE = 1 };

template <typename... Args> inline void osd_printf_debug(const char *, Args...) {}
template <typename... Args> inline void osd_printf_verbose(const char *, Args...) {}
template <typename... Args> inline void popmessage(const char *, Args...) {}

class emu_fatalerror : public std::runtime_error
{
public:
	template <typename... Args>
	emu_fatalerror(const char *format, Args... args) : std::runtime_error(format_message(format, args...)) {}

private:
	template <typename... Args>
	static std::string format_message(const char *format, Args... args)
	{
		char message[512];
		std::snprintf(message, sizeof(message), format, args...);
		return message;
	}
};

struct device_missing_dependencies {};

template <typename... Args>
[[noreturn]] inline void fatalerror(const char *format, Args... args)
{
	throw emu_fatalerror(format, args...);
}

template <typename T>
inline std::unique_ptr<T> make_unique_clear(size_t count)
{
	auto result = std::make_unique<T>(count);
	std::fill_n(result.get(), count, 0);
	return result;
}

// The profiler's scopes: nothing is measured
enum { PROFILER_DRAWGFX, PROFILER_COPYBITMAP, PROFILER_TILEMAP_DRAW, PROFILER_TILEMAP_DRAW_ROZ, PROFILER_TILEMAP_UPDATE };
struct profiler_stub
{
	int start(int) { return 0; }
};
inline profiler_stub g_profiler;

#include "palette.h"
#include "bitmap.h"

class device_t;
class running_machine;
class screen_device;
class gfx_element;
class tilemap_t;
class tilemap_device;
class tilemap_manager;
using machine_config = int;
using device_type = const char *;

// MAME's intrusive list of tilemaps (the tilemap manager's), as tilemap.cpp uses it
template <class T>
class simple_list
{
public:
	class iterator
	{
	public:
		explicit iterator(T *item) : m_item(item) {}
		T &operator*() const { return *m_item; }
		iterator &operator++() { m_item = m_item->m_next; return *this; }
		bool operator!=(const iterator &other) const { return m_item != other.m_item; }

	private:
		T *m_item;
	};

	T &append(T &item)
	{
		item.m_next = nullptr;
		if (m_tail)
			m_tail->m_next = &item;
		else
			m_head = &item;
		m_tail = &item;
		m_count++;
		return item;
	}
	T *detach(T &item)
	{
		T *previous = nullptr;
		for (T *current = m_head; current; previous = current, current = current->m_next)
			if (current == &item)
			{
				(previous ? previous->m_next : m_head) = current->m_next;
				if (m_tail == current)
					m_tail = previous;
				m_count--;
				return current;
			}
		return nullptr;
	}
	T *find(int index) const
	{
		T *current = m_head;
		for (; current && index > 0; current = current->m_next)
			index--;
		return current;
	}
	int count() const { return m_count; }
	T *first() const { return m_head; }
	iterator begin() const { return iterator(m_head); }
	iterator end() const { return iterator(nullptr); }

private:
	T *m_head = nullptr;
	T *m_tail = nullptr;
	int m_count = 0;
};

// A device callback (MAME's delegates): a member of an object, or any function the host sets
template <typename Signature> class device_delegate;
template <typename R, typename... Args>
class device_delegate<R (Args...)>
{
public:
	device_delegate() = default;
	template <typename Owner, std::enable_if_t<!std::is_invocable_v<Owner &, Args...>, int> = 0>
	explicit device_delegate(Owner &owner) { (void)owner; }
	template <typename C, typename R2, typename... A2>
	device_delegate(C &object, R2 (C::*method)(A2...), const char *name)
		: m_function([&object, method](Args... args) -> R { return R((object.*method)(args...)); }) { (void)name; }
	template <typename C, typename R2, typename... A2>
	device_delegate(R2 (C::*method)(A2...), const char *name, C *object)
		: m_function([object, method](Args... args) -> R { return R((object->*method)(args...)); }) { (void)name; }
	template <typename C, typename R2, typename... A2>
	device_delegate(R2 (C::*method)(A2...), C *object)
		: m_function([object, method](Args... args) -> R { return R((object->*method)(args...)); }) {}

	void set(std::function<R (Args...)> function) { m_function = std::move(function); }
	template <typename C, typename R2, typename... A2>
	void set(C &object, R2 (C::*method)(A2...), const char *name) { *this = device_delegate(object, method, name); }
	void resolve() {}
	bool isnull() const { return !m_function; }
	explicit operator bool() const { return bool(m_function); }
	R operator()(Args... args) const { return m_function(args...); }

private:
	std::function<R (Args...)> m_function;
};

using save_prepost_delegate = device_delegate<void ()>;
using timer_expired_delegate = device_delegate<void (s32)>;
using vblank_state_delegate = device_delegate<void (screen_device &, bool)>;

// An output line of a device (MAME's devcb): none is connected
class devcb_write_line
{
public:
	template <typename Owner> explicit devcb_write_line(Owner &owner) { (void)owner; }
	devcb_write_line &bind() { return *this; }
	bool isunset() const { return true; }
	void operator()(int state) { (void)state; }
};

class finder_base
{
public:
	static constexpr const char *DUMMY_TAG = "";
};

// A device's ROM region: the host gives it (set_target)
template <typename T>
class region_ptr_finder
{
public:
	region_ptr_finder(device_t &owner, const char *tag) { (void)owner; (void)tag; }
	void set_target(T *target, size_t length) { m_target = target; m_length = length; }
	bool found() const { return m_target != nullptr; }
	size_t length() const { return m_length; }
	size_t bytes() const { return m_length * sizeof(T); }
	T *target() const { return m_target; }
	T &operator[](size_t index) const { return m_target[index]; }

private:
	T *m_target = nullptr;
	size_t m_length = 0;
};
template <typename T> using required_region_ptr = region_ptr_finder<T>;
template <typename T> using optional_region_ptr = region_ptr_finder<T>;

// A device another one finds by its tag: the host gives it
template <typename T>
class device_finder
{
public:
	template <typename Owner> device_finder(Owner &owner, const char *tag) { (void)owner; (void)tag; }
	template <typename Tag> void set_tag(Tag &&tag) { (void)tag; }
	void set_target(T *target) { m_target = target; }
	bool found() const { return m_target != nullptr; }
	T *operator->() const { return m_target; }
	T &operator*() const { return *m_target; }
	explicit operator bool() const { return m_target != nullptr; }

private:
	T *m_target = nullptr;
};
template <typename T> using required_device = device_finder<T>;
template <typename T> using optional_device = device_finder<T>;

class memory_share {};

// The memory a tilemap device decodes from (tilemap_device, which no Konami chip uses)
class memory_array
{
public:
	void set(const memory_share &share, int bytes_per_entry) { (void)share; (void)bytes_per_entry; }
	u32 read(offs_t offset) const { (void)offset; return 0; }
	void write(offs_t offset, u32 data) { (void)offset; (void)data; }
	void write8(offs_t offset, u8 data) { (void)offset; (void)data; }
	void write16(offs_t offset, u16 data, u16 mem_mask) { (void)offset; (void)data; (void)mem_mask; }
	void write32(offs_t offset, u32 data, u32 mem_mask) { (void)offset; (void)data; (void)mem_mask; }
	void *base() const { return nullptr; }
};

struct attotime
{
	u64 ticks = 0;
	static attotime from_ticks(u64 count, u32 clock) { (void)clock; return attotime{ count }; }
	static attotime from_usec(u64 count) { return attotime{ count }; }
	static const attotime zero;
	static const attotime never;
};
inline const attotime attotime::zero{ 0 };
inline const attotime attotime::never{ ~u64(0) };

// A timer of a device: the video chips' timers raise interrupts the board model raises itself
class emu_timer
{
public:
	void adjust(attotime start, s32 param = 0, attotime period = attotime::never) { (void)start; (void)param; (void)period; }
	bool enabled() const { return false; }
};

class save_manager
{
public:
	template <typename... Args> void save_item(Args &&...) {}
	template <typename... Args> void register_postload(Args &&...) {}
};

class device_t
{
public:
	device_t(const machine_config &config, device_type type, const char *tag, device_t *owner, u32 clock)
		: m_tag(tag), m_owner(owner), m_clock(clock)
	{
		(void)config; (void)type;
	}
	virtual ~device_t() = default;

	running_machine &machine() const;
	const char *tag() const { return m_tag; }
	device_t *owner() const { return m_owner; }
	u32 clock() const { return m_clock; }
	bool started() const { return true; }
	template <typename Tag> memory_share *memshare(Tag &&tag) const { (void)tag; return nullptr; }

	template <typename... Args> void logerror(const char *, Args...) const {}
	template <typename... Args> void save_item(Args &&...) {}
	template <typename... Args> void save_pointer(Args &&...) {}
	template <typename... Args> emu_timer *timer_alloc(Args &&...) { m_timers.emplace_back(); return &m_timers.back(); }

	// host API
	void start() { device_start(); }
	void reset() { device_reset(); }

protected:
	virtual void device_start() {}
	virtual void device_reset() {}
	virtual void device_post_load() {}

private:
	const char *m_tag;
	device_t *m_owner;
	u32 m_clock;
	std::list<emu_timer> m_timers;
};

// The palette: MAME's palette_t with its groups (normal, shadows, highlights), as device_palette_interface
// allocates it for an indexed screen with shadows and highlights (dipalette.cpp)
class device_palette_interface
{
public:
	static constexpr float DEFAULT_SHADOW_FACTOR = 0.6f;
	static constexpr float DEFAULT_HIGHLIGHT_FACTOR = 1.0f / DEFAULT_SHADOW_FACTOR;

	device_palette_interface(device_t &device, u32 entries)
		: m_device(device), m_entries(entries), m_shadow_array(65536), m_highlight_array(65536)
	{
		m_palette = palette_t::alloc(entries, 3);
		m_palette->group_set_contrast(1, DEFAULT_SHADOW_FACTOR);
		m_palette->group_set_contrast(2, DEFAULT_HIGHLIGHT_FACTOR);
		for (u32 pen = 0; pen < 65536; pen++)
		{
			m_shadow_array[pen] = pen < entries ? pen + entries : pen;
			m_highlight_array[pen] = pen < entries ? pen + 2 * entries : pen;
		}
		for (u32 pen = 0; pen < entries; pen++)
			m_palette->entry_set_color(pen, rgb_t::black());
	}
	~device_palette_interface() { m_palette->deref(); }

	device_t &device() const { return m_device; }
	palette_t *palette() const { return m_palette; }
	u32 entries() const { return m_entries; }
	const pen_t *pens() const { return reinterpret_cast<const pen_t *>(m_palette->entry_list_adjusted()); }
	pen_t pen(int index) const { return pens()[index]; }
	const pen_t *shadow_table() const { return m_shadow_array.data(); }
	int shadow_mode() const { return 0; }
	bool shadows_enabled() const { return true; }
	bool hilights_enabled() const { return true; }
	void set_shadow_factor(double factor) { m_palette->group_set_contrast(1, float(factor)); }
	void set_highlight_factor(double factor) { m_palette->group_set_contrast(2, float(factor)); }
	void set_pen_color(pen_t pen, rgb_t color) { m_palette->entry_set_color(pen, color); }
	// (an indirect palette's: the Konami boards have none)
	u32 transpen_mask(gfx_element &gfx, u32 color, indirect_pen_t transcolor) const
	{
		(void)gfx; (void)color; (void)transcolor;
		return 0;
	}

private:
	device_t &m_device;
	u32 m_entries;
	palette_t *m_palette;
	std::vector<pen_t> m_shadow_array;
	std::vector<pen_t> m_highlight_array;
};

// MAME's digfx.h: graphics layouts and decoding tables
constexpr u8 MAX_GFX_ELEMENTS = 32;
constexpr u16 MAX_GFX_PLANES = 8;
constexpr u16 MAX_GFX_SIZE = 32;

#define GFX_RAW                 0x12345678
#define RGN_FRAC(num,den)       (0x80000000 | (((num) & 0x0f) << 27) | (((den) & 0x0f) << 23))
#define IS_FRAC(offset)         ((offset) & 0x80000000)
#define FRAC_NUM(offset)        (((offset) >> 27) & 0x0f)
#define FRAC_DEN(offset)        (((offset) >> 23) & 0x0f)
#define FRAC_OFFSET(offset)     ((offset) & 0x007fffff)

#define GFXENTRY_XSCALEMASK   0x000000ff
#define GFXENTRY_YSCALEMASK   0x0000ff00
#define GFXENTRY_GETXSCALE(x) ((((x) & GFXENTRY_XSCALEMASK) >> 0) + 1)
#define GFXENTRY_GETYSCALE(x) ((((x) & GFXENTRY_YSCALEMASK) >> 8) + 1)
#define GFXENTRY_RAM          0x00010000
#define GFXENTRY_ISRAM(x)     (((x) & GFXENTRY_RAM) != 0)
#define GFXENTRY_DEVICE       0x00020000
#define GFXENTRY_REVERSE      0x00040000
#define GFXENTRY_ISREVERSE(x) (((x) & GFXENTRY_REVERSE) != 0)

#define GFXDECODE_END { 0 } };
#define DECLARE_GFXDECODE_MEMBER( name ) static const gfx_decode_entry name[]
#define GFXDECODE_MEMBER( name ) const gfx_decode_entry name[] = {
#define GFXDECODE_DEVICE(region,offset,layout,start,colors) { region, offset, &layout, start, colors, GFXENTRY_DEVICE },
#define GFXDECODE_DEVICE_RAM(region,offset,layout,start,colors) { region, offset, &layout, start, colors, GFXENTRY_DEVICE | GFXENTRY_RAM },

struct gfx_layout
{
	u32 xoffs(int x) const { return (extxoffs != nullptr) ? extxoffs[x] : xoffset[x]; }
	u32 yoffs(int y) const { return (extyoffs != nullptr) ? extyoffs[y] : yoffset[y]; }

	u16             width;              // pixel width of each element
	u16             height;             // pixel height of each element
	u32             total;              // total number of elements, or RGN_FRAC()
	u16             planes;             // number of bitplanes
	u32             planeoffset[MAX_GFX_PLANES]; // bit offset of each bitplane
	u32             xoffset[MAX_GFX_SIZE]; // bit offset of each horizontal pixel
	u32             yoffset[MAX_GFX_SIZE]; // bit offset of each vertical pixel
	u32             charincrement;      // distance between two consecutive elements (in bits)
	const u32 *     extxoffs;           // extended X offset array for really big layouts
	const u32 *     extyoffs;           // extended Y offset array for really big layouts
};

struct gfx_decode_entry
{
	const char *    memory_region;      // memory region where the data resides
	u32             start;              // offset of beginning of data to decode
	const gfx_layout *gfxlayout;        // pointer to gfx_layout describing the layout; nullptr marks the end of the array
	u16             color_codes_start;  // offset in the color lookup table where color codes start
	u16             total_color_codes;  // total number of color codes
	u32             flags;              // flags and optional scaling factors
};

// A device's graphics (MAME's device_gfx_interface): decode_gfx as MAME's, from the region the host gives
// (set_gfx_region), a byte wide and in the host's order
class device_gfx_interface
{
public:
	device_gfx_interface(const machine_config &config, device_t &device, const gfx_decode_entry *gfxinfo = nullptr,
		const char *palette_tag = finder_base::DUMMY_TAG)
		: m_gfxdecodeinfo(gfxinfo)
	{
		(void)config; (void)device; (void)palette_tag;
	}
	virtual ~device_gfx_interface() = default;

	void set_info(const gfx_decode_entry *gfxinfo) { m_gfxdecodeinfo = gfxinfo; }
	template <typename T> void set_palette(T &&tag) { (void)tag; }
	device_palette_interface &palette() const { return *m_palette; }
	gfx_element *gfx(u8 index) const { return m_gfx[index].get(); }
	void decode_gfx(const gfx_decode_entry *gfxdecodeinfo);
	void decode_gfx() { decode_gfx(m_gfxdecodeinfo); }
	void set_gfx(u8 index, std::unique_ptr<gfx_element> &&element) { m_gfx[index] = std::move(element); }

	// host API
	void set_palette_interface(device_palette_interface &palette) { m_palette = &palette; }
	void set_gfx_region(const u8 *base, u32 bytes) { m_region_base = base; m_region_bytes = bytes; }

private:
	device_palette_interface *m_palette = nullptr;
	std::unique_ptr<gfx_element> m_gfx[MAX_GFX_ELEMENTS];
	const gfx_decode_entry *m_gfxdecodeinfo;
	const u8 *m_region_base = nullptr;
	u32 m_region_bytes = 0;
};

// The one screen: its priority bitmap, its visible area, and the VBLANK callbacks the host fires
class screen_device
{
public:
	bitmap_ind8 &priority() { return m_priority; }
	const rectangle &visible_area() const { return m_visible_area; }
	const rectangle &cliprect() const { return m_visible_area; }
	void set_visible_area(int left, int right, int top, int bottom) { m_visible_area.set(left, right, top, bottom); }
	void register_vblank_callback(vblank_state_delegate callback) { m_vblank_callbacks.push_back(std::move(callback)); }
	bool started() const { return true; }
	int vpos() const { return 0; }
	attotime time_until_pos(int vpos, int hpos = 0) const { (void)vpos; (void)hpos; return attotime::zero; }

	// host API
	void vblank(bool state) { for (auto &callback : m_vblank_callbacks) callback(*this, state); }

private:
	bitmap_ind8 m_priority;
	rectangle m_visible_area;
	std::vector<vblank_state_delegate> m_vblank_callbacks;
};

class device_video_interface
{
public:
	device_video_interface(const machine_config &config, device_t &device, bool screen_required = true)
	{
		(void)config; (void)device; (void)screen_required;
	}
	template <typename T> void set_screen(T &&tag) { (void)tag; }
	screen_device &screen() const;
};

#include "drawgfx.h"
#include "tilemap.h"

class running_machine
{
public:
	running_machine() : m_tilemap(*this), m_root(0, "root", "root", nullptr, 0) {}
	tilemap_manager &tilemap() { return m_tilemap; }
	save_manager &save() { return m_save; }
	device_t &root_device() { return m_root; }
	bool side_effects_disabled() const { return false; }
	std::string describe_context() const { return std::string(); }

private:
	tilemap_manager m_tilemap;
	save_manager m_save;
	device_t m_root;
};

inline running_machine &the_machine()
{
	static running_machine machine;
	return machine;
}

inline screen_device &the_screen()
{
	static screen_device screen;
	return screen;
}

inline running_machine &device_t::machine() const
{
	return the_machine();
}

inline screen_device &device_video_interface::screen() const
{
	return the_screen();
}

// MAME's device_gfx_interface::decode_gfx (digfx.cpp), for a device's own region given by the host
inline void device_gfx_interface::decode_gfx(const gfx_decode_entry *gfxdecodeinfo)
{
	if (!gfxdecodeinfo)
		return;

	gfx_layout glcopy;
	std::vector<u32> extxoffs(0);
	std::vector<u32> extyoffs(0);

	for (u8 curgfx = 0; curgfx < MAX_GFX_ELEMENTS && gfxdecodeinfo[curgfx].gfxlayout != nullptr; curgfx++)
	{
		const gfx_decode_entry &gfx = gfxdecodeinfo[curgfx];
		u32 xscale = GFXENTRY_GETXSCALE(gfx.flags);
		u32 yscale = GFXENTRY_GETYSCALE(gfx.flags);
		u32 xormask = GFXENTRY_ISREVERSE(gfx.flags) ? 7 : 0;
		const u32 region_length = 8 * m_region_bytes;
		const u8 *region_base = m_region_base;

		std::memcpy(&glcopy, gfx.gfxlayout, sizeof(gfx_layout));
		if (IS_FRAC(glcopy.total))
			glcopy.total = region_length / glcopy.charincrement * FRAC_NUM(glcopy.total) / FRAC_DEN(glcopy.total);

		if (glcopy.planeoffset[0] != GFX_RAW)
		{
			extxoffs.resize(glcopy.width * xscale);
			extyoffs.resize(glcopy.height * yscale);
			std::memcpy(&extxoffs[0], (glcopy.extxoffs != nullptr) ? glcopy.extxoffs : glcopy.xoffset, glcopy.width * sizeof(u32));
			std::memcpy(&extyoffs[0], (glcopy.extyoffs != nullptr) ? glcopy.extyoffs : glcopy.yoffset, glcopy.height * sizeof(u32));
			glcopy.extxoffs = &extxoffs[0];
			glcopy.extyoffs = &extyoffs[0];

			if (xscale > 1)
			{
				glcopy.width *= xscale;
				for (int j = glcopy.width - 1; j >= 0; j--)
					extxoffs[j] = extxoffs[j / xscale];
			}
			if (yscale > 1)
			{
				glcopy.height *= yscale;
				for (int j = glcopy.height - 1; j >= 0; j--)
					extyoffs[j] = extyoffs[j / yscale];
			}
			for (int j = 0; j < glcopy.planes; j++)
				if (IS_FRAC(glcopy.planeoffset[j]))
					glcopy.planeoffset[j] = FRAC_OFFSET(glcopy.planeoffset[j])
						+ region_length * FRAC_NUM(glcopy.planeoffset[j]) / FRAC_DEN(glcopy.planeoffset[j]);
			for (int j = 0; j < glcopy.width; j++)
				if (IS_FRAC(extxoffs[j]))
					extxoffs[j] = FRAC_OFFSET(extxoffs[j]) + region_length * FRAC_NUM(extxoffs[j]) / FRAC_DEN(extxoffs[j]);
			for (int j = 0; j < glcopy.height; j++)
				if (IS_FRAC(extyoffs[j]))
					extyoffs[j] = FRAC_OFFSET(extyoffs[j]) + region_length * FRAC_NUM(extyoffs[j]) / FRAC_DEN(extyoffs[j]);
		}
		else
		{
			int base = gfx.start;
			int end = region_length / 8;
			int linemod = glcopy.yoffset[0];
			while (glcopy.total > 0)
			{
				int elementbase = base + (glcopy.total - 1) * glcopy.charincrement / 8;
				int lastpixelbase = elementbase + glcopy.height * linemod / 8 - 1;
				if (lastpixelbase < end)
					break;
				glcopy.total--;
			}
		}

		m_gfx[curgfx] = std::make_unique<gfx_element>(m_palette, glcopy,
			(region_base != nullptr) ? region_base + gfx.start : nullptr, xormask, gfx.total_color_codes, gfx.color_codes_start);
	}
}
