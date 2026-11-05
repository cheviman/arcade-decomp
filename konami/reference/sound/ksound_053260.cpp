/**
 *    Name:        ksound_053260.cpp
 *    Description: reference sound board of The Simpsons: MAME's Z80 and K053260 and ymfm's YM2151, on the board
 *                 model
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    05-11-2026
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
 *    The Z80 runs cycle by cycle in slices that end where the board's time asks, at the K053260's timer steps
 *    (every 16 clocks, from 16 clocks after the reset, as its device_reset sets it) or at the YM2151's timers (a
 *    timer set while it runs cuts its slice short), and takes the 053248's IRQ as MAME's HOLD_LINE does: the
 *    line stays up until the Z80 takes it. The Z80's RAM is hashed whole (its stack too, for now).
 */
#include "ksound_053260.h"

#include "game/data.h"
#include "simpsons/machine.h"

#include "ymfm_opm.h"

#include <algorithm>
#include <bit>
#include <cassert>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
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
		// a running Z80 stops by then, for the timer to run out on time
		if (g_running && g_now_override == UINT64_MAX && m_deadline[number] < g_slice_start + uint64_t(g_slice_budget))
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
			g_pcm_samples.push(values);
		}
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
		if (sh1_up && when >= g_nmi_blocked_until)
			g_z80->set_input_line(ks::INPUT_LINE_NMI, ks::ASSERT_LINE);
		g_now_override = UINT64_MAX;
		g_timer_steps++;
	}
}

// FA00: the NMI's line down, and an SH1 pulse in the next 4 cycles does not raise it
void arm_nmi()
{
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
	g_z80->set_input_line(ks::INPUT_LINE_IRQ0, ks::ASSERT_LINE);
}

uint32_t place_of(uint16_t address)
{
	const int32_t offset = simpsons_sound_offset(address);
	return offset >= 0 ? uint32_t(offset) : kRamPlaces + address;
}

void on_instruction(ks::offs_t address)
{
	g_instruction = place_of(uint16_t(address));
	g_executed[g_instruction] = 1;
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

// ---- recording ----

uint64_t g_frame_samples_hash = 0;
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
}

extern "C" void ks_run_until(uint64_t time)
{
	run_to(z80_time(time));
}

extern "C" void ks_end_frame(uint64_t time)
{
	const uint64_t cycle = z80_time(time);
	run_to(cycle);
	g_fm->catch_up(cycle);
	g_pcm->catch_up(cycle);

	// the samples that start before the frame's end
	std::vector<float> fm, pcm;
	uint64_t hash = 0xcbf29ce484222325ull;
	hash = take(g_fm_samples, samples_before(cycle), hash, fm);
	g_frame_samples_hash = take(g_pcm_samples, samples_before(cycle), hash, pcm);

	// the mix (simpsons.cpp's routes to its stereo speaker): the YM2151's first output to both sides at 0.5,
	// the K053260's two to theirs at 0.5
	g_mix.clear();
	const size_t count = std::min(fm.size(), pcm.size()) / 2;
	for (size_t index = 0; index < count; index++)
	{
		g_mix.push_back(to_sample(0.5f * fm[index * 2] + 0.5f * pcm[index * 2]));
		g_mix.push_back(to_sample(0.5f * fm[index * 2] + 0.5f * pcm[index * 2 + 1]));
	}
}

extern "C" uint64_t ks_frame_samples_hash(void)
{
	return g_frame_samples_hash;
}

extern "C" uint64_t ks_state_hash(void)
{
	return hash_bytes(simpsons_board.sound_ram, sizeof(simpsons_board.sound_ram), 0xcbf29ce484222325ull);
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
	return g_z80_time;
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
