"""GPS readers. A clip's track is a list with one (lat, lon, heading) per second."""
import datetime as dt
import math
import re
import struct
import xml.etree.ElementTree as ET


def _ddmm_to_degrees(value):
    value /= 1e5
    degrees = int(value // 100)
    return degrees + (value - degrees * 100) / 60


def read_70mai(path):
    """Read the `freeGPS ` boxes a 70mai dashcam embeds in its MP4 files.

    Layout found by inspecting real files (not an official format): at offset
    +26 a status byte ('A' = valid fix), int32 LE lat and lon as DDMM.mmmmm x 1e5,
    and at +43 an int32 LE heading. Only tested in the northern/eastern hemisphere.
    Returns a list with None for seconds without a fix.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    points, previous = [], None
    for match in re.finditer(b"freeGPS ", data):
        box = data[match.start() + 26 : match.start() + 48]
        if box == previous or len(box) < 21:
            continue
        previous = box
        if box[:1] != b"A":
            points.append(None)
            continue
        lat, lon = struct.unpack("<ii", box[1:9])
        heading = struct.unpack("<i", box[17:21])[0]
        points.append((_ddmm_to_degrees(lat), _ddmm_to_degrees(lon), float(heading)))
    return points


def fill_gaps(points):
    """Replace missing fixes with the nearest known point."""
    first = next((p for p in points if p), None)
    if first is None:
        return []
    filled, last = [], first
    for point in points:
        last = point or last
        filled.append(last)
    return filled


def _parse_time(text):
    moment = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


def read_gpx(path):
    """Return sorted (unix_time, lat, lon) from a GPX file's timestamped points."""
    samples = []
    for node in ET.parse(path).getroot().findall(".//{*}trkpt"):
        when = node.find("{*}time")
        if when is not None and when.text:
            samples.append((_parse_time(when.text), float(node.get("lat")), float(node.get("lon"))))
    samples.sort()
    if len(samples) < 2:
        raise ValueError(f"{path}: need at least 2 track points with timestamps")
    return samples


def bearing(lat1, lon1, lat2, lon2):
    a1, a2 = math.radians(lat1), math.radians(lat2)
    d = math.radians(lon2 - lon1)
    y = math.sin(d) * math.cos(a2)
    x = math.cos(a1) * math.sin(a2) - math.sin(a1) * math.cos(a2) * math.cos(d)
    return math.degrees(math.atan2(y, x)) % 360


def gpx_track(samples, start, duration):
    """Interpolate a GPX track to one point per second from `start` for `duration` s."""
    track, i = [], 0
    for second in range(int(math.ceil(duration)) + 1):
        t = start + second
        while i < len(samples) - 2 and samples[i + 1][0] <= t:
            i += 1
        t0, la0, lo0 = samples[i]
        t1, la1, lo1 = samples[i + 1]
        k = min(1.0, max(0.0, (t - t0) / ((t1 - t0) or 1)))
        track.append((la0 + (la1 - la0) * k, lo0 + (lo1 - lo0) * k, bearing(la0, lo0, la1, lo1)))
    return track
