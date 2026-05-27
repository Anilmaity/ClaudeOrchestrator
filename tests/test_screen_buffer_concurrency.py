"""Regression test for the host-killing pyte race.

pyte's Screen/ByteStream are not thread-safe. The host pump feeds bytes on one
thread while control-socket CAPTURE handlers read text() on others. Before
ScreenBuffer serialized access, this raised inside pyte ("generator already
executing" / "dictionary changed size during iteration"); surfacing on the pump
thread, it killed the host process and took claude down with it. This test
hammers feed() and text() from multiple threads and asserts neither raises.
"""
import threading

from screen_buffer import ScreenBuffer

# Bytes that exercise pyte's parser (clears, cursor moves, SGR colors, scroll).
CHUNK = ("\x1b[2J\x1b[H" + ("hello \x1b[31mworld\x1b[0m line of text\r\n" * 40)).encode()


def test_concurrent_feed_and_read_never_raises():
    sb = ScreenBuffer(cols=120, rows=50)
    errors: list[tuple[str, Exception]] = []
    stop = threading.Event()

    def feeder():
        while not stop.is_set():
            try:
                sb.feed(CHUNK)
            except Exception as e:  # noqa: BLE001 - the bug under test
                errors.append(("feed", e))
                return

    def reader():
        while not stop.is_set():
            try:
                sb.text(lines=400)
                sb.contains_any(("world", "missing"))
            except Exception as e:  # noqa: BLE001
                errors.append(("read", e))
                return

    threads = [threading.Thread(target=feeder) for _ in range(2)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    stop.wait(2.5)
    stop.set()
    for t in threads:
        t.join(timeout=3)

    assert not errors, f"concurrent screen access raised: {errors[:3]}"
