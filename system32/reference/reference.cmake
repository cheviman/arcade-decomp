# Name:        reference.cmake
# Description: builds the reference tools of a System 32 / Multi 32 game on MAME's cores: s32ref (the original
#              program on MAME's V60 core against the game's board model, with MAME's video and sound),
#              the disassemblers' oracles and pcmcheck
# Version:     0.1.0
# Created:     26-10-2026
# Modified:    26-10-2026
# Author:      Sebastian Toyos
#
# The game's CMakeLists.txt includes it when its reference tools are on, after it has made the board's
# runtime (the s32runtime library), its data library and Python3::Interpreter, and set:
#   S32_GAME_DATA        the game's data library (the assets pack and the ROM set: orunners_data)
#   S32_RUNTIME_INCLUDE  the board runtime's headers (default: the game's runtime/include)
# Today the tools are Multi 32's (OutRunners': two screens, the MultiPCM).

set(S32_REFERENCE ${CMAKE_CURRENT_LIST_DIR})
if(NOT S32_RUNTIME_INCLUDE)
	set(S32_RUNTIME_INCLUDE ${CMAKE_SOURCE_DIR}/runtime/include)
endif()

# MAME's V60 disassembler (the V70's instructions are the V60's), the one v60/disassembler.py is compared with
add_executable(oracle
	${S32_REFERENCE}/oracle.cpp
	${S32_REFERENCE}/mame/v60d.cpp
)
target_include_directories(oracle PRIVATE ${S32_REFERENCE}/stub ${S32_REFERENCE}/mame)
# MAME's Z80 disassembler, the one z80/disassembler.py is compared with
add_executable(oracle_z80
	${S32_REFERENCE}/oracle_z80.cpp
	${S32_REFERENCE}/mame/z80/z80dasm.cpp
)
target_include_directories(oracle_z80 PRIVATE ${S32_REFERENCE}/stub ${S32_REFERENCE}/mame/z80)
# MAME's Multi 32 video drawing the board model's two screens: the frames the game's video has to match
add_library(s32video_reference STATIC ${S32_REFERENCE}/video/s32video.cpp)
target_include_directories(s32video_reference PUBLIC ${S32_REFERENCE}/video PRIVATE ${S32_RUNTIME_INCLUDE})
if(MSVC)
	target_compile_options(s32video_reference PRIVATE /wd4244 /wd4267 /wd4100 /wd4146)
endif()
# MAME's Z80 and MultiPCM and ymfm's YM3438 on the board model: the sound the game's has to match. The
# Z80 core's steps are written from z80.lst at build time, as MAME does
set(S32_Z80_STEPS ${CMAKE_BINARY_DIR}/generated/cpu/z80/z80.hxx)
add_custom_command(OUTPUT ${S32_Z80_STEPS}
	COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/generated/cpu/z80
	COMMAND Python3::Interpreter ${S32_REFERENCE}/mame/z80/z80make.py z80
		${S32_REFERENCE}/mame/z80/z80.lst ${S32_Z80_STEPS}
	DEPENDS ${S32_REFERENCE}/mame/z80/z80make.py ${S32_REFERENCE}/mame/z80/z80.lst
	VERBATIM)
add_library(s32sound_reference STATIC
	${S32_REFERENCE}/sound/s32sound.cpp
	${S32_REFERENCE}/ymfm/ymfm_opn.cpp
	${S32_REFERENCE}/ymfm/ymfm_adpcm.cpp
	${S32_REFERENCE}/ymfm/ymfm_ssg.cpp
	${S32_Z80_STEPS}
)
target_include_directories(s32sound_reference PUBLIC ${S32_REFERENCE}/sound
	PRIVATE ${S32_REFERENCE}/ymfm ${S32_RUNTIME_INCLUDE} ${CMAKE_BINARY_DIR}/generated)
if(MSVC)
	target_compile_options(s32sound_reference PRIVATE /bigobj /wd4244 /wd4267 /wd4100 /wd4146 /wd4127)
endif()
# the game's MultiPCM against MAME's, sample for sample, over what s32ref --sound-log records
add_executable(pcmcheck ${S32_REFERENCE}/sound/pcmcheck.cpp)
target_include_directories(pcmcheck PRIVATE ${S32_REFERENCE}/sound ${S32_RUNTIME_INCLUDE})
target_link_libraries(pcmcheck PRIVATE s32runtime ${S32_GAME_DATA})
if(MSVC)
	target_compile_options(pcmcheck PRIVATE /wd4244 /wd4267 /wd4100 /wd4146)
endif()
add_executable(s32ref
	${S32_REFERENCE}/s32ref.cpp
	${S32_REFERENCE}/mame/v60.cpp
	${S32_REFERENCE}/mame/v60d.cpp
)
target_include_directories(s32ref PRIVATE ${S32_REFERENCE}/stub ${S32_REFERENCE}/mame)
target_link_libraries(s32ref PRIVATE s32runtime ${S32_GAME_DATA} s32video_reference s32sound_reference)
if(MSVC)
	# MAME's core marks error handlers [[noreturn]] even though they return uint32_t
	target_compile_options(s32ref PRIVATE /wd4646 /wd4244)
endif()
