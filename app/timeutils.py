from datetime import datetime, timezone

ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(ISO_UTC)


def utc_now_iso() -> str:
    return format_utc(datetime.now(timezone.utc))
