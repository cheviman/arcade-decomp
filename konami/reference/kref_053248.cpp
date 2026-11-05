/**
 *    Name:        kref_053248.cpp
 *    Description: development reference: runs the original program of a board on Konami's own CPU (the 053248, a
 *                 6809 with Konami's opcodes) on MAME's core against the runtime's model of the board (The
 *                 Simpsons'), to find code and record state per frame
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    05-11-2026
 *    Author:      Sebastian Toyos
 *
 *    Not part of the game. Usage:
 *      kref [--timing cycles] [--frames N] [--hashes hashes.txt] [--trace executed.txt] [--calls called.txt]
 *           [--jumps jumps.txt] [--rom-reads reads.txt] [--text FRAME ...] [--dump FRAME ... --dump-dir DIR]
 *           [--script controls.txt] [--service] [--rom FOLDER] [--no-sound] [--sound-hashes hashes.txt]
 *           [--sound-wav sound.wav] [--sound-trace executed.txt] [--sound-rom-reads reads.txt]
 *           [--video-hashes hashes.txt] [--video-dump FRAME ... --video-dir DIR]
 *
 *    It runs the original program, so it reads the player's ROM set when it starts, as MAME does: the set in
 *    --rom's folder or .zip (rom/ by default), into the board's memory (game/data.h, runtime/src/rom_set.c).
 *
 *    --timing cycles, the only one yet, keeps MAME's pace: the 053248 runs its cycles a line at a time (192 of
 *    them, 64 us), VBLANK ends at line 16 and begins at line 240, where the board takes its events (the
 *    K052109's IRQ, the sprite DMA, whose FIRQ the CPU sees from 128 to 1152 cycles later) and the frame's state
 *    hash is taken.
 *
 *    The sound board runs with it (sound/ksound_053260.cpp: MAME's Z80 and K053260, ymfm's YM2151): the
 *    053248's RAM and ROM test asks the Z80 for its own, through the K053260's ports, and reports the sound
 *    chips bad if nothing answers. The Z80 catches up with the 053248 before the 053248 reads or writes those
 *    ports or raises its IRQ, as MAME synchronizes them. --no-sound leaves it standing still (the 053248 reads
 *    0 from the ports). --sound-hashes writes, for each frame, the hash of the samples the chips made and of the
 *    Z80's RAM; --sound-wav the sound mixed, in stereo; --sound-trace the Z80's places executed (the sound ROM's
 *    region offsets: 0000-7FFF fixed, 10000-27FFF its banks'; 30000 plus the address above BFFF);
 *    --sound-rom-reads the sound ROM's bytes the Z80 reads as data, as --rom-reads does the 053248's.
 *
 *    --video-hashes and --video-dump draw the frames with MAME's K052109, K053246/K053247 and K053251
 *    (video/kvideo_053246.cpp): the hash of every frame's pixels, and frames as BMP (DIR/frame_N.bmp), 320x224
 *    from line 16. As simpsons.cpp's screen updates after VBLANK, frame N's picture is drawn when its VBLANK
 *    ends (line 16), with the scroll the K052109 takes then and the sprites the DMA copied as the VBLANK before
 *    it started; its hash goes with the frame's state hash.
 *
 *    The CPU sees 64 KB, the program's banks one at a time at 6000-7FFF: the addresses this writes are the
 *    program ROM's places (its region's offsets, 000000-07FFFF: the bank's at 6000-7FFF when the instruction
 *    ran, the fixed four banks' 078000-07FFFF at 8000-FFFF), and 080000 plus the CPU's address for code below
 *    6000 (the RAM's).
 *
 *    --script applies a control script (runtime/include/simpsons/script.h) when each VBLANK starts, and runs
 *    the frames its "end" says unless --frames is given.
 *
 *    --hashes writes "FRAME HASH" per frame (simpsons_state_hash); --trace and --calls the addresses executed
 *    and the ones reached by JSR, BSR or LBSR, one per line; --jumps where each JMP or JSR whose target the
 *    instruction does not hold went ("FROM TO": through a register, a table, the direct page), the ones the
 *    static analysis has to work out; --rom-reads the program ROM's bytes the program reads as data (not as its
 *    instructions), by the instruction that reads them: "PC FIRST-LAST" ranges (FFFFFF for the CPU's own reads
 *    of its vectors), a measure for the coverage of the ROM; --text prints the K052109's fixed layer of those
 *    frames as text; --dump writes the board's memories at the end of those frames, a file each; --service
 *    turns the test switch on.
 */
#include "emu.h"
#include "konami.h"
#include "6x09dasm.h"

#include "game/data.h"
#include "game/extract.h"
#include "simpsons/machine.h"
#include "simpsons/script.h"
#include "ksound_053260.h"
#include "kvideo_053246.h"

#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace {

class reference_cpu : public konami_cpu_device
{
public:
	reference_cpu() : konami_cpu_device(m_config, "maincpu", nullptr, SIMPSONS_CPU_CLOCK * 4) {}

private:
	static inline machine_config m_config;
};

constexpr uint32_t kOutside = 0xFFFFFF;                  // no instruction (the CPU's own reads: its vectors)
constexpr uint32_t kRamCode = GAME_PROGRAM_SIZE;         // code below 6000: 080000 plus its address
constexpr uint32_t kPlaces = GAME_PROGRAM_SIZE + 0x6000; // the places an instruction can be at

reference_cpu *g_cpu = nullptr;
bool g_irq = false, g_firq = false;
std::vector<uint8_t> g_executed(kPlaces), g_called(kPlaces);
uint32_t g_current = kOutside;      // the instruction running: its place
uint32_t g_previous = kOutside;     // the one before it, unless an interrupt came between
uint16_t g_previous_pc = 0;
uint64_t g_instructions = 0;
uint64_t g_irq_frames = 0, g_firq_frames = 0;

// An instruction's place: its program ROM's offset, or 080000 plus its address below 6000
uint32_t place_of(uint16_t pc)
{
	const int32_t offset = simpsons_program_offset(pc);
	return offset >= 0 ? uint32_t(offset) : kRamCode + pc;
}

// The board's interrupt lines as the CPU sees them, after anything that can change them
void sync_lines()
{
	if (simpsons_board.irq != g_irq)
	{
		g_irq = simpsons_board.irq;
		g_cpu->set_input_line(KONAMI_IRQ_LINE, g_irq ? ASSERT_LINE : CLEAR_LINE);
	}
	if (simpsons_board.firq != g_firq)
	{
		g_firq = simpsons_board.firq;
		g_cpu->set_input_line(KONAMI_FIRQ_LINE, g_firq ? ASSERT_LINE : CLEAR_LINE);
	}
}

// --rom-reads: (instruction, program ROM offset) pairs of the program's data reads of its ROM
std::set<uint64_t> g_rom_reads;
bool g_record_rom_reads = false;

u8 bus_read(offs_t address, int access)
{
	if (g_record_rom_reads && access == BUS_DATA && address >= 0x6000)
		g_rom_reads.insert(uint64_t(g_current) << 24 | uint32_t(simpsons_program_offset(uint16_t(address))));
	return simpsons_read(uint16_t(address));
}

void bus_write(offs_t address, u8 data)
{
	simpsons_write(uint16_t(address), data);
	sync_lines();
}

// The ROM reads as "PC FIRST-LAST" lines: by instruction, runs of consecutive bytes
void write_rom_reads(const std::string &path)
{
	std::ofstream out(path);
	out << "# program ROM bytes read as data by kref (region offsets): PC FIRST-LAST\n";
	uint32_t pc = 0, first = 0, last = 0;
	bool open = false;
	auto flush = [&]() {
		char line[32];
		std::snprintf(line, sizeof(line), "%06X %06X-%06X\n", pc, first, last);
		out << line;
	};
	for (const uint64_t pair : g_rom_reads)
	{
		const uint32_t here_pc = uint32_t(pair >> 24), here = uint32_t(pair & 0xFFFFFF);
		if (open && here_pc == pc && here == last + 1)
		{
			last = here;
			continue;
		}
		if (open)
			flush();
		pc = here_pc;
		first = last = here;
		open = true;
	}
	if (open)
		flush();
}

// What an instruction is, from MAME's disassembler: a call (JSR, BSR, LBSR), a jump whose target it does not
// hold (JMP or JSR through a register, a table or the direct page), cached by place for the program ROM's
enum : uint8_t { kKnown = 1, kCall = 2, kComputedJump = 4 };
std::vector<uint8_t> g_kinds(kPlaces);
konami_disassembler g_disassembler;
std::vector<u8> g_view(0x10000);

uint8_t kind_of(uint16_t pc, uint32_t place)
{
	if (place < kRamCode && g_kinds[place])
		return g_kinds[place];
	// the instruction's bytes as the CPU sees them now (no device read: the program runs from ROM or RAM)
	for (uint16_t at = pc, count = 0; count < 8; count++, at++)
	{
		const int32_t offset = simpsons_program_offset(at);
		g_view[at] = offset >= 0 ? game_program[offset]
			: at >= 0x4000 ? simpsons_board.work_ram[at - 0x4000]
			: (at >= 0x3000 && (simpsons_board.video_bank & 2)) ? simpsons_board.extra_ram[at - 0x3000] : 0;
	}
	const util::disasm_interface::data_buffer buffer(g_view.data(), 0x10000);
	std::ostringstream text;
	const u32 result = g_disassembler.disassemble(text, pc, buffer, buffer);
	const std::string line = text.str();
	uint8_t kind = kKnown;
	if (result & util::disasm_interface::STEP_OVER)
		kind |= kCall;
	if (line.rfind("JMP", 0) == 0 || line.rfind("JSR", 0) == 0)
	{
		const std::string operand = line.size() > 7 ? line.substr(7) : std::string();
		const bool fixed = operand.size() == 5 && operand[0] == '$';
		if (!fixed)
			kind |= kComputedJump;
	}
	if (place < kRamCode)
		g_kinds[place] = kind;
	return kind;
}

std::set<std::pair<uint32_t, uint32_t>> g_jumps;    // --jumps: where each computed jump went

uint8_t g_previous_kind = 0;

void on_instruction(offs_t pc)
{
	const uint32_t place = place_of(uint16_t(pc));
	g_instructions++;
	g_executed[place] = 1;
	if (g_previous != kOutside)
	{
		if (g_previous_kind & kCall)
			g_called[place] = 1;
		if (g_previous_kind & kComputedJump)
			g_jumps.emplace(g_previous, place);
	}
	// (what it is, from its bytes now: a SETLN of its own may change the bank before the next one)
	g_previous_kind = kind_of(uint16_t(pc), place);
	g_previous = g_current = place;
	g_previous_pc = uint16_t(pc);
}

// The CPU takes an interrupt: what follows is the handler, not where the last instruction went
void on_interrupt(int line)
{
	(void)line;
	g_previous = g_current = kOutside;
}

void write_places(const std::string &path, const std::vector<uint8_t> &places, const char *title)
{
	std::ofstream out(path);
	out << "# " << title << " (region offsets; 080000 plus the address for code below 6000), one per line\n";
	for (uint32_t place = 0; place < kPlaces; place++)
		if (places[place])
		{
			char line[16];
			std::snprintf(line, sizeof(line), "%06X\n", place);
			out << line;
		}
}

// The CPU's slice while it runs: where it started and how long it is
uint64_t g_slice_start = 0;
int g_slice_budget = 0;
bool g_in_slice = false;

uint64_t present()
{
	return g_in_slice ? g_slice_start + uint64_t(g_slice_budget - g_cpu->cycles_left()) : simpsons_board.cycles;
}

// The CPU runs until the board's clock reaches 'cycle', a line at a time at most, stopping at the DMA's FIRQ
void run_until(reference_cpu &cpu, uint64_t cycle)
{
	while (simpsons_board.cycles < cycle)
	{
		uint64_t until = std::min<uint64_t>(cycle, simpsons_board.cycles + SIMPSONS_LINE_CYCLES);
		if (simpsons_board.dma_start_at && simpsons_board.dma_start_at < until)
			until = simpsons_board.dma_start_at;
		if (simpsons_board.dma_end_at && simpsons_board.dma_end_at < until)
			until = simpsons_board.dma_end_at;
		if (until > simpsons_board.cycles)
		{
			g_slice_start = simpsons_board.cycles;
			g_slice_budget = int(until - simpsons_board.cycles);
			g_in_slice = true;
			simpsons_board.cycles += uint64_t(cpu.run(g_slice_budget));
			g_in_slice = false;
		}
		if (simpsons_board.dma_start_at && simpsons_board.cycles >= simpsons_board.dma_start_at)
		{
			simpsons_dma_start();
			sync_lines();
		}
		if (simpsons_board.dma_end_at && simpsons_board.cycles >= simpsons_board.dma_end_at)
		{
			simpsons_dma_end();
			sync_lines();
		}
	}
}

// The board's memories at the end of a frame, a file each: DIR/FRAME_work_ram.bin...
void dump_frame(const std::string &folder, uint64_t frame)
{
	const struct { const char *name; const uint8_t *data; size_t size; } memories[] = {
		{ "work_ram", simpsons_board.work_ram, sizeof(simpsons_board.work_ram) },
		{ "extra_ram", simpsons_board.extra_ram, sizeof(simpsons_board.extra_ram) },
		{ "palette", simpsons_board.palette, sizeof(simpsons_board.palette) },
		{ "tile_ram", simpsons_board.tile_ram, sizeof(simpsons_board.tile_ram) },
		{ "sprite_ram", simpsons_board.sprite_ram, sizeof(simpsons_board.sprite_ram) },
		{ "object_ram", simpsons_board.object_ram, sizeof(simpsons_board.object_ram) },
		{ "eeprom", simpsons_board.eeprom.data, sizeof(simpsons_board.eeprom.data) },
	};
	for (const auto &memory : memories)
	{
		std::ofstream out(folder + "/" + std::to_string(frame) + "_" + memory.name + ".bin", std::ios::binary);
		out.write(reinterpret_cast<const char *>(memory.data), std::streamsize(memory.size));
	}
}

// The K052109's fixed layer (its codes at 2000-27FF) as text, 64 columns by 32 rows: a tile is shown as the
// character 0x30 above it, '@' as a space (TMNT's font; to be checked for this game)
void print_text(uint64_t frame)
{
	std::cout << "frame " << frame << ", fixed layer:\n";
	for (int row = 0; row < 32; row++)
	{
		std::string line;
		for (int column = 0; column < 64; column++)
		{
			const int character = simpsons_board.tile_ram[0x2000 + row * 64 + column] + 0x30;
			line += character == '@' ? ' ' : (character < 0x7F ? char(character) : '.');
		}
		while (!line.empty() && (line.back() == ' ' || line.back() == '.'))
			line.pop_back();
		std::cout << "  |" << line << '\n';
	}
}

void write_wav(const std::string &path, const std::vector<int16_t> &samples, uint32_t rate, uint16_t channels)
{
	std::vector<uint8_t> file(44 + samples.size() * 2, 0);
	auto put32 = [&](size_t at, uint32_t value) { for (int byte = 0; byte < 4; byte++) file[at + byte] = uint8_t(value >> (8 * byte)); };
	auto put16 = [&](size_t at, uint16_t value) { file[at] = uint8_t(value); file[at + 1] = uint8_t(value >> 8); };
	std::memcpy(&file[0], "RIFF", 4);
	put32(4, uint32_t(file.size() - 8));
	std::memcpy(&file[8], "WAVEfmt ", 8);
	put32(16, 16);
	put16(20, 1);
	put16(22, channels);
	put32(24, rate);
	put32(28, rate * 2u * channels);
	put16(32, uint16_t(2u * channels));
	put16(34, 16);
	std::memcpy(&file[36], "data", 4);
	put32(40, uint32_t(samples.size() * 2));
	for (size_t index = 0; index < samples.size(); index++)
		put16(44 + index * 2, uint16_t(samples[index]));
	std::ofstream out(path, std::ios::binary);
	out.write(reinterpret_cast<const char *>(file.data()), std::streamsize(file.size()));
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
			uint8_t *out = &file[54 + size_t(KV_HEIGHT - 1 - y) * size_t(row) + size_t(x) * 3];
			out[0] = uint8_t(pixel);
			out[1] = uint8_t(pixel >> 8);
			out[2] = uint8_t(pixel >> 16);
		}
	std::ofstream out(path, std::ios::binary);
	out.write(reinterpret_cast<const char *>(file.data()), std::streamsize(file.size()));
}

// The sound board catches up with the 053248's present
void sound_sync()
{
	ks_run_until(present());
}

} // namespace

int main(int argc, char **argv)
{
	const char *usage = "Usage: kref [--timing cycles] [--frames N] [--hashes FILE] [--trace FILE] [--calls FILE]"
		" [--jumps FILE] [--rom-reads FILE] [--text FRAME ...] [--dump FRAME ... --dump-dir DIR] [--script FILE]"
		" [--service] [--rom FOLDER] [--no-sound] [--sound-hashes FILE] [--sound-wav FILE] [--sound-trace FILE]"
		" [--sound-rom-reads FILE] [--video-hashes FILE] [--video-dump FRAME ... --video-dir DIR]\n";
	std::string rom_folder = "rom";
	std::string hashes_path, trace_path, calls_path, jumps_path, rom_reads_path, dump_folder = ".", script_path;
	std::set<uint64_t> text_frames, dump_frames;
	uint64_t frames = 600;
	bool frames_given = false, service = false, sound = true;
	std::string sound_hashes_path, sound_wav_path, sound_trace_path, sound_rom_reads_path;
	std::string video_hashes_path, video_folder = ".";
	std::set<uint64_t> video_dump_frames;

	for (int index = 1; index < argc; index++)
	{
		const std::string argument = argv[index];
		const bool has_value = index + 1 < argc;
		if (argument == "--frames" && has_value)
		{
			frames = std::strtoull(argv[++index], nullptr, 10);
			frames_given = true;
		}
		else if (argument == "--timing" && has_value)
		{
			if (std::string(argv[++index]) != "cycles")
			{
				std::cerr << "Only --timing cycles yet: the frame-wait loop is not known\n" << usage;
				return 1;
			}
		}
		else if (argument == "--script" && has_value)
			script_path = argv[++index];
		else if (argument == "--hashes" && has_value)
			hashes_path = argv[++index];
		else if (argument == "--trace" && has_value)
			trace_path = argv[++index];
		else if (argument == "--calls" && has_value)
			calls_path = argv[++index];
		else if (argument == "--jumps" && has_value)
			jumps_path = argv[++index];
		else if (argument == "--rom-reads" && has_value)
			rom_reads_path = argv[++index];
		else if (argument == "--text" && has_value)
			while (index + 1 < argc && argv[index + 1][0] != '-')
				text_frames.insert(std::strtoull(argv[++index], nullptr, 10));
		else if (argument == "--dump" && has_value)
			while (index + 1 < argc && argv[index + 1][0] != '-')
				dump_frames.insert(std::strtoull(argv[++index], nullptr, 10));
		else if (argument == "--dump-dir" && has_value)
			dump_folder = argv[++index];
		else if (argument == "--service")
			service = true;
		else if (argument == "--no-sound")
			sound = false;
		else if (argument == "--sound-hashes" && has_value)
			sound_hashes_path = argv[++index];
		else if (argument == "--sound-wav" && has_value)
			sound_wav_path = argv[++index];
		else if (argument == "--sound-trace" && has_value)
			sound_trace_path = argv[++index];
		else if (argument == "--sound-rom-reads" && has_value)
			sound_rom_reads_path = argv[++index];
		else if (argument == "--video-hashes" && has_value)
			video_hashes_path = argv[++index];
		else if (argument == "--video-dump" && has_value)
			while (index + 1 < argc && argv[index + 1][0] != '-')
				video_dump_frames.insert(std::strtoull(argv[++index], nullptr, 10));
		else if (argument == "--video-dir" && has_value)
			video_folder = argv[++index];
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
	simpsons_reset();
	simpsons_board.now = present;
	if (service)
		simpsons_board.test &= uint8_t(~SIMPSONS_TEST_SERVICE);
	simpsons_script *script = nullptr;
	if (!script_path.empty())
	{
		char error[256];
		script = simpsons_script_load(script_path.c_str(), error, sizeof(error));
		if (!script)
		{
			std::cerr << error << '\n';
			return 1;
		}
		if (!frames_given && simpsons_script_frames(script))
			frames = simpsons_script_frames(script);
		simpsons_script_apply(script, 0);
	}

	reference_cpu cpu;
	g_cpu = &cpu;
	cpu.bus.read = bus_read;
	cpu.bus.write = bus_write;
	cpu.line().set([](offs_t, u8 data) { simpsons_set_lines(data); });
	cpu.instruction_hook = on_instruction;
	cpu.irq_acknowledge = on_interrupt;
	cpu.log_handler = [](const std::string &message) { std::cerr << message; };
	g_record_rom_reads = !rom_reads_path.empty();
	cpu.start();
	cpu.reset();

	std::ofstream hashes, sound_hashes, video_hashes;
	if (!hashes_path.empty())
		hashes.open(hashes_path);
	if (!video_hashes_path.empty())
		video_hashes.open(video_hashes_path);
	const bool video = video_hashes.is_open() || !video_dump_frames.empty();
	if (video)
		kv_attach();
	std::vector<int16_t> sound_mix;
	if (sound)
	{
		ks_attach();
		simpsons_board.sound_sync = sound_sync;
		if (!sound_rom_reads_path.empty())
			ks_record_rom_reads();
		if (!sound_hashes_path.empty())
			sound_hashes.open(sound_hashes_path);
	}

	int status = 0;
	uint64_t frame = 0;
	try
	{
		for (frame = 1; frame <= frames; frame++)
		{
			const uint64_t start = (frame - 1) * SIMPSONS_FRAME_CYCLES;
			run_until(cpu, start + uint64_t(SIMPSONS_VBLANK_END) * SIMPSONS_LINE_CYCLES);
			// VBLANK ends: the K052109 takes its scroll, and the screen updates (after VBLANK, as simpsons.cpp's)
			if (video)
				kv_vblank_end(video_hashes.is_open() || video_dump_frames.count(frame) != 0);
			run_until(cpu, start + uint64_t(SIMPSONS_VBLANK_START) * SIMPSONS_LINE_CYCLES);
			// VBLANK begins: the board takes its events, the controls change, and the frame's hash is taken
			if (video)
				kv_vblank_start();
			simpsons_vblank_start();
			simpsons_script_apply(script, simpsons_board.frame);
			sync_lines();
			if (sound)
			{
				ks_end_frame(simpsons_board.cycles);
				if (sound_hashes.is_open())
				{
					char line[64];
					std::snprintf(line, sizeof(line), "%" PRIu64 " %016" PRIx64 " %016" PRIx64 "\n", frame,
						ks_frame_samples_hash(), ks_state_hash());
					sound_hashes << line;
				}
				if (!sound_wav_path.empty())
				{
					size_t count = 0;
					const int16_t *mix = ks_frame_mix(&count);
					sound_mix.insert(sound_mix.end(), mix, mix + count);
				}
			}
			g_irq_frames += g_irq;
			g_firq_frames += simpsons_board.dma_start_at && simpsons_board.firq_enabled;
			if (hashes.is_open())
			{
				char line[48];
				std::snprintf(line, sizeof(line), "%" PRIu64 " %016" PRIx64 "\n", frame, simpsons_state_hash());
				hashes << line;
			}
			if (video_hashes.is_open())
			{
				char line[48];
				std::snprintf(line, sizeof(line), "%" PRIu64 " %016" PRIx64 "\n", frame, kv_frame_hash());
				video_hashes << line;
			}
			if (video_dump_frames.count(frame))
				write_bmp(video_folder + "/frame_" + std::to_string(frame) + ".bmp");
			if (text_frames.count(frame))
				print_text(frame);
			if (dump_frames.count(frame))
				dump_frame(dump_folder, frame);
		}
		frame--;
	}
	catch (const std::exception &error)
	{
		std::cerr << "053248 stopped at " << std::hex << g_current << std::dec << " in frame " << frame << ": "
			<< error.what() << '\n';
		status = 2;
	}

	size_t unique = 0;
	for (uint8_t executed : g_executed)
		unique += executed;
	char final_place[32];
	std::snprintf(final_place, sizeof(final_place), "%06X (%04X, bank %02X)", g_current, g_previous_pc,
		unsigned(simpsons_board.bank));
	std::cout << "frames:            " << frame << '\n'
		<< "instructions:      " << g_instructions << '\n'
		<< "unique addresses:  " << unique << '\n'
		<< "frames with IRQ:   " << g_irq_frames << '\n'
		<< "frames with DMA:   " << g_firq_frames << " (its FIRQ enabled)\n"
		<< "sound IRQs:        " << simpsons_board.sound_irqs << '\n'
		<< "watchdog resets:   " << simpsons_board.watchdog_resets << '\n'
		<< "writes to the ROM: " << simpsons_board.unmapped_writes << '\n'
		<< "final place:       " << final_place << '\n';
	if (!trace_path.empty())
		write_places(trace_path, g_executed, "053248 instructions executed by kref");
	if (!calls_path.empty())
		write_places(calls_path, g_called, "053248 places reached by JSR, BSR or LBSR");
	if (!jumps_path.empty())
	{
		std::ofstream out(jumps_path);
		out << "# where each 053248 JMP or JSR whose target it does not hold went, by kref: FROM TO\n";
		for (const auto &jump : g_jumps)
		{
			char line[24];
			std::snprintf(line, sizeof(line), "%06X %06X\n", jump.first, jump.second);
			out << line;
		}
	}
	if (!rom_reads_path.empty())
		write_rom_reads(rom_reads_path);
	if (sound)
	{
		std::cout << "Z80 cycles:        " << ks_z80_cycles() << '\n';
		if (!sound_wav_path.empty())
			write_wav(sound_wav_path, sound_mix, ks_mix_rate(), 2);
		if (!sound_trace_path.empty())
		{
			std::ofstream out(sound_trace_path);
			out << "# Z80 places executed by kref (sound ROM region offsets; 30000 plus the address above BFFF), one per line\n";
			size_t count = 0;
			const uint8_t *executed = ks_executed(&count);
			for (size_t place = 0; place < count; place++)
				if (executed[place])
				{
					char line[16];
					std::snprintf(line, sizeof(line), "%05zX\n", place);
					out << line;
				}
		}
		if (!sound_rom_reads_path.empty())
		{
			// by instruction, runs of consecutive bytes, as --rom-reads writes the 053248's
			std::ofstream out(sound_rom_reads_path);
			out << "# Z80 sound ROM bytes read as data by kref (region offsets): PC FIRST-LAST\n";
			size_t count = 0;
			const uint64_t *reads = ks_rom_reads(&count);
			for (size_t index = 0; index < count;)
			{
				const uint32_t pc = uint32_t(reads[index] >> 32), first = uint32_t(reads[index]);
				uint32_t last = first;
				while (++index < count && uint32_t(reads[index] >> 32) == pc && uint32_t(reads[index]) == last + 1)
					last++;
				char line[32];
				std::snprintf(line, sizeof(line), "%05X %05X-%05X\n", unsigned(pc), unsigned(first), unsigned(last));
				out << line;
			}
		}
	}
	simpsons_script_free(script);
	return status;
}
