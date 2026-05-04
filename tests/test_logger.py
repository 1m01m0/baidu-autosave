import logging
import tempfile
import unittest
from pathlib import Path

import logger as logger_module


class LoggerSetupTests(unittest.TestCase):
    def setUp(self):
        self._reset_logger()

    def tearDown(self):
        self._reset_logger()

    def _reset_logger(self):
        active_logger = logger_module._logger
        if active_logger is not None:
            for handler in list(active_logger.handlers):
                active_logger.removeHandler(handler)
                handler.close()
        logger_module._logger = None

    def test_setup_logging_updates_existing_logger_level_and_handlers(self):
        first = logger_module.setup_logging(level="INFO")
        self.assertEqual(logging.INFO, first.level)
        self.assertEqual(1, len(first.handlers))

        second = logger_module.setup_logging(level="ERROR")

        self.assertIs(first, second)
        self.assertEqual(logging.ERROR, second.level)
        self.assertEqual(1, len(second.handlers))
        self.assertEqual(logging.ERROR, second.handlers[0].level)

    def test_setup_logging_can_disable_console_output(self):
        configured_logger = logger_module.setup_logging(console_output=False)

        self.assertEqual([], configured_logger.handlers)

    def test_setup_logging_replaces_file_handler(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            first_log = Path(tmp_dir) / "first.log"
            second_log = Path(tmp_dir) / "second.log"

            logger_module.setup_logging(log_file=str(first_log), console_output=False)
            configured_logger = logger_module.setup_logging(
                log_file=str(second_log), console_output=False
            )
            configured_logger.info("hello")
            for handler in configured_logger.handlers:
                handler.flush()

            self.assertEqual(1, len(configured_logger.handlers))
            self.assertEqual(str(second_log), configured_logger.handlers[0].baseFilename)
            self.assertIn("hello", second_log.read_text(encoding="utf-8"))

    def test_get_logger_does_not_block_later_setup_logging(self):
        default_logger = logger_module.get_logger()
        self.assertEqual(logging.INFO, default_logger.level)

        configured_logger = logger_module.setup_logging(level="DEBUG", console_output=False)

        self.assertIs(default_logger, configured_logger)
        self.assertEqual(logging.DEBUG, configured_logger.level)
        self.assertEqual([], configured_logger.handlers)


if __name__ == "__main__":
    unittest.main()
