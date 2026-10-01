"""A minimal terminal emulator: enough of ANSI to reconstruct what ratatui has drawn.

ratatui redraws only the cells that changed, so searching the raw byte stream for a phrase can
miss text that is plainly on screen. This rebuilds the current screen instead.
"""
import codecs
import re

CSI = re.compile(rb"\x1b\[([0-9;?]*)([A-Za-z@`])")
OSC = re.compile(rb"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
OTHER = re.compile(rb"\x1b[()][A-Za-z0-9]|\x1b[=>78]")


class VScreen:
    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0
        self.pending = b""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def resize(self, rows, cols):
        self.__init__(rows, cols)

    def feed(self, data):
        buf = self.pending + data
        self.pending = b""
        i = 0
        while i < len(buf):
            if buf[i] == 0x1B:
                m = CSI.match(buf, i) or OSC.match(buf, i) or OTHER.match(buf, i)
                if m is None:
                    if len(buf) - i < 64:           # an escape sequence split across reads
                        self.pending = buf[i:]
                        return
                    i += 1
                    continue
                if m.re is CSI:
                    self._csi(m.group(1).decode(), m.group(2).decode())
                i = m.end()
            elif buf[i] == 0x0D:
                self.col = 0
                i += 1
            elif buf[i] == 0x0A:
                self.row = min(self.row + 1, self.rows - 1)
                i += 1
            else:
                j = i
                while j < len(buf) and buf[j] not in (0x1B, 0x0D, 0x0A):
                    j += 1
                for ch in self.decoder.decode(buf[i:j]):
                    self._put(ch)
                i = j

    def _put(self, ch):
        if self.col >= self.cols:
            return
        if 0 <= self.row < self.rows:
            self.grid[self.row][self.col] = ch
        self.col += 1

    def _csi(self, params, final):
        nums = [int(p) if p.isdigit() else 0 for p in params.lstrip("?").split(";")] or [0]
        n = nums[0] or 1
        if final in "Hf":
            self.row = min(max((nums[0] or 1) - 1, 0), self.rows - 1)
            self.col = min(max((nums[1] if len(nums) > 1 and nums[1] else 1) - 1, 0), self.cols)
        elif final == "A":
            self.row = max(self.row - n, 0)
        elif final == "B":
            self.row = min(self.row + n, self.rows - 1)
        elif final == "C":
            self.col = min(self.col + n, self.cols)
        elif final == "D":
            self.col = max(self.col - n, 0)
        elif final == "G":
            self.col = min(max(n - 1, 0), self.cols)
        elif final == "J":
            if (nums[0] or 0) in (2, 3):
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
        elif final == "K":
            mode = nums[0] or 0
            row = self.grid[self.row]
            if mode == 0:
                row[self.col:] = [" "] * (self.cols - self.col)
            elif mode == 2:
                row[:] = [" "] * self.cols

    def lines(self):
        return ["".join(r).rstrip() for r in self.grid]

    def text(self):
        return "\n".join(self.lines())
