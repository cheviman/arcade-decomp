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
 *      kref [--timing frames|cycles] [--frames N] [--hashes hashes.txt] [--trace executed.txt] [--calls called.txt]
 *           [--jumps jumps.txt] [--rom-reads reads.txt] [--text FRAME ...] [--dump FRAME ... --dump-dir DIR]
 *           [--video-hashes hashes.txt] [--video-dump FRAME ... --video-dir DIR] [--video-check FILE]
 *           [--sound-hashes hashes.txt]
 *           [--sound-wav sound.wav] [--sound-trace executed.txt] [--sound-rom-reads reads.txt]
 *           [--sound-commands commands.txt] [--script controls.txt] [--service] [--rom FOLDER]
 *           [--sound-check] [--assets-check FOLDER] [--sound-timing timing.txt]
 *           [--sound-board-check FILE] [--assets FOLDER]
 *
 *    It runs the original program, so it reads the player's ROM set when it starts, as MAME does: the set
 *    in --rom's folder or .zip (rom/ by default), into the board's memory (game/data.h,
 *    runtime/src/rom_set.c).
 *
 *    --timing frames, the default, runs the program the way the translated and the decompiled game run it, as
 *    system32/reference/s32ref does: instructions take no time, and the board moves on to its next frame only
 *    where the program has nothing to do (runtime/include/tmnt/frame.h: the main loop finds no job): VBLANK
 *    ends and begins there, and the program takes the IRQ5 it raises after that instruction. --timing cycles
 *    keeps MAME's pace: the 68000 runs its cycles a line at a time (512 of them, 64 us), VBLANK ends at line
 *    16 and begins at line 240. Either way a frame ends when VBLANK begins, where the board takes its events
 *    (the sprite DMA, IRQ5) and the frame's state hash is taken. Without the sound options the sound board
 *    stands still: the 68000 reads nothing from it.
 *
 *    --script applies a control script (runtime/include/tmnt/script.h) when each VBLANK starts, and runs
 *    the frames its "end" says unless --frames is given.
 *
 *    --hashes writes "FRAME HASH" per frame (tmnt_state_hash); --trace and --calls the addresses executed
 *    and the ones reached by jsr or bsr, one per line; --jumps where each jmp or jsr through a register went
 *    ("FROM TO", the ones the static analysis has to work out); --rom-reads the program ROM's bytes the
 *    program reads as data (not as its instructions), by the instruction that reads them: "PC FIRST-LAST"
 *    ranges, a measure for the coverage of the ROM (what is data, and the code that reads it); --text prints
 *    the fixed layer's tiles of those frames as text (the program's own character codes, '@' a space);
 *    --dump writes the board's memories at the end of those frames, a file each; --service turns the test
 *    mode's DIP switch on.
 *
 *    --video-hashes and --video-dump draw the frames with MAME's K052109 and K051960 (video/kvideo.cpp): the
 *    hash of every frame's pixels, and frames as BMP (DIR/frame_N.bmp), 320x224 from line 16. The video
 *    takes the board's memories when VBLANK ends (line 16, the scroll) and when it starts (the frame).
 *    --video-check draws every frame with the game's own video too (runtime/src/video.c) and writes "FRAME
 *    REFERENCE OWN" hashes; the first frame that differs goes to DIR/reference_N.bmp and DIR/own_N.bmp.
 *
 *    --sound-hashes, --sound-wav and --sound-trace run the sound board (sound/ksound.cpp: MAME's Z80, K007232
 *    and uPD7759, ymfm's YM2151, the title music): for each frame the hash of the samples the sources made
 *    and of the Z80's RAM; the sound mixed, as a WAV; the Z80 addresses executed. The Z80 catches up with
 *    the 68000 before the 68000 writes its latch or raises its IRQ, as MAME synchronizes them.
 *    --sound-rom-reads runs it too, and writes the sound program's bytes the Z80 reads as data (not its
 *    opcode and argument fetches), as --rom-reads does the 68000's: "PC FIRST-LAST" ranges; --sound-commands
 *    the commands the 68000 gives it (the latch as the Z80's IRQ goes up): "FRAME COMMAND" lines.
 *    --sound-check runs the game's own sound chips too (runtime/src/ym2151.c, sound_chips.c) on what MAME's
 *    get, and prints how many samples of each differ.
 *    --sound-timing keeps the Z80's time with the timing model the game's own sound board has
 *    (tools/decompile_sound.py writes the table, build/sound/timing.txt; ksound.cpp keeps it around MAME's
 *    Z80), so that the two compare sample for sample; without it the Z80 runs cycle by cycle.
 *    --sound-board-check runs the game's own sound board beside MAME's (runtime/src/sound_cpu.c and
 *    sound_board.c: the decompiled sound program of src/sound/ on the board's own chips, its data from the
 *    assets in --assets' folder, assets/ by default), fed the same commands at the same times, and writes
 *    "FRAME REFERENCE_SAMPLES REFERENCE_RAM OWN_SAMPLES OWN_RAM" hashes a frame (the --sound-hashes ones); it
 *    prints how many frames differ, and for the first one what differs. It needs --sound-timing, and a kref
 *    built with the game's sound program (K_GAME_SOUND in reference.cmake).
 *
 *    --assets-check loads the game's assets from FOLDER (runtime/src/assets.c, what the game reads instead of
 *    the ROM) and compares what they give with the ROM set's regions: the tiles, the sprites, the samples,
 *    and every byte of both programs a piece of data gives. It prints what differs and runs nothing.
 */
#include "emu.h"
#include "m68000.h"

#include "game/assets.h"
#include "game/data.h"
#include "game/extract.h"
#include "ksound.h"
#include "kvideo.h"
#include "tmnt/frame.h"
#include "tmnt/machine.h"
#include "tmnt/script.h"
#include "tmnt/video.h"
#ifdef K_GAME_SOUND_BOARD
#include "tmnt/sound_board.h"
#endif

#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <iterator>
#include <fstream>
#include <functional>
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

// --rom-reads: (instruction, program ROM address) pairs of the program's data reads of its ROM
std::set<uint64_t> g_rom_reads;
bool g_record_rom_reads = false;

// --sound-commands: the commands the 68000 gives the sound board, with the frame they come in
std::vector<std::pair<uint64_t, uint8_t>> g_sound_commands;
void (*g_raise_sound_irq)(void) = nullptr;
uint64_t g_frame = 0;

void log_sound_command()
{
	g_sound_commands.emplace_back(g_frame, tmnt_board.sound_latch);
	g_raise_sound_irq();
}

u16 bus_read(offs_t address, u16 mem_mask)
{
	if (g_record_rom_reads && !g_reading_instructions && (address & 0xFFFFFF) < GAME_PROGRAM_SIZE)
	{
		const uint64_t pc = uint64_t(g_cpu->pc()) << 24;
		if (mem_mask & 0xFF00)
			g_rom_reads.insert(pc | (address & 0xFFFFFE));
		if (mem_mask & 0x00FF)
			g_rom_reads.insert(pc | ((address & 0xFFFFFE) + 1));
	}
	return tmnt_read16(address, mem_mask);
}

// The ROM reads as "PC FIRST-LAST" lines: by instruction, runs of consecutive bytes
void write_rom_reads(const std::string &path)
{
	std::ofstream out(path);
	out << "# 68000 program ROM bytes read as data by kref: PC FIRST-LAST\n";
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

// A jmp or jsr through a register or a register's memory: (An), (d16,An), (d8,An,Xn), (d8,PC,Xn)
bool is_computed_jump(uint32_t pc)
{
	if (pc + 1 >= GAME_PROGRAM_SIZE)
		return false;
	const uint16_t opcode = uint16_t(game_program[pc] << 8 | game_program[pc + 1]);
	const int mode = (opcode >> 3) & 7;
	return ((opcode & 0xFFC0) == 0x4E80 || (opcode & 0xFFC0) == 0x4EC0)
		&& (mode == 2 || mode == 5 || mode == 6 || (opcode & 0x3F) == 0x3B);
}

std::set<std::pair<uint32_t, uint32_t>> g_jumps;    // --jumps: where each computed jump went

// With frame timing: the board's next frame, when the program has nothing to do (tmnt/frame.h); a program
// that stops getting there has stopped making frames
bool g_frame_timing = true;
std::function<void()> g_frame_wait;
uint64_t g_instructions_at_frame = 0;
constexpr uint64_t kInstructionsPerFrameLimit = 200000000;    // the boot's tests run some millions at once

// The frames asked for are done (frame timing)
struct frames_done {};

void on_instruction(offs_t pc)
{
	g_instructions++;
	mark(g_executed, pc);
	if (g_previous_pc != 0xFFFFFFFF && is_call(g_previous_pc))
		mark(g_called, pc);
	if (g_previous_pc != 0xFFFFFFFF && is_computed_jump(g_previous_pc))
		g_jumps.emplace(g_previous_pc, pc & 0xFFFFFF);
	g_previous_pc = pc;
	if (g_frame_timing)
	{
		if ((pc & 0xFFFFFF) == TMNT_FRAME_WAIT && tmnt_frame_idle())
		{
			g_instructions_at_frame = g_instructions;
			g_frame_wait();
		}
		else if (g_instructions - g_instructions_at_frame > kInstructionsPerFrameLimit)
			throw std::runtime_error("the program no longer reaches its frame loop");
	}
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

// The 68000's slice while it runs: where it started and how long it is
uint64_t g_slice_start = 0;
int g_slice_budget = 0;
bool g_in_slice = false;

// The 68000 runs until the board's clock reaches 'cycle', a line at a time at most
void run_until(reference_cpu &cpu, uint64_t cycle)
{
	while (tmnt_board.cycles < cycle)
	{
		const uint64_t slice = std::min<uint64_t>(cycle - tmnt_board.cycles, TMNT_LINE_CYCLES);
		g_slice_start = tmnt_board.cycles;
		g_slice_budget = int(slice);
		g_in_slice = true;
		tmnt_board.cycles += uint64_t(cpu.run(int(slice)));
		g_in_slice = false;
	}
}

// --sound-board-check: the game's own sound board runs beside the reference's, on the same commands
bool g_sound_board = false;
void (*g_reference_sound_irq)(void) = nullptr;

void raise_both_sound_irqs()
{
	g_reference_sound_irq();
#ifdef K_GAME_SOUND_BOARD
	tmnt_sound_irq();
#endif
}

// The sound board catches up with the 68000's present, as MAME synchronizes the latch and the Z80's IRQ
void sound_sync()
{
	const uint64_t now = g_in_slice ? g_slice_start + uint64_t(g_slice_budget - g_cpu->cycles_left()) : tmnt_board.cycles;
	ks_run_until(now);
#ifdef K_GAME_SOUND_BOARD
	if (g_sound_board)
		tmnt_sound_run(now);
#endif
}

void write_wav(const std::string &path, const std::vector<int16_t> &samples, uint32_t rate)
{
	std::vector<uint8_t> file(44 + samples.size() * 2, 0);
	auto put32 = [&](size_t at, uint32_t value) { for (int byte = 0; byte < 4; byte++) file[at + byte] = uint8_t(value >> (8 * byte)); };
	auto put16 = [&](size_t at, uint16_t value) { file[at] = uint8_t(value); file[at + 1] = uint8_t(value >> 8); };
	std::memcpy(&file[0], "RIFF", 4);
	put32(4, uint32_t(file.size() - 8));
	std::memcpy(&file[8], "WAVEfmt ", 8);
	put32(16, 16);
	put16(20, 1);
	put16(22, 1);
	put32(24, rate);
	put32(28, rate * 2);
	put16(32, 2);
	put16(34, 16);
	std::memcpy(&file[36], "data", 4);
	put32(40, uint32_t(samples.size() * 2));
	for (size_t index = 0; index < samples.size(); index++)
		put16(44 + index * 2, uint16_t(samples[index]));
	std::ofstream out(path, std::ios::binary);
	out.write(reinterpret_cast<const char *>(file.data()), std::streamsize(file.size()));
}

// The last frame the reference video drew (or 'frame' when given), as a 24-bit BMP
void write_bmp(const std::string &path, const uint32_t *frame = nullptr)
{
	const uint32_t *pixels = frame ? frame : kv_frame();
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
	// (the work RAM in the order of its addresses: the board keeps it the other way round)
	uint8_t work_ram[TMNT_WORK_RAM_SIZE];
	for (uint32_t offset = 0; offset < TMNT_WORK_RAM_SIZE; offset++)
		work_ram[offset] = *tmnt_work_ram_byte(TMNT_WORK_RAM + offset);
	const struct { const char *name; const uint8_t *data; size_t size; } memories[] = {
		{ "work_ram", work_ram, sizeof(work_ram) },
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

// --assets-check: the assets' regions against the ROM set's, and both programs' data where a piece gives it
static int check_assets(const std::string &folder)
{
	struct region { const char *name; uint8_t *memory; size_t size; };
	const region regions[] = {
		{ "tiles", game_tiles, GAME_TILES_SIZE }, { "sprites", game_sprites, GAME_SPRITES_SIZE },
		{ "K007232 samples", game_pcm, GAME_PCM_SIZE }, { "uPD7759 speech", game_speech, GAME_SPEECH_SIZE },
		{ "title music", game_title, GAME_TITLE_SIZE } };
	std::vector<std::vector<uint8_t>> from_rom;
	for (const region &each : regions)
	{
		from_rom.emplace_back(each.memory, each.memory + each.size);
		std::memset(each.memory, 0, each.size);
	}
	if (const char *problem = assets_load(folder.c_str()))
	{
		std::cerr << "The assets in " << folder << " do not load: " << problem << '\n';
		return 1;
	}
	int differ = 0;
	for (size_t index = 0; index < std::size(regions); index++)
	{
		const region &each = regions[index];
		size_t count = 0, first = 0;
		for (size_t at = 0; at < each.size; at++)
			if (each.memory[at] != from_rom[index][at] && count++ == 0)
				first = at;
		std::printf("%-16s %zu bytes differ", each.name, count);
		if (count)
			std::printf(" (the first at %06zX)", first);
		std::printf("\n");
		differ += count != 0;
	}
	struct program { const char *name; const uint8_t *rom; size_t size; void (*image)(uint8_t *, uint8_t *, size_t); };
	const program programs[] = { { "68000 data", game_program, GAME_PROGRAM_SIZE, assets_program_image },
		{ "Z80 data", game_sound, GAME_SOUND_SIZE, assets_sound_image } };
	for (const program &each : programs)
	{
		std::vector<uint8_t> image(each.size), placed(each.size);
		each.image(image.data(), placed.data(), each.size);
		size_t given = 0, count = 0, first = 0;
		for (size_t at = 0; at < each.size; at++)
			if (placed[at])
			{
				given++;
				if (image[at] != each.rom[at] && count++ == 0)
					first = at;
			}
		std::printf("%-16s %zu bytes given, %zu differ", each.name, given, count);
		if (count)
			std::printf(" (the first at %06zX)", first);
		std::printf("\n");
		differ += count != 0;
	}
	return differ ? 1 : 0;
}

int main(int argc, char **argv)
{
	const char *usage = "Usage: kref [--timing frames|cycles] [--frames N] [--hashes FILE] [--trace FILE] [--calls FILE] [--jumps FILE]"
		" [--rom-reads FILE] [--text FRAME ...]"
		" [--dump FRAME ... --dump-dir DIR] [--video-hashes FILE] [--video-dump FRAME ... --video-dir DIR]"
		" [--sound-hashes FILE] [--sound-wav FILE] [--sound-trace FILE] [--sound-rom-reads FILE]"
		" [--sound-commands FILE] [--script FILE] [--service] [--rom FOLDER] [--video-check FILE] [--sound-check]"
		" [--assets-check FOLDER] [--sound-timing FILE] [--sound-board-check FILE] [--assets FOLDER]\n";
	std::string rom_folder = "rom";
	std::string hashes_path, trace_path, calls_path, dump_folder = ".", video_hashes_path, video_folder = ".";
	std::string video_check_path, assets_folder;
	std::string sound_timing_path, sound_board_path, own_assets_folder = "assets";
	bool sound_check = false;
	std::string sound_hashes_path, sound_wav_path, sound_trace_path, jumps_path, rom_reads_path;
	std::string sound_rom_reads_path, sound_commands_path;
	std::set<uint64_t> text_frames, dump_frames, video_dump_frames;
	uint64_t frames = 600;
	bool frames_given = false;
	std::string script_path;
	bool service = false;

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
			const std::string timing = argv[++index];
			if (timing != "frames" && timing != "cycles")
			{
				std::cerr << usage;
				return 1;
			}
			g_frame_timing = timing == "frames";
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
		else if (argument == "--video-hashes" && has_value)
			video_hashes_path = argv[++index];
		else if (argument == "--video-check" && has_value)
			video_check_path = argv[++index];
		else if (argument == "--sound-check")
			sound_check = true;
		else if (argument == "--assets-check" && has_value)
			assets_folder = argv[++index];
		else if (argument == "--sound-timing" && has_value)
			sound_timing_path = argv[++index];
		else if (argument == "--sound-board-check" && has_value)
			sound_board_path = argv[++index];
		else if (argument == "--assets" && has_value)
			own_assets_folder = argv[++index];
		else if (argument == "--video-dump" && has_value)
			while (index + 1 < argc && argv[index + 1][0] != '-')
				video_dump_frames.insert(std::strtoull(argv[++index], nullptr, 10));
		else if (argument == "--video-dir" && has_value)
			video_folder = argv[++index];
		else if (argument == "--sound-hashes" && has_value)
			sound_hashes_path = argv[++index];
		else if (argument == "--sound-wav" && has_value)
			sound_wav_path = argv[++index];
		else if (argument == "--sound-trace" && has_value)
			sound_trace_path = argv[++index];
		else if (argument == "--sound-rom-reads" && has_value)
			sound_rom_reads_path = argv[++index];
		else if (argument == "--sound-commands" && has_value)
			sound_commands_path = argv[++index];
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
	if (!assets_folder.empty())
		return check_assets(assets_folder);
	if (!sound_board_path.empty())
	{
#ifdef K_GAME_SOUND_BOARD
		if (sound_timing_path.empty())
		{
			std::cerr << usage;
			return 1;
		}
		// the own sound board reads the program's data from the game's assets (the samples they hold are the
		// ROM set's, byte for byte: --assets-check)
		if (const char *problem = assets_load(own_assets_folder.c_str()))
		{
			std::cerr << "The assets in " << own_assets_folder << " do not load: " << problem << '\n';
			return 1;
		}
		g_sound_board = true;
#else
		std::cerr << "This kref has no game sound board to compare\n";
		return 1;
#endif
	}
	tmnt_reset();
	if (service)
		tmnt_board.dsw[2] &= uint8_t(~TMNT_DSW3_SERVICE);
	tmnt_script *script = nullptr;
	if (!script_path.empty())
	{
		char error[256];
		script = tmnt_script_load(script_path.c_str(), error, sizeof(error));
		if (!script)
		{
			std::cerr << error << '\n';
			return 1;
		}
		if (!frames_given && tmnt_script_frames(script))
			frames = tmnt_script_frames(script);
		tmnt_script_apply(script, 0);
	}

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
	g_record_rom_reads = !rom_reads_path.empty();
	cpu.start();
	cpu.reset();

	std::ofstream hashes, video_hashes;
	if (!hashes_path.empty())
		hashes.open(hashes_path);
	if (!video_hashes_path.empty())
		video_hashes.open(video_hashes_path);
	// --video-check: the game's own video (runtime/src/video.c) draws every frame too, and the two are compared
	std::ofstream video_check;
	if (!video_check_path.empty())
		video_check.open(video_check_path);
	static tmnt_frame own_frame;
	uint64_t video_differences = 0, first_difference = 0;
	const bool video = video_hashes.is_open() || !video_dump_frames.empty() || video_check.is_open();
	if (video)
		kv_attach();
	if (video_check.is_open())
		tmnt_video_reset();
	std::ofstream sound_hashes;
	if (!sound_hashes_path.empty())
		sound_hashes.open(sound_hashes_path);
	std::vector<int16_t> sound_mix;
	const bool sound = sound_hashes.is_open() || !sound_wav_path.empty() || !sound_trace_path.empty()
		|| !sound_rom_reads_path.empty() || !sound_commands_path.empty() || sound_check || g_sound_board;
	std::ofstream sound_board;
	uint64_t sound_board_differences = 0, sound_board_first = 0;
	if (sound)
	{
		ks_attach();
		if (!sound_timing_path.empty() && !ks_use_timing(sound_timing_path.c_str()))
		{
			std::cerr << sound_timing_path << ": no timing model in it\n";
			return 1;
		}
		if (sound_check)
			ks_check_chips();
#ifdef K_GAME_SOUND_BOARD
		if (g_sound_board)
		{
			if (const char *problem = tmnt_sound_init())
			{
				std::cerr << "The game's sound board does not start: " << problem << '\n';
				return 1;
			}
			sound_board.open(sound_board_path);
			g_reference_sound_irq = tmnt_board.sound_irq;
			tmnt_board.sound_irq = raise_both_sound_irqs;
		}
#endif
		tmnt_board.sound_sync = sound_sync;
		if (!sound_rom_reads_path.empty())
			ks_record_rom_reads();
		if (!sound_commands_path.empty())
		{
			g_raise_sound_irq = tmnt_board.sound_irq;
			tmnt_board.sound_irq = log_sound_command;
		}
	}

	int status = 0;
	uint64_t frame = 0;
	// VBLANK ends (line 16): the video takes the scroll
	auto vblank_end = [&]() {
		if (video)
			kv_vblank_end();
		if (video_check.is_open())
			tmnt_video_vblank_end();
	};
	// VBLANK begins (line 240): the frame is drawn, the board takes its events and the controls change, and
	// the frame's hashes are taken
	auto vblank_start = [&]() {
		if (video)
			kv_vblank_start(video_hashes.is_open() || video_dump_frames.count(frame) != 0 || video_check.is_open());
		if (video_check.is_open())
		{
			tmnt_video_vblank_start(&own_frame);
			const uint64_t reference = kv_frame_hash(), own = tmnt_video_hash(&own_frame);
			char line[64];
			std::snprintf(line, sizeof(line), "%" PRIu64 " %016" PRIx64 " %016" PRIx64 "\n", frame, reference, own);
			video_check << line;
			if (reference != own && video_differences++ == 0)
			{
				first_difference = frame;
				write_bmp(video_folder + "/reference_" + std::to_string(frame) + ".bmp");
				write_bmp(video_folder + "/own_" + std::to_string(frame) + ".bmp", own_frame.pixels);
			}
		}
		tmnt_vblank_start();
		tmnt_script_apply(script, tmnt_board.frame);
		sync_irq();
		if (sound)
		{
			ks_end_frame(tmnt_board.cycles);
			if (sound_hashes.is_open())
			{
				char line[64];
				std::snprintf(line, sizeof(line), "%" PRIu64 " %016" PRIx64 " %016" PRIx64 "\n", frame,
					ks_frame_samples_hash(), ks_state_hash());
				sound_hashes << line;
			}
#ifdef K_GAME_SOUND_BOARD
			if (g_sound_board)
			{
				// the game's own sound board's frame against the reference's
				tmnt_sound_end_frame(tmnt_board.cycles);
				const uint64_t samples = ks_frame_samples_hash(), ram = ks_state_hash();
				const uint64_t own_samples = tmnt_sound_samples_hash(), own_ram = tmnt_sound_state_hash();
				char line[96];
				std::snprintf(line, sizeof(line), "%" PRIu64 " %016" PRIx64 " %016" PRIx64 " %016" PRIx64 " %016" PRIx64 "\n",
					frame, samples, ram, own_samples, own_ram);
				sound_board << line;
				if ((samples != own_samples || ram != own_ram) && sound_board_differences++ == 0)
				{
					sound_board_first = frame;
					std::cerr << "sound board check, frame " << frame << ": the " << (samples != own_samples ? "samples" : "")
						<< (samples != own_samples && ram != own_ram ? " and the " : "") << (ram != own_ram ? "Z80's RAM" : "")
						<< " differ; Z80 cycles " << ks_z80_cycles() << " and " << tmnt_sound_z80_cycles() << '\n';
					static const char *const sources[4] = { "YM2151", "K007232", "uPD7759", "title music" };
					uint64_t reference_sources[4], own_sources[4];
					ks_frame_source_hashes(reference_sources);
					tmnt_sound_source_hashes(own_sources);
					for (int source = 0; source < 4; source++)
						if (reference_sources[source] != own_sources[source])
							std::cerr << "  the " << sources[source] << "'s samples differ\n";
					int shown = 0;
					for (size_t at = 0; at < sizeof(tmnt_board.sound_ram) && shown < 16; at++)
						if (tmnt_board.sound_ram[at] != tmnt_sound.ram[at]
							&& (0x8000u + at < TMNT_SOUND_STACK_START || 0x8000u + at >= TMNT_SOUND_STACK_END))
						{
							char bytes[48];
							std::snprintf(bytes, sizeof(bytes), "  %04X: %02X and %02X\n", unsigned(0x8000u + at),
								tmnt_board.sound_ram[at], tmnt_sound.ram[at]);
							std::cerr << bytes;
							shown++;
						}
				}
			}
#endif
			if (!sound_wav_path.empty())
			{
				size_t count = 0;
				const int16_t *mix = ks_frame_mix(&count);
				sound_mix.insert(sound_mix.end(), mix, mix + count);
			}
		}
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
	};
	try
	{
		if (g_frame_timing)
		{
			// the board's next frame where the program has nothing to do, as the translated program has it
			frame = 1;
			g_frame = frame;
			g_frame_wait = [&]() {
				const uint64_t start = (frame - 1) * TMNT_FRAME_CYCLES;
				tmnt_board.cycles = start + uint64_t(TMNT_VBLANK_END) * TMNT_LINE_CYCLES;
				vblank_end();
				tmnt_board.cycles = start + uint64_t(TMNT_VBLANK_START) * TMNT_LINE_CYCLES;
				vblank_start();
				if (frame == frames)
					throw frames_done();
				g_frame = ++frame;
			};
			try
			{
				if (frames)
					for (;;)
						cpu.run(1 << 20);
			}
			catch (const frames_done &)
			{
			}
		}
		else
		{
			for (frame = 1; frame <= frames; frame++)
			{
				g_frame = frame;
				const uint64_t start = (frame - 1) * TMNT_FRAME_CYCLES;
				run_until(cpu, start + uint64_t(TMNT_VBLANK_END) * TMNT_LINE_CYCLES);
				vblank_end();
				run_until(cpu, start + uint64_t(TMNT_VBLANK_START) * TMNT_LINE_CYCLES);
				vblank_start();
			}
			frame--;
		}
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
	if (video_check.is_open())
	{
		std::cout << "video check:       " << video_differences << " frames differ";
		if (video_differences)
			std::cout << ", the first " << first_difference;
		std::cout << '\n';
	}
	if (sound_check)
	{
		static const char *const chips[4] = { "YM2151", "K007232", "uPD7759", "title music" };
		uint64_t compared[4], differences[4], first[4];
		ks_chip_report(compared, differences, first);
		for (int chip = 0; chip < 4; chip++)
			if (compared[chip])
			{
				std::cout << "sound check:       " << chips[chip] << ": " << differences[chip] << " of " << compared[chip]
					<< " samples differ";
				if (differences[chip])
					std::cout << ", the first " << first[chip];
				std::cout << '\n';
			}
	}
	if (!trace_path.empty())
		write_addresses(trace_path, g_executed, "68000 addresses executed by kref");
	if (!calls_path.empty())
		write_addresses(calls_path, g_called, "68000 addresses reached by jsr or bsr");
	if (!jumps_path.empty())
	{
		std::ofstream out(jumps_path);
		out << "# where each 68000 jmp or jsr through a register went, by kref: FROM TO\n";
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
		std::cout << "Z80 cycles:        " << ks_z80_cycles() << '\n';
	if (g_sound_board)
	{
		std::cout << "sound board check: " << sound_board_differences << " frames differ";
		if (sound_board_differences)
			std::cout << ", the first " << sound_board_first;
		std::cout << '\n';
	}
	if (!sound_wav_path.empty())
		write_wav(sound_wav_path, sound_mix, ks_mix_rate());
	if (!sound_trace_path.empty())
	{
		std::ofstream out(sound_trace_path);
		out << "# Z80 addresses executed by kref, one per line\n";
		for (uint32_t address = 0; address < 0x10000; address++)
			if (ks_executed()[address >> 3] & (1u << (address & 7)))
			{
				char line[16];
				std::snprintf(line, sizeof(line), "%04X\n", address);
				out << line;
			}
	}
	if (!sound_rom_reads_path.empty())
	{
		// by instruction, runs of consecutive bytes, as --rom-reads writes the 68000's
		std::ofstream out(sound_rom_reads_path);
		out << "# Z80 sound program bytes read as data by kref: PC FIRST-LAST\n";
		size_t count = 0;
		const uint32_t *reads = ks_rom_reads(&count);
		for (size_t index = 0; index < count;)
		{
			const uint32_t pc = reads[index] >> 16, first = reads[index] & 0xFFFF;
			uint32_t last = first;
			while (++index < count && reads[index] >> 16 == pc && (reads[index] & 0xFFFF) == last + 1)
				last++;
			char line[24];
			std::snprintf(line, sizeof(line), "%04X %04X-%04X\n", unsigned(pc), unsigned(first), unsigned(last));
			out << line;
		}
	}
	if (!sound_commands_path.empty())
	{
		std::ofstream out(sound_commands_path);
		out << "# the sound commands the 68000 gave the Z80 in kref: FRAME COMMAND\n";
		for (const auto &[at, command] : g_sound_commands)
		{
			char line[32];
			std::snprintf(line, sizeof(line), "%" PRIu64 " %02X\n", at, unsigned(command));
			out << line;
		}
	}
	tmnt_script_free(script);
	return status;
}
