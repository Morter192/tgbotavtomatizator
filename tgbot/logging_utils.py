"""Маскування токенів у повідомленнях та traceback сторонніх бібліотек."""

import logging
import re

TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")


class RedactingFormatter(logging.Formatter):
    def __init__(self, token: str = ""):
        super().__init__("%(levelname)s: %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        if self.token:
            output = output.replace(self.token, "[TOKEN]")
        return TOKEN_PATTERN.sub("[TOKEN]", output)


def configure_logging(token: str = "") -> None:
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        handler.setFormatter(RedactingFormatter(token))
