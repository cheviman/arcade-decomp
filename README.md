# arcade-decomp

Tools to decompile arcade games into readable C that does exactly what the original does, checked frame by
frame against MAME: the CPUs' frontends, the decompiler, the static analysis, the checks and the asset
formats, shared by the games decompiled with them, [segasonic-decomp](https://github.com/cheviman/segasonic-decomp)
(SegaSonic The Hedgehog, Sega System 32) and [outrunners-decomp](https://github.com/cheviman/outrunners-decomp)
(OutRunners, Sega System Multi 32). Arcade boards only: a game is its CPUs' code, its board's chips and its
ROM, which this is made for (a PC or console game, with an operating system and its libraries, is not).

The root holds what any arcade game uses; a family of boards gets a folder of its own: `system32/` holds
Sega's System 32 and Multi 32 (the reference that runs the original on MAME's cores; the board in C will
follow), `konami/` Konami's 68000 boards (MAME's `tmnt.cpp` family: TMNT, Cue Brick, M.I.A. and their
successors), and the CPUs and boards of the next families come the same way (Konami's 053248 boards).

Each game keeps this repository in its `tools/core/` (a git submodule: the game's repository holds which
commit of the core it uses, not the core), and its own `tools/game_profile.py` tells the core about it.
Nothing here is a game's: no ROM, no names, no data.

## What is here

- `v60/`: the NEC V60 and V70's decoder, disassembler and lift into the decompiler's IR (the V70's extra
  instructions too).
- `z80/`: the Z80's decoder, analysis, lift, timing model and C writer, for the sound boards' programs.
- `m68000/`: the Motorola 68000's decoder and disassembler, ported from MAME's (Musashi's) and checked
  against it at every even offset of a program and of an image of every opcode
  (`konami/reference/compare_m68k_with_mame.py`), with each instruction's operands and control flow; and
  its static analysis (`m68000/analysis.py`): the program's code from its vectors and `symbols.txt`, without
  traces, following what the address registers can hold into the tables of handlers it jumps through. A
  game uses it with `ANALYSIS = "m68000.analysis"` and `DISASSEMBLER = "m68000.disassembler"` in its
  profile, which `check_scenarios.py` reads (the V70's `disassemble` and `v60.disassembler` by default).
- `decompiler/`: the decompiler, from the IR to C with no CPU left in it: data flow, the values registers can
  hold where the code reads them, signatures, the structuring of loops and branches (no goto), variables, the
  types of pointers (structures, families of objects, records of the program ROM, fixed instances), the
  program's memory by name, and the C writer.
- `disassemble.py`: the static analysis of the main program from `symbols.txt` (no traces), and its listing.
- `decompile.py`, `decompile_sound.py`: the game's C, `src/` and `src/sound/`, from its ROM set, `symbols.txt`
  and `sound_symbols.txt`.
- `recompile.py`: the translation scaffold the decompiler grew from (the program an instruction at a time).
- `check_structure.py`: the C's ifs, loops and switches run against the program's jumps, every function.
- `noise_report.py`: what the C still has of the CPU and of the ROM's places, against the game's
  `tools/noise_baseline.txt`.
- `coverage.py`: every byte of the program ROM classified as code, data or fill.
- `data_formats.py`, `asset_formats.py`: the program's data as text, and the graphics and samples as PNG and
  WAV files, and back, byte for byte. `formats/common.py` holds the data formats' helpers; `formats` is a
  namespace package (no `__init__.py`), so the game's `tools/formats/` adds its own formats to it.
- `check_scenarios.py`: every control script of the game's `tests/controls/` run on the reference and on the
  game (or the translation scaffold), compared frame by frame (state, and with `--video` and `--sound` the
  pictures and the sound), and the static analysis checked against all the code the reference ran.
- `verify_rom.py`: the player's ROM set checked file by file (CRC32 and SHA-1).
- `render_state.py`: a board state the reference saved, its text layer and tilemap pages drawn as PNG.
- `memory_census.py`: which of the board's memories each line of the C that reaches memory through an
  address reaches, over the scenarios (a build of the game with its census on).
- `system32/reference/`: the reference of a System 32 or Multi 32 game, which the scenarios compare the game
  with: `s32ref` runs the original program on MAME's V60 core against the game's board model, with MAME's
  System 32 video and its sound board (MAME's Z80, the sound chips, ymfm's YM3438), and writes each frame's
  hashes (state, picture, sound) and the addresses it executed; `oracle` and `oracle_z80`, MAME's
  disassemblers, against which `compare_with_mame.py` and `compare_z80_with_mame.py` check `v60/` and
  `z80/` at every byte; `pcmcheck`, the game's MultiPCM against MAME's. `reference.cmake` builds them: the
  game's CMake includes it, after its board runtime (`s32runtime`) and with its data library
  (`S32_GAME_DATA`). MAME's and ymfm's sources are in `mame/` and `ymfm/`. Today they are Multi 32's
  (OutRunners': two screens, the MultiPCM).
- `konami/reference/`: the reference of a game on Konami's 68000 boards: `kref` runs the original program
  on MAME's 68000 core against the game's board model (its runtime's `tmnt/machine.h`) and writes each
  frame's state hash, the addresses it executed and the ones it called, where each jump through a register
  went (`--jumps`, to find what the static analysis misses), the fixed layer as text and the
  board's memories, with MAME's video (`video/kvideo.cpp`) each frame's picture hash and frames as BMP,
  and with its sound board (`sound/ksound.cpp`: MAME's Z80, K007232 and uPD7759, ymfm's YM2151, the title
  music) each frame's samples and sound RAM hashes, the sound as a WAV and the Z80 addresses executed;
  `oracle_m68k`, MAME's 68000 disassembler, which `compare_m68k_with_mame.py` checks `m68000/` against
  (every even offset of the game's program and of an image of every opcode). `reference.cmake` builds
  them: the game's CMake includes it after its board runtime (`K_GAME_RUNTIME`). MAME 0.289's sources are
  in `mame/` (the 68000 core, whose handlers `m68000gen.py` writes from `m68000.lst` at build time, the
  direct ones only; util's bitmap and palette and emu's drawgfx and tilemap; the `tmnt.cpp` driver and its
  video chips, the K052109 and the K051960; the K007232, the uPD7759 and the YM2151) and ymfm's OPM in
  `ymfm/`; the Z80 core is `system32/reference/mame/z80`'s. `stub/emu.h` stands in for MAME's framework
  around the 68000, `video/emu.h` around the video and `sound/emu.h` around the sound board, which
  `video/kvideo.cpp` and `sound/ksound.cpp` compile unchanged inside namespaces of their own.

## A game's profile

The game's `tools/game_profile.py` is imported before anything else: it puts this repository on the path,
and gives the core

- `ROOT`, the game's repository; `rom`, its ROM set's module (`load_set`, `build_program`, `build_sound`,
  `PROGRAM_AREAS`, `canonical_address`, `ProgramMemory`);
- `GAME`, `ROM_SET_TITLE`, `CPU`, `GUARD`: what the generated files say of the game; `BINARY`, the prefix of
  its executables (`sonic_game_headless`, `orunners_headless`); `REFERENCE`, its reference's executable
  (`s32ref` by default, `kref`), and `STATIC_ANALYSIS`, false while its CPU has no frontend here yet (its
  scenarios then run on the reference alone);
- the decompiler's settings, which it writes into the core's modules: the structures the game keeps
  (`pointers.STRUCTS`), what the registers the whole program shares hold (`signatures.CONTEXT`,
  `pointers.CONTEXT_KINDS`), the base of its variables, its object handler's slots, the fixed structures and
  blocks of variables, how its frame-wait loop takes interrupts, its sound chip (`SOUND_HARDWARE`)...

Each module keeps neutral defaults, and says which of its settings a profile gives.

## Using it in a game

The game's scripts in `tools/` (`decompile.py`, `decompile_sound.py`, `recompile.py`, `disassemble.py`,
`check_structure.py`, `noise_report.py`, `coverage.py`, `check_scenarios.py`, `verify_rom.py`,
`render_state.py`, `memory_census.py`) run the core's with the core and the game's tools on
the path; the game's other tools add `tools/core` to their path to import the core's modules. A game takes
the core with

```bash
git submodule add https://github.com/cheviman/arcade-decomp tools/core
```

and a newer core (checked on every game that uses it: its scenarios frame by frame, its structure check)
with

```bash
git submodule update --remote tools/core
```

and a commit of `tools/core`. A clone of a game takes its core with `git clone --recursive`, or
`git submodule update --init` in a clone made without it.

## Legal

BSD 3-Clause License (see `LICENSE`). `system32/reference/mame/`, `system32/reference/ymfm/`,
`konami/reference/mame/` and `konami/reference/ymfm/` are MAME's and ymfm's code, BSD 3-Clause too, under
their copyright holders' terms (`mame/LICENSE`, `ymfm/LICENSE`): they go into the reference tools only, never
into a game.
