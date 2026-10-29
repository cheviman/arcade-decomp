/**
 *    Name:        emu.h
 *    Description: minimal stand-in for MAME's emu.h, enough to build MAME's Z80 core and disassembler, the
 *                 K007232 and the uPD7759 alone
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    ksound.cpp includes it, and MAME's files after it, inside a namespace of their own (ks), so that their
 *    names keep away from the 68000 core's and the video's stand-ins in the same program; every standard
 *    header they use is included before that. With MAME's meaning: devices with a clock, a CPU that runs
 *    for a number of cycles, memory as functions the host sets (the Z80's), a device's ROM region found by
 *    the device's tag among the ones the host gives (set_region), a sound stream the host pulls samples
 *    from, writes that MAME synchronizes to the present (scheduler().synchronize) done at once (the host
 *    brings the chips up to the present first), and no-ops for saving state, logging and the debugger.
 *    Built on system32/reference/sound/emu.h, the Z80's.
 */
#pragma once

using u8 = uint8_t;
using u16 = uint16_t;
using u32 = uint32_t;
using u64 = uint64_t;
using s8 = int8_t;
using s16 = int16_t;
using s32 = int32_t;
using s64 = int64_t;
using offs_t = uint32_t;

#define ATTR_COLD
#define NAME(x) x, #x
#define STRUCT_MEMBER(array, member) array, #member
#define FUNC(x) &x, #x
#define DEVICE_SELF ""
#define DECLARE_DEVICE_TYPE(Type, Class) extern const device_type Type;
#define DEFINE_DEVICE_TYPE(Type, Class, ShortName, FullName) const device_type Type = ShortName;
#define TIMER_CALLBACK_MEMBER(name) void name(s32 param)
#define ALLOW_SAVE_TYPE(type)

enum { CLEAR_LINE = 0, ASSERT_LINE = 1, HOLD_LINE = 2 };
enum { INPUT_LINE_IRQ0 = 0, INPUT_LINE_NMI = 32 };
enum { AS_PROGRAM = 0, AS_DATA = 1, AS_IO = 2, AS_OPCODES = 3 };
enum { STATE_GENPC = -1, STATE_GENPCBASE = -2, STATE_GENFLAGS = -3 };
enum endianness_t { ENDIANNESS_LITTLE, ENDIANNESS_BIG };

// The cores call this on conditions they cannot continue from; the host catches it
[[noreturn]] inline void fatalerror(const char *format, ...)
{
	char message[512];
	va_list arguments;
	va_start(arguments, format);
	std::vsnprintf(message, sizeof(message), format, arguments);
	va_end(arguments);
	throw std::runtime_error(message);
}

template <typename... Args>
std::string string_format(const char *format, Args... args)
{
	char buffer[512];
	std::snprintf(buffer, sizeof(buffer), format, args...);
	return buffer;
}

template <typename... Args> inline void osd_printf_error(const char *, Args...) {}

// an array of 'size' value-initialized elements, as MAME's make_unique_clear
template <typename T>
std::enable_if_t<std::is_array<T>::value, std::unique_ptr<T>> make_unique_clear(size_t size)
{
	return std::unique_ptr<T>(new std::remove_extent_t<T>[size]());
}

// bit 'n' of 'x', as MAME's BIT
template <typename T, typename U>
constexpr T BIT(T x, U n)
{
	return (x >> n) & T(1);
}

// 'w' bits of 'x' from bit 'n' on, as MAME's three-argument BIT
template <typename T, typename U, typename V>
constexpr T BIT(T x, U n, V w)
{
	return (x >> n) & T((T(1) << w) - 1);
}

// a register pair, as MAME's PAIR16 (little-endian host)
union PAIR16
{
	struct { u8 l, h; } b;
	struct { s8 l, h; } sb;
	u16 w;
	s16 sw;
};

namespace util {

// an argument as printf takes it: MAME's formatter also accepts std::string, which printf does not
template <typename T>
T format_argument(T value)
{
	return value;
}

inline const char *format_argument(const std::string &value)
{
	return value.c_str();
}

// printf-style formatting into a stream, like MAME's util::stream_format
template <typename... Args>
void stream_format(std::ostream &stream, const char *format, Args... args)
{
	char buffer[256];
	std::snprintf(buffer, sizeof(buffer), format, format_argument(args)...);
	stream << buffer;
}

class disasm_interface
{
public:
	enum : u32
	{
		LENGTHMASK = 0x0000ffff,
		STEP_COND = 0x10000000,
		STEP_OVER = 0x20000000,
		STEP_OUT = 0x40000000,
		SUPPORTED = 0x80000000
	};

	// reads from a flat image; addresses wrap with a mask, like a mirrored bus
	class data_buffer
	{
	public:
		data_buffer(const u8 *data, u32 mask) : m_data(data), m_mask(mask) {}
		u8 r8(offs_t address) const { return m_data[address & m_mask]; }
		u16 r16(offs_t address) const { return u16(r8(address) | (r8(address + 1) << 8)); }

	private:
		const u8 *m_data;
		u32 m_mask;
	};

	virtual ~disasm_interface() = default;
	virtual u32 opcode_alignment() const = 0;
	virtual offs_t disassemble(std::ostream &stream, offs_t pc, const data_buffer &opcodes, const data_buffer &params) = 0;
};

// the K007232's sample dump (K007232_LOG_PCM), which stays off
struct wav_file {};
using wav_file_ptr = std::unique_ptr<wav_file>;
inline wav_file_ptr wav_open(const char *, int, int) { return nullptr; }
inline void wav_add_data_16(wav_file &, const s16 *, int) {}

} // namespace util

class machine_config {};
class validity_checker {};
class device_t;
using device_type = const char *;

// ---- callbacks ----

// a callback of a device (MAME's delegates): a member of an object, or any function the host sets
template <typename Signature> class device_delegate;
template <typename R, typename... A>
class device_delegate<R (A...)>
{
public:
	device_delegate() = default;
	template <typename T, std::enable_if_t<!std::is_invocable_v<T &, A...>, int> = 0>
	explicit device_delegate(T &owner) { (void)owner; }
	template <typename C, typename R2, typename... A2>
	device_delegate(C &object, R2 (C::*method)(A2...), const char *name)
		: m_function([&object, method](A... args) -> R { return R((object.*method)(args...)); }) { (void)name; }
	template <typename C, typename R2, typename... A2>
	device_delegate(R2 (C::*method)(A2...), const char *name, C *object)
		: m_function([object, method](A... args) -> R { return R((object->*method)(args...)); }) { (void)name; }

	bool isnull() const { return !m_function; }
	void resolve() {}
	R operator()(A... args) const { return m_function ? m_function(args...) : R(); }
	void set(std::function<R (A...)> function) { m_function = std::move(function); }

private:
	std::function<R (A...)> m_function;
};

using timer_expired_delegate = device_delegate<void (s32)>;
using read8sm_delegate = device_delegate<u8 (offs_t)>;

// an output line of a device: none is connected
class devcb_write_line
{
public:
	template <typename T> explicit devcb_write_line(T &) {}
	devcb_write_line &bind() { return *this; }
	void operator()(int) {}
	bool isunset() const { return true; }
	void resolve_safe() {}
};

// an 8-bit output of a device: the host may connect it (set)
class devcb_write8
{
public:
	template <typename T> explicit devcb_write8(T &) {}
	devcb_write8 &bind() { return *this; }
	void set(std::function<void (offs_t, u8)> function) { m_function = std::move(function); }
	void operator()(offs_t offset, u8 data, u8 mem_mask = 0xff) { (void)mem_mask; if (m_function) m_function(offset, data); }
	void operator()(u8 data) { if (m_function) m_function(0, data); }
	bool isunset() const { return !m_function; }

private:
	std::function<void (offs_t, u8)> m_function;
};

// ---- time ----

struct attotime
{
	double seconds = 0;
	static attotime from_hz(u32 hz) { return attotime{ hz ? 1.0 / hz : 0 }; }
	attotime operator*(u32 factor) const { return attotime{ seconds * factor }; }
	static const attotime zero;
	static const attotime never;
};
inline const attotime attotime::zero{ 0 };
inline const attotime attotime::never{ 1e30 };

// a timer of a device: the uPD7759's (its slave mode's data requests), which TMNT's stand-alone chip never needs
class emu_timer
{
public:
	void adjust(attotime start, s32 param = 0, attotime period = attotime::never) { (void)start; (void)param; (void)period; }
	bool enabled() const { return false; }
};

// what MAME's scheduler does when a device synchronizes: here at once, the host having brought the sound
// board up to the present
struct scheduler_stub
{
	void synchronize(timer_expired_delegate callback, s32 param = 0) { callback(param); }
};

struct running_machine_stub
{
	bool side_effects_disabled() const { return false; }
	scheduler_stub &scheduler() { return m_scheduler; }

private:
	scheduler_stub m_scheduler;
};

// ---- memory ----

// the ROM regions the host gives, by their device's tag
inline std::map<std::string, std::pair<u8 *, size_t>> &region_table()
{
	static std::map<std::string, std::pair<u8 *, size_t>> table;
	return table;
}

inline void set_region(const std::string &tag, u8 *base, size_t bytes)
{
	region_table()[tag] = { base, bytes };
}

class address_space_config
{
public:
	address_space_config(const char *name, endianness_t endian, int data_width, int address_width, int shift = 0)
	{
		(void)name; (void)endian; (void)data_width; (void)address_width; (void)shift;
	}
};

class address_space
{
public:
	std::function<u8(offs_t)> read;
	std::function<void(offs_t, u8)> write;

	template <typename Access> void cache(Access &target) { target.space = this; }
	template <typename Access> void specific(Access &target) { target.space = this; }
	void install_rom(offs_t start, offs_t end, u8 *base)
	{
		read = [start, end, base](offs_t address) { return address >= start && address <= end ? base[address - start] : u8(0); };
	}
	template <typename Delegate>
	void install_read_handler(offs_t start, offs_t end, Delegate handler)
	{
		read = [start, end, handler](offs_t address) { return address >= start && address <= end ? handler(address - start) : u8(0); };
	}
};

template <int Width, int AddressShift, int Unused, endianness_t Endian>
struct memory_access
{
	struct cache
	{
		address_space *space = nullptr;
		u8 read_byte(offs_t address) { return space->read ? space->read(address) : 0; }
		void write_byte(offs_t address, u8 data) { if (space->write) space->write(address, data); }
	};
	struct specific
	{
		address_space *space = nullptr;
		u8 read_byte(offs_t address) { return space->read(address); }
		void write_byte(offs_t address, u8 data) { space->write(address, data); }
		u8 read_interruptible(offs_t address) { return space->read(address); }
		void write_interruptible(offs_t address, u8 data) { space->write(address, data); }
	};
};

// an address map: the host decodes the Z80's itself
class address_map
{
public:
	struct entry
	{
		entry &mirror(offs_t) { return *this; }
		template <typename... T> entry &w(T &&...) { return *this; }
		template <typename... T> entry &r(T &&...) { return *this; }
		template <typename... T> entry &rw(T &&...) { return *this; }
	};
	entry &operator()(offs_t, offs_t) { return m_entry; }

private:
	entry m_entry;
};

// ---- devices ----

class device_state_entry
{
public:
	explicit device_state_entry(int index) : m_index(index) {}
	int index() const { return m_index; }

private:
	int m_index;
};

class device_t
{
public:
	device_t(const machine_config &config, device_type type, const char *tag, device_t *owner, u32 clock)
		: m_tag(tag), m_clock(clock)
	{
		(void)config; (void)type; (void)owner;
	}
	virtual ~device_t() = default;

	// what a device does not emulate perfectly (MAME flags the systems that use it)
	struct feature
	{
		enum type : u32 { NONE = 0, SOUND = 1 };
	};
	using feature_type = feature::type;

	u32 clock() const { return m_clock; }
	const char *tag() const { return m_tag; }
	running_machine_stub &machine() { return m_machine; }
	const running_machine_stub &machine() const { return m_machine; }
	template <typename... Args> void logerror(const char *, Args...) {}
	template <typename... T> void save_item(T &&...) {}
	template <typename... T> void save_pointer(T &&...) {}
	template <typename... T> emu_timer *timer_alloc(T &&...) { m_timers.emplace_back(); return &m_timers.back(); }

	// host API
	void start() { device_start(); }
	void reset() { device_reset(); }

protected:
	virtual void device_start() {}
	virtual void device_reset() {}
	virtual void device_clock_changed() {}
	virtual void device_validity_check(validity_checker &) const {}
	virtual void device_pre_save() {}
	virtual void device_post_load() {}

private:
	const char *m_tag;
	u32 m_clock;
	running_machine_stub m_machine;
	std::list<emu_timer> m_timers;
};

// a device's ROM region: the host's, by the device's tag (DEVICE_SELF) or the region's own
template <typename T>
class region_ptr_finder
{
public:
	region_ptr_finder(device_t &owner, const char *tag)
	{
		const auto found = region_table().find(tag[0] ? std::string(tag) : std::string(owner.tag()));
		if (found != region_table().end())
		{
			m_target = reinterpret_cast<T *>(found->second.first);
			m_length = found->second.second / sizeof(T);
		}
	}
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

class device_memory_interface
{
public:
	using space_config_vector = std::vector<std::pair<int, const address_space_config *>>;

	device_memory_interface(const machine_config &, device_t &) {}
	virtual ~device_memory_interface() = default;

	address_space &space(int index = AS_PROGRAM) { return m_spaces[index]; }
	bool has_space(int index) const { return index != AS_OPCODES; }
	bool has_configured_map(int index) const { (void)index; return false; }

protected:
	virtual space_config_vector memory_space_config() const = 0;

private:
	address_space m_spaces[4];
};

// a sound stream: the host asks a device for a number of samples, which it writes as MAME's floats
class sound_stream
{
public:
	using sample_t = float;

	int samples() const { return m_samples; }
	void put(int output, int index, sample_t value) { m_outputs[output][size_t(index)] = value; }
	void put_int(int output, int index, s32 value, s32 range) { put(output, index, sample_t(value) * (1.0f / sample_t(range))); }
	void put_int_clamp(int output, int index, s32 value, s32 range)
	{
		value = std::clamp(value, -range, range - 1);
		put_int(output, index, value, range);
	}
	void update() { if (m_before_change) m_before_change(); }
	void set_sample_rate(u32 rate) { m_rate = rate; }
	u32 sample_rate() const { return m_rate; }

	// host side
	std::function<void()> m_before_change;    // brings the stream up to the present before a change
	int m_samples = 0;
	std::vector<sample_t> m_outputs[2];
	u32 m_rate = 0;
};

class device_sound_interface
{
public:
	device_sound_interface(const machine_config &, device_t &) {}
	virtual ~device_sound_interface() = default;

	// host API: the next 'count' samples
	void generate(int count)
	{
		m_stream.m_samples = count;
		for (auto &output : m_stream.m_outputs)
			output.assign(size_t(count), 0.0f);
		sound_stream_update(m_stream);
	}
	sound_stream &stream() { return m_stream; }

protected:
	sound_stream *stream_alloc(int inputs, int outputs, u32 rate)
	{
		(void)inputs; (void)outputs;
		m_stream.m_rate = rate;
		return &m_stream;
	}
	virtual void sound_stream_update(sound_stream &stream) = 0;

private:
	sound_stream m_stream;
};

// the debugger is never on
struct debugger_stub
{
	void interrupt_hook(int, offs_t) {}
};

class device_execute_interface
{
public:
	struct owner_stub
	{
		debugger_stub *debug() { return nullptr; }
	};
	owner_stub &device() { return m_owner; }

private:
	owner_stub m_owner;
};

class cpu_device : public device_t, public device_memory_interface, public device_execute_interface
{
public:
	cpu_device(const machine_config &config, device_type type, const char *tag, device_t *owner, u32 clock)
		: device_t(config, type, tag, owner, clock), device_memory_interface(config, *this) {}

	// host API
	int run(int cycles)
	{
		*m_icountptr = cycles;
		execute_run();
		m_total += u64(cycles - *m_icountptr);
		return cycles - *m_icountptr;
	}
	int cycles_left() const { return *m_icountptr; }
	void set_input_line(int line, int state) { execute_set_input(line, state); }
	std::function<int()> irq_vector;            // what the interrupt cycle reads from the bus
	std::function<void(offs_t)> instruction_hook;    // before each instruction, with its address

protected:
	struct state_entry
	{
		state_entry &formatstr(const char *) { return *this; }
		state_entry &callimport() { return *this; }
		state_entry &callexport() { return *this; }
		state_entry &noshow() { return *this; }
		state_entry &mask(u64) { return *this; }
	};

	virtual bool cpu_is_interruptible() const { return false; }
	virtual u32 execute_min_cycles() const noexcept { return 1; }
	virtual u32 execute_max_cycles() const noexcept { return 1; }
	virtual u32 execute_default_irq_vector(int) const noexcept { return 0; }
	virtual bool execute_input_edge_triggered(int) const noexcept { return false; }
	virtual void execute_run() = 0;
	virtual void execute_set_input(int, int) {}
	virtual void state_import(const device_state_entry &) {}
	virtual void state_export(const device_state_entry &) {}
	virtual void state_string_export(const device_state_entry &, std::string &) const {}
	virtual std::unique_ptr<util::disasm_interface> create_disassembler() { return nullptr; }

	void set_icountptr(int &icount) { m_icountptr = &icount; }
	template <typename T> state_entry &state_add(int, const char *, T &) { return m_state_entry; }
	int standard_irq_callback(int, offs_t) { return irq_vector ? irq_vector() : 0xff; }
	u64 total_cycles() const { return m_total; }
	bool access_to_be_redone() { return false; }
	bool debugger_enabled() const { return false; }
	void debugger_instruction_hook(offs_t pc) { if (instruction_hook) instruction_hook(pc); }
	void debugger_wait_hook() {}

private:
	int m_dummy_icount = 0;
	int *m_icountptr = &m_dummy_icount;
	u64 m_total = 0;
	state_entry m_state_entry;
};
