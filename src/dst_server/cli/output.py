import sys

from logbook import Handler, LogRecord, StringFormatter

_FORMATTER = StringFormatter("{record.message}")


def single_line(message: str) -> str:
    return (
        message
        .rstrip("\r\n")
        .replace("\r", r"\r")
        .replace("\n", r"\n")
        .replace("\0", r"\0")
    )


def format_log(record: LogRecord, handler: Handler) -> str:
    return single_line(_FORMATTER(record, handler))


def diagnostic(message: str) -> None:
    if not sys.stderr.isatty():
        message = single_line(message)
    sys.stderr.write(message + ("" if message.endswith("\n") else "\n"))
