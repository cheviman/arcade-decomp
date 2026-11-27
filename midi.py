"""
Name:        midi.py
Description: Standard MIDI Files for a sound driver's music: the driver's own events in them, and back
Version:     0.1.0
Created:     27-11-2026
Modified:    27-11-2026
Author:      Sebastian Toyos

What every game's sound_music.py shares (The Simpsons' was the first, TMNT's has the same; Sunset Riders' is the
first to take it from here): a file is format 1, its tracks' events in order at a tick, Note Offs first; the driver's
events that MIDI has none for are sequencer-specific meta events, FF 7F LEN 7D TAG DATA (7D: the non-commercial
manufacturer id; LEN a variable-length quantity), the tags the games share:
  'N' (4E)  the song's header: its place's name (ASCII), a 0 byte, then the commands that play it
  'S' (53)  the place a channel's track starts at: a name of the layout, as ASCII text, maybe +0xNN
  'C' (43)  a command, its bytes as the driver reads them (an address as a place's name, ASCII, where the game's
            tool says)
  'R' (52)  a rest: no data, its length the time to the channel's next step
  'H' (48)  a held note: the bytes the game's driver reads for it (the Note On at the same tick, if any, plays it)
  'Z' (5A)  where the file stops showing the channel: no data; the step before it ends here
  'U' (55)  bytes no file shows: a place's name, a 0 byte, the bytes from there
The way back: parse() reads a file's events, driver_tracks() the driver's tracks from them (a read a tick: its
commands, then its step), Rebuilt keeps the bytes they give by place, and a byte two places give differently is an
error (MusicError says where).
"""
import re
from bisect import bisect_right

TAG = 0x7D                      # the non-commercial manufacturer id of the meta events FF 7F


class MusicError(Exception):
    """What does not come back from the files, or does not go into them"""



def vlq(value):
    """A variable-length quantity"""
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def meta(kind, data):
    return bytes([0xFF, kind]) + vlq(len(data)) + data


def driver_event(tag, data=b""):
    return meta(0x7F, bytes([TAG, ord(tag)]) + data)


class Track:
    """A track's events, in order at a tick, Note Offs first"""

    def __init__(self):
        self.events = []

    def add(self, tick, data, note_off=False):
        self.events.append((tick, 0 if note_off else 1, len(self.events), data))

    def chunk(self):
        out = bytearray()
        now = 0
        for tick, _, _, data in sorted(self.events):
            out += vlq(tick - now) + data
            now = tick
        out += b"\x00\xFF\x2F\x00"
        return b"MTrk" + len(out).to_bytes(4, "big") + bytes(out)


class Names:
    """The places' names: 'pieces' {first: (stop, name)}"""

    def __init__(self, pieces):
        self.starts = sorted(pieces)
        self.pieces = pieces
        self.addresses = {name: first for first, (_, name) in pieces.items()}

    def name(self, place):
        index = bisect_right(self.starts, place) - 1
        if index >= 0:
            first = self.starts[index]
            stop, name = self.pieces[first]
            if first <= place < stop:
                return name if place == first else f"{name}+0x{place - first:X}"
        raise MusicError(f"{place:05X} is in no piece of the sound region")

    def place(self, text, where):
        base, plus, offset = text.partition("+")
        if base not in self.addresses or (plus and not re.fullmatch(r"0x[0-9A-Fa-f]+", offset)):
            raise MusicError(f"{where}: no place is named {text}")
        return self.addresses[base] + (int(offset, 16) if plus else 0)



def parse(data, where):
    """A Standard MIDI File: (division, [track's events: (tick, kind, values)]), kinds 'on' (channel, key, velocity),
    'off' (channel, key) and 'meta' (type, bytes); the others left out"""
    if data[:4] != b"MThd" or len(data) < 14:
        raise MusicError(f"{where} is no Standard MIDI File")
    division = int.from_bytes(data[12:14], "big")
    if division & 0x8000 or not division:
        raise MusicError(f"{where}: its time is no ticks a quarter note")
    at = 8 + int.from_bytes(data[4:8], "big")
    tracks = []
    while at + 8 <= len(data):
        kind, size = data[at:at + 4], int.from_bytes(data[at + 4:at + 8], "big")
        body = data[at + 8:at + 8 + size]
        at += 8 + size
        if kind != b"MTrk":
            continue
        events, tick, place, status = [], 0, 0, None

        def number():
            nonlocal place
            value = 0
            for _ in range(4):
                if place >= len(body):
                    raise MusicError(f"{where}: track {len(tracks) + 1} ends inside an event")
                byte = body[place]
                place += 1
                value = value << 7 | byte & 0x7F
                if not byte & 0x80:
                    return value
            raise MusicError(f"{where}: track {len(tracks) + 1}: a variable-length quantity of more than 4 bytes")

        while place < len(body):
            tick += number()
            if place >= len(body):
                raise MusicError(f"{where}: track {len(tracks) + 1} ends inside an event")
            byte = body[place]
            if byte == 0xFF:
                if place + 1 >= len(body):
                    raise MusicError(f"{where}: track {len(tracks) + 1} ends inside an event")
                kind_ = body[place + 1]
                place += 2
                size_ = number()
                if kind_ == 0x2F:
                    break
                events.append((tick, "meta", (kind_, bytes(body[place:place + size_]))))
                place += size_
                continue
            if byte in (0xF0, 0xF7):
                place += 1
                place += number()
                continue
            if byte & 0x80:
                if byte > 0xF0:
                    raise MusicError(f"{where}: track {len(tracks) + 1}, tick {tick}: a status byte {byte:02X}")
                status = byte
                place += 1
            elif status is None:
                raise MusicError(f"{where}: track {len(tracks) + 1}, tick {tick}: data with no status before it")
            size_ = 1 if status & 0xE0 == 0xC0 else 2
            if place + size_ > len(body):
                raise MusicError(f"{where}: track {len(tracks) + 1} ends inside an event")
            values = body[place:place + size_]
            place += size_
            if status & 0xF0 == 0x90 and values[1]:
                events.append((tick, "on", (status & 0x0F, values[0], values[1])))
            elif status & 0xF0 in (0x80, 0x90):
                events.append((tick, "off", (status & 0x0F, values[0])))
        tracks.append(events)
    return division, tracks


class Rebuilt:
    """The bytes the files give, by place, and where each came from"""

    def __init__(self, names):
        self.names = names
        self.bytes = {}

    def put(self, place, data, where):
        for offset, byte in enumerate(data):
            at = place + offset
            if at in self.bytes and self.bytes[at][0] != byte:
                raise MusicError(f"{where}: the byte at {self.names.name(at)} comes back as {byte:02X}, and as "
                                 f"{self.bytes[at][0]:02X} from {self.bytes[at][1]}")
            self.bytes.setdefault(at, (byte, where))


def driver_tracks(tracks, where):
    """The tracks with an 'S' event, as [(start name, [read: [tick, [command bytes], step, Note Off tick]], here)],
    and the file's 'N' and 'U' events. A read is a tick's events, the commands and then a step: ('note', key),
    ('rest',), ('held', bytes, key), ('cut',); after a command FF, nothing. The 'N' event as (place, where, commands)"""
    header, unshown, found = None, [], []
    for index, events in enumerate(tracks):
        start, reads, open_note, ended = None, [], None, False
        here = f"{where}, track {index + 1}"
        for tick, kind, values in sorted(events, key=lambda event: (event[0], event[1] != "off")):
            if kind == "off":
                if open_note is not None and open_note[0] == values[1]:
                    open_note[1][3] = tick
                    open_note = None
                continue
            if kind == "meta":
                if values[0] != 0x7F or values[1][:1] != bytes([TAG]) or len(values[1]) < 2:
                    continue
                tag, data = chr(values[1][1]), values[1][2:]
                if tag == "N":
                    place, zero, commands = data.partition(b"\x00")
                    if not zero:
                        raise MusicError(f"{here}, tick {tick}: an 'N' event with no place")
                    header = (place.decode("ascii", "replace"), f"{here}, tick {tick}", bytes(commands))
                    continue
                if tag == "U":
                    place, zero, rest = data.partition(b"\x00")
                    if not zero:
                        raise MusicError(f"{here}, tick {tick}: a 'U' event with no place")
                    unshown.append((place.decode("ascii", "replace"), rest, f"{here}, tick {tick}"))
                    continue
                if tag == "S":
                    start = data.decode("ascii", "replace")
                    continue
                if tag not in "CRHZ":
                    continue
            if start is None:
                raise MusicError(f"{here}, tick {tick}: the driver's events come before its start ('S')")
            if ended:
                raise MusicError(f"{here}, tick {tick}: an event after the channel's end")
            if not reads or reads[-1][0] != tick:
                if reads and reads[-1][2] is None:
                    raise MusicError(f"{here}, tick {reads[-1][0]}: commands with no step after them")
                reads.append([tick, [], None, None])
            read = reads[-1]
            if kind == "on":
                if read[2] is not None and not (read[2][0] == "held" and read[2][2] is None):
                    raise MusicError(f"{here}, tick {tick}: a second step at a tick")
                if open_note is not None:
                    raise MusicError(f"{here}, tick {tick}: a note starts before the one at tick {open_note[1][0]} "
                                     f"ends")
                read[2] = ("held", read[2][1], values[1]) if read[2] is not None else ("note", values[1])
                open_note = (values[1], read)
                continue
            if read[2] is not None:
                raise MusicError(f"{here}, tick {tick}: an event after the tick's step")
            if tag == "C":
                if not data:
                    raise MusicError(f"{here}, tick {tick}: a command with no bytes")
                read[1].append(data)
                ended = data[0] == 0xFF
            elif tag == "R":
                read[2] = ("rest",)
            elif tag == "H":
                read[2] = ("held", data, None)
            else:
                read[2] = ("cut",)
                ended = True
        if start is None:
            continue
        if reads and reads[-1][2] is None and not ended:
            raise MusicError(f"{here}, tick {reads[-1][0]}: commands with no step after them")
        found.append((start, reads, here))
    return header, unshown, found
