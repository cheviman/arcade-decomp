/**
 *    Name:        emu.h
 *    Description: minimal stand-in for MAME's emu.h, enough to build MAME's 68000 core and its disassembler
 *                 alone
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    Only what m68000.cpp, m68kdasm.cpp and the handlers m68000gen.py writes (m68000-sdf.cpp,
 *    m68000-sdp.cpp, m68000-decode.cpp) use. The bus is 16 bits wide and big endian: every access is a
 *    16-bit word at an even address with MAME's mem_mask (0xFF00 the even byte, 0x00FF the odd one), through
 *    function pointers the host program sets. Accesses take no wait states, and the autovector acknowledge
 *    takes no VPA synchronisation (MAME's m68000_device::vpa_sync), so cycle counts can differ from MAME's
 *    there. cpu_device adds a small public API (start, reset, run, registers).
 */
#pragma once

#include <algorithm>
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

#define ATTR_COLD
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

template <typename T>
constexpr T make_bitmask(unsigned int bits)
{
	return bits >= 8 * sizeof(T) ? T(~T(0)) : T((T(1) << bits) - 1);
}

namespace util {

// The low 'width' bits of 'value', sign-extended (MAME's coretmpl.h)
template <typename T, typename U>
constexpr std::make_signed_t<T> sext(T value, U width)
{
	const unsigned shift = 8 * sizeof(T) - unsigned(width);
	return std::make_signed_t<T>(value << shift) >> shift;
}

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

	// Big-endian reads from a flat image; addresses wrap with a mask (the 68000's 24 address lines)
	class data_buffer
	{
	public:
		data_buffer(const u8 *data, u32 size) : m_data(data), m_size(size) {}
		u8 r8(offs_t address) const { address &= 0xffffff; return address < m_size ? m_data[address] : 0xff; }
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

// What the core's address maps (its autovector map) are written with; nothing here builds a map
class address_map_entry
{
public:
	template <typename... Args> address_map_entry &before_time(Args &&...) { return *this; }
	template <typename... Args> address_map_entry &after_delay(Args &&...) { return *this; }
	template <typename... Args> address_map_entry &lr8(Args &&...) { return *this; }
};

class address_map
{
public:
	address_map_entry &operator()(offs_t start, offs_t end) { (void)start; (void)end; return m_entry; }

private:
	address_map_entry m_entry;
};

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

// A 16-bit big-endian bus as the CPU sees it; the host fills in the two access functions
class address_space
{
public:
	u16 (*read16)(offs_t address, u16 mem_mask) = nullptr;
	void (*write16)(offs_t address, u16 data, u16 mem_mask) = nullptr;
	bool instructions = false;    // the opcode space: what the CPU reads as its instructions

	u16 read_word(offs_t address) { return read16(address & ~1u, 0xffff); }

	template <typename Specific>
	void specific(Specific &target) { target.space = this; }
};

// true while the CPU reads its instruction stream (the opcode space), false for its data reads
inline bool g_reading_instructions = false;

template <int Width, int AddressShift, int Unused, endianness_t Endian>
struct memory_access
{
	struct specific
	{
		address_space *space = nullptr;

		u16 read_interruptible(offs_t address, u16 mem_mask = 0xffff)
		{
			g_reading_instructions = space->instructions;
			const u16 value = space->read16(address & 0xffffff, mem_mask);
			g_reading_instructions = false;
			return value;
		}
		void write_interruptible(offs_t address, u16 data, u16 mem_mask = 0xffff)
		{
			space->write16(address & 0xffffff, data, mem_mask);
		}
	};
};

// A callback the driver may set (MAME's delegates and devcb): none is set here
template <typename... Args>
class callback
{
public:
	template <typename Owner> explicit callback(Owner &owner) { (void)owner; }
	template <typename... Bound> void set(Bound &&...) {}
	void resolve() {}
	bool isnull() const { return !m_function; }
	void operator()(Args... args) { if (m_function) m_function(args...); }
	callback &bind() { return *this; }

private:
	std::function<void(Args...)> m_function;
};

using write32sm_delegate = callback<offs_t, u32>;
using write_line_delegate = callback<int>;
using write8sm_delegate = callback<offs_t, u8>;
using devcb_write_line = callback<int>;

class device_state_entry
{
public:
	explicit device_state_entry(int index) : m_index(index) {}
	int index() const { return m_index; }

private:
	int m_index;
};

class running_machine
{
public:
	struct side_effects_disabler {};
	bool side_effects_disabled() const { return false; }
	side_effects_disabler disable_side_effects() { return {}; }
};

class device_t
{
public:
	device_t(const machine_config &config, device_type type, const char *tag, device_t *owner, u32 clock)
	{
		(void)config; (void)type; (void)tag; (void)owner; (void)clock;
	}
	virtual ~device_t() = default;

	// Messages from the core about odd situations; the host may print them
	std::function<void(const std::string &)> log_handler;

	template <typename... Args>
	void logerror(const char *format, Args... args)
	{
		if (log_handler)
			log_handler(string_format(format, args...));
	}

	running_machine &machine() { return m_machine; }
	const running_machine &machine() const { return m_machine; }

protected:
	virtual void device_start() {}
	virtual void device_reset() {}

private:
	running_machine m_machine;
};

class device_memory_interface
{
public:
	using space_config_vector = std::vector<std::pair<int, const address_space_config *>>;

	virtual ~device_memory_interface() = default;
	virtual space_config_vector memory_space_config() const = 0;
	virtual space_config_vector memory_logical_space_config() const { return memory_space_config(); }
	virtual bool memory_translate(int spacenum, int intention, offs_t &address, address_space *&target_space)
	{
		(void)spacenum; (void)intention; (void)address; (void)target_space;
		return true;
	}
};

class cpu_device : public device_t, public device_memory_interface
{
public:
	cpu_device(const machine_config &config, device_type type, const char *tag, device_t *owner, u32 clock)
		: device_t(config, type, tag, owner, clock) {}

	// Host API
	std::function<void(offs_t)> instruction_hook;
	void start() { device_start(); }
	void reset() { device_reset(); }
	int run(int cycles) { *m_icountptr = cycles; execute_run(); return cycles - *m_icountptr; }
	int cycles_left() const { return *m_icountptr; }
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
	};

	virtual u32 execute_min_cycles() const noexcept { return 1; }
	virtual u32 execute_max_cycles() const noexcept { return 1; }
	virtual bool execute_input_edge_triggered(int inputnum) const noexcept { (void)inputnum; return false; }
	virtual bool cpu_is_interruptible() const { return false; }
	virtual void execute_run() = 0;
	virtual void execute_set_input(int inputnum, int state) { (void)inputnum; (void)state; }
	virtual void state_import(const device_state_entry &entry) { (void)entry; }
	virtual void state_export(const device_state_entry &entry) { (void)entry; }
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
	void standard_irq_callback(int line, offs_t pc) { (void)line; (void)pc; }
	bool debugger_enabled() const { return bool(instruction_hook); }
	void debugger_instruction_hook(offs_t pc) { if (instruction_hook) instruction_hook(pc); }
	void debugger_exception_hook(int exception) { (void)exception; }
	void debugger_wait_hook() {}
	bool access_to_be_redone() { return false; }

private:
	address_space m_spaces[AS_SPACES];
	int m_dummy_icount = 0;
	int *m_icountptr = &m_dummy_icount;
	std::map<int, std::function<u32()>> m_state;
	state_entry m_state_entry;
};
