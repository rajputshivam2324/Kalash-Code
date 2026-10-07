# Offline transcript previews

These screenshots use the installed Textual 0.89.1 runtime with synthetic text,
tool events, a diff, and shell output. No provider requests were made. The cache
fix and “12 tests passed” displayed inside the previews are sample content, not
evidence of a model solving that task.

| Terminal | During tool output | Completed response |
| --- | --- | --- |
| 100 × 34 | [Streaming](wide-streaming.svg) | [Complete](wide-complete.svg) |
| 52 × 26 | [Streaming](narrow-streaming.svg) | [Complete](narrow-complete.svg) |

The previews show compact tool rows, a shared output gutter, folded details,
chronological commentary and response placement, and a smaller input area.
The narrow view follows the newest content; older entries remain scrollable.
Keyboard hints that exceed the terminal width are clipped.

Automated regression coverage in `tests/unit/test_streaming_transcript.py` checks
that progress ticks retain output, fragmented lines remain distinct, the last
chunk paints during a pause, errors retain partial text, entries appear in order,
and long output follows the bottom only while the reader is following it.
Static previews cannot establish flicker-free behavior on every physical terminal.

Design references consulted:

- [Textual widget updates and refresh](https://textual.textualize.io/guide/widgets/)
- [Textual scrolling and layout APIs](https://textual.textualize.io/api/widget/)
- [Textual Markdown streaming](https://textual.textualize.io/widgets/markdown/)

Current upstream documentation includes APIs unavailable in the installed 0.89.1
release. The change uses that installed release's timer, Static update and
after-refresh APIs. It does not introduce a dependency upgrade. Streaming text is
literal during generation and formatted as Markdown at each response boundary.
