/**
 *    Name:        ksound_053260.cpp
 *    Description: reference sound board of The Simpsons: MAME's Z80 and K053260 and ymfm's YM2151, on the board
 *                 model
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    06-11-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    MAME's files are compiled here, unchanged, inside a namespace of their own (ks), over the stand-ins of
 *    this folder (emu.h, dirom.h, machine/z80daisy.h, logmacro.h): the Z80 core is system32/reference/mame/z80's
 *    (MAME's, as 0.289 has it but for its debugger's IR register), the K053260 MAME 0.289's. ymfm is a library
 *    of its own and builds as it is. What simpsons.cpp adds (MAME 0.289, Ernesto Corvi, BSD-3-Clause) is
 *    written below as it has it: the Z80's NMI from the K053260's SH1 and its arming (z80_nmi_w,
 *    z80_arm_nmi_w), the NMI held from the reset (machine_reset), and the routes' gains.
 *
 *    The Z80's time is kept one of two ways:
 *      - free: the Z80 runs cycle by cycle in slices that end where the board's time asks, at the K053260's
 *        timer steps (every 16 clocks, from 16 clocks after the reset, as its device_reset sets it) or at the
 *        YM2151's timers (a timer set while it runs cuts its slice short), and takes the 053248's IRQ as MAME's
 *        HOLD_LINE does: the line stays up until the Z80 takes it;
 *      - the timing model (ks_use_timing, the table tools/decompile_sound.py writes): the rules of the game's own
 *        sound board (runtime/src/sound_cpu.c), kept around MAME's core by its instruction hook, as TMNT's
 *        ksound.cpp keeps them. The time is the model's: every block of the table adds its cycles when it
 *        starts, and a condition's longer way its extra ones; the timers run out, the K053260's timer steps and
 *        the IRQ is taken only where a block starts (this code pushes the address, drops the held line and
 *        jumps to 0038 itself: the core never sees the line); every access happens at its block's start; a
 *        HALT waits for the first SH1 pulse 4 cycles after the write of FA00 or later, in halted NOPs of 4
 *        cycles, and the NMI is this code's too (11 cycles, to 0066: the core never sees its line either); an
 *        LDIR's every repeat adds 21; the board stops the Z80 where the main loop's top finds its time past the
 *        board's. The game's own sound board keeps the same rules, so the two compare sample for sample.
 *    The Z80's RAM is hashed without its stack (SIMPSONS_SOUND_STACK_START-END, simpsons/sound_board.h), which the
 *    decompiled program does not keep, and with the two ports the Z80 writes (its answers to the 053248).
 *
 *    --sound-check (ks_check_chips) runs the game's own chips (runtime/src/ym2151.c and k053260.c) beside
 *    MAME's: each gets the same writes and reads at the same times, makes its samples where MAME's does, and has
 *    its timer stepped where MAME's is; every sample (the YM2151's two outputs, the K053260's two), every read,
 *    the YM2151's timers (where each runs out) and the K053260's SH1 pulses are compared, and a frame differs
 *    where any of them does.
 */
#include "ksound_053260.h"

#include "game/data.h"
#include "simpsons/k053260.h"
#include "simpsons/machine.h"
#include "simpsons/sound_board.h"
#include "simpsons/ym2151.h"

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
#undef LOG    // (the K053260 has a LOG of its own)
#include "../mame/sound/k053260.cpp"
}

namespace
{

constexpr uint64_t kBoardClock = SIMPSONS_CPU_CLOCK;
constexpr uint64_t kSoundClock = SIMPSONS_SOUND_CLOCK;
constexpr uint64_t kSampleClocks = 64;      // the YM2151's and the K053260's samples, both on the Z80's clock
constexpr uint64_t kTimerClocks = 16;       // the K053260's timer step
constexpr uint64_t kNmiBlock = 4;           // z80_arm_nmi_w: an SH1 pulse within 4 cycles does not raise the NMI

// the first Z80 cycle at or after board time 'time'
uint64_t z80_time(uint64_t time)
{
	return (time * kSoundClock + kBoardClock - 1) / kBoardClock;
}

// how many samples start before Z80 cycle 'cycle'
uint64_t samples_before(uint64_t cycle)
{
	return (cycle + kSampleClocks - 1) / kSampleClocks;
}

// ---- time: the Z80's cycles, and while the Z80 runs, where its slice started and how long it is ----

uint64_t g_z80_time = 0;
bool g_running = false;
uint64_t g_slice_start = 0;
int g_slice_budget = 0;
uint64_t g_now_override = UINT64_MAX;       // while a timer fires, its own time

// the board's time when the Z80 has run 'cycles'
uint64_t board_time(uint64_t cycles)
{
	return cycles * kBoardClock / kSoundClock;
}

// ---- the timing model ----

constexpr int kInterruptCycles = 13;        // the IRQ's acknowledge (IM 1), in the timing model
constexpr int kNmiCycles = 11;              // the NMI's
constexpr uint64_t kHaltStep = 4;           // a halted Z80's NOP
constexpr uint64_t kRepeatCycles = 21;      // another round of a block move
constexpr uint16_t kNmiHandler = 0x0066;

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
	bool line = false;              // the 053248's IRQ, held until the Z80 takes it
	int previous = -1;              // the instruction before this one
	uint8_t previous_opcode = 0;
	const block_timing *current = nullptr;
} g_model;

class sound_cpu : public ks::z80_device
{
public:
	explicit sound_cpu(const ks::machine_config &config) : ks::z80_device(config, "audiocpu", nullptr, SIMPSONS_SOUND_CLOCK) {}

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

	// the timing model takes the interrupts itself, as the core's take_interrupt (interrupt mode 1) and take_nmi
	// do: the return address on the stack, IFF1 off (and IFF2 for the IRQ), the refresh register counted up, on to
	// the handler; 'back' is where the handler returns to
	void interrupt_to(uint16_t handler, uint16_t back, bool nmi)
	{
		m_pc.w = back;
		m_sp.w = uint16_t(m_sp.w - 1);
		simpsons_z80_write(m_sp.w, m_pc.b.h);
		m_sp.w = uint16_t(m_sp.w - 1);
		simpsons_z80_write(m_sp.w, m_pc.b.l);
		m_pc.w = handler;
		m_iff1 = false;
		if (!nmi)
			m_iff2 = false;
		m_r++;
	}

	bool interrupts_on() const { return m_iff1; }

	// back to the start of the instruction whose opcode the core read as the slice ended: the read's step ran
	// out of cycles before the fetch counted (no PC++, no refresh count), so only the rest of the instruction
	// is dropped
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
constexpr uint32_t kRamPlaces = 0x30000;    // code above BFFF: 30000 plus its address
std::vector<uint8_t> g_executed(kRamPlaces + 0x10000);
uint32_t g_instruction = 0;     // the place of the instruction the Z80 runs
bool g_record_rom_reads = false;
std::set<uint64_t> g_rom_read_set;
std::vector<uint64_t> g_rom_reads;

uint64_t now()
{
	if (g_now_override != UINT64_MAX)
		return g_now_override;
	if (g_model.on)
		return g_running ? g_model.access : g_model.clock;
	return g_running ? g_slice_start + uint64_t(g_z80->cycles_done()) : g_z80_time;
}

// ---- a chip's samples: 'outputs' channels interleaved, from sample number 'first' on ----

struct samples
{
	int outputs = 1;
	std::vector<float> data;
	uint64_t first = 0;
	uint64_t made = 0;                // samples made so far

	explicit samples(int count) : outputs(count) {}
	void push(const float *values)
	{
		data.insert(data.end(), values, values + outputs);
		made++;
	}
};

samples g_fm_samples(2);
samples g_pcm_samples(2);

// ---- --sound-check: the game's own chips fed what MAME's get, at the same times, and compared with them ----

bool g_check = false;
simpsons_ym2151 g_own_fm;
simpsons_k053260 g_own_pcm;
uint64_t g_own_fm_deadline[2] = { UINT64_MAX, UINT64_MAX };    // the own YM2151's timers, in Z80 cycles
uint64_t g_own_fm_busy_until = 0;

enum { kFmSamples, kPcmSamples, kFmReads, kPcmReads, kFmTimers, kPcmTimer, kCheckKinds };
const char *const kCheckNames[kCheckKinds] = {
	"YM2151 samples", "K053260 samples", "YM2151 reads", "K053260 reads", "YM2151 timers", "K053260 SH1 steps" };

struct check_counts
{
	uint64_t compared[kCheckKinds] = {};
	uint64_t differing[kCheckKinds] = {};
};

check_counts g_frame_check, g_total_check;
std::string g_frame_first;                  // what differed first in the frame
uint64_t g_check_frames = 0, g_differing_frames = 0, g_first_frame = 0;
std::string g_first;                        // what differed first, in the first frame that differed
constexpr uint64_t kFramesShown = 10;       // the differing frames told about one by one (on stderr)

// A comparison of kind 'kind': whether the two are the same, and if not, what differed (the frame's first)
void check(int kind, bool same, const char *format, ...)
{
	g_frame_check.compared[kind]++;
	if (same)
		return;
	g_frame_check.differing[kind]++;
	if (g_frame_first.empty())
	{
		char text[512];
		va_list arguments;
		va_start(arguments, format);
		std::vsnprintf(text, sizeof(text), format, arguments);
		va_end(arguments);
		g_frame_first = text;
	}
}

// The frame's comparisons into the totals (ks_end_frame)
void close_check_frame()
{
	g_check_frames++;
	bool differs = false;
	for (int kind = 0; kind < kCheckKinds; kind++)
	{
		g_total_check.compared[kind] += g_frame_check.compared[kind];
		g_total_check.differing[kind] += g_frame_check.differing[kind];
		differs = differs || g_frame_check.differing[kind] != 0;
	}
	if (differs)
	{
		if (g_differing_frames++ == 0)
		{
			g_first_frame = g_check_frames;
			g_first = g_frame_first;
		}
		if (g_differing_frames <= kFramesShown)
		{
			std::string line = "sound check: frame " + std::to_string(g_check_frames) + ":";
			for (int kind = 0; kind < kCheckKinds; kind++)
				if (g_frame_check.differing[kind])
					line += " " + std::string(kCheckNames[kind]) + " " + std::to_string(g_frame_check.differing[kind])
						+ " of " + std::to_string(g_frame_check.compared[kind]) + ",";
			line += " the first: " + g_frame_first + "\n";
			std::fputs(line.c_str(), stderr);
		}
	}
	g_frame_check = check_counts();
	g_frame_first.clear();
}

// The own YM2151's host: its timers and its busy flag on the Z80's clock, as fm_chip keeps MAME's
void own_fm_set_timer(void *, int timer, int32_t clocks)
{
	g_own_fm_deadline[timer] = clocks < 0 ? UINT64_MAX : now() + uint64_t(clocks);
}

void own_fm_set_busy_end(void *, uint32_t clocks)
{
	g_own_fm_busy_until = now() + clocks;
}

bool own_fm_is_busy(void *)
{
	return now() < g_own_fm_busy_until;
}

// ---- the YM2151 (its IRQ goes nowhere on this board) ----

class fm_chip : public ymfm::ymfm_interface
{
public:
	fm_chip() : m_chip(*this) { m_chip.reset(); }

	// every sample that starts before Z80 cycle 'cycle', as MAME's ymfm device puts them (x / 32768)
	void catch_up(uint64_t cycle)
	{
		const uint64_t due = samples_before(cycle);
		ymfm::ym2151::output_data output;
		while (g_fm_samples.made < due)
		{
			m_chip.generate(&output);
			if (g_check)
			{
				int32_t own[2];
				simpsons_ym2151_generate(&g_own_fm, own);
				check(kFmSamples, own[0] == output.data[0] && own[1] == output.data[1],
					"YM2151 sample %llu (the frame's %llu): own %d %d, reference %d %d",
					(unsigned long long)g_fm_samples.made, (unsigned long long)(g_fm_samples.made - g_fm_samples.first),
					int(own[0]), int(own[1]), int(output.data[0]), int(output.data[1]));
			}
			const float values[2] = { float(output.data[0]) * (1.0f / 32768.0f), float(output.data[1]) * (1.0f / 32768.0f) };
			g_fm_samples.push(values);
		}
	}
	uint8_t read(int offset)
	{
		catch_up(now());
		const uint8_t value = m_chip.read(uint32_t(offset));
		if (g_check)
		{
			const uint8_t own = simpsons_ym2151_read(&g_own_fm, offset);
			check(kFmReads, own == value, "YM2151 read of %d at Z80 cycle %llu: own %02X, reference %02X", offset,
				(unsigned long long)now(), own, value);
		}
		return value;
	}
	void write(int offset, uint8_t data)
	{
		catch_up(now());
		m_chip.write(uint32_t(offset), data);
		if (g_check)
		{
			simpsons_ym2151_write(&g_own_fm, offset, data);
			check_timers("write");
		}
	}
	// --sound-check: the own YM2151's timers run out where MAME's do
	void check_timers(const char *after)
	{
		check(kFmTimers, m_deadline[0] == g_own_fm_deadline[0] && m_deadline[1] == g_own_fm_deadline[1],
			"YM2151 timers after a %s at Z80 cycle %llu: own %lld %lld, reference %lld %lld", after,
			(unsigned long long)now(), (long long)g_own_fm_deadline[0], (long long)g_own_fm_deadline[1],
			(long long)m_deadline[0], (long long)m_deadline[1]);
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
			if (g_check)
			{
				simpsons_ym2151_timer_expired(&g_own_fm, number);
				check_timers("timer");
			}
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

// ---- the K053260: MAME's, its stream pulled up to the present before each change, its timer stepped here ----

class pcm_chip : public ks::k053260_device
{
public:
	pcm_chip(const ks::machine_config &config) : ks::k053260_device(config, "k053260", nullptr, SIMPSONS_SOUND_CLOCK)
	{
		this->stream().m_before_change = [this]() { catch_up(now()); };
	}
	void catch_up(uint64_t cycle)
	{
		const uint64_t due = samples_before(cycle);
		if (due <= g_pcm_samples.made)
			return;
		const int count = int(due - g_pcm_samples.made);
		this->generate(count);
		for (int index = 0; index < count; index++)
		{
			const float values[2] = { this->stream().m_outputs[0][size_t(index)], this->stream().m_outputs[1][size_t(index)] };
			if (g_check)
			{
				// the own chip's sample as MAME's stream puts it (put_int_clamp: the value / 32768)
				int32_t own[2];
				simpsons_k053260_generate(&g_own_pcm, own);
				const float own_values[2] = { float(own[0]) * (1.0f / 32768.0f), float(own[1]) * (1.0f / 32768.0f) };
				if (own_values[0] == values[0] && own_values[1] == values[1])
					check(kPcmSamples, true, "");
				else
					check(kPcmSamples, false, "K053260 sample %llu (the frame's %llu): own %d %d, reference %.0f %.0f; own voices %s",
					(unsigned long long)g_pcm_samples.made, (unsigned long long)(g_pcm_samples.made - g_pcm_samples.first),
					int(own[0]), int(own[1]), double(values[0]) * 32768.0, double(values[1]) * 32768.0, voices().c_str());
			}
			g_pcm_samples.push(values);
		}
	}
	uint8_t read(ks::offs_t offset)
	{
		const uint8_t value = ks::k053260_device::read(offset);
		if (g_check)
		{
			const uint8_t own = simpsons_k053260_read(&g_own_pcm, int(offset));
			check(kPcmReads, own == value, "K053260 read of %02X at Z80 cycle %llu: own %02X, reference %02X",
				unsigned(offset), (unsigned long long)now(), own, value);
		}
		return value;
	}
	void write(ks::offs_t offset, uint8_t data)
	{
		ks::k053260_device::write(offset, data);
		if (g_check)
			simpsons_k053260_write(&g_own_pcm, int(offset), data);
	}
	// the own chip's voices, for a difference: playing, start, length, pitch, position, output
	static std::string voices()
	{
		std::string text;
		for (const simpsons_k053260_voice &voice : g_own_pcm.voices)
		{
			char one[96];
			std::snprintf(one, sizeof(one), "[%d %06X %04X %03X %X %d]", int(voice.playing), unsigned(voice.start),
				unsigned(voice.length), unsigned(voice.pitch), unsigned(voice.position), int(voice.output));
			text += one;
		}
		return text;
	}
	// the timer's step (MAME's timer runs update_state_outputs): its SH1 and SH2 pulses, its 2 ms count
	void step_timer() { update_state_outputs(0); }
};

std::unique_ptr<pcm_chip> g_pcm;

// ---- the K053260's timer and the Z80's NMI (simpsons.cpp: z80_nmi_w, z80_arm_nmi_w) ----

uint64_t g_timer_steps = 0;             // the steps taken: step n is at Z80 cycle 16 x (n + 1)
uint64_t g_nmi_blocked_until = 0;       // the Z80 cycle the arming's block ends

uint64_t next_step()
{
	return kTimerClocks * (g_timer_steps + 1);
}

// the timer's steps due by Z80 cycle 'cycle': SH1 goes up at every fourth, from the first
void step_timer(uint64_t cycle)
{
	while (next_step() <= cycle)
	{
		const uint64_t when = next_step();
		const bool sh1_up = (g_timer_steps & 3u) == 0;
		g_now_override = when;
		g_pcm->step_timer();
		if (g_check)
		{
			const bool own_up = simpsons_k053260_step_timer(&g_own_pcm);
			check(kPcmTimer, own_up == sh1_up, "K053260 timer step %llu: own SH1 %s, reference %s",
				(unsigned long long)g_timer_steps, own_up ? "up" : "not up", sh1_up ? "up" : "not up");
		}
		// (the timing model raises the NMI itself, at the HALT that waits for it)
		if (sh1_up && when >= g_nmi_blocked_until && !g_model.on)
			g_z80->set_input_line(ks::INPUT_LINE_NMI, ks::ASSERT_LINE);
		g_now_override = UINT64_MAX;
		g_timer_steps++;
	}
}

// FA00: the NMI's line down, and an SH1 pulse in the next 4 cycles does not raise it
void arm_nmi()
{
	if (!g_model.on)
		g_z80->set_input_line(ks::INPUT_LINE_NMI, ks::CLEAR_LINE);
	g_nmi_blocked_until = now() + kNmiBlock;
}

uint8_t chip_read(int chip, int offset)
{
	return chip == SIMPSONS_CHIP_FM ? g_fm->read(offset) : g_pcm->read(ks::offs_t(offset));
}

void chip_write(int chip, int offset, uint8_t data)
{
	if (chip == SIMPSONS_CHIP_FM)
		g_fm->write(offset, data);
	else
		g_pcm->write(ks::offs_t(offset), data);
}

// the 053248 raises the Z80's IRQ, held until the Z80 takes it
void raise_irq()
{
	g_irq_held = true;
	if (g_model.on)
		g_model.line = true;
	else
		g_z80->set_input_line(ks::INPUT_LINE_IRQ0, ks::ASSERT_LINE);
}

uint32_t place_of(uint16_t address)
{
	const int32_t offset = simpsons_sound_offset(address);
	return offset >= 0 ? uint32_t(offset) : kRamPlaces + address;
}

// ---- the timing model's hook ----

// a block starts at 'pc': the timers due, the IRQ if the Z80 takes it, then the block's time
void start_block(uint16_t pc)
{
	for (;;)
	{
		g_fm->fire(g_model.clock);
		step_timer(g_model.clock);
		if (!g_z80->interrupts_on() || g_model.previous_opcode == 0xFB || !g_model.line)
			break;
		// the interrupt: its cycles, the held line down, then the handler's first block starts
		g_z80->interrupt_to(g_model.irq, pc, false);
		g_model.line = false;
		g_irq_held = false;
		pc = g_model.irq;
		g_instruction = place_of(pc);
		g_executed[g_instruction] = 1;
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

// HALT at 'pc' (the last instruction of its block, whose time is counted): the halted Z80 runs NOPs until the
// first SH1 pulse 4 cycles after the write of FA00 or later, takes the NMI after the NOP it came in, and its
// handler's block starts; the core runs the handler (RETN) instead of the HALT, back to the instruction after it
void halt_until_nmi(uint16_t pc)
{
	constexpr uint64_t period = 4 * kTimerClocks;    // SH1's pulses, at every fourth step from the first
	uint64_t nmi = kTimerClocks;
	if (g_nmi_blocked_until > kTimerClocks)
		nmi = kTimerClocks + (g_nmi_blocked_until - kTimerClocks + period - 1) / period * period;
	if (nmi > g_model.clock)
		g_model.clock += (nmi - g_model.clock + kHaltStep - 1) / kHaltStep * kHaltStep;
	g_model.clock += kNmiCycles;
	g_z80->interrupt_to(kNmiHandler, uint16_t(pc + 1), true);
	g_instruction = place_of(kNmiHandler);
	g_executed[g_instruction] = 1;
	g_model.previous_opcode = 0;
	start_block(kNmiHandler);
	g_model.previous = kNmiHandler;
}

void on_instruction(ks::offs_t address)
{
	const uint16_t pc = uint16_t(address);
	g_instruction = place_of(pc);
	g_executed[g_instruction] = 1;
	if (!g_model.on)
		return;
	const int previous = g_model.previous;
	g_model.previous = pc;
	if (previous == pc && g_model.repeats.count(pc))
	{
		g_model.clock += kRepeatCycles;    // another round of a block move
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
	g_model.previous_opcode = simpsons_z80_read(pc);
	if (g_model.previous_opcode == 0x76)
		halt_until_nmi(pc);
}

// a data read of the Z80's (not an opcode or an argument fetch): the program ROM's bytes are noted with the
// instruction that reads them
uint8_t data_read(ks::offs_t address)
{
	if (g_record_rom_reads && address < 0xC000u)
		g_rom_read_set.insert(uint64_t(g_instruction) << 32 | uint32_t(simpsons_sound_offset(uint16_t(address))));
	return simpsons_z80_read(uint16_t(address));
}

// the Z80, the K053260's timer and the YM2151's timers up to Z80 cycle 'until'
void run_to(uint64_t until)
{
	while (g_z80_time < until)
	{
		g_fm->fire(g_z80_time);
		step_timer(g_z80_time);
		const uint64_t end = std::min({ until, std::max(g_fm->next_timer(), g_z80_time + 1), next_step() });
		g_slice_start = g_z80_time;
		g_slice_budget = int(end - g_z80_time);
		g_running = true;
		const int done = g_z80->run_slice(g_slice_budget);
		g_running = false;
		g_z80_time += uint64_t(std::max(done, 0));
	}
	g_fm->fire(g_z80_time);
	step_timer(g_z80_time);
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
uint64_t g_source_hashes[2] = { 0, 0 };
std::vector<int16_t> g_mix;

uint64_t hash_bytes(const void *data, size_t size, uint64_t hash)
{
	const uint8_t *bytes = static_cast<const uint8_t *>(data);
	for (size_t index = 0; index < size; index++)
		hash = (hash ^ bytes[index]) * 0x100000001b3ull;
	return hash;
}

// the chip's samples that start before 'due' out of it, into the hash
uint64_t take(samples &source, uint64_t due, uint64_t hash, std::vector<float> &out)
{
	const size_t count = size_t(std::min<uint64_t>(due - source.first, source.data.size() / size_t(source.outputs))) * size_t(source.outputs);
	out.assign(source.data.begin(), source.data.begin() + ptrdiff_t(count));
	source.data.erase(source.data.begin(), source.data.begin() + ptrdiff_t(count));
	source.first += count / size_t(source.outputs);
	return hash_bytes(out.data(), out.size() * sizeof(float), hash);
}

int16_t to_sample(float value)
{
	return int16_t(std::clamp(int(value * 32768.0f), -32768, 32767));
}

} // namespace

extern "C" void ks_attach(void)
{
	ks::set_region("k053260", game_samples, GAME_SAMPLES_SIZE);

	g_z80 = std::make_unique<sound_cpu>(g_config);
	g_z80->space(ks::AS_PROGRAM).read = [](ks::offs_t address) { return simpsons_z80_read(uint16_t(address)); };
	g_z80->space(ks::AS_PROGRAM).write = [](ks::offs_t address, uint8_t data) { simpsons_z80_write(uint16_t(address), data); };
	g_z80->space(ks::AS_PROGRAM).data_read = data_read;
	g_z80->space(ks::AS_IO).read = [](ks::offs_t) { return uint8_t(0); };
	g_z80->space(ks::AS_IO).write = [](ks::offs_t, uint8_t) {};
	// HOLD_LINE: the line goes down as the Z80 takes the interrupt (interrupt mode 1 reads no vector)
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
	// machine_reset: the Z80's NMI goes low as the reset does
	g_z80->set_input_line(ks::INPUT_LINE_NMI, ks::ASSERT_LINE);

	g_fm = std::make_unique<fm_chip>();
	g_pcm = std::make_unique<pcm_chip>(g_config);
	g_pcm->start();
	g_pcm->reset();

	simpsons_board.sound_chip_read = chip_read;
	simpsons_board.sound_chip_write = chip_write;
	simpsons_board.sound_irq = raise_irq;
	simpsons_board.sound_nmi_arm = arm_nmi;
	g_z80_time = z80_time(simpsons_board.cycles);
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

	// the samples that start before the frame's end
	std::vector<float> fm, pcm;
	uint64_t hash = 0xcbf29ce484222325ull;
	hash = take(g_fm_samples, samples_before(cycle), hash, fm);
	g_frame_samples_hash = take(g_pcm_samples, samples_before(cycle), hash, pcm);
	g_source_hashes[0] = hash_bytes(fm.data(), fm.size() * sizeof(float), 0xcbf29ce484222325ull);
	g_source_hashes[1] = hash_bytes(pcm.data(), pcm.size() * sizeof(float), 0xcbf29ce484222325ull);

	// the mix (simpsons.cpp's routes to its stereo speaker): the YM2151's first output to both sides at 0.5,
	// the K053260's two to theirs at 0.5
	g_mix.clear();
	const size_t count = std::min(fm.size(), pcm.size()) / 2;
	for (size_t index = 0; index < count; index++)
	{
		g_mix.push_back(to_sample(0.5f * fm[index * 2] + 0.5f * pcm[index * 2]));
		g_mix.push_back(to_sample(0.5f * fm[index * 2] + 0.5f * pcm[index * 2 + 1]));
	}
	if (g_check)
		close_check_frame();
}

extern "C" void ks_check_chips(void)
{
	g_own_fm = simpsons_ym2151{};
	g_own_fm.set_timer = own_fm_set_timer;
	g_own_fm.set_busy_end = own_fm_set_busy_end;
	g_own_fm.is_busy = own_fm_is_busy;
	simpsons_ym2151_init(&g_own_fm);
	simpsons_k053260_init(&g_own_pcm, game_samples, GAME_SAMPLES_SIZE);
	g_check = true;
}

extern "C" bool ks_check_print(void)
{
	std::printf("sound check:       %llu of %llu frames differ", (unsigned long long)g_differing_frames,
		(unsigned long long)g_check_frames);
	if (g_differing_frames)
		std::printf(", the first %llu: %s", (unsigned long long)g_first_frame, g_first.c_str());
	std::printf("\n");
	for (int kind = 0; kind < kCheckKinds; kind++)
		std::printf("sound check:       %s: %llu of %llu differ\n", kCheckNames[kind],
			(unsigned long long)g_total_check.differing[kind], (unsigned long long)g_total_check.compared[kind]);
	std::fflush(stdout);
	return g_differing_frames == 0;
}

extern "C" uint64_t ks_frame_samples_hash(void)
{
	return g_frame_samples_hash;
}

extern "C" void ks_frame_source_hashes(uint64_t hashes[2])
{
	hashes[0] = g_source_hashes[0];
	hashes[1] = g_source_hashes[1];
}

extern "C" uint64_t ks_state_hash(void)
{
	// (the Z80's stack left out: the decompiled program keeps what it pushes in variables of its own; the two
	// ports the Z80 writes, its answers to the 053248, in)
	constexpr size_t stack = SIMPSONS_SOUND_STACK_START - 0xF000u, above = SIMPSONS_SOUND_STACK_END - 0xF000u;
	uint64_t hash = hash_bytes(simpsons_board.sound_ram, stack, 0xcbf29ce484222325ull);
	hash = hash_bytes(simpsons_board.sound_ram + above, sizeof(simpsons_board.sound_ram) - above, hash);
	return hash_bytes(simpsons_board.sound_ports + 2, 2, hash);
}

extern "C" const int16_t *ks_frame_mix(size_t *count)
{
	*count = g_mix.size();
	return g_mix.data();
}

extern "C" uint32_t ks_mix_rate(void)
{
	return uint32_t(kSoundClock / kSampleClocks);
}

extern "C" uint64_t ks_z80_cycles(void)
{
	return g_model.on ? g_model.clock : g_z80_time;
}

extern "C" const uint8_t *ks_executed(size_t *count)
{
	*count = g_executed.size();
	return g_executed.data();
}

extern "C" void ks_record_rom_reads(void)
{
	g_record_rom_reads = true;
}

extern "C" const uint64_t *ks_rom_reads(size_t *count)
{
	g_rom_reads.assign(g_rom_read_set.begin(), g_rom_read_set.end());
	*count = g_rom_reads.size();
	return g_rom_reads.data();
}
