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
- `select_scenarios.py`: which scenarios a change has to run again, the working tree against the state last
  verified (a copy in the game's `build/verified/`, or a commit): the C function by function, comments and
  names apart, with the declarations and the structures' members; the loaded data, by its bytes and by where
  the loader lays out its values; the runtime and the build's files by their hashes. A function that changed
  needs the scenarios whose reference run executed it, data that changed the ones in which the original read
  it (`width_census.py run`), the runtime every one. `--run` runs them, `--big-endian` for a big-endian build.
- `big_endian_scenarios.py`: the control scripts on the game built for a big-endian processor, under an
  emulator (`qemu-ppc`, or the profile's `BIG_ENDIAN_EMULATOR`; through WSL on Windows), each frame's picture
  and sound samples against the reference's results.
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

## The game's state

A profile that sets `memory.STATE` (OutRunners: `"game"`) gets the work RAM as one structure, the game's
state, which the C reaches through a pointer of that name: its variables at their places
(`game->frame_count`) and the blocks `memory.STATE_BLOCKS` names between them (`game->screen_1`,
`game->race_copies[player]`), laid out as the board has it, so that each frame's state still compares byte
for byte with the original's. `src/ram.h` declares it, with static asserts on its size and on its blocks'
places. A structure inside another is a member of it (`member STRUCT OFFSET NAME INNER[N]` in
`symbols.txt`: `&screen->car`).

The first step out of that model: a profile that sets `memory.HOST` (OutRunners: `"world"`) keeps the variables
`memory.HOST_VARIABLES` names in a structure of their own in the host's memory (`world->game_frame_count`), the
ones the code reaches only by name. The state's structure keeps their places, where the generated
`world_project` puts them (`table.c`: the runtime calls it for the state's hash each frame) and `world_unproject`
takes them back; the functions `memory.HOST_SYNCED` names, which reach memory by its address, open the runtime's
window around their body (`world_ram_begin`, `world_ram_end`), while which the variables are in the work RAM. Such
a variable has no `NAME_ADDRESS`, so that a use of its address does not compile, and the decompiler stops when one
is read or written with the next ones at once (`AS16`).

Where the code reaches a structure the C keeps a pointer, not an address: in the registers the whole
program shares (`pointers.CONTEXT_KINDS`: `current_object->state`); in a function's own variables,
parameters and results that point at one structure of the state or one record of the program ROM (`struct
object *object`, `const struct sprite_animation *animation`, read in place in the game's data); and in a
variable that walks its record, reading one field and moving on to the next, as a pointer to the record it
is in (`place->width` where the original reads 2 bytes past the place it moved to; its steps within the
record go, `place++` takes it onto the next record), and where the original holds its address while it
points into the record, the C takes the address of the field there
(`address_of(&road_row_record->hit_box_along)`); a variable that walks an array of plain values is a pointer
to its values (`uint8_t *race`, `race[0]`, `race++`), from the table, variable or field it starts at.

The game's runtime gives what that C calls, and takes what it gives:

- `game`, a pointer to the state, which the runtime may put anywhere (several of them, one at a time);
- `ram(address)`: the place an address the code works out reaches, in the state or in another of the
  board's memories;
- `address_of(pointer)`: the address the original holds for the place a pointer the C keeps points at (in
  the state, its work RAM address; in another of the board's memories or in the program's data, its
  address there; for NULL, 0), where the C still needs the number; and the other way, `pointer_to(address)`
  for a place of the state or of the board's memories, `rom_pointer(address)` for one of the program's
  data, read in place;
- `game_context` (from the C): the registers the whole program shares, each one's variable and whether the
  C keeps a pointer in it, for the runtime's tasks to keep and start with.

`noise_report.py` counts what still keeps the state in the board's memory model (`MODEL`: structures
reached by casting an address, addresses used as values, functions kept as their address, tables reached
through an address, places worked out within a page, memory moved by its address, and the conversions
between the pointers the C keeps and the addresses the original holds), and the share of the game's
functions and lines of code with none of it.

## Using it in a game

The game's scripts in `tools/` (`decompile.py`, `decompile_sound.py`, `recompile.py`, `disassemble.py`,
`check_structure.py`, `noise_report.py`, `coverage.py`, `check_scenarios.py`, `verify_rom.py`,
`render_state.py`, `memory_census.py`, `select_scenarios.py`, `big_endian_scenarios.py`) run the core's with
the core and the game's tools on the path; the game's other tools add `tools/core` to their path to import the core's modules. A game takes
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

## A port's wider screen

A decompilation whose port shows a wider screen than the board's (WIDE: 16:9, 20:9, 16:10) names every place where
the code takes an object away, stops it, keeps it in or pushes it back at a side of the screen, by the edge and the
margin past it, instead of leaving the board's numbers. The port moves them all at once by its sides, and nobody has
to find them area by area from players' reports.

The Simpsons' port fixed WIDE's edges one report at a time (issues 24, 29, 31, 32, 34, 35) until the 36 places were
named (simpsons-decomp ce5a2e6). TMNT's issue 30: the skateboards' helicopters that come in at the left were put
past the wider screen's side, where the game's screen x, unsigned, went under 0: the game read it as past the right
edge, flew them away from the turtles and never took them off, and the area never ended.

In the core, `symbols.txt` lines name the comparisons. `constant FUNCTION VALUE NAME(ARGS)` is a number the function
compares with, written as a macro of the runtime's (ram.h does not define it); for a signed compare. `window
FUNCTION LOW HIGH MACRO ARG...` is an unsigned 16-bit test of a value against LOW-HIGH (HIGH not in it), written
`MACRO(value, ARG...)` or `!MACRO(...)` for outside; for an unsigned compare: x < HIGH is the window from 0, and 0
is where an x left of the screen wraps, so the window's start moves left with the side too. The decompiler stops on
a line the C does not use.

In the game, `runtime/include/game/support.h` defines `SCREEN_LEFT` and `SCREEN_RIGHT` (the board's edges in the
game's screen x: 0x40 and 0x180 in The Simpsons, 0x60 and 0x1A0 in TMNT), `SCREEN_MORE_LEFT` and `SCREEN_MORE_RIGHT`
(0 on the board; `screen_more_left` and `screen_more_right` in a port's console build), `PAST_LEFT(margin)`,
`PAST_RIGHT(margin)` and `X_BETWEEN(x, from, to)`, and `SCREEN_SPAN(left, right)` where the code keeps an x less the
left limit to compare (The Simpsons').

What is named is removal, stop, keep-in, push-back at an edge or past it. What keeps its number are the places
inside the screen (a door that opens at the middle, a soldier who turns 0x60 in from the edge), which are the play's
pace, and what a port decides to keep as the board's (where the Foot soldiers attack from, in TMNT).

The port sets `screen_more_left` and `screen_more_right` to its sides at each frame's end; a function it replaces
that must keep the board's edges calls the original with them at 0; a replacement the macros now do is dropped.

The check is the scenarios identical (on the board the numbers are the same), and a port's headless check that
reports the objects put in or gone in the sides' sight, with every area run in each WIDE against 4:3.

## Projects built with it

Each decompilation verified frame by frame against MAME, and its native port:

- **SegaSonic The Hedgehog** (Sega System 32):
  [segasonic-decomp](https://github.com/cheviman/segasonic-decomp) and
  [segasonic-port](https://github.com/cheviman/segasonic-port).
- **OutRunners** (Sega System Multi 32): [outrunners-decomp](https://github.com/cheviman/outrunners-decomp) and
  [outrunners-port](https://github.com/cheviman/outrunners-port).
- **Teenage Mutant Ninja Turtles** (Konami's 68000 board):
  [tmntarcade-decomp](https://github.com/cheviman/tmntarcade-decomp) and
  [tmnt-port](https://github.com/cheviman/tmnt-port).
- **The Simpsons** (Konami's 053248 board): [simpsons-decomp](https://github.com/cheviman/simpsons-decomp) and
  [simpsons-port](https://github.com/cheviman/simpsons-port).

And by others:

- **DonPachi** (Cave, 68000): [DonPachi Recomp](https://github.com/CapsLock4284/DonPachi-Recomp) by CapsLock4284,
  for Windows and Linux, with the board's slowdown put back cycle for cycle.

Made something with it? Open an issue and it goes on this list.

## Legal

BSD 3-Clause License (see `LICENSE`). `system32/reference/mame/`, `system32/reference/ymfm/`,
`konami/reference/mame/` and `konami/reference/ymfm/` are MAME's and ymfm's code, BSD 3-Clause too, under
their copyright holders' terms (`mame/LICENSE`, `ymfm/LICENSE`): they go into the reference tools only, never
into a game.
