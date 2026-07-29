import logging

from pawmate.core.observability.stream_trace import StreamTraceAggregator


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_stream_trace_aggregates_token_deltas_until_sentence_end():
    logger = logging.getLogger("pawmate.tests.stream_trace")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)

    aggregator = StreamTraceAggregator(logger)
    pieces = [chr(0x5FAE), chr(0x8F6F), chr(0x4E91), chr(0x76D8), chr(0x3002)]
    for seq, piece in enumerate(pieces, 1):
        aggregator.record("turn-4", component="bridge.turn_seq", turn_id=4, seq=seq, text=piece)

    assert len(handler.messages) == 1
    assert "chunks=5" in handler.messages[0]
    assert "\\u5fae\\u8f6f\\u4e91\\u76d8\\u3002" in handler.messages[0].encode("unicode_escape").decode("ascii")
