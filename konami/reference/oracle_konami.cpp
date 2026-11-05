/**
 *    Name:        oracle_konami.cpp
 *    Description: runs MAME's own disassembler of Konami's CPU (the 052001 and the 053248) over an image, one
 *                 line per address
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    05-11-2026
 *    Author:      Sebastian Toyos
 *
 *    Usage: oracle_konami <image.bin> <first address> <end address> <output.txt>
 *    The image is the CPU's 64 KB as it sees them from address 0 (a bank in its window, the fixed program
 *    above), big endian; addresses wrap at 16 bits and a place past the image's end reads FF. Output:
 *    "AAAA LEN TEXT" per address (the CPU fetches its instructions from any address).
 */
#include "emu.h"
#include "6x09dasm.h"

#include <cstdlib>
#include <fstream>
#include <sstream>
#include <vector>

int main(int argc, char **argv)
{
	if (argc != 5)
	{
		std::fprintf(stderr, "Usage: oracle_konami <image.bin> <first address> <end address> <output.txt>\n");
		return 1;
	}

	std::ifstream input(argv[1], std::ios::binary);
	std::vector<u8> image((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
	if (image.empty())
	{
		std::fprintf(stderr, "The image is empty\n");
		return 1;
	}

	const u32 first = u32(std::strtoul(argv[2], nullptr, 0));
	const u32 end = u32(std::strtoul(argv[3], nullptr, 0));
	const util::disasm_interface::data_buffer buffer(image.data(), u32(image.size()));
	konami_disassembler disassembler;

	std::ofstream output(argv[4], std::ios::binary);
	char prefix[32];
	for (u32 address = first; address < end; address++)
	{
		std::ostringstream text;
		const u32 result = disassembler.disassemble(text, address, buffer, buffer);
		std::snprintf(prefix, sizeof(prefix), "%04X %u ", address, result & util::disasm_interface::LENGTHMASK);
		output << prefix << text.str() << '\n';
	}
	return 0;
}
