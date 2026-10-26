"""
Name:        verify_rom.py
Description: checks that the user-provided ROM is the game's complete set
Version:     0.1.0
Created:     11-10-2026
Modified:    26-10-2026
Author:      Sebastian Toyos

Usage: python tools/verify_rom.py [folder or .zip]   (default: rom/; a folder that holds the set's .zip
       is read through the .zip)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_profile  # noqa: E402  (the game's: its root, its ROM set)

ROOT = game_profile.ROOT
rom = game_profile.rom


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "rom"
    if not source.exists():
        raise SystemExit(f"{source} does not exist")
    found, report = rom.identify_set(source)
    if not found and source.is_dir():
        for archive in sorted(source.glob("*.zip")):
            found, report = rom.identify_set(archive)
            if found:
                break
    print("\n".join(report))
    total = len(rom.ROM_FILES)
    print(f"\n{len(found)}/{total} files of the '{rom.SET_NAME}' set")
    return 0 if len(found) == total else 1


if __name__ == "__main__":
    sys.exit(main())
