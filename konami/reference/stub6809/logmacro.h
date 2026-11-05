/**
 *    Name:        logmacro.h
 *    Description: stand-in for MAME's logmacro.h: the CPU core's logging, which the reference does not print
 *    Version:     0.1.0
 *    Created:     05-11-2026
 *    Modified:    05-11-2026
 *    Author:      Sebastian Toyos
 *
 *    license:BSD-3-Clause
 */
#ifndef LOG_GENERAL
#define LOG_GENERAL (1U << 0)
#define LOGMASKED(mask, ...) do {} while (false)
#define LOG(...) do {} while (false)
#endif
