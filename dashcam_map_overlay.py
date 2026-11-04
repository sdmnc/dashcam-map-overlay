#!/usr/bin/env python3
"""Overlay an animated GPS minimap on dashcam video. See README.md."""
import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

import gps_sources

VERSION = "0.1.0"
WIDTH, HEIGHT = 1920, 1080
USER_AGENT = f"dashcam-map-overlay/{VERSION} (+https://github.com/sdmnc/dashcam-map-overlay)"
FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Overlay an animated GPS minimap on dashcam video.")
    p.add_argument("videos", nargs="+", help="clips in driving order")
    p.add_argument("-o", "--out", default="output.mp4")
    p.add_argument("--gpx", help="GPX track, for cameras whose GPS format is not supported")
    p.add_argument("--start", help="UTC start of the first clip for --gpx, e.g. 2026-07-15T10:09:44Z "
                                   "(default: start of the GPX track)")
    p.add_argument("--speed", type=float, default=1, help="speed-up factor, e.g. 8 (default 1)")
    p.add_argument("--zoom", type=int, default=13, help="map zoom, 12 for highways, 14 for cities")
    p.add_argument("--map-size", type=int, default=380, help="minimap size in px")
    p.add_argument("--title", help="title shown for the first seconds")
    p.add_argument("--subtitle", default="")
    p.add_argument("--crop", help="ffmpeg crop W:H:X:Y applied before scaling to 1920x1080")
    p.add_argument("--audio", help="audio file to loop as the soundtrack (default: no sound)")
    p.add_argument("--places", help="GeoNames cities500.txt, to show place names")
    p.add_argument("--tile-url", default="https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    p.add_argument("--attribution", default="© OpenStreetMap")
    p.add_argument("--max-tiles", type=int, default=500, help="refuse to download more tiles than this")
    p.add_argument("--preset", default="veryfast", help="x264 preset")
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--work", help="keep segments here, so an interrupted run can resume")
    p.add_argument("--version", action="version", version=VERSION)
    args = p.parse_args(argv)
    if args.speed <= 0:
        p.error("--speed must be positive")
    if args.start and not args.gpx:
        p.error("--start only makes sense with --gpx")
    return args


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    stream = info["streams"][0]
    return float(info["format"]["duration"]), stream["width"], stream["height"]


class WebMercator:
    def __init__(self, zoom):
        self.zoom, self.size = zoom, 256 * 2 ** zoom

    def __call__(self, lat, lon):
        x = (lon + 180) / 360 * self.size
        rad = math.radians(lat)
        y = (1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2 * self.size
        return x, y


def fetch_tile(url_template, zoom, x, y):
    """Return a tile from the local cache, downloading it politely if needed."""
    host = hashlib.sha1(url_template.encode()).hexdigest()[:10]
    root = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    path = os.path.join(root, "dashcam-map-overlay", host, str(zoom), f"{x}_{y}.png")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        url = url_template.format(z=zoom, x=x, y=y)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
        with open(path + ".part", "wb") as fh:
            fh.write(data)
        os.replace(path + ".part", path)
        time.sleep(0.2)
    return Image.open(path).convert("RGB")


class Minimap:
    """Pre-renders the map, then draws one rounded minimap frame per position."""

    def __init__(self, args, points):
        self.size, self.attribution = args.map_size, args.attribution
        self.project = WebMercator(args.zoom)
        self.xy = [self.project(p[0], p[1]) for p in points]
        xs, ys = zip(*self.xy)
        tx0, tx1 = int((min(xs) - self.size) // 256), int((max(xs) + self.size) // 256)
        ty0, ty1 = int((min(ys) - self.size) // 256), int((max(ys) + self.size) // 256)
        self.ox, self.oy = tx0 * 256, ty0 * 256
        radius = self.size // 256 + 1
        near = set()
        for x, y in self.xy[::5]:
            cx, cy = int(x // 256), int(y // 256)
            near.update((cx + dx, cy + dy) for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1))
        if len(near) > args.max_tiles:
            sys.exit(f"This route needs {len(near)} map tiles (limit {args.max_tiles}). Use a lower --zoom, "
                     "or raise --max-tiles if your tile provider allows bulk downloads.")
        print(f"map: {len(near)} tiles at zoom {args.zoom}", flush=True)
        base = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (230, 230, 230))
        for x, y in sorted(near):
            base.paste(fetch_tile(args.tile_url, args.zoom, x, y), ((x - tx0) * 256, (y - ty0) * 256))
        base = Image.blend(base, Image.new("RGB", base.size, (245, 245, 245)), 0.15)
        metres_per_px = 156543.03 * math.cos(math.radians(points[0][0])) / 2 ** args.zoom
        self.jump = 500 / metres_per_px  # longer gaps between clips are not drawn as a line
        draw = ImageDraw.Draw(base)
        for line in self._split([(x - self.ox, y - self.oy) for x, y in self.xy]):
            draw.line(line, fill=(70, 110, 200), width=6, joint="curve")
        self.layer, self.done = base, ImageDraw.Draw(base)
        self.last = None
        font = next((f for f in FONT_PATHS if os.path.exists(f)), None)
        self.label_font = ImageFont.truetype(font, 22) if font else ImageFont.load_default()
        self.small_font = ImageFont.truetype(font, 13) if font else ImageFont.load_default()
        self.mask = Image.new("L", (self.size, self.size), 0)
        ImageDraw.Draw(self.mask).rounded_rectangle((0, 0, self.size - 1, self.size - 1), 28, fill=255)

    def _split(self, route):
        line = route[:1]
        for a, b in zip(route, route[1:]):
            if math.dist(a, b) > self.jump:
                if len(line) > 1:
                    yield line
                line = []
            line.append(b)
        if len(line) > 1:
            yield line

    def advance(self, lat, lon):
        x, y = self.project(lat, lon)
        x, y = x - self.ox, y - self.oy
        if self.last and math.dist(self.last, (x, y)) < self.jump:
            self.done.line([self.last, (x, y)], fill=(255, 120, 0), width=7)
        self.last = (x, y)
        return x, y

    def frame(self, lat, lon, heading, label):
        x, y = self.advance(lat, lon)
        s = self.size
        left, top = int(x - s / 2), int(y - s / 2)
        crop = self.layer.crop((left, top, left + s, top + s)).convert("RGBA")
        g = ImageDraw.Draw(crop)
        c, h = s / 2, math.radians(heading)
        tip = (c + math.sin(h) * 26, c - math.cos(h) * 26)
        back_l = (c + math.sin(h + 2.5) * 13, c - math.cos(h + 2.5) * 13)
        back_r = (c + math.sin(h - 2.5) * 13, c - math.cos(h - 2.5) * 13)
        g.polygon([tip, back_l, back_r], fill=(255, 255, 255))
        g.ellipse((c - 16, c - 16, c + 16, c + 16), fill=(255, 255, 255))
        g.ellipse((c - 13, c - 13, c + 13, c + 13), fill=(230, 60, 30))
        if label:
            width = g.textlength(label, font=self.label_font)
            g.rounded_rectangle((c - width / 2 - 12, 14, c + width / 2 + 12, 50), 10, fill=(0, 0, 0, 170))
            g.text((c - width / 2, 17), label, font=self.label_font, fill=(255, 255, 255))
        width = g.textlength(self.attribution, font=self.small_font)
        g.text((s - width - 12, s - 20), self.attribution, font=self.small_font, fill=(60, 60, 60))
        out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        out.paste(crop, (0, 0), self.mask)
        ImageDraw.Draw(out).rounded_rectangle((0, 0, s - 1, s - 1), 28, outline=(255, 255, 255, 220), width=4)
        return out.tobytes()


class Places:
    def __init__(self, path, points):
        self.rows = []
        if not path:
            return
        lats, lons = [p[0] for p in points], [p[1] for p in points]
        with open(path, encoding="utf8") as fh:
            for row in fh:
                f = row.split("\t")
                lat, lon = float(f[4]), float(f[5])
                if min(lats) - .3 < lat < max(lats) + .3 and min(lons) - .3 < lon < max(lons) + .3:
                    self.rows.append((f[1], lat, lon))

    def nearest(self, lat, lon, km):
        best = None
        for name, a, b in self.rows:
            d = math.hypot(a - lat, (b - lon) * math.cos(math.radians(lat))) * 111
            if d < km and (best is None or d < best[0]):
                best = (d, name)
        return best[1] if best else ""


def ass_time(s):
    s = max(0, s)
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def ass_text(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_titles(args, path, clips, total, places):
    events = []
    if args.title:
        events.append((0.5, 7.5, "Title", r"{\fad(800,800)\pos(960,480)}" + ass_text(args.title)))
    if args.subtitle:
        events.append((1.2, 7.5, "Sub", r"{\fad(800,800)\pos(960,600)}" + ass_text(args.subtitle)))
    last, shown, busy = None, {}, 0
    for clip in clips:
        for i, p in enumerate(clip["track"]):
            if i % 5:
                continue
            name = places.nearest(p[0], p[1], 3.0)
            when = clip["start"] + i / args.speed
            if name and name != last and when > 9 and when - shown.get(name, -1e9) > 600 / args.speed:
                begin = max(when, busy + 0.3)
                if begin < total - 8:
                    events.append((begin, begin + 6, "Place", r"{\fad(500,500)}" + ass_text(name)))
                    busy = begin + 6
                shown[name] = when
            last = name or last
    if not events:
        return False
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,Sans,100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,4,0,1,3,4,5,60,60,0,1
Style: Sub,Sans,50,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,2,3,5,60,60,0,1
Style: Place,Sans,54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H50000000,1,0,0,0,100,100,1,0,1,2,3,3,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf8") as fh:
        fh.write(head)
        for begin, end, style, text in events:
            fh.write(f"Dialogue: 0,{ass_time(begin)},{ass_time(end)},{style},,0,0,0,,{text}\n")
    return True


def crop_filter(args, clip):
    if args.crop:
        return f"crop={args.crop},scale={WIDTH}:{HEIGHT}:flags=lanczos"
    if clip["camera"] == "70mai" and clip["width"] == 2592 and clip["height"] in (1456, 1944):
        # Hides the burned-in speed/coordinates strip. 4:3 clips sit lower in the frame.
        y = 456 if clip["height"] == 1944 else 40
        return f"crop=1856:1044:368:{y},scale={WIDTH}:{HEIGHT}:flags=lanczos"
    return f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT}"


def load_clips(args):
    samples = gps_sources.read_gpx(args.gpx) if args.gpx else None
    start_time = None
    if samples:
        start_time = gps_sources._parse_time(args.start) if args.start else samples[0][0]
    clips, clock = [], 0.0
    for path in args.videos:
        duration, width, height = probe(path)
        if samples:
            track, camera = gps_sources.gpx_track(samples, start_time, duration), "gpx"
            start_time += duration
        else:
            track, camera = gps_sources.fill_gaps(gps_sources.read_70mai(path)), "70mai"
            if not track:
                sys.exit(f"No GPS data found in {path}. Only 70mai files are read directly; "
                         "for other cameras export a GPX track and pass --gpx.")
        shown = duration / args.speed
        clips.append(dict(path=os.path.abspath(path), track=track, camera=camera, width=width,
                          height=height, duration=duration, shown=shown, start=clock))
        clock += shown
    return clips, clock


def render_segment(args, clip, index, count, minimap, places, has_titles, work):
    fps_map = 5 if args.speed == 1 else 10
    filters = (f"[0:v]setpts=(PTS-STARTPTS)/{args.speed},fps=30,{crop_filter(args, clip)},")
    if has_titles:
        filters += f"setpts=PTS-STARTPTS+{clip['start']}/TB,subtitles=titles.ass,setpts=PTS-STARTPTS,"
    filters += "format=yuv420p"
    if index == 0:
        filters += ",fade=in:st=0:d=1.5"
    if index == count - 1:
        filters += f",fade=out:st={max(0, clip['shown'] - 2)}:d=2"
    size = args.map_size
    filters += f"[v];[v][1:v]overlay=36:H-h-36:eof_action=repeat,format=yuv420p[o]"
    seg = f"seg_{index:04d}.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", clip["path"], "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{size}x{size}", "-r", str(fps_map), "-i", "pipe:", "-filter_complex", filters,
           "-map", "[o]", "-an", "-t", str(clip["shown"]), "-c:v", "libx264", "-preset", args.preset,
           "-crf", str(args.crf), "-maxrate", "12M", "-bufsize", "24M", "-r", "30",
           "-video_track_timescale", "30000", "part_" + seg]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, cwd=work)
    track, label = clip["track"], ""
    for j in range(int(math.ceil(clip["shown"] * fps_map)) + 1):
        second = j / fps_map * args.speed
        i = min(int(second), len(track) - 1)
        a, b = track[i], track[min(i + 1, len(track) - 1)]
        k = second - int(second)
        lat, lon = a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
        turn = (b[2] - a[2] + 540) % 360 - 180
        if j % fps_map == 0:
            label = places.nearest(lat, lon, 6)
        try:
            process.stdin.write(minimap.frame(lat, lon, a[2] + turn * k, label))
        except BrokenPipeError:
            break
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass
    if process.wait():
        sys.exit(f"ffmpeg failed on {clip['path']}")
    os.replace(os.path.join(work, "part_" + seg), os.path.join(work, seg))
    return seg


def main():
    args = parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found: install ffmpeg first.")
    clips, total = load_clips(args)
    points = [p for clip in clips for p in clip["track"]]
    minimap = Minimap(args, points)
    places = Places(args.places, points)
    temp = None if args.work else tempfile.TemporaryDirectory(prefix="dashcam-map-")
    work = os.path.abspath(args.work) if args.work else temp.name
    os.makedirs(work, exist_ok=True)
    has_titles = write_titles(args, os.path.join(work, "titles.ass"), clips, total, places)
    print(f"output: {total / 60:.1f} min from {len(clips)} clip(s)", flush=True)
    segments = []
    for index, clip in enumerate(clips):
        seg = f"seg_{index:04d}.mp4"
        if os.path.exists(os.path.join(work, seg)):
            for p in clip["track"]:
                minimap.advance(p[0], p[1])
        else:
            began = time.time()
            render_segment(args, clip, index, len(clips), minimap, places, has_titles, work)
            print(f"[{index + 1}/{len(clips)}] {os.path.basename(clip['path'])} {time.time() - began:.0f}s", flush=True)
        segments.append(seg)
    with open(os.path.join(work, "list.txt"), "w") as fh:
        fh.writelines(f"file '{s}'\n" for s in segments)
    out = os.path.abspath(args.out)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "list.txt")]
    if args.audio:
        cmd += ["-stream_loop", "-1", "-i", os.path.abspath(args.audio),
                "-af", f"afade=in:st=0:d=2,afade=out:st={max(0, total - 4)}:d=4", "-t", str(total),
                "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "copy", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    print("done:", args.out)


if __name__ == "__main__":
    main()
