/**
 *    Name:        kref.cpp
 *    Description: development reference: runs the original 68000 program on MAME's 68000 core against the
 *                 runtime's model of a Konami board (MAME's tmnt.cpp family), to find code and record state
 *                 per frame
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    Not part of the game. Usage:
 *      kref [--frames N] [--hashes hashes.txt] [--trace executed.txt] [--calls called.txt]
 *           [--text FRAME ...] [--dump FRAME ... --dump-dir DIR] [--video-hashes hashes.txt]
 *           [--video-dump FRAME ... --video-dir DIR] [--service] [--rom FOLDER]
 *
 *    It runs the original program, so it reads the player's ROM set when it starts, as MAME does: the set
 *    in --rom's folder or .zip (rom/ by default), into the board's memory (game/data.h,
 *    runtime/src/rom_set.c).
 *
 *    Time keeps MAME's pace: the 68000 runs its cycles a line at a time (512 of them, 64 us), and a frame
 *    ends when VBLANK begins, at line 240, where the board takes its events (the sprite DMA, IRQ5) and the
 *    frame's state hash is taken. The sound board is not run yet: the program sees its latch as it leaves it.
 *
 *    --hashes writes "FRAME HASH" per frame (tmnt_state_hash); --trace and --calls the addresses executed
 *    and the ones reached by jsr or bsr, one per line; --text prints the fixed layer's tiles of those frames
 *    as text (the program's own character codes, '@' a space); --dump writes the board's memories at the end
 *    of those frames, a file each; --service turns the test mode's DIP switch on.
 *
 *    --video-hashes and --video-dump draw the frames with MAME's K052109 and K051960 (video/kvideo.cpp): the
 *    hash of every frame's pixels, and frames as BMP (DIR/frame_N.bmp), 320x224 from line 16. The video
 *    takes the board's memories when VBLANK ends (line 16, the scroll) and when it starts (the frame).
 */
#include "emu.h"
#include "m68000.h"

#include "game/data.h"
#include "game/extract.h"
#include "kvideo.h"
#include "tmnt/machine.h"

#include <cinttypes>
#include <fstream>
#include <iostream>
#include <set>
#include <string>
#include <vector>

namespace {

class reference_cpu : public m68000_device
{
public:
	reference_cpu() : m68000_device(m_config, "maincpu", nullptr, TMNT_CPU_CLOCK) {}
	u32 pc() const { return m_ipc; }

private:
	static inline machine_config m_config;
};

reference_cpu *g_cpu = nullptr;
int g_irq_level = 0;
std::vector<uint8_t> g_executed(1 << 21);    // one bit per address of the 24-bit bus
std::vector<uint8_t> g_called(1 << 21);
uint32_t g_previous_pc = 0xFFFFFFFF;
uint64_t g_instructions = 0;
uint64_t g_irq_frames = 0;

// The board's interrupt line as the 68000 sees it, after anything that can change it
void sync_irq()
{
	const int level = tmnt_irq_level();
	if (level == g_irq_level)
		return;
	if (g_irq_level)
		g_cpu->set_input_line(g_irq_level, CLEAR_LINE);
	if (level)
		g_cpu->set_input_line(level, ASSERT_LINE);
	g_irq_level = level;
}

u16 bus_read(offs_t address, u16 mem_mask)
{
	return tmnt_read16(address, mem_mask);
}

void bus_write(offs_t address, u16 data, u16 mem_mask)
{
	tmnt_write16(address, data, mem_mask);
	sync_irq();
}

// The interrupt acknowledge: every level takes its autovector (MAME's default map for the 68000)
u16 acknowledge(offs_t address, u16 mem_mask)
{
	(void)mem_mask;
	return u16(0x18 + ((address >> 1) & 7));
}

void mark(std::vector<uint8_t> &bits, uint32_t address)
{
	address &= 0xFFFFFF;
	bits[address >> 3] |= uint8_t(1u << (address & 7));
}

bool is_call(uint32_t pc)
{
	if (pc + 1 >= GAME_PROGRAM_SIZE)
		return false;
	const uint16_t opcode = uint16_t(game_program[pc] << 8 | game_program[pc + 1]);
	return (opcode & 0xFFC0) == 0x4E80 || (opcode & 0xFF00) == 0x6100;    // jsr, bsr
}

void on_instruction(offs_t pc)
{
	g_instructions++;
	mark(g_executed, pc);
	if (g_previous_pc != 0xFFFFFFFF && is_call(g_previous_pc))
		mark(g_called, pc);
	g_previous_pc = pc;
}

void write_addresses(const std::string &path, const std::vector<uint8_t> &bits, const char *title)
{
	std::ofstream out(path);
	out << "# " << title << ", one per line\n";
	for (uint32_t address = 0; address < (1u << 24); address++)
		if (bits[address >> 3] & (1u << (address & 7)))
		{
			char line[16];
			std::snprintf(line, sizeof(line), "%06X\n", address);
			out << line;
		}
}

// The 68000 runs until the board's clock reaches 'cycle', a line at a time at most
void run_until(reference_cpu &cpu, uint64_t cycle)
{
	while (tmnt_board.cycles < cycle)
	{
		const uint64_t slice = std::min<uint64_t>(cycle - tmnt_board.cycles, TMNT_LINE_CYCLES);
		tmnt_board.cycles += uint64_t(cpu.run(int(slice)));
	}
}

// The last frame the reference video drew, as a 24-bit BMP
void write_bmp(const std::string &path)
{
	const uint32_t *pixels = kv_frame();
	const int row = (KV_WIDTH * 3 + 3) & ~3;
	const uint32_t size = 54 + uint32_t(row) * KV_HEIGHT;
	std::vector<uint8_t> file(size, 0);
	auto put32 = [&](size_t at, uint32_t value) { for (int byte = 0; byte < 4; byte++) file[at + byte] = uint8_t(value >> (8 * byte)); };
	file[0] = 'B';
	file[1] = 'M';
	put32(2, size);
	put32(10, 54);
	put32(14, 40);
	put32(18, uint32_t(KV_WIDTH));
	put32(22, uint32_t(KV_HEIGHT));
	file[26] = 1;
	file[28] = 24;
	for (int y = 0; y < KV_HEIGHT; y++)
		for (int x = 0; x < KV_WIDTH; x++)
		{
			const uint32_t pixel = pixels[y * KV_WIDTH + x];
			uint8_t *out = &file[54 + size_t(KV_HEIGHT - 1 - y) * row + size_t(x) * 3];
			out[0] = uint8_t(pixel);
			out[1] = uint8_t(pixel >> 8);
			out[2] = uint8_t(pixel >> 16);
		}
	std::ofstream out(path, std::ios::binary);
	out.write(reinterpret_cast<const char *>(file.data()), std::streamsize(file.size()));
}

// The board's memories at the end of a frame, a file each: DIR/FRAME_work_ram.bin...
void dump_frame(const std::string &folder, uint64_t frame)
{
	const struct { const char *name; const uint8_t *data; size_t size; } memories[] = {
		{ "work_ram", tmnt_board.work_ram, sizeof(tmnt_board.work_ram) },
		{ "palette", tmnt_board.palette, sizeof(tmnt_board.palette) },
		{ "tile_ram", tmnt_board.tile_ram, sizeof(tmnt_board.tile_ram) },
		{ "sprite_ram", tmnt_board.sprite_ram, sizeof(tmnt_board.sprite_ram) },
	};
	for (const auto &memory : memories)
	{
		std::ofstream out(folder + "/" + std::to_string(frame) + "_" + memory.name + ".bin", std::ios::binary);
		out.write(reinterpret_cast<const char *>(memory.data), std::streamsize(memory.size));
	}
}

// The fixed layer (the K052109's layer F: its codes at 2000-27FF) as text, 64 columns by 32 rows. The
// program's text is ASCII-like with '@' for a space, and its font starts at tile 0 with '0': a tile is the
// character 0x30 above it
void print_text(uint64_t frame)
{
	std::cout << "frame " << frame << ", fixed layer:\n";
	for (int row = 0; row < 32; row++)
	{
		std::string line;
		for (int column = 0; column < 64; column++)
		{
			const int character = tmnt_board.tile_ram[0x2000 + row * 64 + column] + 0x30;
			line += character == '@' ? ' ' : (character < 0x7F ? char(character) : '.');
		}
		while (!line.empty() && (line.back() == ' ' || line.back() == '.'))
			line.pop_back();
		std::cout << "  |" << line << '\n';
	}
}

} // namespace

int main(int argc, char **argv)
{
	const char *usage = "Usage: kref [--frames N] [--hashes FILE] [--trace FILE] [--calls FILE] [--text FRAME ...]"
		" [--dump FRAME ... --dump-dir DIR] [--video-hashes FILE] [--video-dump FRAME ... --video-dir DIR]"
		" [--service] [--rom FOLDER]\n";
	std::string rom_folder = "rom";
	std::string hashes_path, trace_path, calls_path, dump_folder = ".", video_hashes_path, video_folder = ".";
	std::set<uint64_t> text_frames, dump_frames, video_dump_frames;
	uint64_t frames = 600;
	bool service = false;

	for (int index = 1; index < argc; index++)
	{
		const std::string argument = argv[index];
		const bool has_value = index + 1 < argc;
		if (argument == "--frames" && has_value)
			frames = std::strtoull(argv[++index], nullptr, 10);
		else if (argument == "--hashes" && has_value)
			hashes_path = argv[++index];
		else if (argument == "--trace" && has_value)
			trace_path = argv[++index];
		else if (argument == "--calls" && has_value)
			calls_path = argv[++index];
		else if (argument == "--text" && has_value)
			while (index + 1 < argc && argv[index + 1][0] != '-')
				text_frames.insert(std::strtoull(argv[++index], nullptr, 10));
		else if (argument == "--dump" && has_value)
			while (index + 1 < argc && argv[index + 1][0] != '-')
				dump_frames.insert(std::strtoull(argv[++index], nullptr, 10));
		else if (argument == "--dump-dir" && has_value)
			dump_folder = argv[++index];
		else if (argument == "--video-hashes" && has_value)
			video_hashes_path = argv[++index];
		else if (argument == "--video-dump" && has_value)
			while (index + 1 < argc && argv[index + 1][0] != '-')
				video_dump_frames.insert(std::strtoull(argv[++index], nullptr, 10));
		else if (argument == "--video-dir" && has_value)
			video_folder = argv[++index];
		else if (argument == "--service")
			service = true;
		else if (argument == "--rom" && has_value)
			rom_folder = argv[++index];
		else
		{
			std::cerr << usage;
			return 1;
		}
	}

	if (const char *problem = game_rom_set_load(rom_folder.c_str()))
	{
		std::cerr << "The ROM set in " << rom_folder << " cannot be read: " << problem << '\n';
		return 1;
	}
	tmnt_reset();
	if (service)
		tmnt_board.dsw[2] &= uint8_t(~TMNT_DSW3_SERVICE);

	reference_cpu cpu;
	g_cpu = &cpu;
	for (int space : { int(AS_PROGRAM), int(AS_OPCODES) })
	{
		cpu.space(space).read16 = bus_read;
		cpu.space(space).write16 = bus_write;
	}
	cpu.space(AS_OPCODES).instructions = true;
	cpu.space(m68000_base_device::AS_CPU_SPACE).read16 = acknowledge;
	cpu.space(m68000_base_device::AS_CPU_SPACE).write16 = bus_write;
	cpu.instruction_hook = on_instruction;
	cpu.log_handler = [](const std::string &message) { std::cerr << message; };
	cpu.start();
	cpu.reset();

	std::ofstream hashes, video_hashes;
	if (!hashes_path.empty())
		hashes.open(hashes_path);
	if (!video_hashes_path.empty())
		video_hashes.open(video_hashes_path);
	const bool video = video_hashes.is_open() || !video_dump_frames.empty();
	if (video)
		kv_attach();

	int status = 0;
	uint64_t frame = 0;
	try
	{
		for (frame = 1; frame <= frames; frame++)
		{
			const uint64_t start = (frame - 1) * TMNT_FRAME_CYCLES;
			run_until(cpu, start + uint64_t(TMNT_VBLANK_END) * TMNT_LINE_CYCLES);
			if (video)
				kv_vblank_end();
			run_until(cpu, start + uint64_t(TMNT_VBLANK_START) * TMNT_LINE_CYCLES);
			if (video)
				kv_vblank_start(video_hashes.is_open() || video_dump_frames.count(frame) != 0);
			tmnt_vblank_start();
			sync_irq();
			if (g_irq_level)
				g_irq_frames++;
			if (hashes.is_open())
			{
				char line[48];
				std::snprintf(line, sizeof(line), "%" PRIu64 " %016" PRIx64 "\n", frame, tmnt_state_hash());
				hashes << line;
			}
			if (text_frames.count(frame))
				print_text(frame);
			if (dump_frames.count(frame))
				dump_frame(dump_folder, frame);
			if (video_hashes.is_open())
			{
				char line[48];
				std::snprintf(line, sizeof(line), "%" PRIu64 " %016" PRIx64 "\n", frame, kv_frame_hash());
				video_hashes << line;
			}
			if (video_dump_frames.count(frame))
				write_bmp(video_folder + "/frame_" + std::to_string(frame) + ".bmp");
		}
		frame--;
	}
	catch (const std::exception &error)
	{
		std::cerr << "68000 stopped at PC " << std::hex << cpu.pc() << std::dec << " in frame " << frame << ": "
			<< error.what() << '\n';
		status = 2;
	}

	size_t unique = 0;
	for (uint8_t bits : g_executed)
		for (; bits; bits &= uint8_t(bits - 1))
			unique++;
	std::cout << "frames:            " << frame << '\n'
		<< "instructions:      " << g_instructions << '\n'
		<< "unique addresses:  " << unique << '\n'
		<< "frames with IRQ5:  " << g_irq_frames << '\n'
		<< "sound commands:    " << tmnt_board.sound_irqs << '\n'
		<< "watchdog resets:   " << tmnt_board.watchdog_resets << '\n'
		<< "final PC:          " << std::hex << cpu.pc() << std::dec << '\n';
	if (!trace_path.empty())
		write_addresses(trace_path, g_executed, "68000 addresses executed by kref");
	if (!calls_path.empty())
		write_addresses(calls_path, g_called, "68000 addresses reached by jsr or bsr");
	return status;
}
