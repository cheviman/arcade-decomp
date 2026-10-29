/**
 *    Name:        oracle_m68k_step.cpp
 *    Description: runs one 68000 instruction at a time on MAME's 68000 core, from the states a file gives, and
 *                 writes the state each one leaves and the memory accesses it made
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    Not part of the game. Usage: oracle_m68k_step <program.bin> <states.txt> <results.txt>
 *    The program image is the 68000's from address 0, as it sees it: the instruction each state runs is the one
 *    at its PC, with the words after it. Memory past the image holds a pattern of its own address (pattern()
 *    below; m68000/interpreter.py has the same), and every state starts from that memory again.
 *
 *    Each line of the states file: "PC SR IRQ D0 ... D7 A0 ... A6 USP SSP", hexadecimal. IRQ is the interrupt
 *    level held on the CPU's lines while the instruction runs (0 for none): MAME takes it after the
 *    instruction when the SR's mask lets it through, with the autovector of its level. Each line of the
 *    results: "PC SR D0 ... D7 A0 ... A6 USP SSP CYCLES | ACCESSES", where PC is the address of the next
 *    instruction the core starts (an exception's or an interrupt's handler, when the instruction took one),
 *    CYCLES the cycles it took up to there, and each access "R:ADDRESS:MASK:DATA" or "W:ADDRESS:MASK:DATA"
 *    (MAME's mem_mask, the 16-bit word on the bus) in the order the core made them. Only the data accesses
 *    are listed: the core's own instruction fetches, and the reads it makes through the program space (the
 *    PC-relative operands), are not; their values reach the registers. A state whose instruction does not
 *    reach the next one within its cycles (STOP) gives "STOPPED".
 */
#include "emu.h"
#include "m68000.h"

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
	address &= 0xffffff;
	const auto found = g_written.find(address);
	if (found != g_written.end())
		return found->second;
	return address < g_image.size() ? g_image[address] : pattern(address);
}

void log_access(char kind, u32 address, u16 mask, u16 data)
{
	char text[32];
	std::snprintf(text, sizeof(text), " %c:%06X:%04X:%04X", kind, address & 0xffffff, mask, data);
	g_accesses += text;
}

u16 bus_read(offs_t address, u16 mem_mask)
{
	address &= 0xfffffe;
	const u16 data = u16(byte_at(address) << 8 | byte_at(address + 1));
	if (g_logging && !g_reading_instructions)
		log_access('R', address, mem_mask, data);
	return data;
}

void bus_write(offs_t address, u16 data, u16 mem_mask)
{
	address &= 0xfffffe;
	if (g_logging)
		log_access('W', address, mem_mask, data);
	if (mem_mask & 0xff00)
		g_written[address] = u8(data >> 8);
	if (mem_mask & 0x00ff)
		g_written[address + 1] = u8(data);
}

// The interrupt acknowledge: every level takes its autovector, as on the Konami boards
u16 acknowledge(offs_t address, u16 mem_mask)
{
	(void)mem_mask;
	return u16(0x18 + ((address >> 1) & 7));
}

struct next_instruction {};

class stepping_cpu : public m68000_device
{
public:
	stepping_cpu() : m68000_device(m_config, "maincpu", nullptr, 8000000) {}

	// The state of a line of the states file, with the instruction at 'pc' fetched as the core prefetches it
	void load(u32 pc, u16 sr, const u32 *registers)
	{
		for (int index = 0; index < 17; index++)
			m_da[index] = registers[index];
		m_sr = sr & (SR_SR | SR_CCR);
		update_user_super();
		m_virq_state = 0;
		m_nmi_pending = 0;
		m_int_level = 0;
		m_int_next_state = 0;
		m_next_state = 0;
		m_post_run = 0;
		m_post_run_cycles = 0;
		m_count_before_instruction_step = 0;
		m_ipc = pc;
		m_pc = pc + 2;
		m_au = pc + 4;
		m_ir = m_ird = m_irdi = m_s_opcodes->read_word(pc);
		m_irc = m_dbin = m_s_opcodes->read_word(m_pc);
		set_ftu_const();
		m_inst_state = m_decode_table[m_ird];
		m_inst_substate = 0;
	}

	// The exceptions an instruction raises when it is decoded (illegal, privileged in user mode, line A and F)
	// run in the core as instructions of their own, at the instruction's own address
	bool raising(u32 pc) const
	{
		return (m_ipc & 0xffffff) == (pc & 0xffffff) && (m_inst_state == S_ILLEGAL || m_inst_state == S_PRIVILEDGE
			|| m_inst_state == S_LINEA || m_inst_state == S_LINEF);
	}

	u32 reg(int index) const { return m_da[index]; }
	u16 sr() const { return m_sr; }
	u32 ipc() const { return m_ipc; }
	int icount() const { return m_icount; }

private:
	static inline machine_config m_config;
};

} // namespace

int main(int argc, char **argv)
{
	if (argc != 4)
	{
		std::cerr << "Usage: oracle_m68k_step <program.bin> <states.txt> <results.txt>\n";
		return 1;
	}
	std::ifstream image(argv[1], std::ios::binary);
	g_image.assign(std::istreambuf_iterator<char>(image), std::istreambuf_iterator<char>());
	if (g_image.empty())
	{
		std::cerr << argv[1] << ": no program image\n";
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
	for (int space : { int(AS_PROGRAM), int(AS_OPCODES) })
	{
		cpu.space(space).read16 = bus_read;
		cpu.space(space).write16 = bus_write;
	}
	cpu.space(AS_OPCODES).instructions = true;
	cpu.space(m68000_base_device::AS_CPU_SPACE).read16 = acknowledge;
	cpu.space(m68000_base_device::AS_CPU_SPACE).write16 = bus_write;
	cpu.start();
	cpu.reset();

	int hooks = 0;
	u32 started = 0;
	cpu.instruction_hook = [&hooks, &cpu, &started](offs_t) {
		if (++hooks >= 2 && !(hooks == 2 && cpu.raising(started)))
			throw next_instruction();
	};

	constexpr int kBudget = 4000;    // cycles: the longest instruction and an exception take far fewer
	std::string line;
	while (std::getline(states, line))
	{
		if (line.empty())
			continue;
		std::istringstream fields(line);
		u32 values[20];
		for (u32 &value : values)
			fields >> std::hex >> value;
		if (!fields)
		{
			std::cerr << "A states line does not have 20 numbers: " << line << '\n';
			return 1;
		}
		const u32 pc = values[0], sr = values[1], irq = values[2];

		g_written.clear();
		g_accesses.clear();
		g_logging = false;
		cpu.load(pc, u16(sr), &values[3]);
		started = pc;
		if (irq)
			cpu.set_input_line(int(irq), ASSERT_LINE);
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
		const int cycles = kBudget - cpu.icount();
		if (irq)
			cpu.set_input_line(int(irq), CLEAR_LINE);
		if (stopped)
		{
			results << "STOPPED\n";
			continue;
		}
		char text[64];
		std::snprintf(text, sizeof(text), "%06X %04X", cpu.ipc() & 0xffffff, cpu.sr());
		results << text;
		for (int index = 0; index < 17; index++)
		{
			std::snprintf(text, sizeof(text), " %08X", cpu.reg(index));
			results << text;
		}
		results << ' ' << std::dec << cycles << " |" << g_accesses << '\n';
	}
	return 0;
}
