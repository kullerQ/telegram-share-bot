"""Unit tests verifying that inline query state does not leak memory."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_share_bot.handlers import (
    _MAX_CANCELLED_INLINE,
    _MAX_PENDING_INLINE,
    _cancelled_set,
    _pending_map,
    _record_cancelled_inline,
    _store_pending_url,
    chosen_inline_result,
)


class TestHandlersMemory(unittest.IsolatedAsyncioTestCase):
    def test_pending_map_is_bounded(self) -> None:
        context = MagicMock()
        context.application.bot_data = {}

        # Insert more than _MAX_PENDING_INLINE items
        for i in range(_MAX_PENDING_INLINE + 150):
            _store_pending_url(context, f"res_{i}", f"https://example.com/video_{i}")

        pending = _pending_map(context)
        self.assertEqual(len(pending), _MAX_PENDING_INLINE)
        # Verify oldest items were evicted (res_0 should be gone)
        self.assertNotIn("res_0", pending)
        # Newest item should be present
        self.assertIn(f"res_{_MAX_PENDING_INLINE + 149}", pending)

    def test_cancelled_set_is_bounded(self) -> None:
        context = MagicMock()
        context.application.bot_data = {}

        for i in range(_MAX_CANCELLED_INLINE + 100):
            _record_cancelled_inline(context, f"msg_{i}")

        cancelled = _cancelled_set(context)
        self.assertEqual(len(cancelled), _MAX_CANCELLED_INLINE)

    async def test_chosen_inline_result_evicts_pending_entry(self) -> None:
        context = MagicMock()
        context.application.bot_data = {}

        result_id = "chosen_123"
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        _store_pending_url(context, result_id, url)

        update = MagicMock()
        chosen = MagicMock()
        chosen.result_id = result_id
        chosen.inline_message_id = "inline_msg_999"
        chosen.query = url
        update.chosen_inline_result = chosen

        with patch("telegram_share_bot.handlers._prepare_inline_media", AsyncMock()):
            await chosen_inline_result(update, context)

        # Ensure the chosen result was popped from the pending map
        pending = _pending_map(context)
        self.assertNotIn(result_id, pending)


if __name__ == "__main__":
    unittest.main()
