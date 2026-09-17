"""A clip is a list of kept (source_start, source_end) pieces. Maps source time <-> output time."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Timeline:
    pieces: list[tuple[float, float]] = field(default_factory=list)

    @classmethod
    def single(cls, start: float, end: float) -> "Timeline":
        return cls([(float(start), float(end))])

    @property
    def duration(self) -> float:
        return sum(e - s for s, e in self.pieces)

    @property
    def start(self) -> float:
        return self.pieces[0][0]

    @property
    def end(self) -> float:
        return self.pieces[-1][1]

    def to_output(self, t: float) -> float | None:
        """Source time -> output time, or None if the moment was cut out."""
        acc = 0.0
        for s, e in self.pieces:
            if s <= t <= e:
                return acc + (t - s)
            acc += e - s
        return None

    def to_output_clamped(self, t: float) -> float:
        """Like to_output but snaps removed times to the nearest kept edge."""
        acc = 0.0
        best = 0.0
        for s, e in self.pieces:
            if t < s:
                return acc
            if s <= t <= e:
                return acc + (t - s)
            acc += e - s
            best = acc
        return best

    def to_source(self, t_out: float) -> float:
        acc = 0.0
        for s, e in self.pieces:
            if t_out <= acc + (e - s):
                return s + (t_out - acc)
            acc += e - s
        return self.pieces[-1][1]

    def remove(self, cuts: list[tuple[float, float]]) -> "Timeline":
        """Return a new timeline with the given source ranges removed."""
        pieces = list(self.pieces)
        for cs, ce in sorted(cuts):
            out = []
            for s, e in pieces:
                if ce <= s or cs >= e:
                    out.append((s, e))
                    continue
                if cs > s:
                    out.append((s, cs))
                if ce < e:
                    out.append((ce, e))
            pieces = out
        pieces = [(s, e) for s, e in pieces if e - s > 0.04]
        return Timeline(pieces)

    def as_list(self) -> list[list[float]]:
        return [[round(s, 3), round(e, 3)] for s, e in self.pieces]
