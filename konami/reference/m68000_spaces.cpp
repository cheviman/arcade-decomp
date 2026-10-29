/**
 *    Name:        m68000_spaces.cpp
 *    Description: the handler tables of MAME's 68000 core for a CPU behind an MMU, which no board here has
 *    Version:     0.1.0
 *    Created:     29-10-2026
 *    Modified:    29-10-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 *    m68000.cpp takes the "indirect" handlers (m68000gen.py sif and sip) only when an MMU is set, and the
 *    Konami boards set none: the reference builds the direct ones alone (sdf and sdp) and these stand for the
 *    others, so that four 4 MB files are two.
 */
#include "emu.h"
#include "m68000.h"

const m68000_device::handler m68000_device::s_handlers_if[] = { nullptr };
const m68000_device::handler m68000_device::s_handlers_ip[] = { nullptr };
