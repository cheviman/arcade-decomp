# Konami

What Konami's arcade games of 1989 to 1991 have in common, as measured in the two decompiled with this
repository: [tmntarcade-decomp](https://github.com/cheviman/tmntarcade-decomp) (Teenage Mutant Ninja Turtles,
1989, MAME's `tmnt`) and [simpsons-decomp](https://github.com/cheviman/simpsons-decomp) (The Simpsons, 1991,
MAME's `simpsons`). It is here for the next game of the family: what to expect in its ROM, what is already
written for it, and what to measure first.

Every address is of the ROM set its repository's `tools/verify_rom.py` checks, and every name is the one in
that repository's `symbols.txt`. Nothing here is a game's code or data.

## The boards

| | TMNT (GX963) | The Simpsons (GX072) |
|---|---|---|
| Main CPU | 68000, 8 MHz | 053248 (Konami's 6809), 12 MHz input |
| Tiles | K052109 + K051962 | K052109 + K051962 |
| Sprites | K051960 + K051937 | K053246 + K053247 |
| Priority | 2 bits of a register | K053251 |
| Settings | 3 banks of DIP switches | ER5911 EEPROM |
| Sound CPU | Z80, 3.579545 MHz | Z80, 3.579545 MHz, with 16 KB banks |
| FM | YM2151 | YM2151 |
| Samples | K007232, uPD7759, a title sample | K053260 |
| Commands to the Z80 | a latch, one byte | the K053260's ports, two bytes |
| Screen | 320 x 224, 6 MHz, 384 x 264 | the same |
| The frame is drawn | when the VBLANK starts | when the VBLANK ends |

A chip both boards have is the same chip, and what is written for it serves both. The K052109 is the case:
its fixed layer holds the text the same way in the two games, and its two other layers are scrolled the same
way.

Where each chip is:

| | Checked against MAME (`konami/reference/`) | In C |
|---|---|---|
| 68000 | `oracle_m68k*.cpp`, `kref.cpp` | the game's `src/`, from `m68000/` |
| 053248 | `oracle_konami*.cpp`, `kref_053248.cpp` | the game's `src/`, from `konami_cpu/` |
| Z80 | `sound/ksound.cpp`, `sound/ksound_053260.cpp` | the game's `src/sound/`, from `z80/` |
| K052109, K051960 | `video/kvideo.cpp` | TMNT's `runtime/src/video.c` |
| K052109, K053246/7, K053251 | `video/kvideo_053246.cpp` | The Simpsons' `runtime/src/video.c` |
| YM2151 | `ymfm/` | `runtime/src/ym2151.c` (The Simpsons' is a copy of TMNT's) |
| K007232, uPD7759 | `sound/ksound.cpp` | TMNT's `runtime/src/sound_chips.c` |
| K053260 | `sound/ksound_053260.cpp` | The Simpsons' `runtime/src/k053260.c` |
| ER5911 | not MAME's code: the reference runs on the game's board model | The Simpsons' `runtime/src/machine.c`, written from MAME's `eepromser` |

`reference.cmake`'s `K_CPU` picks the board's main CPU (68000 by default), and `kref` runs MAME's CPU core on
the game's own model of the board (its `runtime/`'s `machine.c`). The C of the chips lives in each
game's `runtime/`, copied from the game before it: a third game is when to decide whether it moves here.

## The system both programs are built on

The two main programs share no code: they are for different CPUs, and the 6809's is not a translation of
the 68000's. They share their design, routine by routine. A new game's program is expected to have these, and
finding them is the fastest way into it.

**The main loop and its jobs.** Outside the interrupt the program does one thing: it waits for the frame,
takes a 16-bit job from a ring of them, and runs it. The high byte is the job, an index into a table of
routines; the low byte is its argument. FFFF is an empty place: the loop finds it negative and waits again,
and a job taken is overwritten with FFFF. The first three jobs are the same in both games, in the same order:
0 clears tile layers, 1 prints a text of the game's table, 2 sets the colour job 1 prints in.

| | TMNT | The Simpsons |
|---|---|---|
| The ring | `job_queue` 060380, 64 words | `job_queue` 4944, 30 words |
| Its write and read places | 060010, 060014 | 4940, 4942 |
| `queue_job` | 004164 | 0797B7 |
| The loop's wait | 0013CE | 0789D1 |
| Jobs | 0 to 2 as above, then the scores, the players' boxes, the titles, the test mode's texts | 0 to 2 as above, 3 to 6 a player's score, 7 to 15 empty |

The wait in this loop is the frame: the profile's `FRAME_WAIT` (`FRAME_WAITS`) is its address, and the
reference's time by frames and the decompiled program's `frame_wait()` come from it.

**The frame's interrupt.** Everything else runs inside the VBLANK's interrupt, made of the same pieces in
both: count the frame, send the Z80 one command from a ring of them, read the coins and the four players' controls
(each with last frame's, to find what was just pressed), turn coins into credits, run the palette's requests,
write the two scrolling layers' scroll to the K052109 (the layer's x less a constant: 0x60 in TMNT, 0x5A in
The Simpsons), and run the routine of the game's state.

| | TMNT | The Simpsons |
|---|---|---|
| The interrupt | `vblank_interrupt` 003DC4 | `irq_handler` 078F43 |
| The frame's count | `frame_count` 0600A4 | `frame_count` 4815 |
| Commands for the Z80 | `sound_queue` 060400, 64 bytes; `queue_sound` 0091A4, `send_sound_command` 009202 | `sound_queue` 4904, 30 words; `queue_sound` 0797D3, sent in the interrupt |
| The palette's requests | `post_palette_request` 009242, `run_palette_requests` 009272 | `palette_request` 07BAC7, `run_palette_jobs` 07BC8B |

**The game's state, a byte a level.** One byte picks the mode and each byte below it steps through the level
above, each a jump table: a routine does its part and adds one to its level's byte.

| | TMNT | The Simpsons |
|---|---|---|
| The mode | `game_state` 060001: 0 start-up, 1 attract, 2 game, 3 test mode | `game_mode` 4800: 0 test mode, 1 attract, 2 game |
| Below it | `game_step` 060003, `scene` 060005, `scene_step` 060007, `scene_substep` 060009 | `mode_step` 4801, `mode_phase` 4802, `game_step` 4803, `step_phase` 4804, `step_subphase` 4805 |

Writing these bytes is how a scenario jumps to a stage: with the level below cleared, as the game does when
a step ends.

**Players and coin slots, 8 bytes each.** Four records of 8 bytes for the players and four for the coin
slots, a slot for each player: a slot's credits are turned into its player's lives.

| | TMNT | The Simpsons |
|---|---|---|
| A player | `player` 060100 + 8n: lives (a word) at 0, score (a word) at 4 | `player_values` 49D0 + 8n: lives (a word) at 0, score (a word) at 4 |
| A coin slot | `coin_slot` 060300 + 8n: coins, coins per credit, credits per coins, the counter's pulse, credits | `coin_slot` 4817 + 8n: the same fields in another order |

**Objects.** One array of fixed-size records for everything that moves: a byte of flags first, then what it
is and the state of its routine, a place and a speed on each of three axes, what it hits with and what hit
it, and a link to the next object to draw: every frame the objects are sorted by depth into that list and
written to the sprite chip from it. The record is laid out anew in each game.

| | TMNT | The Simpsons |
|---|---|---|
| The records | 062000 + 0x50 n, 50 of them | 4A40 + 0x60 n, 33 of them |
| The place | 32 bits each axis | 16 bits and a fraction byte each axis |
| The link to draw | `draw_next` at 3E | `draw_next` at 5E |
| What runs them | `run_objects` 0477EA: by the object's kind, routine and state | a list at a time (`run_enemies` 07CB3C...): by the object's type, a table of banks beside a table of routines |

**Texts.** A letter's tile in the fixed layer is its ASCII code less 0x30, so the tables of texts read as
text in the ROM with `@` for the space; `/` ends a line and the next line's place follows it (4 bytes in
TMNT, 2 in The Simpsons). The reference's `--text` reads both games' screens with no change. The test mode
has a printer and a table of its own (`print_test_text` 001EA2 and `test_mode_texts` 005E1E; `test_print`
05E650 and `test_mode_texts` 05F5BE, with a table of whole words).

**The test mode.** A menu of pages, each a step of the test mode's state: the ROM check, the cross hatch, the
colour test, the inputs, the sound test, and the settings (the DIP switches' page in TMNT, the EEPROM's
options in The Simpsons). The check at power on adds up each ROM chip against the sum the program keeps.

## What the two ROMs share byte for byte

Little, and none of it is code. Every run of 24 bytes or more common to the two main programs, and to the two
Z80 programs:

- The colour test's four ramps, 32 bytes each (16 steps of blue, red, green and grey in the palette's
  format): 00A3FE, 00A41E, 00A43E and 00A45E in TMNT; 05FC5C, 05FC18, 05FC3A and 05FBF6 in The Simpsons, in
  another order and each behind 2 bytes of its own. The same palette format gives the same ramps.
- Three phrases of the notice for Asia, 25, 24 and 25 bytes ("THIS GAME IS FOR USE ONLY", "EXPORTED OUTSIDE
  ASIA OR", "MAY VIOLATE INTERNATIONAL"): 002B08, 002B5E and 002C48 in TMNT; 025BEA, 025C38 and 025D09 in The
  Simpsons. The wording is the same and the lines are broken differently.
- In the Z80's program, two tables of the YM2151: a semitone's key code, 96 bytes (`fm_key_codes` 073D in
  TMNT, `note_codes` 0E10 in The Simpsons), and 84 words of the notes' pitches (from 0B34 in TMNT's
  `fm_note_pitches`; from 44A5 in The Simpsons, `fm_pitches` and on into `pcm_pitches`).

That is 276 bytes of The Simpsons' 524288 in the main program (74 of them a second copy of the three phrases,
at the same offset of the bank before) and 264 of the 32768 of each Z80 program. The sound drivers are of two
generations: TMNT's is for the K007232 and the uPD7759, The Simpsons' for the K053260, with other commands
and another format of music. Of the tiles, 22 of 32768 are the same: the letters are another font.

A disassembler lines the two tables of the Z80 up as instructions, and a comparison by instructions takes
them for shared routines. They are data: compare bytes, then look at what the bytes are.

## Starting another game of the family

1. **Measure before planning.** With the new ROM and the two above: the texts, the runs of bytes in common
   between the programs (and, when the main CPU is the same, the routines in common by instruction with the
   addresses masked: only then can names pass from one game to the other by signature), the Z80's programs,
   and the tiles. An afternoon, and it tells how much of the naming is new work.
2. **Find the skeleton.** The reset vector leads to the main loop: a wait, a ring with FFFF for empty, a jump
   table by the high byte. The frame's interrupt leads to the mode byte and its jump tables. From those two,
   the names above apply as they are.
3. **Take the branch that has both.** `simpsons` was cut from `tmnt` and has the 68000, the 053248, both
   sound boards and both sets of video chips; `main` does not have the 053248.
4. **Write only what the board adds**: its map, and the chips no game here has yet. A chip MAME simulates
   rather than emulates (Konami's protection chips) can only be checked against that simulation: port it as
   it is and say so.
5. **Then the usual order**, the one each game's README tells: the reference, the scenarios, the static
   analysis without traces, every byte explained and named before the C, the scaffold, the board in C, and
   the decompiler's rounds.

What each game cost that the next one should not pay again: the sound is not optional (The Simpsons' ROM
check asks the Z80 for its sums and resets without an answer); the K052109's tile cache has to be marked as
MAME marks it; a table of code the program reads with a signed index runs back from its address; and a table
of steps with no count is entered from the table before it on purpose, so giving it a length removes code the
game reaches.
