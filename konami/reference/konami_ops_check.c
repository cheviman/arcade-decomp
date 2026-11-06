/**
 *    Name:        konami_ops_check.c
 *    Description: the translated program's helpers (runtime's konami/ops.h) on the inputs a file gives, for
 *                 compare_konami_ops.py to check against konami_cpu/interpreter.py's
 *    Version:     0.1.0
 *    Created:     06-11-2026
 *    Modified:    06-11-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    Not part of the game. Usage: konami_ops_check <inputs.txt> <outputs.txt>
 *    Each input line: "shift OP VALUE COUNT N Z V C", "daa A H C" or "divx X B", hexadecimal; each output line the
 *    helper's results: the shift's value, N, Z, V and C; DAA's A and carry; DIV's quotient and remainder.
 */
#include "konami/ops.h"

#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
	char kind[16];
	FILE *in, *out;

	if (argc != 3 || !(in = fopen(argv[1], "r")) || !(out = fopen(argv[2], "w")))
	{
		fprintf(stderr, "Usage: konami_ops_check <inputs.txt> <outputs.txt>\n");
		return 1;
	}
	while (fscanf(in, "%15s", kind) == 1)
	{
		unsigned a, b, c, d, e, f, g;

		if (strcmp(kind, "shift") == 0 && fscanf(in, "%x %x %x %x %x %x %x", &a, &b, &c, &d, &e, &f, &g) == 7)
			fprintf(out, "%X %X %X %X %X\n", konami_shift((int)a, KONAMI_RESULT, b, c, d, e, f, g),
				konami_shift((int)a, KONAMI_N, b, c, d, e, f, g), konami_shift((int)a, KONAMI_Z, b, c, d, e, f, g),
				konami_shift((int)a, KONAMI_V, b, c, d, e, f, g), konami_shift((int)a, KONAMI_C, b, c, d, e, f, g));
		else if (strcmp(kind, "daa") == 0 && fscanf(in, "%x %x %x", &a, &b, &c) == 3)
			fprintf(out, "%X %X\n", konami_daa(a, b, c), konami_daa_carry(a, b, c));
		else if (strcmp(kind, "divx") == 0 && fscanf(in, "%x %x", &a, &b) == 2)
			fprintf(out, "%X %X\n", konami_divx(a, b), konami_divx_remainder(a, b));
		else
		{
			fprintf(stderr, "A line konami_ops_check does not read: %s\n", kind);
			return 1;
		}
	}
	fclose(in);
	fclose(out);
	return 0;
}
