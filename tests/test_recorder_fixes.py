"""Regression tests for the "live sessions are not saved" failure modes.

Covered:
* fMP4/CMAF playlists: the ``#EXT-X-MAP`` init segment must head the spool.
* Master playlist variant selection.
* Raw spool is preserved when muxing fails (no silent data loss).
* Playlist token expiry -> re-probe instead of ending the recording.
* Probing falls back to AJAX / room HTML when the API is blocked.
* ``geo_blocked`` is reported instead of a silent "offline".
"""

import json
import threading
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from core.recorder import (
    DEFAULT_MAX_RECORDING_HEIGHT,
    HlsVariant,
    StreamInfo,
    normalize_hls_url,
    parse_hls_playlist,
    probe_room,
    record_stream_ffmpeg,
    record_stream_to_disk,
    select_variant,
)


def _resp(body: bytes, status: int = 200, url: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.geturl.return_value = url
    resp.headers = {}
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


def _media_resp(body: bytes, url: str = "") -> MagicMock:
    """Response for binary media: first read() returns data, second returns EOF."""
    resp = _resp(body, url=url)
    resp.read.side_effect = [body, b""]
    return resp


class TestHlsParsing(unittest.TestCase):
    def test_fmp4_init_segment_is_parsed(self):
        playlist = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:7\n"
            '#EXT-X-MAP:URI="init.mp4"\n'
            "#EXT-X-TARGETDURATION:2\n"
            "#EXTINF:2.0,\n"
            "seg-1.m4s\n"
            "#EXTINF:2.0,\n"
            "seg-2.m4s\n"
        )
        parsed = parse_hls_playlist(playlist, "https://edge.example.com/live/playlist_sfm4s.m3u8")
        self.assertEqual(parsed.init_url, "https://edge.example.com/live/init.mp4")
        self.assertEqual(len(parsed.segments), 2)
        self.assertFalse(parsed.is_master)

    def test_encrypted_playlist_is_flagged(self):
        playlist = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:2.0,\nseg.ts\n"
        )
        parsed = parse_hls_playlist(playlist, "https://edge.example.com/x.m3u8")
        self.assertTrue(parsed.encrypted)

    def test_vod_playlist_is_flagged(self):
        playlist = (
            "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:2.0,\nseg.ts\n#EXT-X-ENDLIST\n"
        )
        parsed = parse_hls_playlist(playlist, "https://edge.example.com/x.m3u8")
        self.assertTrue(parsed.vod)
        self.assertTrue(parsed.ended)

    def test_master_playlist_variants(self):
        master = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360,CODECS="avc1.42e01e,mp4a.40.2"\n'
            "360p.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\n'
            "1080p.m3u8\n"
        )
        parsed = parse_hls_playlist(master, "https://edge.example.com/master.m3u8")
        self.assertTrue(parsed.is_master)
        self.assertEqual(len(parsed.variants), 2)
        best = select_variant(parsed.variants)
        self.assertEqual(best.height, 1080)
        self.assertEqual(best.url, "https://edge.example.com/1080p.m3u8")

    def test_select_variant_deprioritises_fast_variants(self):
        variants = [
            HlsVariant(url="https://e/live_fast.m3u8", height=1080, codecs="avc1,mp4a"),
            HlsVariant(url="https://e/live.m3u8", height=720, codecs="avc1,mp4a"),
        ]
        self.assertEqual(select_variant(variants).url, "https://e/live.m3u8")

    def test_select_variant_ceiling_1080p(self):
        v360 = HlsVariant(url="https://e/360.m3u8", height=360, bandwidth=800000, codecs="avc1,mp4a")
        v720 = HlsVariant(url="https://e/720.m3u8", height=720, bandwidth=2500000, codecs="avc1,mp4a")
        v1080 = HlsVariant(url="https://e/1080.m3u8", height=1080, bandwidth=5000000, codecs="avc1,mp4a")
        v1440 = HlsVariant(url="https://e/1440.m3u8", height=1440, bandwidth=8000000, codecs="avc1,mp4a")
        v2160 = HlsVariant(url="https://e/2160.m3u8", height=2160, bandwidth=15000000, codecs="avc1,mp4a")

        # 1. Full variant list with 4K: ceiling 1080p must pick 1080p
        all_vars = [v360, v720, v1080, v1440, v2160]
        chosen = select_variant(all_vars, max_height=1080)
        self.assertEqual(chosen.height, 1080)
        self.assertEqual(chosen.url, "https://e/1080.m3u8")

        # 2. Max height 720p ceiling: must pick 720p
        chosen_720 = select_variant(all_vars, max_height=720)
        self.assertEqual(chosen_720.height, 720)

        # 3. Streamer only offers 1440p and 2160p: fallback to lowest above 1080p (1440p)
        chosen_above = select_variant([v1440, v2160], max_height=1080)
        self.assertEqual(chosen_above.height, 1440)

        # 4. Streamer max broadcast is 720p: pick 720p
        chosen_lower = select_variant([v360, v720], max_height=1080)
        self.assertEqual(chosen_lower.height, 720)

    def test_normalize_hls_url_unescapes(self):
        self.assertEqual(
            normalize_hls_url(r"https:\/\/edge.example.com\/live\/playlist.m3u8"),
            "https://edge.example.com/live/playlist.m3u8",
        )
        self.assertEqual(
            normalize_hls_url(r"https://edge.example.com/live\u002Dplaylist.m3u8"),
            "https://edge.example.com/live-playlist.m3u8",
        )
        self.assertEqual(normalize_hls_url("//edge.example.com/x.m3u8"),
                         "https://edge.example.com/x.m3u8")


class TestRecordFmp4(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_init_segment_precedes_fragments(self, mock_build, mock_mux):
        init_payload = b"INIT-SEGMENT" * 100
        frag1 = b"FRAG-ONE" * 5000
        frag2 = b"FRAG-TWO" * 5000
        master = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"\n'
            "v/playlist_sfm4s.m3u8\n"
        )
        media = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
            '#EXT-X-MAP:URI="init.mp4"\n'
            "#EXTINF:2.0,\nseg-1.m4s\n"
            "#EXTINF:2.0,\nseg-2.m4s\n"
            "#EXT-X-ENDLIST\n"
        )

        def fake_open(req, timeout=None):
            url = req.full_url
            if url.endswith("master.m3u8"):
                return _resp(master.encode(), url=url)
            if url.endswith("playlist_sfm4s.m3u8"):
                return _resp(media.encode(), url=url)
            if url.endswith("init.mp4"):
                return _media_resp(init_payload, url=url)
            if url.endswith("seg-1.m4s"):
                return _media_resp(frag1, url=url)
            if url.endswith("seg-2.m4s"):
                return _media_resp(frag2, url=url)
            raise AssertionError(f"unexpected url {url}")

        mock_build.return_value.open.side_effect = fake_open

        captured = {}

        def fake_mux(input_path, output_mp4, **kwargs):
            captured["bytes"] = Path(input_path).read_bytes()
            captured["format"] = kwargs.get("input_format")
            Path(output_mp4).write_bytes(b"MP4")
            return True, ""

        mock_mux.side_effect = fake_mux

        out = self.tmp_dir / "fmp4.mp4"
        result = record_stream_to_disk(
            StreamInfo(room_id="cam", is_live=True,
                       hls_url="https://edge.example.com/master.m3u8"),
            out,
            stop_event=threading.Event(),
        )

        self.assertEqual(result, out)
        self.assertEqual(captured["format"], "mov")
        self.assertEqual(captured["bytes"], init_payload + frag1 + frag2)


class TestMuxFailureKeepsData(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @patch("core.recorder.mux_stream_copy", return_value=(False, "ffmpeg missing"))
    @patch("core.net.urllib.request.build_opener")
    def test_raw_ts_is_preserved_when_mux_fails(self, mock_build, _mock_mux):
        payload = b"TSDATA" * 20000  # ~120 KB > MIN_VALID_STREAM_BYTES
        media = "#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n#EXT-X-ENDLIST\n"

        def fake_open(req, timeout=None):
            if req.full_url.endswith("playlist.m3u8"):
                return _resp(media.encode(), url=req.full_url)
            return _media_resp(payload, url=req.full_url)

        mock_build.return_value.open.side_effect = fake_open

        out = self.tmp_dir / "session.mp4"
        result = record_stream_to_disk(
            StreamInfo(room_id="cam_raw", is_live=True,
                       hls_url="https://edge.example.com/playlist.m3u8"),
            out,
            stop_event=threading.Event(),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.suffix, ".ts")
        self.assertFalse(out.exists())
        self.assertEqual(result.read_bytes(), payload)


class TestReprobeOnTokenExpiry(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_403_triggers_reprobe_and_recording_continues(self, mock_build, mock_mux):
        calls = {"playlist": 0}
        media = "#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n#EXT-X-ENDLIST\n"

        def fake_open(req, timeout=None):
            url = req.full_url
            if "expired" in url:
                calls["playlist"] += 1
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if url.endswith("fresh.m3u8"):
                return _resp(media.encode(), url=url)
            return _media_resp(b"SEGMENTDATA" * 100, url=url)

        mock_build.return_value.open.side_effect = fake_open

        def fake_mux(input_path, output_mp4, **kwargs):
            Path(output_mp4).write_bytes(b"MP4")
            return True, ""

        mock_mux.side_effect = fake_mux

        stop = MagicMock(spec=threading.Event)
        stop.is_set.return_value = False
        stop.wait.return_value = False

        result = record_stream_to_disk(
            StreamInfo(room_id="cam_exp", is_live=True,
                       hls_url="https://edge.example.com/expired.m3u8"),
            self.tmp_dir / "out.mp4",
            stop_event=stop,
            reprobe_fn=lambda: StreamInfo(
                room_id="cam_exp", is_live=True,
                hls_url="https://edge.example.com/fresh.m3u8",
            ),
        )

        self.assertIsNotNone(result)
        self.assertGreaterEqual(calls["playlist"], 2)
        self.assertTrue(mock_mux.called)


class TestProbeFallbacks(unittest.TestCase):
    @patch("core.net.urllib.request.build_opener")
    def test_api_blocked_uses_edge_ajax_fallback(self, mock_build):
        def fake_open(req, timeout=None):
            url = req.full_url
            if "/api/chatvideocontext/" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if "get_edge_hls_url_ajax" in url:
                return _resp(json.dumps({
                    "url": "https://edge.example.com/live/playlist.m3u8",
                    "room_status": "public",
                }).encode(), url=url)
            raise AssertionError(f"unexpected {url}")

        mock_build.return_value.open.side_effect = fake_open

        info = probe_room("cam_ajax", prefer_ts=False)
        self.assertTrue(info.is_live)
        self.assertEqual(info.hls_url, "https://edge.example.com/live/playlist.m3u8")

    @patch("core.net.urllib.request.build_opener")
    def test_public_without_hls_is_reported_as_geo_block(self, mock_build):
        def fake_open(req, timeout=None):
            url = req.full_url
            if "get_edge_hls_url_ajax" in url:
                return _resp(json.dumps({"room_status": "public"}).encode(), url=url)
            if "/api/chatvideocontext/" in url:
                return _resp(json.dumps({"room_status": "public"}).encode(), url=url)
            return _resp(b"<html><body>nothing here</body></html>", url=url)

        mock_build.return_value.open.side_effect = fake_open

        info = probe_room("cam_geo")
        self.assertFalse(info.is_live)
        self.assertEqual(info.room_status, "geo_blocked")
        self.assertIn("hls_source", info.error)

    @patch("core.net.urllib.request.build_opener")
    def test_room_page_dossier_fallback(self, mock_build):
        dossier = json.dumps({
            "hls_source": "https://edge.example.com/live/playlist.m3u8",
            "room_status": "public",
        })
        escaped = dossier.replace("\\", "\\\\").replace('"', '\\"')
        page = (
            '<html><script>window.initialRoomDossier = "%s";</script></html>'
            % escaped
        )

        def fake_open(req, timeout=None):
            url = req.full_url
            if "/api/chatvideocontext/" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if "get_edge_hls_url_ajax" in url:
                return _resp(b"<html>blocked</html>", url=url)
            return _resp(page.encode(), url=url)

        mock_build.return_value.open.side_effect = fake_open

        info = probe_room("cam_html", prefer_ts=False)
        self.assertTrue(info.is_live)
        self.assertEqual(info.hls_url, "https://edge.example.com/live/playlist.m3u8")

    @patch("core.recorder.find_ffmpeg")
    @patch("subprocess.Popen")
    @patch("core.recorder.http_request")
    def test_record_stream_ffmpeg_maps_optimal_program_variant(self, mock_http, mock_popen, mock_ffmpeg):
        mock_ffmpeg.return_value = "/usr/bin/ffmpeg"
        master_m3u8 = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=896000,RESOLUTION=640x360\n'
            "chunklist_360.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=3096000,RESOLUTION=1280x720\n'
            "chunklist_720.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=5128000,RESOLUTION=1920x1080\n'
            "chunklist_1080.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=2560x1440\n'
            "chunklist_1440.m3u8\n"
        )
        fake_res = MagicMock(ok=True, text=master_m3u8)
        mock_http.return_value = fake_res

        proc_mock = MagicMock()
        proc_mock.poll.return_value = 0
        proc_mock.wait.return_value = 0
        mock_popen.return_value = proc_mock

        with TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "rec.mp4"
            # Create a fake part file so it passes size check
            part_file = out_file.with_name("rec.part.mp4")
            part_file.write_bytes(b"data" * 20000)

            info = StreamInfo(room_id="star", is_live=True, hls_url="https://edge.example.com/master.m3u8")
            stop_evt = threading.Event()
            res = record_stream_ffmpeg(info, out_file, stop_evt, max_duration_sec=10, max_height=1080)

            self.assertIsNotNone(res)
            # Verify the command passed to Popen
            call_cmd = mock_popen.call_args[0][0]
            # Variant 2 is 1080p (index 2 out of [360, 720, 1080, 1440])
            self.assertIn("-map", call_cmd)
            self.assertIn("0:p:2:v:0", call_cmd)
            self.assertIn("0:p:2:a:0?", call_cmd)
            self.assertIn("-reconnect_streamed", call_cmd)
            self.assertIn("-reconnect_on_network_error", call_cmd)
            self.assertIn("-seg_max_retry", call_cmd)
            self.assertIn("-rw_timeout", call_cmd)


class TestDoctorOffline(unittest.TestCase):
    def test_offline_report_runs_without_network(self):
        from core.config import Config
        from core.doctor import format_report, run_doctor

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = Config(api_id=1, api_hash="h", bot_token="t", chat_id=-1, admin_ids=[1])
            checks = run_doctor(
                cfg,
                base / "settings.txt",
                base / "recordings",
                base / "favorites.txt",
                probe_limit=0,
            )
            self.assertTrue(checks)
            report = format_report(checks)
            self.assertIn("CHẨN ĐOÁN", report)
            self.assertIn("Kết quả:", report)


if __name__ == "__main__":
    unittest.main()
