import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gps_sources


def box(status, lat, lon, heading):
    body = status + struct.pack("<ii", lat, lon) + b"\0" * 8 + struct.pack("<i", heading) + b"\0" * 8
    return b"freeGPS " + b"\0" * 18 + body


class Read70mai(unittest.TestCase):
    def test_reads_fixes_gaps_and_duplicates(self):
        data = box(b"V", 0, 0, 0) + box(b"A", 483000000, 23000000, 90) * 2 + box(b"A", 483060000, 23120000, 180)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(data)
        self.addCleanup(os.unlink, fh.name)
        points = gps_sources.read_70mai(fh.name)
        self.assertEqual(len(points), 3)
        self.assertIsNone(points[0])
        self.assertAlmostEqual(points[1][0], 48.5)
        self.assertAlmostEqual(points[1][1], 2.5, places=5)
        self.assertEqual(points[2][2], 180)
        filled = gps_sources.fill_gaps(points)
        self.assertEqual(filled[0], points[1])

    def test_no_gps(self):
        self.assertEqual(gps_sources.fill_gaps([None, None]), [])


class Gpx(unittest.TestCase):
    GPX = """<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
    <trkpt lat="0" lon="0"><time>2026-01-01T00:00:00Z</time></trkpt>
    <trkpt lat="0" lon="1"><time>2026-01-01T00:00:10Z</time></trkpt>
    </trkseg></trk></gpx>"""

    def test_interpolation_and_heading(self):
        with tempfile.NamedTemporaryFile("w", suffix=".gpx", delete=False) as fh:
            fh.write(self.GPX)
        self.addCleanup(os.unlink, fh.name)
        samples = gps_sources.read_gpx(fh.name)
        track = gps_sources.gpx_track(samples, samples[0][0], 10)
        self.assertEqual(len(track), 11)
        self.assertAlmostEqual(track[5][1], 0.5)
        self.assertAlmostEqual(track[5][2], 90)


if __name__ == "__main__":
    unittest.main()
