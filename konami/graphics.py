"""
Name:        graphics.py
Description: The Konami graphics chips' tiles and sprite cells: from the ROMs' bytes to pens and back, and cells laid
             out on a sheet and back
Version:     0.1.0
Created:     27-11-2026
Modified:    27-11-2026
Author:      Sebastian Toyos

The K052109's tile is MAME's k052109.cpp charlayout: 8 x 8 pixels of 4 bits, 32 bytes; a row is 4 bytes, the pen's bit
3 in the 4th, bit 0 in the 1st, the leftmost pixel in each byte's top bit (planes 24, 16, 8, 0).
The K053244 / K053245's sprite cell is k053244_k053245.cpp's spritelayout: 16 x 16 pixels of 4 bits, 128 bytes; four
such 8 x 8 blocks, the top left at byte 0, the top right at 32, the bottom left at 64, the bottom right at 96, the same
as TMNT's K051960 cells once tmnt_graphics.py has converted them.
The games' tools use it (Sunset Riders' tools/graphics_assets.py; TMNT's and The Simpsons' tiles are to move here).
"""
TILE_BYTES = 32
CELL_BYTES = 128


def _block_pens(data, base, rows):
    """An 8 x 'rows' block of pens from the planar data at 'base' (rows 4 bytes apart)"""
    pens = []
    for row in range(rows):
        at = base + 4 * row
        planes = (data[at + 3], data[at + 2], data[at + 1], data[at])     # most significant pen bit first
        pens.append([((planes[0] >> (7 - x)) & 1) << 3 | ((planes[1] >> (7 - x)) & 1) << 2
                     | ((planes[2] >> (7 - x)) & 1) << 1 | ((planes[3] >> (7 - x)) & 1) for x in range(8)])
    return pens


def _block_bytes(pens):
    """The 32 bytes of an 8 x 8 block of pens"""
    out = bytearray(32)
    for row, line in enumerate(pens):
        for lane, plane_bit in ((3, 3), (2, 2), (1, 1), (0, 0)):
            value = 0
            for x in range(8):
                value |= ((line[x] >> plane_bit) & 1) << (7 - x)
            out[4 * row + lane] = value
    return bytes(out)


def tile_pens(data, code):
    """8 rows of 8 pens of the tile 'code' (the K052109's data)"""
    return _block_pens(data, code * TILE_BYTES, 8)


def tile_bytes(pens):
    """The 32 bytes of 8 rows of 8 pens (the K052109's data)"""
    return _block_bytes(pens)


def cell_pens(data, code):
    """16 rows of 16 pens of the sprite cell 'code' (the K053244 / K053245's data)"""
    base = code * CELL_BYTES
    quarters = [_block_pens(data, base + offset, 8) for offset in (0, 32, 64, 96)]
    return [quarters[0][row] + quarters[1][row] for row in range(8)] + \
           [quarters[2][row] + quarters[3][row] for row in range(8)]


def cell_bytes(pens):
    """The 128 bytes of 16 rows of 16 pens (the K053244 / K053245's data)"""
    top = [line[:8] for line in pens[:8]], [line[8:] for line in pens[:8]]
    bottom = [line[:8] for line in pens[8:]], [line[8:] for line in pens[8:]]
    return _block_bytes(top[0]) + _block_bytes(top[1]) + _block_bytes(bottom[0]) + _block_bytes(bottom[1])


def sheet_rows(cells, size, columns):
    """The pixel rows of a sheet of 'cells' (each 'size' rows of 'size' pens), 'columns' cells across"""
    if not cells:
        return []
    width = columns * size
    rows_per_sheet = -(-len(cells) // columns) * size
    rows = [[0] * width for _ in range(rows_per_sheet)]
    for index, cell in enumerate(cells):
        row, column = divmod(index, columns)
        for y in range(size):
            for x in range(size):
                rows[row * size + y][column * size + x] = cell[y][x]
    return rows


def sheet_cells(rows, size, columns, count):
    """The first 'count' cells of a sheet, back, each 'size' rows of 'size' pens"""
    width = columns * size
    needed_rows = -(-count // columns) * size
    if len(rows) < needed_rows:
        raise SystemExit(f"cell {len(rows) // size * columns}: the sheet ends at row {len(rows)}, "
                         f"{count} cells need {needed_rows}")
    narrow = next((row for row in rows if len(row) < width), None)
    if narrow is not None:
        raise SystemExit(f"cell {len(narrow) // size}: the sheet is {len(narrow)} pens wide, "
                         f"{columns} cells across need {width}")
    cells = []
    for index in range(count):
        row, column = divmod(index, columns)
        cell = [list(rows[row * size + y][column * size:(column + 1) * size]) for y in range(size)]
        top = max(max(line) for line in cell)
        if top > 15:
            raise SystemExit(f"cell {index}: pen {top}, over 15")
        cells.append(cell)
    return cells
