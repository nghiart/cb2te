import io
import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import urllib.error

from core.recorder import (
    StreamInfo,
    probe_room,
    parse_hls_segments,
    record_stream_to_disk,
)


class TestRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # --- StreamInfo dataclass tests ---

    def test_stream_info_dataclass(self):
        info = StreamInfo(room_id="cam_model", is_live=True, hls_url="https://edge.example.com/playlist.m3u8")
        self.assertEqual(info.room_id, "cam_model")
        self.assertTrue(info.is_live)
        self.assertEqual(info.hls_url, "https://edge.example.com/playlist.m3u8")
        self.assertEqual(info.width, 0)
        self.assertEqual(info.height, 0)

        info2 = StreamInfo(room_id="offline_model", is_live=False, width=1280, height=720)
        self.assertFalse(info2.is_live)
        self.assertEqual(info2.hls_url, "")
        self.assertEqual(info2.width, 1280)
        self.assertEqual(info2.height, 720)

    # --- parse_hls_segments tests ---

    def test_parse_target_duration(self):
        from core.recorder import parse_target_duration
        self.assertEqual(parse_target_duration("#EXTM3U\n#EXT-X-TARGETDURATION:4\n"), 4.0)
        self.assertEqual(parse_target_duration("#EXTM3U\n#EXT-X-TARGETDURATION:10\n"), 6.0) # bounded to 6.0
        self.assertEqual(parse_target_duration("#EXTM3U\n#EXT-X-TARGETDURATION:0.5\n"), 1.0) # bounded to 1.0
        self.assertEqual(parse_target_duration("#EXTM3U\n"), 2.0) # default fallback

    def test_parse_hls_segments(self):
        sample_m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.000,\n"
            "segment1.ts\n"
            "#EXTINF:4.000,\n"
            "https://cdn.example.com/segment2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        segs = parse_hls_segments(sample_m3u8, base_url="https://live.example.com/hls/playlist.m3u8")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0], "https://live.example.com/hls/segment1.ts")
        self.assertEqual(segs[1], "https://cdn.example.com/segment2.ts")

    def test_parse_hls_segments_empty_and_whitespace(self):
        self.assertEqual(parse_hls_segments("", "https://live.example.com/"), [])
        self.assertEqual(parse_hls_segments("   \n#EXTM3U\n#EXTINF:2.0\n   \n", "https://live.example.com/"), [])

    def test_parse_hls_segments_relative_subpaths(self):
        sample_m3u8 = (
            "#EXTM3U\n"
            "chunks/sub/segment10.ts\n"
            "../segment11.ts\n"
        )
        segs = parse_hls_segments(sample_m3u8, base_url="https://live.example.com/hls/live/playlist.m3u8")
        self.assertEqual(segs[0], "https://live.example.com/hls/live/chunks/sub/segment10.ts")
        self.assertEqual(segs[1], "https://live.example.com/hls/segment11.ts")

    # --- probe_room tests ---

    @patch("core.net.urllib.request.build_opener")
    def test_probe_room_online_public_hls_source(self, mock_build_opener):
        resp_data = {
            "room_status": "public",
            "hls_source": "https://edge1.example.com/live/model1.m3u8",
            "width": 1920,
            "height": 1080,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        info = probe_room("https://chaturbate.com/model1/")
        self.assertTrue(info.is_live)
        self.assertEqual(info.room_id, "model1")
        self.assertEqual(info.hls_url, "https://edge1.example.com/live/model1.m3u8")
        self.assertEqual(info.width, 1920)
        self.assertEqual(info.height, 1080)

    @patch("core.net.urllib.request.build_opener")
    def test_probe_room_online_public_hls_url(self, mock_build_opener):
        resp_data = {
            "room_status": "public",
            "hls_url": "https://edge2.example.com/live/model2.m3u8",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        info = probe_room("model2")
        self.assertTrue(info.is_live)
        self.assertEqual(info.room_id, "model2")
        self.assertEqual(info.hls_url, "https://edge2.example.com/live/model2.m3u8")
        self.assertEqual(info.width, 0)
        self.assertEqual(info.height, 0)

    @patch("core.net.urllib.request.build_opener")
    def test_probe_room_offline_status(self, mock_build_opener):
        resp_data = {"room_status": "offline"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        info = probe_room("model3")
        self.assertFalse(info.is_live)
        self.assertEqual(info.room_id, "model3")
        self.assertEqual(info.hls_url, "")

    @patch("core.net.urllib.request.build_opener")
    def test_probe_room_private_status(self, mock_build_opener):
        resp_data = {
            "room_status": "private",
            "hls_source": "https://edge.example.com/secret.m3u8",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        info = probe_room("model_priv")
        self.assertFalse(info.is_live)
        self.assertEqual(info.room_id, "model_priv")

    def test_probe_room_empty_identifier(self):
        info = probe_room("  ")
        self.assertFalse(info.is_live)
        self.assertEqual(info.room_id, "")

    @patch("core.net.urllib.request.build_opener")
    def test_probe_room_network_error_handles_gracefully(self, mock_build_opener):
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("Connection refused")
        mock_build_opener.return_value = mock_opener

        info = probe_room("model_err")
        self.assertFalse(info.is_live)
        self.assertEqual(info.room_id, "model_err")

    @patch("core.net.urllib.request.build_opener")
    def test_probe_room_invalid_json_handles_gracefully(self, mock_build_opener):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html>Cloudflare error</html>"
        mock_resp.__enter__.return_value = mock_resp

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        info = probe_room("model_json_err")
        self.assertFalse(info.is_live)
        self.assertEqual(info.room_id, "model_json_err")

    @patch("core.net.urllib.request.ProxyHandler")
    @patch("core.net.urllib.request.build_opener")
    def test_probe_room_with_proxy(self, mock_build_opener, mock_proxy_handler):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"room_status": "offline"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        probe_room("model_proxied", proxy="http://127.0.0.1:8080")
        mock_proxy_handler.assert_called_with({"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"})

    # --- record_stream_to_disk tests ---

    def test_record_stream_not_live_returns_none(self):
        info = StreamInfo(room_id="cam1", is_live=False, hls_url="")
        out_mp4 = self.tmp_dir / "out.mp4"
        stop_event = threading.Event()
        res = record_stream_to_disk(info, out_mp4, stop_event)
        self.assertIsNone(res)
        self.assertFalse(out_mp4.exists())

    @patch("core.recorder.release_os_memory")
    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_record_stream_normal_flow_and_cleanup(self, mock_build_opener, mock_mux, mock_release_mem):
        playlist_m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:2\n"
            "#EXTINF:2.0,\n"
            "seg0.ts\n"
            "#EXTINF:2.0,\n"
            "seg1.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        fake_seg_data = b"MPEG_TS_DUMMY_PAYLOAD_CHUNK"

        def fake_open(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            if "playlist.m3u8" in url:
                resp.read.return_value = playlist_m3u8.encode("utf-8")
            elif "seg0.ts" in url or "seg1.ts" in url:
                # Return chunks of data then empty
                resp.read.side_effect = [fake_seg_data, b""]
            else:
                resp.read.return_value = b""
            resp.__enter__.return_value = resp
            return resp

        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_build_opener.return_value = mock_opener

        out_mp4 = self.tmp_dir / "normal.mp4"

        # Mock mux_stream_copy creating output file
        def fake_mux(input_ts, output_mp4, **kwargs):
            # Input .raw.ts must exist during muxing
            self.assertTrue(Path(input_ts).exists())
            self.assertGreater(Path(input_ts).stat().st_size, 0)
            Path(output_mp4).write_bytes(b"FINAL_MP4_CONTENT")
            return True, ""

        mock_mux.side_effect = fake_mux

        stop_event = threading.Event()
        progress_calls = []

        def on_prog(bytes_wr, elapsed):
            progress_calls.append((bytes_wr, elapsed))

        info = StreamInfo(room_id="cam_live", is_live=True, hls_url="https://live.example.com/playlist.m3u8")
        result = record_stream_to_disk(
            info,
            out_mp4,
            stop_event=stop_event,
            max_bytes=10_000_000,
            on_progress=on_prog,
        )

        self.assertEqual(result, out_mp4)
        self.assertTrue(out_mp4.exists())
        self.assertEqual(out_mp4.read_bytes(), b"FINAL_MP4_CONTENT")
        self.assertTrue(mock_mux.called)
        self.assertTrue(mock_release_mem.called)
        self.assertGreaterEqual(len(progress_calls), 1)

        # Ensure temporary raw.ts file was cleaned up
        raw_files = list(self.tmp_dir.glob("*.raw.ts"))
        self.assertEqual(len(raw_files), 0)

    @patch("core.recorder.release_os_memory")
    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_record_stream_max_bytes_rotation(self, mock_build_opener, mock_mux, mock_release_mem):
        # Playlist provides infinite segments
        counter = {"seg": 0}

        def fake_open(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            if "playlist.m3u8" in url:
                s = counter["seg"]
                counter["seg"] += 1
                body = f"#EXTM3U\n#EXTINF:2.0,\nseg_{s}.ts\n"
                resp.read.return_value = body.encode("utf-8")
            else:
                resp.read.side_effect = [b"A" * 100, b""]
            resp.__enter__.return_value = resp
            return resp

        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_build_opener.return_value = mock_opener

        out_mp4 = self.tmp_dir / "rotated.mp4"

        def fake_mux(input_ts, output_mp4, **kwargs):
            Path(output_mp4).write_bytes(b"ROTATED_MP4")
            return True, ""

        mock_mux.side_effect = fake_mux

        info = StreamInfo(room_id="cam_rotate", is_live=True, hls_url="https://live.example.com/playlist.m3u8")
        stop_event = MagicMock(spec=threading.Event)
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = False

        # Set max_bytes to 250 bytes -> should rotate after ~3 segments (300 bytes)
        result = record_stream_to_disk(
            info,
            out_mp4,
            stop_event=stop_event,
            max_bytes=250,
        )

        self.assertEqual(result, out_mp4)
        self.assertTrue(out_mp4.exists())
        self.assertTrue(mock_mux.called)
        self.assertTrue(mock_release_mem.called)

    @patch("core.recorder.release_os_memory")
    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_record_stream_consecutive_playlist_errors_terminates(self, mock_build_opener, mock_mux, mock_release_mem):
        counter = {"calls": 0}

        def fake_open(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            if "playlist.m3u8" in url:
                counter["calls"] += 1
                if counter["calls"] == 1:
                    # First fetch gives 1 segment
                    resp.read.return_value = b"#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n"
                    resp.__enter__.return_value = resp
                    return resp
                else:
                    # Subsequent fetches fail with 404
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            else:
                resp.read.side_effect = [b"VALID_SEG_DATA", b""]
                resp.__enter__.return_value = resp
                return resp

        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_build_opener.return_value = mock_opener

        out_mp4 = self.tmp_dir / "stream_ended.mp4"

        def fake_mux(input_ts, output_mp4, **kwargs):
            Path(output_mp4).write_bytes(b"MUXED_AFTER_END")
            return True, ""

        mock_mux.side_effect = fake_mux

        info = StreamInfo(room_id="cam_ended", is_live=True, hls_url="https://live.example.com/playlist.m3u8")
        stop_event = MagicMock(spec=threading.Event)
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = False

        result = record_stream_to_disk(
            info,
            out_mp4,
            stop_event=stop_event,
            max_bytes=10_000_000,
        )

        self.assertEqual(result, out_mp4)
        self.assertTrue(out_mp4.exists())
        # Should have called playlist 1 successful + 4 consecutive failures = 5 calls
        self.assertGreaterEqual(counter["calls"], 5)
        self.assertTrue(mock_mux.called)

    @patch("core.recorder.release_os_memory")
    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_record_stream_zero_bytes_does_not_mux(self, mock_build_opener, mock_mux, mock_release_mem):
        # Playlist fetch always fails from the beginning
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("Network unreachable")
        mock_build_opener.return_value = mock_opener

        out_mp4 = self.tmp_dir / "zero.mp4"
        info = StreamInfo(room_id="cam_zero", is_live=True, hls_url="https://live.example.com/playlist.m3u8")
        stop_event = MagicMock(spec=threading.Event)
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = False

        result = record_stream_to_disk(info, out_mp4, stop_event)
        self.assertIsNone(result)
        self.assertFalse(out_mp4.exists())
        self.assertFalse(mock_mux.called)
        self.assertTrue(mock_release_mem.called)

    @patch("core.recorder.release_os_memory")
    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_record_stream_mux_failure_cleans_up(self, mock_build_opener, mock_mux, mock_release_mem):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
        mock_resp.__enter__.return_value = mock_resp

        def fake_open(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            r = MagicMock()
            if "playlist.m3u8" in url:
                r.read.return_value = b"#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
            else:
                r.read.side_effect = [b"RAW_DATA", b""]
            r.__enter__.return_value = r
            return r

        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_build_opener.return_value = mock_opener

        out_mp4 = self.tmp_dir / "fail_mux.mp4"
        mock_mux.return_value = (False, "ffmpeg failed stream copy")

        info = StreamInfo(room_id="cam_fail_mux", is_live=True, hls_url="https://live.example.com/playlist.m3u8")
        stop_event = threading.Event()

        result = record_stream_to_disk(info, out_mp4, stop_event)
        self.assertIsNone(result)
        self.assertFalse(out_mp4.exists())
        self.assertTrue(mock_mux.called)
        self.assertTrue(mock_release_mem.called)

    @patch("core.recorder.release_os_memory")
    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_record_stream_handles_master_playlist(self, mock_build_opener, mock_mux, mock_release_mem):
        master_m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=1280x720\n"
            "variant/720p.m3u8\n"
        )
        media_m3u8 = (
            "#EXTM3U\n"
            "#EXTINF:2.0,\n"
            "seg0.ts\n"
            "#EXT-X-ENDLIST\n"
        )

        def fake_open(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            if "master.m3u8" in url:
                resp.read.return_value = master_m3u8.encode("utf-8")
            elif "720p.m3u8" in url:
                resp.read.return_value = media_m3u8.encode("utf-8")
            else:
                resp.read.side_effect = [b"MEDIA_SEG_DATA", b""]
            resp.__enter__.return_value = resp
            return resp

        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_build_opener.return_value = mock_opener

        out_mp4 = self.tmp_dir / "master_handled.mp4"

        def fake_mux(input_ts, output_mp4, **kwargs):
            Path(output_mp4).write_bytes(b"MASTER_MUXED_MP4")
            return True, ""

        mock_mux.side_effect = fake_mux

        info = StreamInfo(room_id="cam_master", is_live=True, hls_url="https://live.example.com/master.m3u8")
        stop_event = threading.Event()

        result = record_stream_to_disk(info, out_mp4, stop_event)
        self.assertEqual(result, out_mp4)
        self.assertTrue(out_mp4.exists())
        self.assertTrue(mock_mux.called)

    @patch("core.recorder.release_os_memory")
    @patch("core.recorder.mux_stream_copy")
    @patch("core.net.urllib.request.build_opener")
    def test_record_stream_seen_segments_fifo_pruning_preserves_sliding_window(
        self, mock_build_opener, mock_mux, mock_release_mem
    ):
        """Verify that when seen_segments exceeds 2000 items, pruning strictly discards
        the oldest items in FIFO order so recent sliding-window segments are not re-downloaded."""
        poll_count = {"count": 0}
        downloaded_urls: list[str] = []

        # Poll 1 returns 2005 segments (triggers pruning when count > 2000)
        # Poll 2 returns sliding window overlapping the last 5 segments + 1 new segment
        def fake_open(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            if "playlist.m3u8" in url:
                poll_count["count"] += 1
                if poll_count["count"] == 1:
                    lines = ["#EXTM3U", "#EXTINF:2.0,"]
                    for i in range(2005):
                        lines.append(f"seg_{i}.ts")
                    body = "\n".join(lines) + "\n"
                    resp.read.return_value = body.encode("utf-8")
                else:
                    # Sliding window: seg_2000 through seg_2004 are existing, seg_2005 is new
                    lines = ["#EXTM3U", "#EXTINF:2.0,"]
                    for i in range(2000, 2006):
                        lines.append(f"seg_{i}.ts")
                    lines.append("#EXT-X-ENDLIST")
                    body = "\n".join(lines) + "\n"
                    resp.read.return_value = body.encode("utf-8")
            else:
                downloaded_urls.append(url)
                resp.read.side_effect = [b"X", b""]
            resp.__enter__.return_value = resp
            return resp

        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_build_opener.return_value = mock_opener

        out_mp4 = self.tmp_dir / "fifo_prune.mp4"

        def fake_mux(input_ts, output_mp4, **kwargs):
            Path(output_mp4).write_bytes(b"FIFO_MUXED")
            return True, ""

        mock_mux.side_effect = fake_mux

        info = StreamInfo(room_id="cam_fifo", is_live=True, hls_url="https://live.example.com/playlist.m3u8")
        stop_event = MagicMock(spec=threading.Event)
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = False

        result = record_stream_to_disk(info, out_mp4, stop_event, max_bytes=100_000_000)

        self.assertEqual(result, out_mp4)
        self.assertTrue(out_mp4.exists())
        self.assertEqual(poll_count["count"], 2)

        # Total unique segments: 2005 in poll 1 + 1 (seg_2005) in poll 2 = 2006
        self.assertEqual(len(downloaded_urls), 2006)
        # Verify no duplicate downloads occurred for the sliding-window segments
        self.assertEqual(len(downloaded_urls), len(set(downloaded_urls)))
        self.assertEqual(downloaded_urls.count("https://live.example.com/seg_2000.ts"), 1)
        self.assertEqual(downloaded_urls.count("https://live.example.com/seg_2004.ts"), 1)
        self.assertEqual(downloaded_urls[-1], "https://live.example.com/seg_2005.ts")


if __name__ == "__main__":
    unittest.main()
