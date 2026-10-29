/**
 *    Name:        ksound.cpp
 *    Description: reference sound board of TMNT: MAME's Z80, K007232 and uPD7759, ymfm's YM2151 and the
 *                 title music, on the board model
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    MAME's files are compiled here, unchanged, inside a namespace of their own (ks), over the stand-ins of
 *    this folder (emu.h, dirom.h, machine/z80daisy.h, logmacro.h, wavwrite.h): the Z80 core is
 *    system32/reference/mame/z80's (MAME's, as 0.289 has it but for its debugger's IR register), the
 *    K007232 and the uPD7759 MAME 0.289's. ymfm is a library of its own and builds as it is. What tmnt.cpp
 *    adds (MAME 0.289, Nicola Salmoria, BSD-3-Clause) is written below as it has it: the K007232's volume
 *    callback, the title music (tmnt_decode_sample, sres_w) and the routes' gains.
 *
 *    The Z80's time is kept one of two ways:
 *      - free: the Z80 runs cycle by cycle in slices that end where the board's time asks, or at the YM2151's
 *        timers (a timer set while it runs cuts its slice short), and takes the 68000's IRQ as MAME's
 *        HOLD_LINE does: the line stays up until the Z80 takes it;
 *      - the timing model (ks_use_timing, the table tools/decompile_sound.py --timing writes): the rules of
 *        the game's own sound board (runtime/src/sound_cpu.c), kept around MAME's core by its instruction
 *        hook, as system32/reference/sound/s32sound.cpp keeps them for System 32. The time is the model's:
 *        every block of the table adds its cycles when it starts, and a condition's longer way its extra
 *        ones; the timers run out and the IRQ is taken only where a block starts (this code pushes the
 *        address, drops the held line and jumps to 0038 itself: the core never sees the line); every access
 *        happens at its block's start; the board stops the Z80 where the main loop's top finds its time past
 *        the board's. The game's own sound board keeps the same rules, so the two compare sample for sample.
 *    The title music is MAME's samples device at its own rate (20 kHz: a sample of the ROM each step), not
 *    resampled to an output rate. The Z80's RAM is hashed without its stack (TMNT_SOUND_STACK_START-END,
 *    tmnt/sound_board.h), which the decompiled program does not keep.
 */
#include "ksound.h"

#include "game/data.h"
#include "tmnt/machine.h"
#include "tmnt/sound_board.h"
#include "tmnt/sound_chips.h"
#include "tmnt/ym2151.h"

#include "ymfm_opm.h"

#include <algorithm>
#include <bit>
#include <cassert>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <functional>
#include <list>
#include <map>
#include <memory>
#include <ostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <set>
#include <type_traits>
#include <utility>
#include <vector>

namespace ks
{
#include "emu.h"
#include "logmacro.h"
#include "z80.cpp"
#include "z80dasm.cpp"
#include "../mame/sound/k007232.cpp"
#include "../mame/sound/upd7759.cpp"
}

namespace
{

constexpr uint64_t kBoardClock = TMNT_CPU_CLOCK;
constexpr uint64_t kSoundClock = TMNT_SOUND_CLOCK;
constexpr size_t kTitleSamples = 0x40000;

// a source's rate: num / den samples a second
struct rate
{
	uint64_t num, den;
};
constexpr rate kFmRate{ kSoundClock, 64 };
constexpr rate kPcmRate{ kSoundClock, 128 };
constexpr rate kSpeechRate{ TMNT_SPEECH_CLOCK, 4 };
constexpr rate kTitleRate{ TMNT_SPEECH_CLOCK, 32 };

// the board's time when the Z80 has run 'cycles', and the first Z80 cycle at or after board time 'time'
uint64_t board_time(uint64_t cycles)
{
	return cycles * kBoardClock / kSoundClock;
}

uint64_t z80_time(uint64_t time)
{
	return (time * kSoundClock + kBoardClock - 1) / kBoardClock;
}

// how many of a source's samples start before Z80 cycle 'cycle', and which one is playing at it
uint64_t samples_before(uint64_t cycle, rate source)
{
	return (cycle * source.num + kSoundClock * source.den - 1) / (kSoundClock * source.den);
}

uint64_t sample_at(uint64_t cycle, rate source)
{
	return cycle * source.num / (kSoundClock * source.den);
}

// ---- time: the Z80's cycles, and while the Z80 runs, where its slice started and how long it is ----

uint64_t g_z80_time = 0;
bool g_running = false;
uint64_t g_slice_start = 0;
int g_slice_budget = 0;
uint64_t g_now_override = UINT64_MAX;       // while a timer fires, its own time

// ---- the timing model ----

constexpr int kInterruptCycles = 13;        // the interrupt's acknowledge, in the timing model

struct block_timing
{
	int cycles;
	uint16_t last;          // the block's last instruction
	uint8_t length;         // its length
	int extra;              // its condition's longer way
};

struct
{
	bool on = false;
	std::map<uint16_t, block_timing> blocks;
	std::set<uint16_t> repeats;
	uint16_t loop = 0;
	uint16_t irq = 0;
	uint64_t clock = 0;             // where the next block starts, Z80 cycles
	uint64_t access = 0;            // when the current block's accesses happen
	uint64_t until = 0;             // where the board stops the Z80 (board cycles)
	bool stop = false;              // the hook stopped the core at the loop's top
	bool rewind = false;            // the core fetched the loop top's opcode before it stopped
	bool line = false;              // the 68000's IRQ, held until the Z80 takes it
	int previous = -1;              // the instruction before this one
	uint8_t previous_opcode = 0;
	const block_timing *current = nullptr;
} g_model;

class sound_cpu : public ks::z80_device
{
public:
	explicit sound_cpu(const ks::machine_config &config) : ks::z80_device(config, "audiocpu", nullptr, TMNT_SOUND_CLOCK) {}

	// how many of the slice's cycles have gone by
	int cycles_done() const { return g_slice_budget - cycles_left(); }

	// end the running slice early, with no more than 'left' cycles to go: how many it took off
	int cut_slice(int left)
	{
		const int taken = cycles_left() - left;
		if (taken <= 0)
			return 0;
		m_icount -= taken;
		m_cut += taken;
		return taken;
	}
	int run_slice(int cycles)
	{
		m_cut = 0;
		const int done = run(cycles);
		return done - m_cut;
	}

	// the timing model takes the interrupt itself, as the core's take_interrupt does in interrupt mode 1:
	// the return address on the stack, IFF1 and IFF2 off, the refresh register counted up, on to the handler
	void interrupt_to(uint16_t handler)
	{
		m_sp.w = uint16_t(m_sp.w - 1);
		tmnt_z80_write(m_sp.w, m_pc.b.h);
		m_sp.w = uint16_t(m_sp.w - 1);
		tmnt_z80_write(m_sp.w, m_pc.b.l);
		m_pc.w = handler;
		m_iff1 = m_iff2 = false;
		m_r++;
	}

	bool interrupts_on() const { return m_iff1; }

	// back to the start of the instruction whose opcode the core read as the slice ended: the read's
	// step ran out of cycles before the fetch counted (no PC++, no refresh count), so only the rest of
	// the instruction is dropped
	void restart_at(uint16_t address)
	{
		m_pc.w = address;
		m_ref = 0xffff00;
	}

private:
	int m_cut = 0;
};

ks::machine_config g_config;
std::unique_ptr<sound_cpu> g_z80;
bool g_irq_held = false;
uint8_t g_executed[0x10000 / 8];
uint16_t g_instruction = 0;     // the address of the instruction the Z80 runs
// ks_record_rom_reads: (instruction << 16 | address) of the Z80's data reads of its program ROM
bool g_record_rom_reads = false;
std::set<uint32_t> g_rom_read_set;
std::vector<uint32_t> g_rom_reads;

uint64_t now()
{
	if (g_now_override != UINT64_MAX)
		return g_now_override;
	if (g_model.on)
		return g_running ? g_model.access : g_model.clock;
	return g_running ? g_slice_start + uint64_t(g_z80->cycles_done()) : g_z80_time;
}

// ---- a source's samples: 'outputs' channels interleaved, from sample number 'first' on ----

struct samples
{
	int outputs = 1;
	std::vector<float> data;
	uint64_t first = 0;
	std::vector<float> last;          // the last sample taken out, for the mix
	uint64_t made = 0;                // samples made so far

	explicit samples(int count) : outputs(count), last(size_t(count), 0.0f) {}
	void push(const float *values)
	{
		data.insert(data.end(), values, values + outputs);
		made++;
	}
};

// --sound-check: the game's own chips (runtime/src) fed the same writes, their samples compared with these
// (0 the YM2151, 1 the K007232, 2 the uPD7759, 3 the title music)
bool g_check_chips = false;
ym2151 g_own_fm;
k007232 g_own_pcm;
upd7759 g_own_speech;
title_music g_own_title;
uint64_t g_chip_samples[4] = { 0, 0, 0, 0 };
uint64_t g_chip_differences[4] = { 0, 0, 0, 0 };
uint64_t g_first_difference[4] = { 0, 0, 0, 0 };

samples g_fm_samples(2);
samples g_pcm_samples(2);
samples g_speech_samples(1);
samples g_title_samples(1);

// ---- the YM2151 (its IRQ goes nowhere on this board) ----

class fm_chip : public ymfm::ymfm_interface
{
public:
	fm_chip() : m_chip(*this) { m_chip.reset(); }

	// every sample that starts before Z80 cycle 'cycle', as MAME's ymfm device puts them (x / 32768)
	void catch_up(uint64_t cycle)
	{
		const uint64_t due = samples_before(cycle, kFmRate);
		ymfm::ym2151::output_data output;
		while (g_fm_samples.made < due)
		{
			m_chip.generate(&output);
			if (g_check_chips)
			{
				// the game's own YM2151 (runtime/src/ym2151.c), sample for sample
				int32_t own[2];
				ym2151_generate(&g_own_fm, own);
				if (own[0] != output.data[0] || own[1] != output.data[1])
				{
					if (g_chip_differences[0]++ == 0)
						g_first_difference[0] = g_fm_samples.made;
				}
				g_chip_samples[0]++;
			}
			const float values[2] = { float(output.data[0]) * (1.0f / 32768.0f), float(output.data[1]) * (1.0f / 32768.0f) };
			g_fm_samples.push(values);
		}
	}
	uint8_t read(int offset)
	{
		catch_up(now());
		return m_chip.read(uint32_t(offset));
	}
	void write(int offset, uint8_t data)
	{
		catch_up(now());
		m_chip.write(uint32_t(offset), data);
		if (g_check_chips)
			ym2151_write(&g_own_fm, offset, data);
	}
	uint64_t next_timer() const { return std::min(m_deadline[0], m_deadline[1]); }
	// the timers due by Z80 cycle 'cycle', in the order they run out
	void fire(uint64_t cycle)
	{
		while (next_timer() <= cycle)
		{
			const int number = m_deadline[0] <= m_deadline[1] ? 0 : 1;
			const uint64_t when = m_deadline[number];
			m_deadline[number] = UINT64_MAX;
			catch_up(when);
			g_now_override = when;
			m_engine->engine_timer_expired(uint32_t(number));
			if (g_check_chips)
				ym2151_timer_expired(&g_own_fm, int(number));
			g_now_override = UINT64_MAX;
		}
	}

	// ymfm_interface
	void ymfm_set_timer(uint32_t number, int32_t clocks) override
	{
		if (clocks < 0)
		{
			m_deadline[number] = UINT64_MAX;
			return;
		}
		m_deadline[number] = now() + uint64_t(clocks);
		// free timing: a running Z80 stops by then, for the timer to run out on time
		if (!g_model.on && g_running && g_now_override == UINT64_MAX
			&& m_deadline[number] < g_slice_start + uint64_t(g_slice_budget))
		{
			const uint64_t at = g_slice_start + uint64_t(g_z80->cycles_done());
			g_slice_budget -= g_z80->cut_slice(m_deadline[number] > at ? int(m_deadline[number] - at) : 0);
		}
	}
	void ymfm_set_busy_end(uint32_t clocks) override
	{
		m_busy_until = now() + clocks;
	}
	bool ymfm_is_busy() override
	{
		return now() < m_busy_until;
	}

private:
	ymfm::ym2151 m_chip;
	uint64_t m_deadline[2] = { UINT64_MAX, UINT64_MAX };
	uint64_t m_busy_until = 0;
};

std::unique_ptr<fm_chip> g_fm;

// ---- the K007232 and the uPD7759: MAME's, their streams pulled up to the present before each change ----

template <typename Device>
class streamed : public Device
{
public:
	streamed(const ks::machine_config &config, const char *tag, uint32_t clock, rate source, samples &out)
		: Device(config, tag, nullptr, clock), m_rate(source), m_out(out)
	{
		this->stream().m_before_change = [this]() { catch_up(now()); };
	}
	void catch_up(uint64_t cycle)
	{
		const uint64_t due = samples_before(cycle, m_rate);
		if (due <= m_out.made)
			return;
		const int count = int(due - m_out.made);
		this->generate(count);
		if (g_check_chips && m_check)
			m_check(count, this->stream().m_outputs);
		for (int index = 0; index < count; index++)
		{
			float values[2];
			for (int output = 0; output < m_out.outputs; output++)
				values[output] = this->stream().m_outputs[output][size_t(index)];
			m_out.push(values);
		}
	}
	// --sound-check: the game's own chip makes the same block, and its samples are compared
	std::function<void(int count, const std::vector<float> *outputs)> m_check;

private:
	rate m_rate;
	samples &m_out;
};

std::unique_ptr<streamed<ks::k007232_device>> g_pcm;
std::unique_ptr<streamed<ks::upd7759_device>> g_speech;

// ---- the title music: tmnt.cpp's samples device ----

struct title_music
{
	std::vector<int16_t> data = std::vector<int16_t>(kTitleSamples);
	bool playing = false;
	size_t position = 0;

	// tmnt_decode_sample: the ROM's samples are in Yamaha's floating-point format
	void decode()
	{
		for (size_t index = 0; index < kTitleSamples; index++)
		{
			const int value = game_title[2 * index] + game_title[2 * index + 1] * 256;
			data[index] = ymfm::decode_fp(int16_t(value >> 3));
		}
	}
	void catch_up(uint64_t cycle)
	{
		const uint64_t due = samples_before(cycle, kTitleRate);
		while (g_title_samples.made < due)
		{
			float value = 0.0f;
			if (playing)
			{
				value = float(data[position]) * (1.0f / 32768.0f);
				if (++position == kTitleSamples)
					playing = false;
			}
			if (g_check_chips)
			{
				if (title_music_generate(&g_own_title) != value && g_chip_differences[3]++ == 0)
					g_first_difference[3] = g_title_samples.made;
				g_chip_samples[3]++;
			}
			g_title_samples.push(&value);
		}
	}
	void start()
	{
		catch_up(now());
		if (!playing)
		{
			playing = true;
			position = 0;
		}
	}
	void stop()
	{
		catch_up(now());
		playing = false;
	}
};

title_music g_title;

// ---- the board's hooks ----

uint8_t chip_read(int chip, int offset)
{
	switch (chip)
	{
	case TMNT_CHIP_PCM:
	{
		const uint8_t value = g_pcm->read(ks::offs_t(offset));
		if (g_check_chips)
			k007232_read(&g_own_pcm, offset);
		return value;
	}
	case TMNT_CHIP_FM: return g_fm->read(offset);
	case TMNT_CHIP_SPEECH_BUSY: return g_speech->busy_r() ? 1 : 0;    // tmnt_upd_busy_r
	default: return 0;
	}
}

void chip_write(int chip, int offset, uint8_t data)
{
	switch (chip)
	{
	case TMNT_CHIP_PCM:
		g_pcm->write(ks::offs_t(offset), data);
		if (g_check_chips)
			k007232_write(&g_own_pcm, offset, data);
		break;
	case TMNT_CHIP_FM:
		g_fm->write(offset, data);
		break;
	case TMNT_CHIP_SPEECH:
		g_speech->port_w(data);
		if (g_check_chips)
			upd7759_port_write(&g_own_speech, data);
		break;
	case TMNT_CHIP_SPEECH_START:
		g_speech->start_w(!(data & 1));        // tmnt_upd_start_w
		if (g_check_chips)
			upd7759_start_write(&g_own_speech, !(data & 1));
		break;
	case TMNT_CHIP_SOUND_RESET:
		// sres_w: bit 1 the uPD7759's reset, bit 2 the title music
		g_speech->reset_w((data >> 1) & 1);
		if (g_check_chips)
			upd7759_reset_write(&g_own_speech, (data >> 1) & 1);
		if (data & 4)
			g_title.start();
		else
			g_title.stop();
		if (g_check_chips)
		{
			if (data & 4)
				title_music_start(&g_own_title);
			else
				title_music_stop(&g_own_title);
		}
		break;
	default:
		break;
	}
}

// the 68000 raises the Z80's IRQ, held until the Z80 takes it
void raise_irq()
{
	g_irq_held = true;
	if (g_model.on)
		g_model.line = true;
	else
		g_z80->set_input_line(ks::INPUT_LINE_IRQ0, ks::ASSERT_LINE);
}

// ---- the timing model's hook ----

// a block starts at 'pc': the timers due, the IRQ if the Z80 takes it, then the block's time
void start_block(uint16_t pc)
{
	for (;;)
	{
		g_fm->fire(g_model.clock);
		if (!g_z80->interrupts_on() || g_model.previous_opcode == 0xFB || !g_model.line)
			break;
		// the interrupt: its cycles, the held line down, then the handler's first block starts
		g_z80->interrupt_to(g_model.irq);
		g_model.line = false;
		g_irq_held = false;
		pc = g_model.irq;
		g_executed[pc >> 3] |= uint8_t(1u << (pc & 7));
		g_instruction = pc;
		g_model.clock += kInterruptCycles;
		g_model.previous_opcode = 0;
	}
	const auto found = g_model.blocks.find(pc);
	if (found == g_model.blocks.end())
		throw std::runtime_error("the Z80 starts a block the timing model does not have");
	g_model.current = &found->second;
	g_model.access = g_model.clock;
	g_model.clock += uint64_t(found->second.cycles);
}

void on_instruction(ks::offs_t address)
{
	const uint16_t pc = uint16_t(address);
	g_executed[pc >> 3] |= uint8_t(1u << (pc & 7));
	g_instruction = pc;
	if (!g_model.on)
		return;
	const int previous = g_model.previous;
	g_model.previous = pc;
	if (previous == pc && g_model.repeats.count(pc))
	{
		g_model.clock += 21;    // another round of a block move
		return;
	}
	if (g_model.blocks.count(pc))
	{
		// the way the last block left: its condition's longer way adds its extra cycles
		const block_timing *last = g_model.current;
		if (last != nullptr && previous == last->last && pc != uint16_t(last->last + last->length))
			g_model.clock += uint64_t(last->extra);
		if (pc == g_model.loop && board_time(g_model.clock) >= g_model.until)
		{
			// the main loop's top with the board's time reached: the board takes over, and when the Z80
			// goes on, the loop's top starts its block (its way in is counted already)
			g_model.stop = true;
			g_model.rewind = true;
			g_model.previous = previous;
			g_model.current = nullptr;
			g_slice_budget -= g_z80->cut_slice(0);
			return;
		}
		start_block(pc);
	}
	g_model.previous_opcode = tmnt_z80_read(pc);
}

// a data read of the Z80's (not an opcode or an argument fetch): the program ROM's bytes are noted with the
// instruction that reads them
uint8_t data_read(ks::offs_t address)
{
	if (g_record_rom_reads && address < GAME_SOUND_SIZE)
		g_rom_read_set.insert((uint32_t(g_instruction) << 16) | uint32_t(address));
	return tmnt_z80_read(uint16_t(address));
}

// the Z80 and the YM2151's timers up to Z80 cycle 'until'
void run_to(uint64_t until)
{
	while (g_z80_time < until)
	{
		g_fm->fire(g_z80_time);
		const uint64_t end = std::min(until, std::max(g_fm->next_timer(), g_z80_time + 1));
		g_slice_start = g_z80_time;
		g_slice_budget = int(end - g_z80_time);
		g_running = true;
		const int done = g_z80->run_slice(g_slice_budget);
		g_running = false;
		g_z80_time += uint64_t(std::max(done, 0));
	}
	g_fm->fire(g_z80_time);
}

// the timing model: the Z80 up to the main loop's top past board time 'until'
void run_model(uint64_t until)
{
	if (g_model.rewind)
	{
		if (board_time(g_model.clock) >= until)
			return;
		g_z80->restart_at(g_model.loop);
		g_model.rewind = false;
	}
	g_model.until = until;
	g_model.stop = false;
	while (!g_model.stop)
	{
		g_slice_start = g_model.clock;
		g_slice_budget = 1 << 20;
		g_running = true;
		g_z80->run_slice(g_slice_budget);
		g_running = false;
	}
}

// ---- recording ----

uint64_t g_frame_samples_hash = 0;
uint64_t g_source_hashes[4] = { 0, 0, 0, 0 };
std::vector<int16_t> g_mix;

uint64_t hash_bytes(const void *data, size_t size, uint64_t hash)
{
	const uint8_t *bytes = static_cast<const uint8_t *>(data);
	for (size_t index = 0; index < size; index++)
		hash = (hash ^ bytes[index]) * 0x100000001b3ull;
	return hash;
}

// the source's samples that start before 'due' out of it, into the hash
uint64_t take(samples &source, uint64_t due, uint64_t hash, std::vector<float> &out)
{
	const size_t count = size_t(std::min<uint64_t>(due - source.first, source.data.size() / size_t(source.outputs))) * size_t(source.outputs);
	out.assign(source.data.begin(), source.data.begin() + ptrdiff_t(count));
	source.data.erase(source.data.begin(), source.data.begin() + ptrdiff_t(count));
	source.first += count / size_t(source.outputs);
	return hash_bytes(out.data(), out.size() * sizeof(float), hash);
}

// a source's value at sample 'index' of what was taken (from 'first' on), or the one before it
float value_at(const std::vector<float> &taken, uint64_t first, uint64_t index, int outputs, int output, const samples &source)
{
	if (index < first)
		return source.last[size_t(output)];
	const size_t at = size_t(index - first) * size_t(outputs) + size_t(output);
	return at < taken.size() ? taken[at] : source.last[size_t(output)];
}

void remember_last(samples &source, const std::vector<float> &taken)
{
	if (taken.size() >= size_t(source.outputs))
		std::copy(taken.end() - source.outputs, taken.end(), source.last.begin());
}

} // namespace

extern "C" void ks_attach(void)
{
	ks::set_region("k007232", game_pcm, GAME_PCM_SIZE);
	ks::set_region("upd", game_speech, GAME_SPEECH_SIZE);

	g_z80 = std::make_unique<sound_cpu>(g_config);
	g_z80->space(ks::AS_PROGRAM).read = [](ks::offs_t address) { return tmnt_z80_read(uint16_t(address)); };
	g_z80->space(ks::AS_PROGRAM).write = [](ks::offs_t address, uint8_t data) { tmnt_z80_write(uint16_t(address), data); };
	g_z80->space(ks::AS_PROGRAM).data_read = data_read;
	g_z80->space(ks::AS_IO).read = [](ks::offs_t) { return uint8_t(0); };
	g_z80->space(ks::AS_IO).write = [](ks::offs_t, uint8_t) {};
	// HOLD_LINE: the line goes down as the Z80 takes the interrupt (interrupt mode 1 reads no vector; with the
	// timing model the core never sees the line)
	g_z80->irq_vector = []() {
		if (g_irq_held)
		{
			g_irq_held = false;
			g_z80->set_input_line(ks::INPUT_LINE_IRQ0, ks::CLEAR_LINE);
		}
		return 0xff;
	};
	g_z80->instruction_hook = on_instruction;
	g_z80->start();
	g_z80->reset();

	g_fm = std::make_unique<fm_chip>();
	g_pcm = std::make_unique<streamed<ks::k007232_device>>(g_config, "k007232", TMNT_SOUND_CLOCK, kPcmRate, g_pcm_samples);
	// tmnt.cpp's volume_callback: the K007232's port sets its two channels' volumes
	g_pcm->port_write().set([](ks::offs_t, uint8_t data) {
		g_pcm->set_volume(0, (data >> 4) * 0x11, 0);
		g_pcm->set_volume(1, 0, (data & 0x0f) * 0x11);
	});
	static_cast<ks::device_t &>(*g_pcm).start();    // (the K007232 has a start of its own)
	static_cast<ks::device_t &>(*g_pcm).reset();
	g_speech = std::make_unique<streamed<ks::upd7759_device>>(g_config, "upd", TMNT_SPEECH_CLOCK, kSpeechRate, g_speech_samples);
	g_speech->start();
	g_speech->reset();
	g_title.decode();

	tmnt_board.sound_chip_read = chip_read;
	tmnt_board.sound_chip_write = chip_write;
	tmnt_board.sound_irq = raise_irq;
	g_z80_time = z80_time(tmnt_board.cycles);
	g_model.clock = g_model.access = g_z80_time;
}

extern "C" bool ks_use_timing(const char *path)
{
	std::ifstream file(path);
	if (!file)
		return false;
	std::string line;
	while (std::getline(file, line))
	{
		std::istringstream words(line);
		std::string kind;
		words >> kind;
		unsigned address = 0;
		if (kind == "loop")
			words >> std::hex >> address, g_model.loop = uint16_t(address);
		else if (kind == "irq")
			words >> std::hex >> address, g_model.irq = uint16_t(address);
		else if (kind == "repeat")
			words >> std::hex >> address, g_model.repeats.insert(uint16_t(address));
		else if (kind == "block")
		{
			block_timing block{};
			unsigned last = 0, length = 0;
			words >> std::hex >> address >> std::dec >> block.cycles >> std::hex >> last >> std::dec >> length >> block.extra;
			block.last = uint16_t(last);
			block.length = uint8_t(length);
			g_model.blocks[uint16_t(address)] = block;
		}
	}
	g_model.on = !g_model.blocks.empty();
	return g_model.on;
}

extern "C" void ks_run_until(uint64_t time)
{
	if (g_model.on)
		run_model(time);
	else
		run_to(z80_time(time));
}

extern "C" void ks_end_frame(uint64_t time)
{
	const uint64_t cycle = z80_time(time);
	if (g_model.on)
		run_model(time);
	else
		run_to(cycle);
	g_fm->catch_up(cycle);
	g_pcm->catch_up(cycle);
	g_speech->catch_up(cycle);
	g_title.catch_up(cycle);

	// the samples that start before the frame's end
	const uint64_t fm_first = g_fm_samples.first, pcm_first = g_pcm_samples.first;
	const uint64_t speech_first = g_speech_samples.first, title_first = g_title_samples.first;
	std::vector<float> fm, pcm, speech, title;
	uint64_t hash = 0xcbf29ce484222325ull;
	hash = take(g_fm_samples, samples_before(cycle, kFmRate), hash, fm);
	hash = take(g_pcm_samples, samples_before(cycle, kPcmRate), hash, pcm);
	hash = take(g_speech_samples, samples_before(cycle, kSpeechRate), hash, speech);
	g_frame_samples_hash = take(g_title_samples, samples_before(cycle, kTitleRate), hash, title);
	g_source_hashes[0] = hash_bytes(fm.data(), fm.size() * sizeof(float), 0xcbf29ce484222325ull);
	g_source_hashes[1] = hash_bytes(pcm.data(), pcm.size() * sizeof(float), 0xcbf29ce484222325ull);
	g_source_hashes[2] = hash_bytes(speech.data(), speech.size() * sizeof(float), 0xcbf29ce484222325ull);
	g_source_hashes[3] = hash_bytes(title.data(), title.size() * sizeof(float), 0xcbf29ce484222325ull);

	// the mix, at the YM2151's rate (tmnt.cpp's routes to its one speaker)
	g_mix.clear();
	for (size_t index = 0; index < fm.size() / 2; index++)
	{
		const uint64_t at = (fm_first + index) * kFmRate.den;    // the Z80 cycle this sample starts at
		const float mixed = 0.40f * (fm[index * 2] + fm[index * 2 + 1])
			+ 0.15f * (value_at(pcm, pcm_first, sample_at(at, kPcmRate), 2, 0, g_pcm_samples)
				+ value_at(pcm, pcm_first, sample_at(at, kPcmRate), 2, 1, g_pcm_samples))
			+ 0.30f * value_at(speech, speech_first, sample_at(at, kSpeechRate), 1, 0, g_speech_samples)
			+ 0.25f * value_at(title, title_first, sample_at(at, kTitleRate), 1, 0, g_title_samples);
		g_mix.push_back(int16_t(std::clamp(int(mixed * 32768.0f), -32768, 32767)));
	}
	remember_last(g_pcm_samples, pcm);
	remember_last(g_speech_samples, speech);
	remember_last(g_title_samples, title);
}

extern "C" uint64_t ks_frame_samples_hash(void)
{
	return g_frame_samples_hash;
}

extern "C" void ks_frame_source_hashes(uint64_t hashes[4])
{
	for (int source = 0; source < 4; source++)
		hashes[source] = g_source_hashes[source];
}

extern "C" uint64_t ks_state_hash(void)
{
	// (the Z80's stack left out: the decompiled program keeps what it pushes in variables of its own)
	constexpr size_t stack = TMNT_SOUND_STACK_START - 0x8000u, above = TMNT_SOUND_STACK_END - 0x8000u;
	const uint64_t hash = hash_bytes(tmnt_board.sound_ram, stack, 0xcbf29ce484222325ull);
	return hash_bytes(tmnt_board.sound_ram + above, sizeof(tmnt_board.sound_ram) - above, hash);
}

extern "C" const int16_t *ks_frame_mix(size_t *count)
{
	*count = g_mix.size();
	return g_mix.data();
}

extern "C" uint32_t ks_mix_rate(void)
{
	return uint32_t(kSoundClock / kFmRate.den);
}

extern "C" uint64_t ks_z80_cycles(void)
{
	return g_model.on ? g_model.clock : g_z80_time;
}

extern "C" const uint8_t *ks_executed(void)
{
	return g_executed;
}

extern "C" void ks_record_rom_reads(void)
{
	g_record_rom_reads = true;
}

extern "C" const uint32_t *ks_rom_reads(size_t *count)
{
	g_rom_reads.assign(g_rom_read_set.begin(), g_rom_read_set.end());
	*count = g_rom_reads.size();
	return g_rom_reads.data();
}

extern "C" void ks_check_chips(void)
{
	g_own_fm = ym2151{};
	ym2151_init(&g_own_fm);
	// the K007232's port: tmnt.cpp's volume_callback, as on the reference's
	g_own_pcm = k007232{};
	g_own_pcm.port_write = [](void *, uint8_t data) {
		k007232_set_volume(&g_own_pcm, 0, (data >> 4) * 0x11, 0);
		k007232_set_volume(&g_own_pcm, 1, 0, (data & 0x0f) * 0x11);
	};
	k007232_init(&g_own_pcm, game_pcm, GAME_PCM_SIZE);
	upd7759_init(&g_own_speech, game_speech, GAME_SPEECH_SIZE);
	title_music_init(&g_own_title, game_title, GAME_TITLE_SIZE);
	g_pcm->m_check = [](int count, const std::vector<float> *outputs) {
		for (int index = 0; index < count; index++)
		{
			float own[2];
			k007232_generate(&g_own_pcm, own);
			if ((own[0] != outputs[0][size_t(index)] || own[1] != outputs[1][size_t(index)]) && g_chip_differences[1]++ == 0)
			{
				g_first_difference[1] = g_chip_samples[1];
				std::fprintf(stderr, "K007232 sample %llu: own %f %f, reference %f %f; voices %d %06X %04X %d, %d %06X %04X %d\n",
					(unsigned long long)g_chip_samples[1], own[0], own[1], outputs[0][size_t(index)], outputs[1][size_t(index)],
					g_own_pcm.voices[0].playing, g_own_pcm.voices[0].address, g_own_pcm.voices[0].step, g_own_pcm.voices[0].volume[0],
					g_own_pcm.voices[1].playing, g_own_pcm.voices[1].address, g_own_pcm.voices[1].step, g_own_pcm.voices[1].volume[1]);
			}
			g_chip_samples[1]++;
		}
	};
	g_speech->m_check = [](int count, const std::vector<float> *outputs) {
		std::vector<float> own(size_t(count) + 1);
		upd7759_generate(&g_own_speech, own.data(), count);
		for (int index = 0; index < count; index++)
		{
			if (own[size_t(index)] != outputs[0][size_t(index)] && g_chip_differences[2]++ == 0)
				g_first_difference[2] = g_chip_samples[2];
			g_chip_samples[2]++;
		}
	};
	g_check_chips = true;
}

extern "C" void ks_chip_report(uint64_t samples[4], uint64_t differences[4], uint64_t first[4])
{
	for (int chip = 0; chip < 4; chip++)
	{
		samples[chip] = g_chip_samples[chip];
		differences[chip] = g_chip_differences[chip];
		first[chip] = g_first_difference[chip];
	}
}
