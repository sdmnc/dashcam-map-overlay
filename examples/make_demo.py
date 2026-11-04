#!/usr/bin/env python3
"""Create a 40 s test video and a matching GPX track, so you can try the tool without a dashcam."""
import datetime as dt
import subprocess

# A made-up drive through central Paris: (lat, lon) every 8 seconds.
ROUTE = [(48.8584, 2.2945), (48.8606, 2.2982), (48.8635, 2.3013), (48.8656, 2.3061),
         (48.8673, 2.3120), (48.8690, 2.3180)]
START = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)

points = "\n".join(
    f'<trkpt lat="{lat}" lon="{lon}"><time>{(START + dt.timedelta(seconds=8 * i)).isoformat()}</time></trkpt>'
    for i, (lat, lon) in enumerate(ROUTE))
with open("demo.gpx", "w") as fh:
    fh.write('<?xml version="1.0"?>\n<gpx version="1.1" creator="make_demo" '
             f'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>\n{points}\n</trkseg></trk></gpx>\n')
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=40",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "demo.mp4"], check=True)
print("Created demo.mp4 and demo.gpx")
