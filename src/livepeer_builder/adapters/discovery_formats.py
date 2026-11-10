"""livepeer/node's ``date-time``, ``uri`` and ``integer`` rules, ported so the reader agrees with node (D63).

Node's own schema test (``discovery/schema_test.go`` at the pin) validates with
``github.com/santhosh-tekuri/jsonschema/v6`` v6.0.3, ``AssertFormat()`` and numbers decoded as tokens, on
Go 1.27. Python's ``jsonschema`` checks neither format without extra packages, and those packages follow
other RFC readings. So each rule below follows the Go code it names, byte for byte, as Go indexes a string.

``tests/unit/discovery_verdicts.json`` holds node's own verdict for every edge case these rules were
written against, produced by ``scripts/discovery-verdicts`` with that validator and Go version. A pin
bump regenerates it, and a changed verdict means a rule here moved upstream.
"""

import ipaddress
import re

_ASCII_DIGITS = frozenset(b"0123456789")
_HEX = frozenset(b"0123456789ABCDEFabcdef")
# Go net/url encoding_table.go: the ASCII bytes a host (and a zone) may hold unescaped.
_HOST_OK = frozenset(b"!\"$&'()*+,-.0123456789:;<=>ABCDEFGHIJKLMNOPQRSTUVWXYZ[]_abcdefghijklmnopqrstuvwxyz~")
# Go net/url validUserinfo: the runes userinfo may hold.
_USERINFO_OK = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._:~!$&'()*+,;=%@")
# A JSON number token (RFC 8259). The decoder already accepted it; this splits it into its parts.
_NUMBER = re.compile(r"-?(?P<int>0|[1-9][0-9]*)(?:\.(?P<frac>[0-9]+))?(?:[eE](?P<exp>[+-]?[0-9]+))?")
_INT64 = 1 << 63


def _bytes(value: str) -> bytes:
    # Go strings are bytes; JSON text decodes to UTF-8. A lone surrogate still takes three bytes.
    return value.encode("utf-8", "surrogatepass")


def _atoi(token: bytes) -> int | None:
    """Go strconv.Atoi: an optional sign, then one or more ASCII digits."""
    digits = token[1:] if token[:1] in (b"+", b"-") else token
    if not digits or any(b not in _ASCII_DIGITS for b in digits):
        return None
    number = int(digits)
    return -number if token[:1] == b"-" else number


# --- date-time: santhosh-tekuri/jsonschema v6.0.3 format.go validateDateTime ---


def _days_in(month: int, year: int) -> int:
    """Go time.daysIn: the proleptic Gregorian calendar, where year 0 is a leap year."""
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _date(text: bytes) -> bool:
    """validateDate: Go time.Parse("2006-01-02"): four digits, two, two, then a real day of that month."""
    if len(text) != 10 or text[4:5] != b"-" or text[7:8] != b"-":
        return False
    year, month, day = text[0:4], text[5:7], text[8:10]
    if any(b not in _ASCII_DIGITS for b in year + month + day):
        return False
    return 1 <= int(month) <= 12 and 1 <= int(day) <= _days_in(int(month), int(year))


def _time(text: bytes) -> bool:
    """validateTime: RFC 3339 full-time, with Atoi's optional sign, and a leap second only at 23:59 UTC."""
    if len(text) < 9 or text[2:3] != b":" or text[5:6] != b":":
        return False
    fields = [_atoi(token) for token in text[:8].split(b":", 2)]
    if len(fields) != 3 or any(field is None or field < 0 for field in fields):
        return False
    hour, minute, second = (field or 0 for field in fields)
    if hour > 23 or minute > 59 or second > 60:
        return False
    rest = text[8:]
    if rest[:1] == b".":
        digits = len(rest[1:]) - len(rest[1:].lstrip(b"0123456789"))
        if digits == 0:
            return False
        rest = rest[1 + digits :]
    if rest not in (b"z", b"Z"):
        if len(rest) != 6 or rest[:1] not in (b"+", b"-"):
            return False
        sign = -1 if rest[:1] == b"+" else 1
        offset = rest[1:]
        if offset[2:3] != b":":
            return False
        parts = [_atoi(token) for token in offset.split(b":", 1)]
        if any(part is None or part < 0 for part in parts):
            return False
        offset_hour, offset_minute = (part or 0 for part in parts)
        if offset_hour > 23 or offset_minute > 59:
            return False
        minutes = hour * 60 + minute + sign * (offset_hour * 60 + offset_minute)
        if minutes < 0:
            minutes += 24 * 60
        hour, minute = divmod(minutes, 60)
    return not (second >= 60 and (hour != 23 or minute != 59))


def is_date_time(value: object) -> bool:
    """Node's ``date-time``. A value that is not a string is the ``type`` keyword's to judge."""
    if not isinstance(value, str):
        return True
    text = _bytes(value)
    if len(text) < 20 or text[10:11] not in (b"t", b"T"):
        return False
    return _date(text[:10]) and _time(text[11:])


# --- uri: santhosh-tekuri/jsonschema v6.0.3 validateURI = Go 1.27 net/url.Parse, an IPv6 check, IsAbs ---


def _unescapes(text: bytes, mode: str) -> bool:
    """Go net/url unescape: every % starts two hex digits. A host also refuses %00-%7F other than %25,
    and any ASCII byte its table does not allow."""
    i = 0
    while i < len(text):
        byte = text[i]
        if byte == 0x25:  # %
            if i + 2 >= len(text) or text[i + 1] not in _HEX or text[i + 2] not in _HEX:
                return False
            if mode == "host" and int(chr(text[i + 1]), 16) < 8 and text[i : i + 3] != b"%25":
                return False
            i += 3
            continue
        if mode == "host" and byte < 0x80 and byte not in _HOST_OK:
            return False
        i += 1
    return True


def _valid_optional_port(port: bytes) -> bool:
    """Go net/url validOptionalPort: empty, or a colon and ASCII digits (any number of them)."""
    return not port or (port[:1] == b":" and all(b in _ASCII_DIGITS for b in port[1:]))


def _ipv6(text: bytes) -> bool:
    """Go netip.ParseAddr accepts it as IPv6 (not IPv4), and it names no zone."""
    if b"%" in text or b":" not in text:
        return False
    try:
        return isinstance(ipaddress.ip_address(text.decode("ascii")), ipaddress.IPv6Address)
    except (UnicodeDecodeError, ValueError):
        return False


def _host(host: bytes) -> bool:
    """Go net/url parseHost, strict colons (the default since Go 1.26), and the validator's IPv6 check.

    Any % inside brackets fails: an escape below %80 is refused in a host, one above leaves a byte no
    address holds, and %25 starts a zone, which the validator refuses.
    """
    bracket = host.rfind(b"[")
    if bracket > 0:
        return False
    if bracket == 0:
        close = host.rfind(b"]")
        if close < 0 or not _valid_optional_port(host[close + 1 :]):
            return False
        return _unescapes(host[close + 1 :], "host") and _ipv6(host[1:close])
    colon = host.find(b":")
    if colon >= 0 and not _valid_optional_port(host[colon:]):
        return False
    return _unescapes(host, "host")


def _authority(authority: bytes) -> bool:
    """Go net/url parseAuthority: the host after the last @, and valid userinfo before it."""
    at = authority.rfind(b"@")
    if not _host(authority[at + 1 :] if at >= 0 else authority):
        return False
    if at < 0:
        return True
    userinfo = authority[:at]
    if any(char not in _USERINFO_OK for char in userinfo.decode("utf-8", "replace")):
        return False
    return all(_unescapes(part, "userinfo") for part in userinfo.split(b":", 1))


def is_uri(value: object) -> bool:
    """Node's ``uri``: Go net/url.Parse succeeds and the URL has a scheme."""
    if not isinstance(value, str):
        return True
    text = _bytes(value)
    before, _, fragment = text.partition(b"#")
    if fragment and not _unescapes(fragment, "fragment"):
        return False
    if any(byte < 0x20 or byte == 0x7F for byte in before):  # the fragment is not checked for these
        return False
    scheme_end = -1
    for i, byte in enumerate(before):  # Go net/url getScheme
        if 0x41 <= byte <= 0x5A or 0x61 <= byte <= 0x7A:
            continue
        if byte in _ASCII_DIGITS or byte in b"+-.":
            if i == 0:
                return False  # no scheme: relative
            continue
        if byte == 0x3A and i > 0:  # :
            scheme_end = i
        break
    if scheme_end < 0:
        return False  # no scheme (or an empty one): not absolute
    rest = before[scheme_end + 1 :].partition(b"?")[0]  # the query is not checked
    if not rest.startswith(b"/"):
        return True  # opaque
    if rest.startswith(b"//"):
        authority, slash, path = rest[2:].partition(b"/")
        if not _authority(authority):
            return False
        rest = slash + path
    return _unescapes(rest, "path")


# --- integer: santhosh-tekuri/jsonschema v6.0.3 isInteger = Go big.Rat SetString, then IsInt ---


def _rat_parts(token: str) -> tuple[str, int] | None:
    """The token's digits and power of ten, as big.Rat.SetString reads them; None where it gives up."""
    match = _NUMBER.fullmatch(token)
    if match is None:
        return None
    fraction = match.group("frac") or ""
    exponent = int(match.group("exp") or "0")
    if not -_INT64 <= exponent < _INT64:
        return None  # the exponent does not scan
    digits = match.group("int") + fraction
    if not digits.strip("0"):
        return digits, 0  # zero: SetString returns before it applies the exponent
    power = exponent - len(fraction)
    if abs(power) > 1_000_000:
        return None  # big.Rat refuses to build 10**power
    return digits, power


def is_integer_token(token: str) -> bool:
    """Node's ``integer`` for one JSON number token: big.Rat holds it, and its value is whole."""
    parts = _rat_parts(token)
    if parts is None:
        return False
    digits, power = parts
    if power >= 0:
        return True
    significant = digits.lstrip("0")
    return not significant or len(significant) - len(significant.rstrip("0")) >= -power


def int64_value(token: str) -> int | None:
    """An integer token's value when it fits a signed 64-bit field, as node's own structs hold it.

    A larger one still passes the schema, so its runner is kept; that field is absent on ``Runner``.
    """
    parts = _rat_parts(token)
    if parts is None or not is_integer_token(token):
        return None
    digits, power = parts
    significant = digits.lstrip("0")
    if not significant:
        return 0
    length = len(significant) + power  # digits in the whole value; trailing zeros of a fraction drop
    if length > 19:
        return None  # at least 10**19, past 2**63, and never int() over thousands of digits
    whole = significant + "0" * power if power >= 0 else significant[:length]
    value = -int(whole) if token.startswith("-") else int(whole)
    return value if -_INT64 <= value < _INT64 else None
