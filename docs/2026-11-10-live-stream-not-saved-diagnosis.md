# Sự cố: hệ thống không lưu phiên live stream Chaturbate

**Ngày:** 2026-11-10 · **Môi trường:** Android + Termux (không root) · **Phiên bản:** `cb2te` (nhánh `main`)

---

## 0. TL;DR (đọc 30 giây)

> Hệ thống **không hề ghi được** phiên live, chứ không phải "ghi xong rồi mất".
> Có 5 lỗi xếp tầng, mỗi lỗi đều **thất bại im lặng** nên log trông như "streamer offline":

| # | Mắt xích | Lỗi | Hậu quả |
|---|----------|-----|---------|
| 1 | Probe (`probe_room`) | gửi request trần (UA mobile, **không Referer/Origin/Cookie/CSRF**, không có đường dự phòng AJAX/HTML) | Chaturbate/WAF trả `403` → code bắt `Exception` rồi `return is_live=False` → **mọi phòng "offline"** |
| 2 | Probe | `room_status=public` + **không có** `hls_source` (geo-block) bị coi là offline | nằm im vĩnh viễn, không có cảnh báo |
| 3 | Ingest HLS | bỏ qua `#EXT-X-MAP` (init segment của **fMP4/CMAF**) | spool là "rác không header" → ffmpeg không mux được |
| 4 | Mux | ffmpeg thiếu/lỗi → `finally` **xoá** file `.raw.ts` | **mất trắng** cả phiên (kể cả khi tải đủ GB) |
| 5 | Vòng đời | `Ctrl+C` chỉ chờ 3 s; mux chưa xong → thread daemon bị kill; `os.replace` lỗi `EXDEV` khi spool ở internal storage còn `RECORDINGS_DIR` ở `/sdcard` | file dở/mất khi dừng |

Đã sửa hết + thêm lệnh **`python3 rec2_app.py doctor`** để chỉ đúng mắt xích hỏng trên máy thật.

---

## 1. Kiến trúc và 4 mắt xích có thể mất dữ liệu

```
favorites.txt
   │  mỗi 30 s
   ▼
[1] probe_room()          GET /api/chatvideocontext/<room>/   ── 403 / geo-block ──► "offline" (im lặng)
   │  hls_url
   ▼
[2] record_stream_to_disk()  playlist ─► variant ─► init(#EXT-X-MAP) ─► segments
   │  spool .ts / .m4s  (thư mục tạm)
   ▼
[3] mux_stream_copy()     ffmpeg -c copy -movflags +faststart  ── lỗi ──► xoá spool (!!)
   │  .mp4
   ▼
[4] TaskManager ─► UploadQueue (Telegram) ─► HistoryStore ─► RetentionCleaner 48 h
```

Chỉ cần **một** mắt xích trả `None`/`False` là `/status`, `/files` trống và trên điện thoại "không thấy video nào".
Trước khi sửa, cả 5 lỗi dưới đây đều có thể xảy ra **đồng thời** và đều **không để lại log lỗi**.

---

## 2. Tái hiện (reproduction)

Không thể gọi Chaturbate thật từ môi trường kiểm thử (bị chặn TLS/geo), nên sự cố được tái hiện bằng một HTTP server cục bộ mô phỏng đúng hành vi của edge Chaturbate:

* `403` nếu request **không có `Referer: https://chaturbate.com/<room>/`**;
* master playlist → `playlist_sfm4s.m3u8` (CMAF) → `#EXT-X-MAP:URI="init.mp4"` + `seg-N.m4s`;
* segment chỉ trả khi có Referer.

Kết quả với code **trước khi sửa** (mô phỏng):

```
[1] probe:  -> live=False  (api HTTP 403 ; edge-ajax HTTP 403 ; page HTTP 403)
[2] record: không chạy vì is_live=False   ← "không lưu phiên nào"
```

Kết quả với code **sau khi sửa** (chạy thật, có socket thật):

```
[1] probe không header:  -> live=False status=error
[2] probe có header mới: -> live=True  hls=http://127.0.0.1:.../live/master.m3u8
[3] ghi: master → variant → init.mp4 (256 B) → 3 × seg-N.m4s (32 KB)
    -> saved bytes=98560  init_first=True  all_fragments=True
    (ffmpeg không có trên máy test nên file được giữ dạng .m4s — xem lỗi #4)
```

> **Giới hạn của chẩn đoán:** môi trường phân tích không gọi được CDN Chaturbate (bị chặn TLS/geo), nên kết luận về hành vi của Chaturbate dựa trên (a) code của chính repo (`rec2-depseek.py` từng chạy được với đủ header) và (b) các client đang bảo trì (yt-dlp, streamlink, streamonitor). Bước xác nhận cuối cùng trên máy thật chính là `python3 rec2_app.py doctor` — nó in ra HTTP status thật của API/playlist/segment.

---

## 3. Nguyên nhân gốc, bằng chứng và cách khắc phục

### 3.1. Probe bị Chaturbate chặn vì thiếu "bộ header trình duyệt" — **nguyên nhân số 1**

Code cũ (`core/recorder.py` trước khi sửa):

```python
_DEFAULT_USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) ... Mobile Safari/537.36"

def _default_headers():
    return {
        "User-Agent": _DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
    }          # thiếu Referer, Origin, Cookie, Accept-Language, CSRF

def probe_room(...):
    ...
    except Exception as e:
        logger.debug(f"probe_room failed for {user}: {e}")   # ← log mức DEBUG
        return StreamInfo(room_id=user, is_live=False)        # ← "offline" giả
```

Bằng chứng từ các implementation đang chạy được:

* **Chaturbate API hiện tại** được yt-dlp gọi với `impersonate=True` + `X-Requested-With` + `Accept: application/json` (`yt_dlp/extractor/chaturbate.py`); streamlink phải fallback sang `cloudscraper` khi request trần lỗi. Nguồn: [yt-dlp PR #12583](https://github.com/yt-dlp/yt-dlp/pull/12583), [streamlink chaturbate plugin](https://raw.githubusercontent.com/community-plugins/streamlink-plugins/cloudscraper/plugins/chaturbate.py).
* Chính repo này **đã từng có** header đúng trong `rec2-depseek.py` (`_cb_hdrs`, dòng 1123–1140): `Referer`, `Origin`, `Cookie: agreeterms=1; csrftoken=...`, `X-CSRFToken`, và hai đường dự phòng (`get_edge_hls_url_ajax` + `window.initialRoomDossier`).
  → Bản refactor sang `core/` đã **làm rơi** toàn bộ phần này. Đây là regression, không phải "Chaturbate đổi API".

**Sửa:** `core/net.py::chaturbate_headers()` dựng header giống Chrome desktop, có `Referer` theo từng phòng, `Origin`, `Accept-Language`, cookie `agreeterms=1`, CSRF khi cần; mọi request đều dùng chung (probe, playlist, segment).

### 3.2. Lỗi bị nuốt + `public` không có `hls_source` bị coi là offline

```python
status = str(data.get("room_status")).lower()
if status == "public" and hls_url:   # còn lại: offline
    ...
return StreamInfo(room_id=user, is_live=False)      # 403 / lỗi JSON / geo → "offline"
```

Người dùng ở Việt Nam rất hay gặp trường hợp `room_status = "public"` nhưng `hls_source` rỗng (Chaturbate giới hạn CDN theo IP — yt-dlp coi đây là `geo_restricted`). Code cũ biến nó thành "offline" nên **không có bất kỳ gợi ý nào** để xử lý.

**Sửa:** `core/recorder.py::probe_room()` (dòng ~390) đi 3 bước: API → `POST /get_edge_hls_url_ajax/` → tải trang phòng và bóc `window.initialRoomDossier` / regex `.m3u8`; phân loại rõ `offline / private / away / password protected / hidden / geo_blocked / error`; ghi `logger.warning` kèm HTTP status. `TaskManager` cũng log lý do thay vì im lặng.

### 3.3. Bỏ qua `#EXT-X-MAP` → hỏng container fMP4/CMAF

`parse_hls_segments()` chỉ lấy các dòng **không** bắt đầu bằng `#`, nên `#EXT-X-MAP:URI="init.mp4"` bị bỏ. Chaturbate phát `playlist_sfm4s.m3u8` (fMP4): thiếu init segment thì chuỗi `.m4s` **không thể demux** → ffmpeg fail → sang lỗi #4.

**Sửa:** `parse_hls_playlist()` (dòng 222) trả đầy đủ `init_url`, `variants`, `ended`, `vod`, `encrypted`, `target_duration`. `record_stream_to_disk()` ghi init **một lần** ở đầu spool, theo dõi hash để phát hiện đổi codec/độ phân giải giữa phiên; ưu tiên playlist MPEG-TS khi có (`_prefer_ts_playlist`), vẫn hỗ trợ fMP4.

### 3.4. Mux thất bại ⇒ xoá luôn dữ liệu đã ghi

```python
ok, err = mux_stream_copy(raw_ts_path, out_path)
if ok: return out_path
logger.error(...)
return None                     # ← finally: raw_ts_path.unlink()
finally:
    raw_ts_path.unlink()        # mất toàn bộ phiên
```

Bất kỳ lý do nào khiến ffmpeg không chạy được (chưa `pkg install ffmpeg`, hết dung lượng khi `+faststart` cần ghi lần 2, file spool bị cắt do Android kill) đều dẫn tới **mất trắng** — kể cả phiên 2 GB tải thành công. Ngoài ra `subprocess.run` **không có timeout** nên có thể treo vô hạn.

**Sửa:** `core/muxer.py::mux_stream_copy()` (dòng 111) thử 3 cấu hình (copy+faststart → copy → video-only), có `timeout`, `-avoid_negative_ts make_zero`, `-max_muxing_queue_size`; `record_stream_to_disk()` nếu mux fail sẽ **giữ lại spool** `.ts`/`.m4s` (≥ 64 KB) và trả về đường dẫn đó. `TaskManager` đẩy file raw vào hàng đợi upload; `UploadQueue` gửi dạng document. Thêm lệnh `remux` để đóng gói lại sau khi cài ffmpeg.

### 3.5. Mất file khi dừng / khi spool khác ổ đĩa

* `rec2_app.py` gọi `stop_all(wait=True, timeout=3.0)`: mux một file vài GB trên điện thoại mất hàng chục giây → thread daemon bị kill giữa lúc mux.
* `spool_path.replace(out_path)` = `os.replace`: **lỗi `EXDEV`** khi `TMPDIR` ở internal storage (`/data/data/com.termux/files/usr/tmp`) còn `RECORDINGS_DIR` ở `/sdcard` (FUSE, khác mount).

**Sửa:** `stop_all(timeout=120)` khi shutdown (`rec2_app.py:362`); `_move_file()` (`core/recorder.py:678`) fallback `shutil.move` (copy + unlink) khi `os.replace` thất bại.

### 3.6. Các lỗi phụ đã xử lý luôn

| Lỗi | Sửa |
|-----|-----|
| `PROXY = socks5://...` được README quảng cáo nhưng `urllib.ProxyHandler` **không hỗ trợ SOCKS** → lỗi mơ hồ, probe fail | `core/net.py::normalize_proxy/build_opener` hỗ trợ `socks5`, `socks5h`, `socks4`, `socks4a` qua PySocks; báo lỗi rõ "pip install pysocks" |
| Token playlist hết hạn (403/404/410) → kết thúc phiên giữa chừng | `reprobe_fn` được `TaskManager` truyền vào: tự probe lại và đổi `hls_url` (tối đa 5 lần), fallback về master 2 lần |
| `#EXT-X-KEY` (AES-128) bị bỏ qua → ghi ra rác | Phát hiện và dừng với log rõ ràng thay vì tạo file hỏng |
| Master playlist chọn variant kém / `_fast` | `select_variant()` ưu tiên độ phân giải + codec browser, hạ ưu tiên `_fast` |
| Không có log nào khi phiên "không lưu" | Log WARNING/ERROR ở mọi nhánh + `doctor` |

---

## 4. Thay đổi trong code

| File | Nội dung |
|------|----------|
| `core/net.py` **(mới)** | Header trình duyệt, proxy HTTP/SOCKS5, `http_request()` trả status thay vì nuốt lỗi |
| `core/recorder.py` | `probe_room()` 3 tầng + phân loại trạng thái; `parse_hls_playlist()` hỗ trợ fMP4/`EXT-X-MAP`; ghi init segment; re-probe khi token hết hạn; giữ spool khi mux lỗi; `_move_file()` an toàn khác ổ đĩa |
| `core/muxer.py` | 3 chiến lược mux, timeout, `input_format`, không bao giờ xoá input |
| `core/task_manager.py` | Truyền `reprobe_fn`, `min_valid_bytes`; log lý do không ghi được; `stop_all` chờ 60 s |
| `core/doctor.py` **(mới)** | 11 kiểm tra nền + 2 kiểm tra/streamer (PASS/WARN/FAIL): ffmpeg, quyền ghi, dung lượng, settings, proxy, telethon, probe + playlist + init + segment thật |
| `rec2_app.py` | Lệnh `doctor`, lệnh `remux`, shutdown chờ 120 s, thông báo lỗi probe rõ ràng |
| `bot/uploader.py` | Upload được cả `.ts/.m4s` (document) để không mất footage khi mux lỗi |
| `tests/` | `test_net.py`, `test_recorder_fixes.py` (fMP4, raw fallback, re-probe, probe fallback, doctor) + sửa test cũ sang API mới |

---

## 5. Quy trình xác minh trên máy Termux

```bash
cd ~/cb2te

# 0. Cập nhật code + thư viện
pkg install ffmpeg -y            # BẮT BUỘC: thiếu ffmpeg là nguyên nhân #4
pip install telethon pysocks     # pysocks chỉ cần nếu dùng SOCKS5

# 1. Chẩn đoán toàn bộ pipeline (kiểm tra 1 streamer trong favorites)
python3 rec2_app.py doctor
python3 rec2_app.py doctor --probe 3          # thử 3 streamer
python3 rec2_app.py doctor sweet_girl         # thử 1 tên cụ thể

# 2. Ghi thử trực tiếp, xem log chi tiết
python3 rec2_app.py -v rec sweet_girl

# 3. Nếu file kết quả là .ts/.m4s (mux lỗi lúc đó): đóng gói lại
python3 rec2_app.py remux recordings/

# 4. Chạy 24/7
python3 rec2_app.py bot
```

`doctor` in ra bảng PASS/WARN/FAIL, ví dụ khi mọi thứ ổn:

```
✅ [PASS] FFmpeg        /data/data/com.termux/files/usr/bin/ffmpeg — ffmpeg version 7.x
✅ [PASS] Thư mục recordings  /storage/emulated/0/Movies/CB — trống 21000 MB
✅ [PASS] Proxy          socks5://10.0.0.2:1080
✅ [PASS] Live sweet_girl    6 segment | MPEG-TS | variant 3 | segment mẫu 98432 B
```

Nếu thấy:

* `❌ FFmpeg ... không tìm thấy` → `pkg install ffmpeg`.
* `❌ HLS ... playlist lỗi: HTTP 403` → IP bị chặn/geo: đặt `PROXY` trong `settings.txt` (ưu tiên proxy cùng quốc gia với tài khoản).
* `❌ Live ... segment mẫu 0 B` → CDN chặn: kiểm tra `Referer`/proxy.
* `⚠️ Chaturbate ... status=geo_blocked` → xác nhận đúng nguyên nhân #2: cần proxy.

---

## 6. Kiểm thử

```
Ran 120 tests — OK (skipped=2)      # 2 test cần ffmpeg thật trên máy chủ CI
```

Các test mới khoá lại đúng 5 lỗi trên:

* `test_recorder_fixes.py::TestRecordFmp4::test_init_segment_precedes_fragments` — init phải đứng đầu spool.
* `TestMuxFailureKeepsData::test_raw_ts_is_preserved_when_mux_fails` — mux hỏng **không** được xoá dữ liệu.
* `TestReprobeOnTokenExpiry::test_403_triggers_reprobe_and_recording_continues` — 403 → re-probe → ghi tiếp.
* `TestProbeFallbacks` — API 403 → AJAX → dossier HTML; `public` không URL ⇒ `geo_blocked`.
* `test_net.py` — header/Referer, proxy SOCKS5, status 403 trả về thay vì raise.

---

## 7. Nếu vẫn chưa ghi được: checklist 60 giây

1. `python3 rec2_app.py doctor` — mục ❌ nào?
2. `ffmpeg -version` chạy được chưa? (`pkg install ffmpeg`)
3. `echo $RECORDINGS_DIR` có trỏ vào thư mục **ghi được**? (`ls -ld`)
4. `termux-wake-lock` có tồn tại? Tắt tối ưu pin cho Termux chưa?
5. `favorites.txt` có đúng username? (`/fav` trong bot)
6. Log có dòng `Room ... not recordable [geo_blocked]`? → cần `PROXY`.
7. Log có `kept RAW ...` không? → ffmpeg lỗi, chạy `remux`.

---

## 8. Giới hạn đã biết

* HLS **mã hoá AES-128** (không phải Chaturbate public) chưa giải mã — hệ thống dừng và báo lỗi rõ.
* Geo-block chỉ giải quyết được bằng proxy/VPN; phần mềm không thể vượt nếu CDN từ chối cấp URL.
* fMP4 được remux bằng `-f mov` + `-c copy`; nếu về sau Chaturbate đổi sang codec không tương thích MP4, cần thêm bước transcode có kiểm soát (không bật mặc định vì tốn CPU điện thoại).
