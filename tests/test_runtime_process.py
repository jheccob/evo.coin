from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime_process import (
    clear_runtime_process_state,
    clear_runtime_stop_request,
    read_runtime_process_state,
    request_runtime_stop,
    runtime_stop_requested,
    tail_text_file,
    write_runtime_process_state,
)


class RuntimeProcessTests(unittest.TestCase):
    def test_write_read_and_clear_runtime_process_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "trader_bot_process.json"
            write_runtime_process_state(
                pid=4321,
                use_testnet=True,
                entrypoint="bot_runner.py",
                source="test",
                command="python bot_runner.py",
                path=state_path,
                extra={"symbol": "BTC/USDT"},
            )

            payload = read_runtime_process_state(state_path)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["pid"], 4321)
            self.assertTrue(payload["use_testnet"])
            self.assertEqual(payload["source"], "test")
            self.assertEqual(payload["symbol"], "BTC/USDT")

            clear_runtime_process_state(state_path)
            self.assertIsNone(read_runtime_process_state(state_path))

    def test_request_and_clear_runtime_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stop_path = Path(tmpdir) / "trader_bot_stop.signal"
            self.assertFalse(runtime_stop_requested(stop_path))
            request_runtime_stop(stop_path)
            self.assertTrue(runtime_stop_requested(stop_path))
            clear_runtime_stop_request(stop_path)
            self.assertFalse(runtime_stop_requested(stop_path))

    def test_tail_text_file_returns_last_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "bot.log"
            log_path.write_text("linha1\nlinha2\nlinha3\nlinha4\n", encoding="utf-8")
            tail = tail_text_file(log_path, max_lines=2)
            self.assertEqual(tail, "linha3\nlinha4")


if __name__ == "__main__":
    unittest.main()
