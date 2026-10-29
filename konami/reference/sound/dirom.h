/**
 *    Name:        dirom.h
 *    Description: stand-in for MAME's dirom.h: a device's ROM, its region by the device's tag (emu.h, set_region)
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    The uPD7759 reads its samples through it. A region smaller than the address space repeats over it, as
 *    MAME maps it.
 */
#pragma once

template <int AddrWidth, int AddrShift = 0, int DataWidth = 0, endianness_t Endian = ENDIANNESS_LITTLE>
class device_rom_interface
{
public:
	device_rom_interface(const machine_config &, device_t &device) : m_device(device) {}
	virtual ~device_rom_interface() = default;

	u8 read_byte(offs_t address)
	{
		if (!m_base)
		{
			const auto found = region_table().find(m_device.tag());
			if (found == region_table().end())
				return 0;
			m_base = found->second.first;
			m_size = found->second.second;
		}
		return m_base[(address & ((1u << AddrWidth) - 1)) % m_size];
	}

protected:
	virtual void rom_bank_pre_change() {}

private:
	device_t &m_device;
	const u8 *m_base = nullptr;
	size_t m_size = 0;
};
