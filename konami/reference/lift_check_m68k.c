/**
 *    Name:        lift_check_m68k.c
 *    Description: runs the C the translator writes for the 68000's instructions (the lift's IR printed by
 *                 m68000/emit_c.py, on the runtime's m68000/ops.h) from the states the lift's oracle gives,
 *                 and writes what each leaves, as konami/reference/oracle_m68k_step writes MAME's
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    Not part of the game. Usage: lift_check_m68k <program.bin> <states.txt> <results.txt>
 *    compare_lift_with_mame.py --c writes the instructions' C (build/lift_check/cases.c: a function per
 *    instruction that returns the address of the next one) and the states, each line "CASE PC SR IRQ D0 ... D7
 *    A0 ... A6 USP SSP" with the number of its instruction's function, builds this and compares its results
 *    with MAME's. Memory is the oracle's (the program image, then a pattern of the address); the bus cycles are
 *    listed as the oracle lists them. The exceptions and the interrupts push their frame as the runtime does
 *    (runtime/src/m68000.c), and their handler is not run: the next address is the handler's.
 */
#include "m68000/cpu.h"
#include "m68000/ops.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint32_t (*lift_case)(void);
extern const lift_case lift_cases[];
extern const size_t lift_case_count;

m68k_cpu cpu;

static uint8_t *image;
static size_t image_size;
static uint8_t written[1u << 24];
static uint8_t touched[1u << 24];
static uint32_t touched_list[4096];
static size_t touched_count;
static char accesses[16384];
static size_t accesses_length;

/* ---- the oracle's memory ---- */

static uint8_t pattern(uint32_t address)
{
	return (uint8_t)((address * 2654435761u) >> 13);
}

static uint8_t byte_at(uint32_t address)
{
	address &= 0xFFFFFFu;
	if (touched[address])
		return written[address];
	return address < image_size ? image[address] : pattern(address);
}

static void log_access(char kind, uint32_t address, uint16_t mask, uint16_t data)
{
	if (accesses_length + 32 < sizeof(accesses))
		accesses_length += (size_t)snprintf(accesses + accesses_length, sizeof(accesses) - accesses_length,
			" %c:%06X:%04X:%04X", kind, address & 0xFFFFFFu, mask, data);
}

static void store_byte(uint32_t address, uint8_t value)
{
	address &= 0xFFFFFFu;
	if (!touched[address] && touched_count < sizeof(touched_list) / sizeof(touched_list[0]))
		touched_list[touched_count++] = address;
	touched[address] = 1;
	written[address] = value;
}

uint16_t tmnt_read16(uint32_t address, uint16_t mem_mask)
{
	uint16_t data;

	address &= 0xFFFFFEu;
	data = (uint16_t)(byte_at(address) << 8 | byte_at(address + 1u));
	log_access('R', address, mem_mask, data);
	return data;
}

/* A PC-relative operand: the core reads it through its program space, which the oracle does not list */
uint32_t program_read8(uint32_t address)
{
	return byte_at(address);
}

uint32_t program_read16(uint32_t address)
{
	return (uint32_t)(byte_at(address) << 8 | byte_at(address + 1u));
}

uint32_t program_read32(uint32_t address)
{
	return program_read16(address) << 16 | program_read16(address + 2u);
}

void tmnt_write16(uint32_t address, uint16_t data, uint16_t mem_mask)
{
	address &= 0xFFFFFEu;
	log_access('W', address, mem_mask, data);
	if (mem_mask & 0xFF00u)
		store_byte(address, (uint8_t)(data >> 8));
	if (mem_mask & 0x00FFu)
		store_byte(address + 1u, (uint8_t)data);
}

/* ---- the status register, the exceptions and the interrupts, as runtime/src/m68000.c has them ---- */

uint32_t m68k_read_sr(void)
{
	return (cpu.r[M68K_SR_SYSTEM] & M68K_SR_MASK) | (uint32_t)cpu.x << 4 | (uint32_t)cpu.s << 3
		| (uint32_t)cpu.z << 2 | (uint32_t)cpu.ov << 1 | cpu.cy;
}

void m68k_write_sr(uint32_t value)
{
	const uint32_t system = value & M68K_SR_MASK;

	if ((system ^ cpu.r[M68K_SR_SYSTEM]) & M68K_SR_S)
	{
		const uint32_t sp = cpu.r[15];

		cpu.r[15] = cpu.r[M68K_OTHER_SP];
		cpu.r[M68K_OTHER_SP] = sp;
	}
	cpu.r[M68K_SR_SYSTEM] = system;
	cpu.x = (uint8_t)(value >> 4 & 1u);
	cpu.s = (uint8_t)(value >> 3 & 1u);
	cpu.z = (uint8_t)(value >> 2 & 1u);
	cpu.ov = (uint8_t)(value >> 1 & 1u);
	cpu.cy = (uint8_t)(value & 1u);
}

/* The frame of the exception 'vector' pushed with 'pc' (with 'level', an interrupt's mask): the handler's
   address */
uint32_t lift_check_exception(uint32_t vector, uint32_t pc, int level)
{
	const uint32_t old = m68k_read_sr();
	uint32_t sr = (old & ~0x8000u) | M68K_SR_S;
	uint32_t sp;

	if (level > 0)
		sr = (sr & ~0x0700u) | (uint32_t)level << 8;
	m68k_write_sr(sr);
	sp = cpu.r[15];
	mem_write16(sp - 2u, (uint16_t)pc);
	mem_write16(sp - 6u, (uint16_t)old);
	mem_write16(sp - 4u, (uint16_t)(pc >> 16));
	cpu.r[15] = sp - 6u;
	return mem_read32(vector * 4u);
}

void m68k_stop(uint32_t value)
{
	m68k_write_sr(value);
}

void m68k_reset_devices(void)
{
}

int main(int argc, char **argv)
{
	FILE *input;
	FILE *states;
	FILE *results;
	char line[1024];

	if (argc != 4)
	{
		fprintf(stderr, "Usage: lift_check_m68k <program.bin> <states.txt> <results.txt>\n");
		return 1;
	}
	input = fopen(argv[1], "rb");
	if (!input)
	{
		fprintf(stderr, "%s: cannot be read\n", argv[1]);
		return 1;
	}
	fseek(input, 0, SEEK_END);
	image_size = (size_t)ftell(input);
	fseek(input, 0, SEEK_SET);
	image = malloc(image_size);
	if (!image || fread(image, 1, image_size, input) != image_size)
	{
		fprintf(stderr, "%s: cannot be read\n", argv[1]);
		return 1;
	}
	fclose(input);
	states = fopen(argv[2], "r");
	results = fopen(argv[3], "w");
	if (!states || !results)
	{
		fprintf(stderr, "%s or %s cannot be opened\n", argv[2], argv[3]);
		return 1;
	}

	while (fgets(line, sizeof(line), states))
	{
		unsigned long values[21];
		uint32_t pc, sr, irq, usp, ssp, next;
		size_t index;
		char *cursor = line;

		for (index = 0; index < 21; index++)
			values[index] = strtoul(cursor, &cursor, index == 0 ? 10 : 16);
		if (values[0] >= lift_case_count)
		{
			fprintf(stderr, "A states line names no instruction: %s", line);
			return 1;
		}
		pc = (uint32_t)values[1];
		sr = (uint32_t)values[2];
		irq = (uint32_t)values[3];
		usp = (uint32_t)values[19];
		ssp = (uint32_t)values[20];
		(void)pc;

		while (touched_count)
			touched[touched_list[--touched_count]] = 0;
		accesses_length = 0;
		accesses[0] = 0;
		memset(&cpu, 0, sizeof(cpu));
		for (index = 0; index < 15; index++)
			cpu.r[index] = (uint32_t)values[4 + index];
		cpu.r[M68K_SR_SYSTEM] = sr & M68K_SR_MASK;
		cpu.r[15] = (sr & M68K_SR_S) ? ssp : usp;
		cpu.r[M68K_OTHER_SP] = (sr & M68K_SR_S) ? usp : ssp;
		cpu.x = (uint8_t)(sr >> 4 & 1u);
		cpu.s = (uint8_t)(sr >> 3 & 1u);
		cpu.z = (uint8_t)(sr >> 2 & 1u);
		cpu.ov = (uint8_t)(sr >> 1 & 1u);
		cpu.cy = (uint8_t)(sr & 1u);

		next = lift_cases[values[0]]();
		if (irq && (irq == 7 || irq > (cpu.r[M68K_SR_SYSTEM] >> 8 & 7u)))
			next = lift_check_exception(24u + irq, next, (int)irq);

		usp = (cpu.r[M68K_SR_SYSTEM] & M68K_SR_S) ? cpu.r[M68K_OTHER_SP] : cpu.r[15];
		ssp = (cpu.r[M68K_SR_SYSTEM] & M68K_SR_S) ? cpu.r[15] : cpu.r[M68K_OTHER_SP];
		fprintf(results, "%06X %04X", next & 0xFFFFFFu, m68k_read_sr());
		for (index = 0; index < 15; index++)
			fprintf(results, " %08X", cpu.r[index]);
		fprintf(results, " %08X %08X |%s\n", usp, ssp, accesses);
	}
	fclose(results);
	return 0;
}
