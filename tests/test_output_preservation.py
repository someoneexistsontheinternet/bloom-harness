from bloom.privacy import StreamRedactor, register


def test_stream_output_preserves_long_binary_and_non_newline_prompts_but_not_split_credentials():
    register("fixture-private-credential-031")
    redactor = StreamRedactor()
    assert redactor.feed(b"Input required: ") == b"Input required: "
    original = b"\xff\x00" + b"X" * (5 * 1024 * 1024)
    output = b"".join(
        redactor.feed(original[i : i + 65536]) for i in range(0, len(original), 65536)
    )
    output += redactor.feed(b"", final=True)
    assert output == original
    output = redactor.feed(b"Before fixture-private-")
    output += redactor.feed(b"credential-031 After")
    output += redactor.feed(b"", final=True)
    assert output == b"Before [REDACTED] After"
