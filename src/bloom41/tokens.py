"""Offline o200k vocabulary for summary limits, document passes and reader admission."""

import base64
from bisect import bisect_left
from functools import lru_cache
import gzip
from importlib.resources import files

import tiktoken


@lru_cache(maxsize=1)
def encoding():
    # The pattern and published ranks follow tiktoken_ext.openai_public.o200k_base.
    # The bundled vocabulary also works in a fresh offline wheel installation.
    pattern = "|".join(
        (
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
            r"\p{N}{1,3}",
            r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
            r"\s*[\r\n]+",
            r"\s+(?!\S)",
            r"\s+",
        )
    )
    raw = gzip.decompress(files("bloom41").joinpath("data/o200k_base.tiktoken.gz").read_bytes())
    ranks = {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in raw.splitlines())
    }
    return tiktoken.Encoding(
        name="bloom_o200k", pat_str=pattern, mergeable_ranks=ranks, special_tokens={}
    )


def count(text):
    return len(encoding().encode_ordinary(text))


def document_ranges(text, maximum):
    """Yield contiguous character ranges with at most `maximum` standalone source tokens."""
    if type(maximum) is not int or maximum < 1:
        raise ValueError("A document pass needs a positive token limit")
    if not text:
        return
    codec = encoding()
    tokens = codec.encode_ordinary(text)
    if len(tokens) <= maximum:
        yield 0, len(text)
        return

    # A token boundary can fall inside a UTF-8 character. Use original character
    # offsets for the slices and for the quotations that later refer to them.
    _, offsets = codec.decode_with_offsets(tokens)
    offsets.append(len(text))
    start = 0
    while start < len(text):
        token_start = bisect_left(offsets, start)
        end = offsets[min(token_start + maximum, len(tokens))]
        part = text[start : max(start + 1, end)]
        # Encoding a slice on its own can differ at the edges from encoding
        # the document. If we back up, the next pass starts at that exact point.
        part_tokens = codec.encode_ordinary(part)
        while len(part_tokens) > maximum:
            part = codec.decode_bytes(part_tokens[:maximum]).decode("utf-8", errors="ignore")
            if not part:
                raise ValueError("A document pass is too small for one source character")
            part_tokens = codec.encode_ordinary(part)
        end = start + len(part)
        yield start, end
        start = end


def limit(text, maximum):
    codec = encoding()
    ids = codec.encode_ordinary(text)
    if len(ids) <= maximum:
        return text
    clipped = ids[: max(0, maximum - 1)]
    while clipped:
        value = codec.decode_bytes(clipped).decode(errors="ignore") + "…"
        if count(value) <= maximum:
            return value
        clipped.pop()
    return ""
