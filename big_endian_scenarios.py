"""
Name:        big_endian_scenarios.py
Description: runs control scripts on the game built for a big-endian processor, under an emulator, and compares
             its picture and its sound with the reference's, frame by frame
Version:     0.2.0
Created:     11-11-2026
Modified:    13-11-2026
Author:      Sebastian Toyos

Usage: python tools/big_endian_scenarios.py [NAME ...] [--state] [--jobs N] [--bin DIR] [--out DIR]
  NAME       scripts of tests/controls to run (file names without .txt); all of them by default
  --state    compare the state too: a build that hashes the memories as the V70 holds them (the game's
             BIG_ENDIAN_STATE_BIN, build/bin-ppc-order by default, unless --bin)
  --jobs N   run N scripts at a time (half the processors by default)
  --bin DIR  the big-endian game's folder (build/bin-ppc by default)
  --out DIR  where the runs go (build/big-endian by default)

The game is the headless one of the big-endian build (cmake/powerpc-linux-gnu.cmake: GAME_game_headless in
build/bin-ppc), which the emulator runs (qemu-ppc, or the profile's BIG_ENDIAN_EMULATOR; on Windows through
WSL). For every script it writes OUT/NAME/game_video_hashes.txt and game_sound_hashes.txt, which are compared
with the reference's results already in build/scenarios/NAME/ (video_hashes.txt and sound_hashes.txt, which
tools/check_scenarios.py --video --sound writes): the picture of every frame, and the sound's samples of every
frame. The state's hash and the sound board's memory's are of the memory as its processor holds it, in another
order than the V70's, unless the game is built to keep where each byte is in the V70's order (the game's
BIG_ENDIAN_STATE_BIN: OutRunners' ORUNNERS_BYTE_ORDER_CHECK, runtime/include/s32/byte_order.h); with --state
they are compared too, with hashes.txt and sound_hashes.txt's third column (tools/state_diff.py compares the
state of one frame of a build without it, each value at the width symbols.txt gives it).
"""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_profile  # noqa: E402  (the game's: its root, its executables' names)

ROOT = game_profile.ROOT
CONTROLS = ROOT / "tests" / "controls"
REFERENCE = ROOT / "build" / "scenarios"
EMULATOR = getattr(game_profile, "BIG_ENDIAN_EMULATOR", "qemu-ppc")
BIN = ROOT / getattr(game_profile, "BIG_ENDIAN_BIN", "build/bin-ppc")
STATE_BIN = ROOT / getattr(game_profile, "BIG_ENDIAN_STATE_BIN", "build/bin-ppc-order")
OUT = ROOT / "build" / "big-endian"
SECONDS_A_FRAME = 0.2      # past this a run is taken as stuck (the emulator runs a frame in about 25 ms)


def run(name, game, out):
    """The script on the big-endian game: what stopped it, or None"""
    out.mkdir(parents=True, exist_ok=True)
    for stale in ("game_hashes.txt", "game_video_hashes.txt", "game_sound_hashes.txt"):
        (out / stale).unlink(missing_ok=True)

    def inside(path):
        return path.relative_to(ROOT).as_posix()

    command = [EMULATOR, inside(game), "--script", inside(CONTROLS / f"{name}.txt"),
               "--hashes", inside(out / "game_hashes.txt"), "--video-hashes", inside(out / "game_video_hashes.txt"),
               "--sound-hashes", inside(out / "game_sound_hashes.txt")]
    if os.name == "nt":
        command = ["wsl", "-e"] + command
    reference = REFERENCE / name / "video_hashes.txt"
    expected = sum(1 for _ in reference.open(encoding="ascii")) if reference.exists() else 0
    limit = int(120 + expected * SECONDS_A_FRAME)
    try:
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT, timeout=limit)
    except subprocess.TimeoutExpired:
        return f"did not finish in {limit} seconds"
    except FileNotFoundError:
        return f"{command[0]}: no such program"
    (out / "run.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    problems = [line for line in result.stderr.splitlines() if not line.startswith("[s32] unmapped write E")]
    if not problems and result.returncode != 0:
        problems = [f"the game ended with {result.returncode}"]
    return problems[0] if problems else None


def columns(path, count):
    if not path.exists():
        return None
    return [tuple(line.split()[:count]) for line in path.read_text(encoding="ascii").splitlines() if line]


def compare(reference_file, game_file, count):
    """(the reference's frames, the first frame whose first columns differ or None); None, None without a
    reference"""
    reference, game = columns(reference_file, count), columns(game_file, count)
    if reference is None:
        return None, None
    if game is None:
        return len(reference), 0
    for index, (expected, actual) in enumerate(zip(reference, game)):
        if expected != actual:
            return len(reference), index
    return len(reference), None if len(game) == len(reference) else min(len(game), len(reference))


def main():
    global BIN, OUT
    arguments = sys.argv[1:]
    state = "--state" in arguments
    if state:
        arguments.remove("--state")
        BIN = STATE_BIN
    for option in ("--bin", "--out"):
        if option in arguments:
            position = arguments.index(option)
            folder = Path(arguments[position + 1]).resolve()
            if option == "--bin":
                BIN = folder
            else:
                OUT = folder
            del arguments[position:position + 2]
    jobs = max(1, (os.cpu_count() or 2) // 2)
    if "--jobs" in arguments:
        position = arguments.index("--jobs")
        jobs = max(1, int(arguments[position + 1]))
        del arguments[position:position + 2]
    unknown = [argument for argument in arguments if argument.startswith("--")]
    if unknown:
        print(__doc__.split("\n\n")[1])
        raise SystemExit(f"{unknown[0]}: no such option")
    names = arguments or sorted(path.stem for path in CONTROLS.glob("*.txt"))
    for name in names:
        if not (CONTROLS / f"{name}.txt").exists():
            raise SystemExit(f"{CONTROLS / (name + '.txt')}: no such script")
    game = BIN / f"{game_profile.BINARY}_game_headless"
    if not game.exists():
        raise SystemExit(f"{game}: no such executable")
    for folder in (BIN, OUT):
        # (the emulator takes the paths from the game's root, which WSL sees as it is)
        if ROOT not in folder.parents:
            raise SystemExit(f"{folder}: not inside {ROOT}")

    def check(name):
        problem = run(name, game, OUT / name)
        frames, picture = compare(REFERENCE / name / "video_hashes.txt", OUT / name / "game_video_hashes.txt", 2)
        _, sound = compare(REFERENCE / name / "sound_hashes.txt", OUT / name / "game_sound_hashes.txt", 2)
        work = sound_board = None
        if state:
            _, work = compare(REFERENCE / name / "hashes.txt", OUT / name / "game_hashes.txt", 2)
            _, sound_board = compare(REFERENCE / name / "sound_hashes.txt", OUT / name / "game_sound_hashes.txt", 3)
        if frames is None or not (REFERENCE / name / "sound_hashes.txt").exists():
            verdict = "no reference to compare with (tools/check_scenarios.py --video --sound)"
        elif picture is not None:
            verdict = f"the picture differs from frame {picture}"
        elif sound is not None:
            verdict = f"the sound differs from frame {sound}"
        elif work is not None:
            verdict = f"the state differs from frame {work}"
        elif sound_board is not None:
            verdict = f"the sound board's memory differs from frame {sound_board}"
        else:
            verdict = "identical"
        if problem and verdict != "identical":
            verdict += f" ({problem})"
        elif problem:
            verdict = problem
        return frames or 0, verdict

    print(f"{'scenario':<16} {'frames':>7}  result ({'state, ' if state else ''}picture and sound, big-endian)")
    failures = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for name, (frames, verdict) in zip(names, pool.map(check, names)):
            failures += verdict != "identical"
            print(f"{name:<16} {frames:7}  {verdict}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
