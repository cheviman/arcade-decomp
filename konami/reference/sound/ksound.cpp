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
 *    The Z80 runs cycle by cycle in slices that end where the board's time asks, or at the YM2151's timers
 *    (a timer set while it runs cuts its slice short), and takes the 68000's IRQ as MAME's HOLD_LINE does:
 *    the line stays up until the Z80 takes it. The title music is MAME's samples device at its own rate
 *    (20 kHz: a sample of the ROM each step), not resampled to an output rate.
 */
#include "ksound.h"

#include "game/data.h"
#include "tmnt/machine.h"

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
#include <stdexcept>
#include <string>
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

private:
	int m_cut = 0;
};

ks::machine_config g_config;
std::unique_ptr<sound_cpu> g_z80;
bool g_irq_held = false;
uint8_t g_executed[0x10000 / 8];

uint64_t now()
{
	if (g_now_override != UINT64_MAX)
		return g_now_override;
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
		if (g_running && g_now_override == UINT64_MAX
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
		for (int index = 0; index < count; index++)
		{
			float values[2];
			for (int output = 0; output < m_out.outputs; output++)
				values[output] = this->stream().m_outputs[output][size_t(index)];
			m_out.push(values);
		}
	}

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
	case TMNT_CHIP_PCM: return g_pcm->read(ks::offs_t(offset));
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
		break;
	case TMNT_CHIP_FM:
		g_fm->write(offset, data);
		break;
	case TMNT_CHIP_SPEECH:
		g_speech->port_w(data);
		break;
	case TMNT_CHIP_SPEECH_START:
		g_speech->start_w(!(data & 1));        // tmnt_upd_start_w
		break;
	case TMNT_CHIP_SOUND_RESET:
		// sres_w: bit 1 the uPD7759's reset, bit 2 the title music
		g_speech->reset_w((data >> 1) & 1);
		if (data & 4)
			g_title.start();
		else
			g_title.stop();
		break;
	default:
		break;
	}
}

// the 68000 raises the Z80's IRQ, held until the Z80 takes it
void raise_irq()
{
	g_irq_held = true;
	g_z80->set_input_line(ks::INPUT_LINE_IRQ0, ks::ASSERT_LINE);
}

void on_instruction(ks::offs_t address)
{
	const uint16_t pc = uint16_t(address);
	g_executed[pc >> 3] |= uint8_t(1u << (pc & 7));
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

extern "C" uint64_t ks_state_hash(void)
{
	return hash_bytes(tmnt_board.sound_ram, sizeof(tmnt_board.sound_ram), 0xcbf29ce484222325ull);
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
	return g_z80_time;
}

extern "C" const uint8_t *ks_executed(void)
{
	return g_executed;
}
