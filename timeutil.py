"""时间工具：所有时间统一为门店本地 ISO 8601（秒精度，无时区后缀）。"""

from datetime import datetime, timedelta

FMT = "%Y-%m-%dT%H:%M:%S"


def parse(s):
    return datetime.strptime(s, FMT)


def fmt(dt):
    return dt.strftime(FMT)


def now_str():
    return fmt(datetime.now())


def add_minutes(s, minutes):
    return fmt(parse(s) + timedelta(minutes=minutes))


def add_hours(s, hours):
    return fmt(parse(s) + timedelta(hours=hours))


def minutes_between(a, b):
    """b - a 的分钟数，可为负。"""
    return (parse(b) - parse(a)).total_seconds() / 60.0


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end
