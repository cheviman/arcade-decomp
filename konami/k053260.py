"""
Name:        k053260.py
Description: the K053260's sample ROM as WAV files and a list, and back byte for byte
Version:     0.1.0
Created:     26-11-2026
Modified:    26-11-2026
Author:      Sebastian Toyos

For any Konami board with a K053260 (The Simpsons, Sunset Riders...): the game's tools say which samples its sound
driver plays (each a place, a length, PCM or KADPCM, a rate), and this module writes them and gives the region back.
The chip plays the bytes after a sample's start, its length of them (MAME's k053260.cpp: the position goes up before
the byte is read), each one as a signed 8-bit level (PCM) or as two 4-bit steps (KADPCM: the low nibble first, each
adding kadpcm_table's step to the level, from 0). Its rate is the chip's clock over 1000 hex less the pitch.

The WAVs are 8-bit: a PCM sample's bytes with their sign bit flipped; a KADPCM sample's levels, which give its steps
back (the 16 steps are all different, mod 256). A sample whose bytes other samples' files hold already (a part of
another one, or a record that names bytes other samples play) has no file: its line in samples.txt says which files
hold it. Two files that share bytes must agree on them. The region's bytes no sample plays are WAVs of their bytes as
they are (unused_PLACE.wav), or, a few bytes, samples.txt's own lines (bytes PLACE HEX).
"""
import asset_formats  # noqa: E402  (the core's WAV reader and writer)

CLOCK = 3579545
KADPCM_STEPS = (0, 1, 2, 4, 8, 16, 32, 64, -128, -64, -32, -16, -8, -4, -2, -1)
STEP_OF = {step & 0xFF: nibble for nibble, step in enumerate(KADPCM_STEPS)}
RAW_RATE = 8000
INLINE = 16                     # the bytes no sample plays that samples.txt holds itself, up to this many
LIST = "samples.txt"


def rate(pitch):
    """The rate in Hz a voice plays at with this 12-bit pitch"""
    return CLOCK // max(1, 0x1000 - pitch)


def decode_kadpcm(data):
    level, levels = 0, bytearray()
    for byte in data:
        for nibble in (byte & 0x0F, byte >> 4):
            level = (level + KADPCM_STEPS[nibble]) & 0xFF
            levels.append(level ^ 0x80)
    return bytes(levels)


def encode_kadpcm(levels):
    previous, nibbles = 0, []
    for index, value in enumerate(levels):
        level = value ^ 0x80
        step = (level - previous) & 0xFF
        if step not in STEP_OF:
            raise ValueError(f"level {index}: a step of {step - 256 if step & 0x80 else step}, which no nibble gives")
        nibbles.append(STEP_OF[step])
        previous = level
    return bytes(nibbles[index] | nibbles[index + 1] << 4 for index in range(0, len(nibbles) - 1, 2))


def owners(ranges):
    """The indexes of the (first, stop) ranges that get a file: visited longest first (equal lengths, the lower index
    first), a range goes when the ranges still kept, itself left out, cover every byte of it"""
    order = sorted(range(len(ranges)), key=lambda index: (-(ranges[index][1] - ranges[index][0]), index))
    alive = set(range(len(ranges)))
    for index in order:
        first, stop = ranges[index]
        covered = first
        for start, end in sorted(ranges[other] for other in alive if other != index):
            if start > covered:
                break
            covered = max(covered, end)
            if covered >= stop:
                alive.discard(index)
                break
    return alive


def write(region, samples, folder):
    """The samples ((name, first, length, kind, rate) each) and the bytes no sample plays, as WAVs in 'folder', and
    samples.txt; (the samples with a file, those with none, the runs no sample plays)"""
    folder.mkdir(parents=True, exist_ok=True)
    kept = owners([(first, first + length) for _, first, length, _, _ in samples])
    lines = []
    played = bytearray(len(region))
    for index, (name, first, length, kind, rate) in enumerate(samples):
        if first + length > len(region):
            raise SystemExit(f"{name}: {first:06X}-{first + length - 1:06X} runs past the region's end")
        line = f"{name} {first:06X} {length} {kind} {rate}"
        if index in kept:
            data = region[first:first + length]
            asset_formats.write_wav(folder / f"{name}.wav",
                                    decode_kadpcm(data) if kind == "kadpcm" else bytes(byte ^ 0x80 for byte in data),
                                    rate, bits=8)
        else:
            holders = sorted((samples[other][1], samples[other][0]) for other in kept
                             if first < samples[other][1] + samples[other][2] and samples[other][1] < first + length)
            line += " in " + " ".join(holder for _, holder in holders)
        lines.append((first, name, line))
        played[first:first + length] = b"\x01" * length
    runs = 0
    at = played.find(0)
    while at >= 0:
        stop = played.find(1, at)
        stop = len(region) if stop < 0 else stop
        if stop - at <= INLINE:
            lines.append((at, "", f"bytes {at:06X} {region[at:stop].hex()}"))
        else:
            name = f"unused_{at:06X}"
            asset_formats.write_wav(folder / f"{name}.wav", region[at:stop], RAW_RATE, bits=8)
            lines.append((at, name, f"{name} {at:06X} {stop - at} raw {RAW_RATE}"))
        runs += 1
        at = played.find(0, stop)
    header = ["# NAME PLACE LENGTH KIND RATE: a sample and its NAME.wav (KIND pcm, kadpcm, raw: bytes nothing plays)",
              "# NAME PLACE LENGTH KIND RATE in OWNER...: a sample with no file, whose bytes the OWNERs' files hold",
              "# bytes PLACE HEX: bytes no sample plays; PLACE is hex, LENGTH and RATE decimal"]
    text = "\n".join(header + [line for _, _, line in sorted(lines)]) + "\n"
    (folder / LIST).write_text(text, encoding="ascii")
    return len(kept), len(samples) - len(kept), runs


def read(folder, size):
    """The region the WAVs in 'folder' and samples.txt give back; two files that give a byte different values, a file
    missing, unreadable or of another length, a line that does not read, and a byte no line gives stop it"""
    region = bytearray(size)
    given = bytearray(size)
    placed = []
    if not (folder / LIST).exists():
        raise SystemExit(f"{folder / LIST}: missing")
    for number, line in enumerate((folder / LIST).read_text(encoding="ascii").splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        try:
            if fields[0] == "bytes":
                first, data, label = int(fields[1], 16), bytes.fromhex(fields[2]), f"bytes {fields[1]}"
            elif len(fields) >= 6 and fields[5] == "in":
                continue
            else:
                name, first, length, kind = fields[0], int(fields[1], 16), int(fields[2]), fields[3]
                label = f"{name}.wav"
                if not (folder / label).exists():
                    raise SystemExit(f"{label}: missing ({LIST} lists it)")
                samples, _ = asset_formats.read_wav_unsigned(folder / label)
                if kind == "kadpcm":
                    data = encode_kadpcm(samples)
                elif kind == "pcm":
                    data = bytes(sample ^ 0x80 for sample in samples)
                else:
                    data = samples
                if len(data) != length:
                    raise SystemExit(f"{label}: {len(data)} bytes, {LIST} says {length}")
        except (ValueError, IndexError) as error:
            raise SystemExit(f"{LIST}, line {number} ({fields[0]}): {error}")
        stop = first + len(data)
        if stop > size:
            raise SystemExit(f"{label}: {first:06X}-{stop - 1:06X} runs past the region's end")
        for start, end, other in placed:
            low, high = max(first, start), min(stop, end)
            if low < high and data[low - first:high - first] != region[low:high]:
                at = next(place for place in range(low, high) if data[place - first] != region[place])
                raise SystemExit(f"{label} and {other} differ at {at:06X}")
        region[first:stop] = data
        given[first:stop] = b"\x01" * len(data)
        placed.append((first, stop, label))
    at = given.find(0)
    if at >= 0:
        raise SystemExit(f"{at:06X}: no line of {LIST} gives this byte")
    return bytes(region)
