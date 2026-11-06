# Name:        reference.cmake
# Description: builds the reference tools of a game on Konami's 68000 boards (MAME's tmnt.cpp family) or on
#              Konami's own CPU (the 053248: The Simpsons) on MAME's cores: kref (the original program on MAME's
#              CPU core against the game's board model) and the oracles of the CPU's disassembler (and of the
#              68000's lift)
# Version:     0.1.0
# Created:     29-10-2026
# Modified:    05-11-2026
# Author:      Sebastian Toyos
#
# The game's CMakeLists.txt includes it when its reference tools are on, after it has made the board's
# runtime and its data library and found Python3::Interpreter, and set:
#   K_CPU                the board's main CPU: 68000 (the default: the tmnt.cpp family) or 053248 (Konami's own
#                        CPU, a 6809 with Konami's opcodes: The Simpsons, Vendetta...)
#   K_GAME_RUNTIME       the board runtime's library (the board model and the ROM set's reader: tmnt_runtime)
#   K_RUNTIME_INCLUDE    the board runtime's headers (default: the game's runtime/include)
#   K_GAME_SOUND         (optional) the game's own sound board's library, for kref --sound-board-check
# MAME's sources are MAME 0.289's (mame/, BSD-3-Clause, mame/LICENSE); ymfm's are in ymfm/ (ymfm/LICENSE).

set(K_REFERENCE ${CMAKE_CURRENT_LIST_DIR})
if(NOT K_RUNTIME_INCLUDE)
	set(K_RUNTIME_INCLUDE ${CMAKE_SOURCE_DIR}/runtime/include)
endif()
if(NOT K_CPU)
	set(K_CPU 68000)
endif()

# The sound boards' Z80: system32/reference's copy of MAME's core, whose steps are written from z80.lst at build
# time, as MAME does
set(K_Z80 ${K_REFERENCE}/../../system32/reference/mame/z80)
set(K_Z80_STEPS ${CMAKE_BINARY_DIR}/generated/cpu/z80/z80.hxx)
add_custom_command(OUTPUT ${K_Z80_STEPS}
	COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/generated/cpu/z80
	COMMAND Python3::Interpreter ${K_Z80}/z80make.py z80 ${K_Z80}/z80.lst ${K_Z80_STEPS}
	DEPENDS ${K_Z80}/z80make.py ${K_Z80}/z80.lst
	VERBATIM)
# Konami's CPU (the 052001 and the 053248): MAME's 6809 core with Konami's opcodes, whose steps are written from
# m6809.lst and konami.lst at build time, as MAME's own build does (m6809make.py), on stub6809/emu.h; and its
# disassembler, the one the CPU's frontend is compared with (oracle_konami)
if(K_CPU STREQUAL "053248")
	set(K_M6809 ${K_REFERENCE}/mame/m6809)
	set(K_M6809_GENERATED ${CMAKE_BINARY_DIR}/generated/cpu/m6809)
	set(K_M6809_OUTPUTS "")
	foreach(K_LIST m6809 konami)
		add_custom_command(OUTPUT ${K_M6809_GENERATED}/${K_LIST}.hxx
			COMMAND ${CMAKE_COMMAND} -E make_directory ${K_M6809_GENERATED}
			COMMAND ${CMAKE_COMMAND} -DPYTHON=${Python3_EXECUTABLE} -DSCRIPT=${K_M6809}/m6809make.py
				-DINPUT=${K_M6809}/${K_LIST}.lst -DOUTPUT=${K_M6809_GENERATED}/${K_LIST}.hxx
				-P ${K_REFERENCE}/run_to_file.cmake
			DEPENDS ${K_M6809}/m6809make.py ${K_M6809}/${K_LIST}.lst ${K_M6809}/base6x09.lst
			VERBATIM)
		list(APPEND K_M6809_OUTPUTS ${K_M6809_GENERATED}/${K_LIST}.hxx)
	endforeach()
	add_library(konami_cpu_reference STATIC
		${K_M6809}/m6809.cpp
		${K_M6809}/konami.cpp
		${K_M6809}/6x09dasm.cpp
		${K_M6809_OUTPUTS}
	)
	target_include_directories(konami_cpu_reference PUBLIC ${K_REFERENCE}/stub6809 ${K_M6809}
		PRIVATE ${CMAKE_BINARY_DIR}/generated)
	target_compile_features(konami_cpu_reference PUBLIC cxx_std_20)
	if(MSVC)
		target_compile_options(konami_cpu_reference PRIVATE /bigobj /wd4244 /wd4267 /wd4100 /wd4146 /wd4127)
	endif()

	add_executable(oracle_konami ${K_REFERENCE}/oracle_konami.cpp)
	target_link_libraries(oracle_konami PRIVATE konami_cpu_reference)

	# The lift's oracle: one instruction at a time on the core, from given states (compare_konami_lift_with_mame.py)
	add_executable(oracle_konami_step ${K_REFERENCE}/oracle_konami_step.cpp)
	target_link_libraries(oracle_konami_step PRIVATE konami_cpu_reference)

	# The sound board: MAME's Z80 and K053260, ymfm's YM2151 (sound/ksound_053260.cpp)
	add_library(ksound_053260_reference STATIC
		${K_REFERENCE}/sound/ksound_053260.cpp
		${K_REFERENCE}/ymfm/ymfm_opm.cpp
		${K_Z80_STEPS}
	)
	target_include_directories(ksound_053260_reference
		PUBLIC ${K_REFERENCE}/sound
		PRIVATE ${K_RUNTIME_INCLUDE} ${K_REFERENCE}/ymfm ${K_Z80} ${K_REFERENCE}/mame/sound
			${CMAKE_BINARY_DIR}/generated)
	target_compile_features(ksound_053260_reference PUBLIC cxx_std_20)
	if(MSVC)
		target_compile_options(ksound_053260_reference PRIVATE /bigobj /wd4244 /wd4267 /wd4100 /wd4146 /wd4127 /wd4018)
	endif()

	# MAME's K052109, K053246/K053247 and K053251 drawing the board model's frames (video/kvideo_053246.cpp)
	add_library(kvideo_053246_reference STATIC ${K_REFERENCE}/video/kvideo_053246.cpp)
	target_include_directories(kvideo_053246_reference
		PUBLIC ${K_REFERENCE}/video
		PRIVATE ${K_RUNTIME_INCLUDE} ${K_REFERENCE}/mame/emu ${K_REFERENCE}/mame/util ${K_REFERENCE}/mame/konami)
	target_compile_features(kvideo_053246_reference PUBLIC cxx_std_20)
	if(MSVC)
		target_compile_options(kvideo_053246_reference PRIVATE /bigobj /wd4244 /wd4267 /wd4100 /wd4146 /wd4127 /wd4018)
	endif()

	# kref: the original program on MAME's core against the game's board model (its runtime's simpsons/machine.h)
	# (the CPU's library first: its emu.h is the one kref takes)
	add_executable(kref ${K_REFERENCE}/kref_053248.cpp)
	target_include_directories(kref PRIVATE ${K_RUNTIME_INCLUDE})
	target_link_libraries(kref PRIVATE konami_cpu_reference ksound_053260_reference kvideo_053246_reference
		${K_GAME_RUNTIME})
	if(MSVC)
		target_compile_options(kref PRIVATE /wd4244 /wd4267)
	endif()
	return()    # the rest is the 68000 boards'
endif()

# MAME's 68000 core: its handlers are written from m68000.lst at build time, as MAME's own build does
# (m68000gen.py); the direct ones only (m68000_spaces.cpp)
set(K_M68000 ${K_REFERENCE}/mame/m68000)
set(K_M68000_GENERATED ${CMAKE_BINARY_DIR}/generated/cpu/m68000)
set(K_M68000_OUTPUTS "")
foreach(K_PART header:m68000-head.h decode:m68000-decode.cpp sdf:m68000-sdf.cpp sdp:m68000-sdp.cpp)
	string(REPLACE ":" ";" K_PART "${K_PART}")
	list(GET K_PART 0 K_MODE)
	list(GET K_PART 1 K_FILE)
	add_custom_command(OUTPUT ${K_M68000_GENERATED}/${K_FILE}
		COMMAND ${CMAKE_COMMAND} -E make_directory ${K_M68000_GENERATED}
		COMMAND Python3::Interpreter ${K_M68000}/m68000gen.py ${K_MODE} ${K_M68000}/m68000.lst
			${K_M68000_GENERATED}/${K_FILE}
		DEPENDS ${K_M68000}/m68000gen.py ${K_M68000}/m68000.lst
		VERBATIM)
	list(APPEND K_M68000_OUTPUTS ${K_M68000_GENERATED}/${K_FILE})
endforeach()
add_library(m68000_reference STATIC
	${K_M68000}/m68000.cpp
	${K_M68000}/m68kdasm.cpp
	${K_REFERENCE}/m68000_spaces.cpp
	${K_M68000_OUTPUTS}
)
target_include_directories(m68000_reference PUBLIC ${K_REFERENCE}/stub ${K_M68000} ${K_M68000_GENERATED})
target_compile_features(m68000_reference PUBLIC cxx_std_20)
if(MSVC)
	target_compile_options(m68000_reference PRIVATE /bigobj /wd4244 /wd4267 /wd4100 /wd4146 /wd4127)
endif()

# MAME's 68000 disassembler, the one the 68000 frontend (m68000/) is compared with
add_executable(oracle_m68k ${K_REFERENCE}/oracle_m68k.cpp)
target_link_libraries(oracle_m68k PRIVATE m68000_reference)

# MAME's 68000 core running one instruction at a time from given states, the one the 68000's lift
# (m68000/lift.py) is compared with (compare_lift_with_mame.py)
add_executable(oracle_m68k_step ${K_REFERENCE}/oracle_m68k_step.cpp)
target_link_libraries(oracle_m68k_step PRIVATE m68000_reference)

# The C the translator writes for the program's instructions, on the game runtime's m68000/ops.h, run from the
# same states (compare_lift_with_mame.py --c writes build/lift_check/cases.c from the user's ROM)
file(GLOB K_LIFT_CASES CONFIGURE_DEPENDS ${CMAKE_SOURCE_DIR}/build/lift_check/cases.c)
if(K_LIFT_CASES)
	add_executable(lift_check_m68k ${K_REFERENCE}/lift_check_m68k.c ${K_LIFT_CASES})
	target_include_directories(lift_check_m68k PRIVATE ${K_RUNTIME_INCLUDE})
	if(MSVC)
		target_compile_options(lift_check_m68k PRIVATE /bigobj)
	endif()
endif()

# MAME's K052109 and K051960 drawing the board model's frames: the frames the game's video has to match
add_library(kvideo_reference STATIC ${K_REFERENCE}/video/kvideo.cpp)
target_include_directories(kvideo_reference
	PUBLIC ${K_REFERENCE}/video
	PRIVATE ${K_RUNTIME_INCLUDE} ${K_REFERENCE}/mame/emu ${K_REFERENCE}/mame/util ${K_REFERENCE}/mame/konami)
target_compile_features(kvideo_reference PUBLIC cxx_std_20)
if(MSVC)
	target_compile_options(kvideo_reference PRIVATE /bigobj /wd4244 /wd4267 /wd4100 /wd4146 /wd4127 /wd4018)
endif()

# The sound board: MAME's Z80 (system32/reference's copy of the core), K007232 and uPD7759, ymfm's YM2151
add_library(ksound_reference STATIC
	${K_REFERENCE}/sound/ksound.cpp
	${K_REFERENCE}/ymfm/ymfm_opm.cpp
	${K_Z80_STEPS}
)
target_include_directories(ksound_reference
	PUBLIC ${K_REFERENCE}/sound
	PRIVATE ${K_RUNTIME_INCLUDE} ${K_REFERENCE}/ymfm ${K_Z80} ${K_REFERENCE}/mame/sound
		${CMAKE_BINARY_DIR}/generated)
target_compile_features(ksound_reference PUBLIC cxx_std_20)
if(MSVC)
	target_compile_options(ksound_reference PRIVATE /bigobj /wd4244 /wd4267 /wd4100 /wd4146 /wd4127 /wd4018)
endif()

add_executable(kref ${K_REFERENCE}/kref.cpp)
target_include_directories(kref PRIVATE ${K_RUNTIME_INCLUDE})
target_link_libraries(kref PRIVATE m68000_reference kvideo_reference ksound_reference ${K_GAME_RUNTIME})
# the game's own sound board, when it has one (K_GAME_SOUND: the decompiled sound program on the board's own
# chips), which kref --sound-board-check runs beside MAME's
if(K_GAME_SOUND)
	target_link_libraries(kref PRIVATE ${K_GAME_SOUND})
	target_compile_definitions(kref PRIVATE K_GAME_SOUND_BOARD=1)
endif()
