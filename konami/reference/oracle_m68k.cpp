/**
 *    Name:        oracle_m68k.cpp
 *    Description: runs MAME's own 68000 disassembler over an image, one line per even address
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    Usage: oracle_m68k <image.bin> <first address> <end address> <output.txt>
 *    The image is big endian, as the 68000 sees its program from address 0. Output: "AAAAAA LEN TEXT" per
 *    even address (the 68000 fetches its instructions from even addresses only).
 */
#include "emu.h"
#include "m68kdasm.h"

#include <cstdlib>
#include <fstream>
#include <sstream>
#include <vector>

int main(int argc, char **argv)
{
	if (argc != 5)
	{
		std::fprintf(stderr, "Usage: oracle_m68k <image.bin> <first address> <end address> <output.txt>\n");
		return 1;
	}

	std::ifstream input(argv[1], std::ios::binary);
	std::vector<u8> image((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
	if (image.empty())
	{
		std::fprintf(stderr, "The image is empty\n");
		return 1;
	}

	const u32 first = u32(std::strtoul(argv[2], nullptr, 0)) & ~1u;
	const u32 end = u32(std::strtoul(argv[3], nullptr, 0));
	const util::disasm_interface::data_buffer buffer(image.data(), u32(image.size()));
	m68k_disassembler disassembler(m68k_disassembler::TYPE_68000);

	std::ofstream output(argv[4], std::ios::binary);
	char prefix[32];
	for (u32 address = first; address < end; address += 2)
	{
		std::ostringstream text;
		const u32 result = disassembler.disassemble(text, address, buffer, buffer);
		std::snprintf(prefix, sizeof(prefix), "%06X %u ", address, result & util::disasm_interface::LENGTHMASK);
		output << prefix << text.str() << '\n';
	}
	return 0;
}
