/**
 *    Name:        oracle_konami_step.cpp
 *    Description: runs one Konami CPU instruction at a time on MAME's core (the 052001 and the 053248), from the
 *                 states a file gives, and writes the state each one leaves and the data accesses it made
 *    Version:     0.1.0
 *    Created:     06-11-2026
 *    Modified:    06-11-2026
 *    Author:      Sebastian Toyos
 *
 *    Not part of the game. Usage: oracle_konami_step <image.bin> <states.txt> <results.txt>
 *    The image is the CPU's 64 KB as it sees them from address 0: the instruction each state runs is the one at its
 *    PC. Memory past the image holds a pattern of its own address (pattern() below; konami_cpu/interpreter.py has
 *    the same), and every state starts from that memory again.
 *
 *    Each line of the states file: "PC CC A B DP X Y U S IRQ FIRQ", hexadecimal. IRQ and FIRQ are the lines held
 *    once the instruction has started (1 asserted): the core takes an interrupt they hold at its next MAIN, after
 *    the instruction, when the CC lets it through. Each line of the results: "PC CC A B DP X Y U S CYCLES |
 *    ACCESSES", where PC is the address of the next instruction the core starts (an interrupt's handler, when it
 *    took one), CYCLES the cycles it took up to there, and each access "R:AAAA:DD" or "W:AAAA:DD" (the data bus,
 *    in the order the core made them) or "L:00:DD" (SETLN's output lines). The core's reads through its opcode space
 *    (the instruction's bytes, its dummy reads of the stream and of FFFF) are not listed.
 */
#include "emu.h"
#include "konami.h"

#include <cinttypes>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

std::vector<u8> g_image;
std::unordered_map<u32, u8> g_written;    // what the instruction wrote, over the image and the pattern
std::string g_accesses;
bool g_logging = false;

// What memory holds where nothing wrote: the image, then a pattern of the address
u8 pattern(u32 address)
{
	return u8((address * 2654435761u) >> 13);
}

u8 byte_at(u32 address)
{
	address &= 0xffff;
	const auto found = g_written.find(address);
	if (found != g_written.end())
		return found->second;
	return address < g_image.size() ? g_image[address] : pattern(address);
}

void log_access(char kind, u32 address, u8 data)
{
	char text[32];
	std::snprintf(text, sizeof(text), " %c:%04X:%02X", kind, address & 0xffff, data);
	g_accesses += text;
}

u8 bus_read(offs_t address, int access)
{
	const u8 data = byte_at(address);
	if (g_logging && access == BUS_DATA)
		log_access('R', address, data);
	return data;
}

void bus_write(offs_t address, u8 data)
{
	if (g_logging)
		log_access('W', address, data);
	g_written[address & 0xffff] = data;
}

struct next_instruction {};

class stepping_cpu : public konami_cpu_device
{
public:
	stepping_cpu() : konami_cpu_device(m_config, "maincpu", nullptr, 12000000) {}

	// The state of a line of the states file, the core at MAIN, its lines clear
	void load(const u32 *values)
	{
		m_pc.w = u16(values[0]);
		m_cc = u8(values[1]);
		m_q.r.a = u8(values[2]);
		m_q.r.b = u8(values[3]);
		m_dp = u8(values[4]);
		m_x.w = u16(values[5]);
		m_y.w = u16(values[6]);
		m_u.w = u16(values[7]);
		m_s.w = u16(values[8]);
		m_nmi_asserted = false;
		set_input_line(KONAMI_IRQ_LINE, CLEAR_LINE);
		set_input_line(KONAMI_FIRQ_LINE, CLEAR_LINE);
		reset_state();
		pop_state();        // (reset_state() leaves the vector's read next: popped, the core is at MAIN)
	}

	u16 pc() const { return m_pc.w; }
	u8 cc() const { return m_cc; }
	u8 a() const { return m_q.r.a; }
	u8 b() const { return m_q.r.b; }
	u8 dp() const { return m_dp; }
	u16 x() const { return m_x.w; }
	u16 y() const { return m_y.w; }
	u16 u() const { return m_u.w; }
	u16 s() const { return m_s.w; }

private:
	static inline machine_config m_config;
};

} // namespace

int main(int argc, char **argv)
{
	if (argc != 4)
	{
		std::cerr << "Usage: oracle_konami_step <image.bin> <states.txt> <results.txt>\n";
		return 1;
	}
	std::ifstream image(argv[1], std::ios::binary);
	g_image.assign(std::istreambuf_iterator<char>(image), std::istreambuf_iterator<char>());
	if (g_image.empty())
	{
		std::cerr << argv[1] << ": no image\n";
		return 1;
	}
	std::ifstream states(argv[2]);
	if (!states)
	{
		std::cerr << argv[2] << ": cannot be read\n";
		return 1;
	}
	std::ofstream results(argv[3]);

	stepping_cpu cpu;
	cpu.bus.read = bus_read;
	cpu.bus.write = bus_write;
	cpu.line().set([](offs_t, u8 data) { if (g_logging) log_access('L', 0, data); });
	cpu.start();
	cpu.reset();

	int hooks = 0;
	bool irq = false, firq = false;
	cpu.instruction_hook = [&](offs_t) {
		if (++hooks == 1)
		{
			// the instruction has started: the lines the state holds from now on
			if (irq)
				cpu.set_input_line(KONAMI_IRQ_LINE, ASSERT_LINE);
			if (firq)
				cpu.set_input_line(KONAMI_FIRQ_LINE, ASSERT_LINE);
			return;
		}
		throw next_instruction();
	};

	constexpr int kBudget = 2000000;    // cycles: BMOVE of 64 KB is the longest
	std::string line;
	while (std::getline(states, line))
	{
		if (line.empty())
			continue;
		std::istringstream fields(line);
		u32 values[11];
		for (u32 &value : values)
			fields >> std::hex >> value;
		if (!fields)
		{
			std::cerr << "A states line does not have 11 numbers: " << line << '\n';
			return 1;
		}
		g_written.clear();
		g_accesses.clear();
		cpu.load(values);
		irq = values[9] != 0;
		firq = values[10] != 0;
		hooks = 0;
		g_logging = true;
		bool stopped = true;
		try
		{
			cpu.run(kBudget);
		}
		catch (const next_instruction &)
		{
			stopped = false;
		}
		g_logging = false;
		const int cycles = kBudget - cpu.cycles_left();
		if (stopped)
		{
			results << "STOPPED\n";
			continue;
		}
		char text[96];
		std::snprintf(text, sizeof(text), "%04X %02X %02X %02X %02X %04X %04X %04X %04X %d |", cpu.pc(), cpu.cc(),
			cpu.a(), cpu.b(), cpu.dp(), cpu.x(), cpu.y(), cpu.u(), cpu.s(), cycles);
		results << text << g_accesses << '\n';
	}
	return 0;
}
