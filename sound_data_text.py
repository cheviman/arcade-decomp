"""
Name:        sound_data_text.py
Description: a sound program's data pieces (a Z80 program, little endian) as text by name, and back byte for byte
Version:     0.2.0
Created:     26-11-2026
Modified:    27-11-2026
Author:      Sebastian Toyos

For a game's tools (Sunset Riders' tools/sound_data_assets.py; TMNT's and The Simpsons' sound data are to move
here): data_text.py's format for a Z80 program, little endian, its addresses and refs 16 bits, its places 4 hex
digits. The game says what its pieces are, their names, families and lines, and this module writes them and gives
them back; data_text.py says the files and the lines.
"""
from data_text import Format, Names  # noqa: F401  (Names: the game's tools build theirs from here)

Z80 = Format("little", 2, 4, "the sound program", "tools/core/sound_data_text.py")
byte_lines = Z80.byte_lines
word_lines = Z80.word_lines
record_lines = Z80.record_lines
read_lines = Z80.read_lines
write = Z80.write
read = Z80.read
compare = Z80.compare
