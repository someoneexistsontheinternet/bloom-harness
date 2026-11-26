"""Number immutable source text for Luna and resolve its selections against that text."""

from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
import json
import re

from .store import encoded
from .tokens import count


@dataclass(frozen=True)
class QuotedPassage:
    text: str
    start: int
    source: str

    def record(self):
        return {
            "text": self.text,
            "source": self.source,
            "start": self.start,
            "start_char": self.start,
            "end_char": self.start + len(self.text),
        }


FORMAT = "numbered-lines-v1"
# Independent of the prompt budget. The same saved source always has the same line IDs.
SEGMENT = 768
NEWLINE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
JSON_COLON = re.compile(r"\s*:")
JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[][{},:]')
BREAK = re.compile(r"\s+|[,;]")

READER_INSTRUCTION = (
    "Read the numbered source in the next user messages for the question and theme. "
    "Each displayed row begins with its global line number and |. Select the smallest "
    "self-contained verbatim evidence, including dates, units, speaker, negation and any "
    "qualification that changes the claim. Select separate ranges for separated claims. "
    "Return {relevant: boolean, summary: string, passages: [{start_line: integer, "
    "end_line: integer, above: {start_line, end_line, summary}, below: {start_line, "
    "end_line, summary}}]}. All ranges are inclusive and must be visible in this pass. "
    "For each passage briefly summarize relevant surrounding lines above and below it "
    "in their respective fields. Omit a side when it adds nothing or is unavailable. "
    "Bloom extracts the quoted lines itself. Do not copy the passage into your reply. "
    "Return relevant=false and passages=[] when the source supplies no evidence."
)


class LineBudgetError(ValueError):
    def __init__(self, start, number, limit):
        self.start = start
        super().__init__(
            f"Source pass cannot fit line {number} and one source character in {limit} tokens"
        )


def _without_line_ending(row):
    return row.rstrip("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _json_breaks(text, start, end):
    """Prefer record and field boundaries, skipping all punctuation inside quoted strings."""
    previous = ""
    last = start
    for token in JSON_TOKEN.finditer(text, start, end):
        char = text[token.start()]
        container = char in "[{" and previous in ("[", ",")
        field = (
            char == '"' and previous == "," and JSON_COLON.match(text, token.end(), end) is not None
        )
        if (container or field) and token.start() > last:
            yield token.start()
            last = token.start()
        if char == "," and (previous in ("]", "}") or token.end() - last >= SEGMENT // 2):
            yield token.end()
            last = token.end()
        previous = char
    yield end


def _segments(text, start, end, structured):
    stops = _json_breaks(text, start, end) if structured else (end,)
    for stop in stops:
        while stop - start > SEGMENT:
            cut = start + SEGMENT
            for match in BREAK.finditer(text, start + SEGMENT // 2, cut):
                cut = match.end()
            yield cut
            start = cut
        if start < stop:
            yield stop
            start = stop


@dataclass(frozen=True)
class NumberedSource:
    text: str
    ends: tuple[int, ...]  # each includes any original physical line terminator

    @classmethod
    def index(cls, text):
        structured = text.lstrip().startswith(("{", "["))
        single_json_line = structured and not NEWLINE.search(_without_line_ending(text))
        ends = []
        start = 0

        def physical(body_end, end):
            if body_end - start > SEGMENT or (single_json_line and start < body_end):
                ends.extend(_segments(text, start, body_end, structured))
                ends[-1] = end
            else:
                ends.append(end)

        for newline in NEWLINE.finditer(text):
            physical(newline.start(), newline.end())
            start = newline.end()
        if start < len(text):
            physical(len(text), len(text))
        return cls(text, tuple(ends))

    def save(self, store, ref):
        return store.put(
            encoded({"format": FORMAT, "source_sha256": ref["sha256"], "ends": self.ends}),
            "application/json",
        )

    @classmethod
    def load(cls, store, text, ref, index):
        saved = json.loads(store.read(index))
        if saved["format"] != FORMAT or saved["source_sha256"] != ref["sha256"]:
            raise ValueError("Line index belongs to a different original or format")
        return cls(text, tuple(saved["ends"]))

    def line_start(self, number):
        return self.ends[number - 2] if number > 1 else 0

    def window(self, start, end):
        if not 0 <= start < end <= len(self.text):
            raise ValueError("Invalid numbered source window")
        return LineWindow(self, start, end)

    def windows(self, start, maximum) -> Iterator["LineWindow"]:
        if type(maximum) is not int or maximum < 1:
            raise ValueError("A document pass needs a positive token limit")
        while start < len(self.text):
            first = bisect_right(self.ends, start)
            take = first
            estimate = 0
            while take < len(self.ends):
                left = start if take == first else self.ends[take - 1]
                row = self.text[left : self.ends[take]]
                estimate += count(f"{take + 1} | {_without_line_ending(row)}\n")
                if estimate > maximum:
                    break
                take += 1

            # Verify the *rendered pass*. Token boundaries around separators can differ.
            def fits(end):
                return count(self.window(start, end).render()) <= maximum

            while take > first and not fits(self.ends[take - 1]):
                take -= 1
            while take < len(self.ends) and fits(self.ends[take]):
                take += 1
            if take > first:
                end = self.ends[take - 1]
            else:
                # Tiny custom budgets can split a logical line. Keep its global number
                # and expose only its visible fragment, even when that number is selected.
                low, high = (
                    start,
                    start + len(_without_line_ending(self.text[start : self.ends[first]])),
                )
                while high - low > 1:
                    mid = (low + high) // 2
                    if fits(mid):
                        low = mid
                    else:
                        high = mid
                if low == start:
                    raise LineBudgetError(start, first + 1, maximum)
                end = low
            yield self.window(start, end)
            start = end


@dataclass(frozen=True)
class LineWindow:
    document: NumberedSource
    start: int
    end: int

    @property
    def first(self):
        return bisect_right(self.document.ends, self.start) + 1

    @property
    def last(self):
        return bisect_right(self.document.ends, self.end - 1) + 1

    def coordinates(self, first, last):
        if (
            type(first) is not int
            or type(last) is not int
            or not self.first <= first <= last <= self.last
        ):
            raise ValueError("Line range outside the numbered pass")
        start = max(self.start, self.document.line_start(first))
        end = min(self.end, self.document.ends[last - 1])
        last_start = max(start, self.document.line_start(last))
        end = last_start + len(_without_line_ending(self.document.text[last_start:end]))
        return {"start_line": first, "end_line": last, "start_char": start, "end_char": end}

    def metadata(self):
        return {
            "first_line": self.first,
            "last_line": self.last,
            **(
                {"partial_first_line": True}
                if self.start > self.document.line_start(self.first)
                else {}
            ),
            **({"partial_last_line": True} if self.end < self.document.ends[self.last - 1] else {}),
        }

    def render(self):
        rows = []
        for number in range(self.first, self.last + 1):
            start = max(self.start, self.document.line_start(number))
            end = min(self.end, self.document.ends[number - 1])
            rows.append(f"{number} | {_without_line_ending(self.document.text[start:end])}")
        return "\n".join(rows)

    def selections(self, proposals, source):
        """Trust only validated coordinates. Any context is labeled as model-written."""
        quotes, seen = [], set()
        rejected_ranges = rejected_context = 0
        for item in proposals:
            try:
                if not isinstance(item, dict):
                    raise ValueError
                bounds = self.coordinates(item.get("start_line"), item.get("end_line"))
                a, b = bounds["start_char"], bounds["end_char"]
                text = self.document.text[a:b]
                if not text.strip():
                    raise ValueError
            except ValueError:
                rejected_ranges += 1
                continue
            if (a, b) in seen:
                continue
            seen.add((a, b))
            quote = {**QuotedPassage(text, a, source).record(), **bounds}
            if a == self.start and self.start > self.document.line_start(self.first):
                quote["partial_first_line"] = True
            if self.end < self.document.ends[self.last - 1] and bounds["end_line"] == self.last:
                quote["partial_last_line"] = True
            for side in ("above", "below"):
                context = item.get(side)
                if context is None:
                    continue
                try:
                    if (
                        not isinstance(context, dict)
                        or not isinstance(context.get("summary"), str)
                        or not context["summary"].strip()
                    ):
                        raise ValueError
                    c = self.coordinates(context.get("start_line"), context.get("end_line"))
                    if (
                        (side == "above" and c["end_line"] >= bounds["start_line"])
                        or (side == "below" and c["start_line"] <= bounds["end_line"])
                        or not self.document.text[c["start_char"] : c["end_char"]].strip()
                    ):
                        raise ValueError
                except ValueError:
                    rejected_context += 1
                    continue
                quote[side] = {**c, "summary": context["summary"].strip()}
            quotes.append(quote)
        return quotes, rejected_ranges, rejected_context
