# arcade-decomp

Tools to decompile arcade games into readable C that does exactly what the original does, checked frame by
frame against MAME: the CPUs' frontends, the decompiler, the static analysis, the checks and the asset
formats, shared by the games decompiled with them, [segasonic-decomp](https://github.com/cheviman/segasonic-decomp)
(SegaSonic The Hedgehog, Sega System 32) and [outrunners-decomp](https://github.com/cheviman/outrunners-decomp)
(OutRunners, Sega System Multi 32). Arcade boards only: a game is its CPUs' code, its board's chips and its
ROM, which this is made for (a PC or console game, with an operating system and its libraries, is not).

The root holds what any arcade game uses; a family of boards gets a folder of its own: `system32/` will
hold Sega's System 32 and Multi 32 (the board in C and the reference that runs the original on MAME's cores),
and the CPUs and boards of the next families come the same way (Konami's 68000 and 053248 boards).

Each game keeps this repository in its `tools/core/` (a git submodule: the game's repository holds which
commit of the core it uses, not the core), and its own `tools/game_profile.py` tells the core about it.
Nothing here is a game's: no ROM, no names, no data.

## What is here

- `v60/`: the NEC V60 and V70's decoder, disassembler and lift into the decompiler's IR (the V70's extra
  instructions too).
- `z80/`: the Z80's decoder, analysis, lift, timing model and C writer, for the sound boards' programs.
- `decompiler/`: the decompiler, from the IR to C with no CPU left in it: data flow, signatures, the
  structuring of loops and branches (no goto), variables, the types of pointers (structures, families of
  objects, records of the program ROM, fixed instances), the program's memory by name, and the C writer.
- `disassemble.py`: the static analysis of the main program from `symbols.txt` (no traces), and its listing.
- `decompile.py`, `decompile_sound.py`: the game's C, `src/` and `src/sound/`, from its ROM set, `symbols.txt`
  and `sound_symbols.txt`.
- `recompile.py`: the translation scaffold the decompiler grew from (the program an instruction at a time).
- `check_structure.py`: the C's ifs, loops and switches run against the program's jumps, every function.
- `noise_report.py`: what the C still has of the CPU and of the ROM's places, against the game's
  `tools/noise_baseline.txt`.
- `coverage.py`: every byte of the program ROM classified as code, data or fill.
- `data_formats.py`, `asset_formats.py`: the program's data as text, and the graphics and samples as PNG and
  WAV files, and back, byte for byte (the game's `formats/` add its own data formats).

## A game's profile

The game's `tools/game_profile.py` is imported before anything else: it puts this repository on the path,
and gives the core

- `ROOT`, the game's repository; `rom`, its ROM set's module (`load_set`, `build_program`, `build_sound`,
  `PROGRAM_AREAS`, `canonical_address`, `ProgramMemory`);
- `GAME`, `ROM_SET_TITLE`, `CPU`, `GUARD`: what the generated files say of the game;
- the decompiler's settings, which it writes into the core's modules: the structures the game keeps
  (`pointers.STRUCTS`), what the registers the whole program shares hold (`signatures.CONTEXT`,
  `pointers.CONTEXT_KINDS`), the base of its variables, its object handler's slots, the fixed structures and
  blocks of variables, how its frame-wait loop takes interrupts, its sound chip (`SOUND_HARDWARE`)...

Each module keeps neutral defaults, and says which of its settings a profile gives.

## Using it in a game

The game's scripts in `tools/` (`decompile.py`, `decompile_sound.py`, `recompile.py`, `disassemble.py`,
`check_structure.py`, `noise_report.py`, `coverage.py`) run the core's with the core and the game's tools on
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

BSD 3-Clause License (see `LICENSE`). The reference tools the games build on MAME's cores are in the games'
repositories, under MAME's licenses.
