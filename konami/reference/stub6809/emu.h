/**
 *    Name:        emu.h
 *    Description: minimal stand-in for MAME's emu.h, enough to build MAME's 6809-family cores (the 6809 base and
 *                 Konami's CPU, the 052001 and the 053248) and their disassembler alone
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    05-11-2026
 *    Author:      Sebastian Toyos
 *
 *    Only what m6809.cpp, konami.cpp, 6x09dasm.cpp and the steps m6809make.py writes (m6809.hxx, konami.hxx)
 *    use. The bus is 8 bits wide over 16 address lines: every access is one byte, through two function pointers
 *    the host program sets (cpu_device::bus), and each read says what the CPU reads it as: an opcode, an
 *    opcode's argument or data (the bytes of the program the code reads as data). Konami's CPU drives its
 *    output lines (SETLN) through konami_cpu_device::line(), which the host sets. cpu_device adds a small
 *    public API (start, reset, run, input lines, registers, an instruction hook).
 */
#pragma once

#include <algorithm>
#include <array>
#include <bit>
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <map>
#include <memory>
#include <ostream>
#include <set>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

using u8 = uint8_t;
using u16 = uint16_t;
using u32 = uint32_t;
using u64 = uint64_t;
using s8 = int8_t;
using s16 = int16_t;
using s32 = int32_t;
using s64 = int64_t;
using offs_t = uint32_t;

// The host is little endian (the cores' register pairs are laid out for it: M6809Q, PAIR16)
static_assert(std::endian::native == std::endian::little, "the stand-in assumes a little-endian host");
#define LSB_FIRST 1

#define ATTR_COLD
#define ATTR_FORCE_INLINE
#define EXPECTED(x) (x)
#define UNEXPECTED(x) (x)
#define NAME(x) x, #x
#define FUNC(x) &x, #x
#define DECLARE_DEVICE_TYPE(Type, Class) extern const device_type Type;
#define DEFINE_DEVICE_TYPE(Type, Class, ShortName, FullName) const device_type Type = ShortName;
#define BIT(x, n) (((x) >> (n)) & 1)

enum { CLEAR_LINE = 0, ASSERT_LINE = 1 };
enum { INPUT_LINE_NMI = 32 };
enum { AS_PROGRAM = 0, AS_DATA = 1, AS_IO = 2, AS_OPCODES = 3, AS_SPACES = 8 };
enum { STATE_GENPC = -1, STATE_GENPCBASE = -2, STATE_GENFLAGS = -3 };
enum endianness_t { ENDIANNESS_LITTLE, ENDIANNESS_BIG };

// MAME's register pairs (emucore.h), for a little-endian host
union PAIR16
{
	struct { u8 l, h; } b;
	struct { s8 l, h; } sb;
	u16 w;
	s16 sw;
};

union PAIR
{
	struct { u8 l, h, h2, h3; } b;
	struct { u16 l, h; } w;
	struct { s8 l, h, h2, h3; } sb;
	struct { s16 l, h; } sw;
	u32 d;
	s32 sd;
};

// The CPU core calls this on conditions it cannot continue from; the host catches it
[[noreturn]] inline void fatalerror(const char *format, ...)
{
	char message[512];
	va_list arguments;
	va_start(arguments, format);
	std::vsnprintf(message, sizeof(message), format, arguments);
	va_end(arguments);
	throw std::runtime_error(message);
}

namespace util {

// An argument as printf takes it: MAME's formatter also accepts std::string, which printf does not
template <typename T>
T format_argument(T value)
{
	return value;
}

inline const char *format_argument(const std::string &value)
{
	return value.c_str();
}

template <typename... Args>
std::string string_format(const char *format, Args... args)
{
	char buffer[512];
	std::snprintf(buffer, sizeof(buffer), format, format_argument(args)...);
	return buffer;
}

template <typename... Args>
void stream_format(std::ostream &stream, const char *format, Args... args)
{
	stream << string_format(format, args...);
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

	// Big-endian reads from the CPU's 64 KB as an image; addresses wrap at 16 bits, as the CPU's do, and a place
	// past the image's end reads FF
	class data_buffer
	{
	public:
		data_buffer(const u8 *data, u32 size) : m_data(data), m_size(size) {}
		u8 r8(offs_t address) const { address &= 0xffff; return address < m_size ? m_data[address] : 0xff; }
		u16 r16(offs_t address) const { return u16((r8(address) << 8) | r8(address + 1)); }
		u32 r32(offs_t address) const { return (u32(r16(address)) << 16) | r16(address + 2); }

	private:
		const u8 *m_data;
		u32 m_size;
	};

	virtual ~disasm_interface() = default;
	virtual u32 opcode_alignment() const = 0;
	virtual offs_t disassemble(std::ostream &stream, offs_t pc, const data_buffer &opcodes, const data_buffer &params) = 0;
};

} // namespace util

using util::string_format;

class machine_config {};
class device_t;
class address_map;
using device_type = const char *;

class address_map_constructor
{
public:
	address_map_constructor() = default;
	template <typename... Args> address_map_constructor(Args &&...) {}
};

class address_space_config
{
public:
	address_space_config(const char *name, endianness_t endian, int data_width, int address_width, int shift = 0,
		address_map_constructor map = address_map_constructor())
		: m_addr_width(address_width)
	{
		(void)name; (void)endian; (void)data_width; (void)shift; (void)map;
	}

	int m_addr_width;
};

// What the CPU reads a byte as
enum bus_access : int
{
	BUS_DATA = 0,       // an operand, a stack pull, a vector: data
	BUS_OPCODE = 1,     // the first byte of an instruction
	BUS_ARGUMENT = 2    // the rest of an instruction (and the dummy reads the core makes on the instruction stream)
};

// The CPU's bus: 64 KB, 8 bits wide; the host fills in the two functions
struct bus8
{
	u8 (*read)(offs_t address, int access) = nullptr;
	void (*write)(offs_t address, u8 data) = nullptr;
};

template <int Width, int AddressShift, int Unused, endianness_t Endian>
struct memory_access
{
	// The core reads its instructions through caches (the opcode space's for the opcodes, the program space's for
	// their arguments) and its data through the program space's specific
	struct cache
	{
		bus8 *bus = nullptr;
		int access = BUS_ARGUMENT;
		u8 read_byte(offs_t address) { return bus->read(address & 0xffff, access); }
	};

	struct specific
	{
		bus8 *bus = nullptr;
		u8 read_byte(offs_t address) { return bus->read(address & 0xffff, BUS_DATA); }
		void write_byte(offs_t address, u8 data) { bus->write(address & 0xffff, data); }
	};
};

class address_space
{
public:
	bus8 *bus = nullptr;
	int cache_access = BUS_ARGUMENT;   // what a cache on this space reads: opcodes (AS_OPCODES) or their arguments

	template <typename Cache> void cache(Cache &target) { target.bus = bus; target.access = cache_access; }
	template <typename Specific> void specific(Specific &target) { target.bus = bus; }
};

// What a device's configuration accessor hands out (konami_cpu_device::line() returns it by value, as 'auto'):
// setting it sets the device's own callback, as MAME's devcb binder does
template <typename Callback, typename Function>
class callback_binder
{
public:
	explicit callback_binder(Callback &target) : m_target(target) {}
	void set(Function function) { m_target.set(std::move(function)); }

private:
	Callback &m_target;
};

// A callback the host may set (MAME's devcb): one that returns nothing, and one that reads a value
template <typename... Args>
class callback
{
public:
	template <typename Owner> explicit callback(Owner &owner) { (void)owner; }
	callback_binder<callback, std::function<void(Args...)>> bind() { return callback_binder<callback, std::function<void(Args...)>>(*this); }
	void set(std::function<void(Args...)> function) { m_function = std::move(function); }
	bool isunset() const { return !m_function; }
	void operator()(Args... args) { if (m_function) m_function(args...); }

private:
	std::function<void(Args...)> m_function;
};

template <typename Result, typename... Args>
class read_callback
{
public:
	template <typename Owner> read_callback(Owner &owner, Result unset_value) : m_unset(unset_value) { (void)owner; }
	callback_binder<read_callback, std::function<Result(Args...)>> bind()
	{
		return callback_binder<read_callback, std::function<Result(Args...)>>(*this);
	}
	void set(std::function<Result(Args...)> function) { m_function = std::move(function); }
	bool isunset() const { return !m_function; }
	Result operator()(Args... args) { return m_function ? m_function(args...) : m_unset; }

private:
	std::function<Result(Args...)> m_function;
	Result m_unset;
};

using devcb_write8 = callback<offs_t, u8>;
using devcb_write_line = callback<int>;
using devcb_read8 = read_callback<u8, offs_t>;

class device_state_entry
{
public:
	explicit device_state_entry(int index) : m_index(index) {}
	int index() const { return m_index; }

private:
	int m_index;
};

struct attotime {};

class running_machine
{
public:
	std::string describe_context() const { return "konami_cpu"; }
	attotime time() const { return {}; }
};

class device_t
{
public:
	device_t(const machine_config &config, device_type type, const char *tag, device_t *owner, u32 clock)
	{
		(void)config; (void)type; (void)tag; (void)owner; (void)clock;
	}
	virtual ~device_t() = default;

	// Messages from the core about odd situations (an illegal opcode); the host may print them
	std::function<void(const std::string &)> log_handler;

	template <typename... Args>
	void logerror(const char *format, Args... args)
	{
		if (log_handler)
			log_handler(string_format(format, args...));
	}

	running_machine &machine() { return m_machine; }
	const running_machine &machine() const { return m_machine; }
	u64 attotime_to_clocks(const attotime &) const { return 0; }

protected:
	virtual void device_start() {}
	virtual void device_reset() {}
	virtual void device_pre_save() {}
	virtual void device_post_load() {}

private:
	running_machine m_machine;
};

class device_memory_interface
{
public:
	using space_config_vector = std::vector<std::pair<int, const address_space_config *>>;

	virtual ~device_memory_interface() = default;
	virtual space_config_vector memory_space_config() const = 0;
};

class cpu_device : public device_t, public device_memory_interface
{
public:
	cpu_device(const machine_config &config, device_type type, const char *tag, device_t *owner, u32 clock)
		: device_t(config, type, tag, owner, clock)
	{
		for (int index = 0; index < AS_SPACES; index++)
		{
			m_spaces[index].bus = &bus;
			m_spaces[index].cache_access = index == AS_OPCODES ? BUS_OPCODE : BUS_ARGUMENT;
		}
	}

	// Host API
	bus8 bus;
	std::function<void(offs_t)> instruction_hook;         // before each instruction, with its address
	std::function<void(int)> irq_acknowledge;             // when the CPU takes an interrupt, with its line
	void start() { device_start(); }
	void reset() { device_reset(); }
	// runs at least the given cycles (whole instructions); returns the cycles it ran
	int run(int cycles) { *m_icountptr = cycles; execute_run(); return cycles - *m_icountptr; }
	int cycles_left() const { return *m_icountptr; }    // while it runs: what is left of the run's cycles
	void set_input_line(int line, int state) { execute_set_input(line, state); }
	address_space &space(int index = AS_PROGRAM) { return m_spaces[index]; }
	u32 state_value(int index)
	{
		auto found = m_state.find(index);
		return found != m_state.end() ? found->second() : 0;
	}

protected:
	struct state_entry
	{
		state_entry &formatstr(const char *) { return *this; }
		state_entry &callimport() { return *this; }
		state_entry &callexport() { return *this; }
		state_entry &noshow() { return *this; }
		state_entry &mask(u64) { return *this; }
	};

	virtual u32 execute_min_cycles() const noexcept { return 1; }
	virtual u32 execute_max_cycles() const noexcept { return 1; }
	virtual bool execute_input_edge_triggered(int inputnum) const noexcept { (void)inputnum; return false; }
	virtual u64 execute_clocks_to_cycles(u64 clocks) const noexcept { return clocks; }
	virtual u64 execute_cycles_to_clocks(u64 cycles) const noexcept { return cycles; }
	virtual void execute_run() = 0;
	virtual void execute_set_input(int inputnum, int state) { (void)inputnum; (void)state; }
	virtual void state_import(const device_state_entry &entry) { (void)entry; }
	virtual void state_string_export(const device_state_entry &entry, std::string &text) const { (void)entry; (void)text; }
	virtual std::unique_ptr<util::disasm_interface> create_disassembler() { return nullptr; }

	bool has_configured_map(int index) const { (void)index; return false; }
	bool has_space(int index) const { return index == AS_PROGRAM || index == AS_OPCODES; }
	void set_icountptr(int &icount) { m_icountptr = &icount; }
	template <typename T> void save_item(T &item, const char *name) { (void)item; (void)name; }
	template <typename T>
	state_entry &state_add(int index, const char *name, T &value)
	{
		(void)name;
		m_state[index] = [&value] { return u32(value); };
		return m_state_entry;
	}
	void standard_irq_callback(int line, offs_t pc) { (void)pc; if (irq_acknowledge) irq_acknowledge(line); }
	void debugger_instruction_hook(offs_t pc) { if (instruction_hook) instruction_hook(pc); }
	void debugger_exception_hook(int exception) { (void)exception; }
	void debugger_wait_hook() {}

private:
	address_space m_spaces[AS_SPACES];
	int m_dummy_icount = 0;
	int *m_icountptr = &m_dummy_icount;
	std::map<int, std::function<u32()>> m_state;
	state_entry m_state_entry;
};
