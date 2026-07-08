#!/usr/bin/env python3
import argparse
import json
import re
import sys
from datetime import UTC, datetime, timezone

# Expected format at start of record["text"]:
# "2025-12-11 09:00:42.538 | INFO ..."
TEXT_TS_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})(?:\.(?P<ms>\d{1,6}))?")

# Log level before a pipe separator, e.g. "WARNING  |" or "| INFO |"
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
LEVEL_RE = re.compile(r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|")


def parse_text_timestamp(text: str, assume_tz: timezone) -> datetime | None:
    """
    Parse 'YYYY-MM-DD HH:MM:SS(.ffffff)?' at the beginning of text.
    Returns aware datetime in assume_tz, or None if not found/parseable.
    """
    m = TEXT_TS_RE.match(text.strip())
    if not m:
        return None

    date_part = m.group("date")
    time_part = m.group("time")
    ms_part = m.group("ms") or "0"

    # Normalize microseconds to 6 digits
    ms_part = (ms_part + "000000")[:6]

    dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(microsecond=int(ms_part), tzinfo=assume_tz)
    return dt


def parse_cli_datetime(s: str, assume_tz: timezone) -> datetime:
    """
    Accepts:
      - '2025-12-11 09:00:00'
      - '2025-12-11 09:00:00.538'
      - ISO like '2025-12-11T09:00:00' (optionally with .ms)
    Returns aware datetime in assume_tz.
    """
    s = s.strip().replace("T", " ")
    # Try with microseconds
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=assume_tz)
        except ValueError:
            pass
    raise ValueError(f"Invalid datetime format: {s}")


def parse_log_level(text: str) -> str | None:
    """Extract the log level from a log line. Returns uppercase level or None."""
    m = LEVEL_RE.search(text)
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser(description="Filter JSON log lines by timestamp found at start of record['text'].")
    ap.add_argument("input", help="Input log file (NDJSON: one JSON object per line)")
    ap.add_argument("output", nargs="?", help="Output filtered file (default: stdout)")
    ap.add_argument("--start", help="Start datetime (inclusive), e.g. '2025-12-11 09:00:00'")
    ap.add_argument("--end", help="End datetime (inclusive), e.g. '2025-12-11 12:00:00'")
    ap.add_argument(
        "--level",
        nargs="+",
        metavar="LEVEL",
        help="Only keep lines matching these log levels (e.g. --level WARNING ERROR). "
        "Valid levels: DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    )
    ap.add_argument(
        "--tz",
        default="UTC",
        help="Timezone assumption for timestamps in text (default: UTC). Use 'LOCAL' to use local timezone.",
    )
    ap.add_argument(
        "--keep-invalid",
        action="store_true",
        help="Also keep lines where timestamp can't be parsed (useful if file has mixed lines).",
    )
    ap.add_argument("--stats", action="store_true", help="Print summary stats to stderr.")
    args = ap.parse_args()

    level_filter = None
    if args.level:
        level_filter = {lvl.upper() for lvl in args.level}
        invalid_levels = level_filter - VALID_LEVELS
        if invalid_levels:
            raise SystemExit(
                f"Invalid log level(s): {', '.join(sorted(invalid_levels))}. Valid: {', '.join(sorted(VALID_LEVELS))}"
            )

    if args.tz.upper() == "LOCAL":
        # Local timezone aware (Python 3.9+ uses system tzinfo via astimezone)
        assume_tz = datetime.now().astimezone().tzinfo or UTC
    else:
        # Only support UTC in a simple way without external deps
        if args.tz.upper() != "UTC":
            raise SystemExit("Only --tz UTC or --tz LOCAL supported (to avoid extra dependencies).")
        assume_tz = UTC

    start_dt = parse_cli_datetime(args.start, assume_tz) if args.start else None
    end_dt = parse_cli_datetime(args.end, assume_tz) if args.end else None
    if start_dt and end_dt and end_dt < start_dt:
        raise SystemExit("--end must be >= --start")

    in_count = 0
    out_count = 0
    invalid_count = 0

    fout = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    try:
        with open(args.input, encoding="utf-8", errors="replace") as fin:
            for line in fin:
                in_count += 1
                line_stripped = line.strip()
                if not line_stripped:
                    continue

                try:
                    obj = json.loads(line_stripped)
                except json.JSONDecodeError:
                    # Not valid JSON on this line
                    invalid_count += 1
                    if args.keep_invalid:
                        fout.write(line)
                        out_count += 1
                    continue

                text = obj.get("text") or ""
                ts = parse_text_timestamp(text, assume_tz)

                if ts is None:
                    invalid_count += 1
                    if args.keep_invalid:
                        fout.write(line)
                        out_count += 1
                    continue

                if (start_dt and ts < start_dt) or (end_dt and ts > end_dt):
                    continue
                if level_filter:
                    level = parse_log_level(text)
                    if level not in level_filter:
                        continue
                fout.write(line)
                out_count += 1
    finally:
        if args.output:
            fout.close()

    if args.stats:
        print(f"Read lines: {in_count}", file=sys.stderr)
        print(f"Written lines: {out_count}", file=sys.stderr)
        print(f"Invalid/unparsed lines: {invalid_count}", file=sys.stderr)


if __name__ == "__main__":
    main()
