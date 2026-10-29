# Name:        reference.cmake
# Description: builds the reference tools of a game on Konami's 68000 boards (MAME's tmnt.cpp family) on
#              MAME's cores: kref (the original program on MAME's 68000 core against the game's board model)
#              and the 68000 disassembler's oracle
# Version:     0.1.0
# Created:     29-10-2026
# Modified:    29-10-2026
# Author:      Sebastian Toyos
#
# The game's CMakeLists.txt includes it when its reference tools are on, after it has made the board's
# runtime and its data library and found Python3::Interpreter, and set:
#   K_GAME_RUNTIME       the board runtime's library (the board model and the ROM set's reader: tmnt_runtime)
#   K_RUNTIME_INCLUDE    the board runtime's headers (default: the game's runtime/include)
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

# MAME's K052109 and K051960 drawing the board model's frames: the frames the game's video has to match
add_library(kvideo_reference STATIC ${K_REFERENCE}/video/kvideo.cpp)
target_include_directories(kvideo_reference
	PUBLIC ${K_REFERENCE}/video
	PRIVATE ${K_RUNTIME_INCLUDE} ${K_REFERENCE}/mame/emu ${K_REFERENCE}/mame/util ${K_REFERENCE}/mame/konami)
target_compile_features(kvideo_reference PUBLIC cxx_std_20)
if(MSVC)
	target_compile_options(kvideo_reference PRIVATE /bigobj /wd4244 /wd4267 /wd4100 /wd4146 /wd4127 /wd4018)
endif()

add_executable(kref ${K_REFERENCE}/kref.cpp)
target_include_directories(kref PRIVATE ${K_RUNTIME_INCLUDE})
target_link_libraries(kref PRIVATE m68000_reference kvideo_reference ${K_GAME_RUNTIME})
