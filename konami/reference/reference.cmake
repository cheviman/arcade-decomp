# Name:        reference.cmake
# Description: builds the reference tools of a game on Konami's 68000 boards (MAME's tmnt.cpp family) on
#              MAME's cores: kref (the original program on MAME's 68000 core against the game's board model)
#              and the oracles of the 68000 disassembler and of its lift
# Version:     0.1.0
# Created:     29-10-2026
# Modified:    29-10-2026
# Author:      Sebastian Toyos
#
# The game's CMakeLists.txt includes it when its reference tools are on, after it has made the board's
# runtime and its data library and found Python3::Interpreter, and set:
#   K_GAME_RUNTIME       the board runtime's library (the board model and the ROM set's reader: tmnt_runtime)
#   K_RUNTIME_INCLUDE    the board runtime's headers (default: the game's runtime/include)
#   K_GAME_SOUND         (optional) the game's own sound board's library, for kref --sound-board-check
# MAME's sources are MAME 0.289's (mame/, BSD-3-Clause, mame/LICENSE); ymfm's are in ymfm/ (ymfm/LICENSE).

set(K_REFERENCE ${CMAKE_CURRENT_LIST_DIR})
if(NOT K_RUNTIME_INCLUDE)
	set(K_RUNTIME_INCLUDE ${CMAKE_SOURCE_DIR}/runtime/include)
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

# The sound board: MAME's Z80 (system32/reference's copy of the core), K007232 and uPD7759, ymfm's YM2151.
# The Z80 core's steps are written from z80.lst at build time, as MAME does
set(K_Z80 ${K_REFERENCE}/../../system32/reference/mame/z80)
set(K_Z80_STEPS ${CMAKE_BINARY_DIR}/generated/cpu/z80/z80.hxx)
add_custom_command(OUTPUT ${K_Z80_STEPS}
	COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/generated/cpu/z80
	COMMAND Python3::Interpreter ${K_Z80}/z80make.py z80 ${K_Z80}/z80.lst ${K_Z80_STEPS}
	DEPENDS ${K_Z80}/z80make.py ${K_Z80}/z80.lst
	VERBATIM)
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
