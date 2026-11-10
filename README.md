# CB2TE — Ghi hình Chaturbate tự động → Telegram (Termux, không cần root)

> Hệ thống chạy **24/7 trên điện thoại Android** trong Termux: tự phát hiện streamer online,
> ghi hình, đóng gói MP4, tải lên Telegram (tới 2 GB/file), tự dọn file sau 48 giờ.
> Điều khiển từ xa bằng bot Telegram. Không cần root, không cần máy tính.

<p align="center">
  <b>Chaturbate</b> → <i>probe 30 s/lần</i> → <b>ghi HLS</b> → <b>mux MP4 (ffmpeg)</b> → <b>upload Telegram</b> → <b>dọn sau 48 h</b>
</p>

> ⚠️ **Lưu ý:** Chaturbate là nền tảng 18+. Chỉ dùng cho mục đích cá nhân, tôn trọng
> điều khoản của Chaturbate và quyền riêng tư của người khác. Không dùng để phát tán,
> bán lại hoặc xâm phạm quyền hình ảnh.

---

## 📑 Mục lục

1. [Hệ thống này làm gì?](#1-hệ-thống-này-làm-gì)
2. [Điện thoại cần chuẩn bị gì?](#2-điện-thoại-cần-chuẩn-bị-gì)
3. [Cài đặt (người mới bắt đầu từ đây)](#3-cài-đặt-người-mới-bắt-đầu-từ-đây)
4. [Lấy thông tin Telegram](#4-lấy-thông-tin-telegram)
5. [Cấu hình `settings.txt`](#5-cấu-hình-settingstxt)
6. [Danh sách theo dõi `favorites.txt`](#6-danh-sách-theo-dõi-favoritestxt)
7. [Chạy 24/7](#7-chạy-247)
8. [Lệnh bot Telegram](#8-lệnh-bot-telegram)
9. [Lệnh trong Termux (CLI)](#9-lệnh-trong-termux-cli)
10. [Kiểm tra sức khoẻ & chẩn đoán](#10-kiểm-tra-sức-khoẻ--chẩn-đoán)
11. [Cập nhật phiên bản mới](#11-cập-nhật-phiên-bản-mới)
12. [Khắc phục sự cố](#12-khắc-phục-sự-cố)
13. [Câu hỏi thường gặp](#13-câu-hỏi-thường-gặp)
14. [Cấu trúc dự án & kiểm thử](#14-cấu-trúc-dự-án--kiểm-thử)

---

## 1. Hệ thống này làm gì?

Mỗi 30 giây, hệ thống kiểm tra danh sách streamer bạn theo dõi. Ai đang live → ghi hình
ngay; ai không live → bỏ qua. Khi phiên live kết thúc (hoặc file chạm 1.8 GB), hệ thống
đóng gói thành `.mp4` rồi tải lên Telegram, sau đó giữ file trên máy 48 giờ rồi tự xoá.

```
favorites.txt
     │  (mỗi 30 giây)
     ▼
[1] Kiểm tra phòng        GET /api/chatvideocontext/<room>/
     │                     ├─ offline / private / away  → bỏ qua
     │                     └─ public + link HLS         → ghi
     ▼
[2] Ghi luồng HLS         tải segment 64 KB xuống thư mục tạm
     │                     (hỗ trợ cả MPEG-TS và fMP4 #EXT-X-MAP)
     ▼
[3] Đóng gói MP4         ffmpeg -c copy  (không encode lại → CPU rất thấp)
     │                     nếu ffmpeg lỗi → GIỮ file .ts/.m4s, không mất dữ liệu
     ▼
[4] Tải lên Telegram     Telethon MTProto, có thanh tiến độ, file tới 2 GB
     │
     ▼
[5] Lưu 48 giờ           hết hạn thì tự xoá; máy gần đầy (<400 MB) thì xoá sớm file cũ nhất
```

**Điểm mạnh thiết kế cho điện thoại:**

| Đặc điểm | Ý nghĩa thực tế |
|---|---|
| Ghi theo chunk 64 KB xuống đĩa | RAM dùng < 70 MB, không bị Android "Low Memory Killer" giết |
| `ffmpeg -c copy` (không encode) | CPU < 2 %, máy không nóng, pin không tụt nhanh |
| Chia file tại 1.8 GB | Luôn vừa giới hạn 2 GB của Telegram |
| Giữ file thô khi mux lỗi | Mất ffmpeg cũng **không mất** cả phiên ghi |
| `termux-wake-lock` tự động | Tắt màn hình vẫn ghi tiếp |
| Lệnh `doctor` | Chỉ ra chính xác lỗi nằm ở đâu (mạng, ffmpeg, quyền ghi, proxy...) |

---

## 2. Điện thoại cần chuẩn bị gì?

| Yêu cầu | Ghi chú |
|---|---|
| Android 8.0 trở lên | Không cần root |
| **Termux** cài từ **F-Droid** | ⚠️ Bản trên CH Play đã lỗi thời, **không dùng** |
| **Termux:API** (cùng nguồn F-Droid) | Để dùng `termux-wake-lock`, `termux-setup-storage` |
| ~500 MB trống | Cho code + thư viện + thư mục ghi tạm |
| Wi-Fi ổn định + cục sạc | Treo 24/7 nên cắm sạc |
| Tài khoản Telegram | Để tạo bot và nhận video |

### Cài Termux đúng cách

1. Vào **F-Droid**: <https://f-droid.org/packages/com.termux/> → tải APK → cài.
2. Cài tiếp **Termux:API**: <https://f-droid.org/packages/com.termux.api/>
3. Mở **Cài đặt Android → Ứng dụng → Termux → Pin → Không hạn chế (Unrestricted)**.
   (Bắt buộc, nếu không Android sẽ giết tiến trình khi tắt màn hình.)
4. Mở Termux, gõ `termux-wake-lock` thử — nếu báo "command not found" thì chưa cài Termux:API.

---

## 3. Cài đặt (người mới bắt đầu từ đây)

### Cách A — Cài tự động bằng 1 lệnh (khuyến nghị)

Mở Termux và dán **từng dòng** sau (dấu `#` là chú thích, không cần gõ):

```bash
# 1. Cài git để tải code
pkg update -y && pkg install -y git python

# 2. Tải code về máy
cd ~
git clone https://github.com/nghiart/cb2te.git
cd cb2te

# 3. Chạy trình cài đặt tự động
bash install_termux.sh
```

Script sẽ tự: cài `python ffmpeg termux-api`, cấp quyền bộ nhớ, cài thư viện Python,
tạo `settings.txt` + `favorites.txt`, và chạy kiểm tra `doctor`.

> Nếu `git clone` báo lỗi mạng, xem [mục 12](#12-khắc-phục-sự-cố).

### Cách B — Cài thủ công (hiểu rõ từng bước)

```bash
# 1. Cập nhật Termux
pkg update -y && pkg upgrade -y

# 2. Cài Python, FFmpeg (bắt buộc), Git, Termux API
pkg install -y python ffmpeg git termux-api

# 3. Cho phép Termux truy cập bộ nhớ (bấm "Cho phép" khi Android hỏi)
termux-setup-storage

# 4. Tải code
cd ~
git clone https://github.com/nghiart/cb2te.git
cd cb2te

# 5. Cài thư viện Python
pip install -r requirements.txt
#    (nếu chưa có file requirements.txt: pip install telethon pysocks "python-socks[asyncio]")

# 6. Tạo file cấu hình từ mẫu
cp settings.example.txt settings.txt
cp favorites.example.txt favorites.txt

# 7. Kiểm tra
python3 rec2_app.py doctor --probe 0
```

Nếu bước 7 in ra `✅ [PASS] FFmpeg` và `✅ [PASS] Thư mục recordings` là bạn đã cài đúng.

### Cài vào thẻ nhớ / thư mục khác (tuỳ chọn)

Mặc định video ghi vào `recordings/` trong thư mục dự án. Muốn lưu ra bộ nhớ chung:

```bash
mkdir -p /sdcard/CB2TE
export RECORDINGS_DIR="/sdcard/CB2TE"
```

Muốn giữ cấu hình này sau khi đóng Termux, thêm dòng `export RECORDINGS_DIR=...` vào `~/.bashrc`:

```bash
echo 'export RECORDINGS_DIR="/sdcard/CB2TE"' >> ~/.bashrc
```

---

## 4. Lấy thông tin Telegram

Bạn cần **4 thứ**: `API_ID`, `API_HASH`, `BOT_TOKEN`, `CHAT_ID` (+ `ADMIN_IDS` của bạn).

### 4.1. `API_ID` và `API_HASH`

1. Mở <https://my.telegram.org> → đăng nhập bằng số điện thoại Telegram.
2. Chọn **API development tools**.
3. Điền **App title** (ví dụ `cb2te`) và **Short name** (ví dụ `cb2te`) → **Create application**.
4. Copy `App api_id` (một dãy số) và `App api_hash` (32 ký tự).

> `API_HASH` là bí mật, không đăng lên mạng, không commit lên GitHub.

### 4.2. `BOT_TOKEN` (tạo bot)

1. Trong Telegram, tìm **@BotFather** → bấm **Start**.
2. Gõ `/newbot` → đặt tên hiển thị (ví dụ `CB2TE Recorder`) → đặt username kết thúc bằng `bot`
   (ví dụ `cb2te_recorder_bot`).
3. BotFather trả về chuỗi dạng `123456789:ABCdef...` — đó là `BOT_TOKEN`.

**Quan trọng nếu muốn điều khiển bot trong Group:** gõ `/setprivacy` → chọn bot → **Disable**
(để bot đọc được lệnh trong group). Nếu không, hãy nhắn riêng cho bot.

### 4.3. `CHAT_ID` (nơi nhận video)

**Cách dễ nhất:**

1. Tạo một **Group** (hoặc Channel) trên Telegram, ví dụ `CB2TE Videos`.
2. Thêm bot của bạn vào group (**Add members** → tìm username bot).
3. Gửi 1 tin nhắn bất kỳ vào group.
4. Mở trên trình duyệt điện thoại:
   `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
   (thay `<BOT_TOKEN>` bằng token thật).
5. Tìm `"chat":{"id":-1001234567890, ...}` → đó là `CHAT_ID` (nhớ giữ **dấu trừ**).
6. Trong group, cấp quyền cho bot gửi file: **Group → Administrators → Add Admin → chọn bot**
   (hoặc ít nhất bật quyền gửi media).

> Nếu dùng **Channel**: thêm bot làm **Admin** của channel rồi lấy `chat_id` tương tự
> (thường có dạng `-100...`).

### 4.4. `ADMIN_IDS` (ai được ra lệnh cho bot)

1. Nhắn cho **@userinfobot** trên Telegram → nó trả về `Id: 123456789`.
2. Đó là `ADMIN_IDS` của bạn. Nhiều người dùng thì cách nhau dấu phẩy: `123,456`.

---

## 5. Cấu hình `settings.txt`

Mở file: `nano settings.txt` (lưu: `Ctrl+O` → `Enter`; thoát: `Ctrl+X`).

```ini
# 1. Telegram API (lấy ở my.telegram.org)
API_ID = 12345678
API_HASH = "0123456789abcdef0123456789abcdef"

# 2. Token bot (lấy từ @BotFather)
BOT_TOKEN = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"

# 3. Nơi nhận video (Group/Channel, có dấu trừ)
CHAT_ID = -1001234567890

# 4. Telegram User ID của bạn (người được điều khiển bot)
ADMIN_IDS = 123456789

# 5. Giữ video trên máy bao nhiêu giờ sau khi upload (mặc định 48)
RETENTION_HOURS = 48

# 6. Số phòng ghi đồng thời (điện thoại nên để 1 hoặc 2)
MAX_CONCURRENT = 2

# 7. Dung lượng trống tối thiểu (MB) để kích hoạt dọn dẹp khẩn cấp
MIN_FREE_MB = 400

# 8. Proxy (để trống nếu không dùng)
PROXY =
```

| Khoá | Bắt buộc | Ý nghĩa | Mặc định |
|---|---|---|---|
| `API_ID` | ✅ | Số lấy từ my.telegram.org | — |
| `API_HASH` | ✅ | Chuỗi bí mật lấy từ my.telegram.org | — |
| `BOT_TOKEN` | ✅ | Token của bot | — |
| `CHAT_ID` | ✅ | Nơi nhận video, **có dấu trừ** | — |
| `ADMIN_IDS` | ✅ | ID người được dùng lệnh bot, cách nhau dấu phẩy | — |
| `RETENTION_HOURS` | ❌ | Số giờ giữ file sau upload. **Không đặt 0** (sẽ xoá ngay). Muốn giữ lâu: `720` (30 ngày) | `48` |
| `MAX_CONCURRENT` | ❌ | Số phòng ghi cùng lúc. Máy yếu để `1` | `2` |
| `MIN_FREE_MB` | ❌ | Dưới ngưỡng này sẽ xoá bớt file cũ. `0` = tắt | `400` |
| `PROXY` | ❌ | `http://host:port`, `socks5://user:pass@host:1080` (xem mục 12) | trống |

**Nâng cao:** parser còn chấp nhận tên khoá có tiền tố `TELEGRAM_`
(`TELEGRAM_API_ID`, `TELEGRAM_BOT_TOKEN`...), `ADMIN_ID`/`ADMINS`,
`MAX_CONCURRENT_RECORDINGS`, `MIN_MB`.

---

## 6. Danh sách theo dõi `favorites.txt`

Mỗi dòng một streamer — có thể là **username** hoặc **link**:

```text
sweet_girl
hot_model_99
https://chaturbate.com/another_streamer/
# streamer_tam_nghi        ← dấu # ở đầu = tạm dừng, không xoá
```

- Tên có thể viết hoa/thường, có hoặc không có `@`.
- Dòng bắt đầu bằng `#` sẽ bị bỏ qua (cách "tạm nghỉ" một người).
- Có thể thêm ghi chú phía sau: `sweet_girl  tên hiển thị`.
- Sửa xong **không cần khởi động lại** — hệ thống đọc lại danh sách mỗi 30 giây.
- Nếu chạy `bot`, bạn còn có thể thêm/xoá bằng lệnh `/add`, `/del` ngay trong Telegram.

---

## 7. Chạy 24/7

### 7.1. Chạy daemon (bot + ghi hình + upload + dọn dẹp)

```bash
cd ~/cb2te
python3 rec2_app.py bot
```

Khi chạy, hệ thống sẽ:

- bật `termux-wake-lock` (giữ CPU chạy khi tắt màn hình);
- kiểm tra streamer mỗi 30 giây;
- ghi → mux → upload → đặt lịch xoá 48 giờ;
- nhận lệnh từ Telegram (`/status`, `/rec`, `/stop`...).

### 7.2. Giữ nó sống khi tắt màn hình / đóng Termux

| Việc cần làm | Cách làm |
|---|---|
| Không bị Android giết | Cài đặt → Ứng dụng → Termux → **Pin: Không hạn chế** |
| Hiện thông báo Termux | Kéo thanh thông báo → **Acquire wakelock** (hệ thống tự bật khi chạy `bot`) |
| Đóng Termux mà vẫn chạy | Cài **Termux:Boot** (F-Droid) và tạo `~/.termux/boot/start.sh` chứa lệnh chạy |
| Chạy nền trong 1 phiên | `pkg install tmux` → `tmux new -s cb` → chạy lệnh → `Ctrl+B` rồi `D` để thoát tạm |

Ví dụ file `~/.termux/boot/start.sh` (tự chạy khi khởi động máy):

```bash
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/cb2te
nohup python3 rec2_app.py bot > ~/cb2te/daemon.log 2>&1 &
```

Cấp quyền chạy: `chmod +x ~/.termux/boot/start.sh`.

### 7.3. Tắt an toàn

Nhấn **Ctrl + C** trong Termux. Hệ thống sẽ **đợi tối đa 120 giây** để đóng gói nốt file
đang ghi rồi mới thoát — đừng tắt ngay, hãy đợi thấy dòng `Daemon stopped gracefully.`

> Nếu lỡ tắt ngang: lần sau mở lên, chạy `python3 rec2_app.py remux` để đóng gói lại
> các file `.ts`/`.m4s` còn sót (xem [mục 10](#10-kiểm-tra-sức-khoẻ--chẩn-đoán)).

---

## 8. Lệnh bot Telegram

Nhắn trực tiếp cho bot (hoặc trong group nếu đã tắt privacy):

| Lệnh | Chức năng |
|---|---|
| `/status` | Xem phòng đang ghi, dung lượng trống, hàng đợi upload |
| `/rec <user>` | Bắt đầu ghi một streamer ngay (không cần có trong favorites) |
| `/stop <user>` | Dừng ghi một streamer, đóng gói file hiện tại |
| `/stop all` | Dừng tất cả |
| `/fav` | Xem danh sách đang theo dõi |
| `/add <user>` | Thêm vào `favorites.txt` và bắt đầu theo dõi |
| `/del <user>` | Xoá khỏi `favorites.txt` |
| `/files` | Danh sách video đã upload trong 48 h + đếm ngược xoá |
| `/help` | Hướng dẫn nhanh |

Ví dụ:

```text
/rec sweet_girl
/status
/files
/stop sweet_girl
```

> Chỉ `ADMIN_IDS` trong `settings.txt` mới ra lệnh được. Người khác nhắn bot sẽ bị bỏ qua.

---

## 9. Lệnh trong Termux (CLI)

Cú pháp chung (**cờ phải đứng trước lệnh con**):

```bash
python3 rec2_app.py [--settings FILE] [--favorites FILE] [--recordings-dir DIR] [--db FILE] [-v] <lệnh>
```

| Lệnh | Việc nó làm |
|---|---|
| `python3 rec2_app.py bot` | Chạy daemon 24/7 (bot + ghi hình + upload + dọn dẹp) |
| `python3 rec2_app.py status` | In nhanh: thư mục ghi, dung lượng trống, số streamer, video đã upload |
| `python3 rec2_app.py rec <user>` | Ghi 1 streamer ngay từ terminal (Ctrl+C để dừng) |
| `python3 rec2_app.py doctor` | Chẩn đoán toàn bộ pipeline (xem mục 10) |
| `python3 rec2_app.py remux` | Đóng gói lại các file `.ts`/`.m4s` thành `.mp4` |

Ví dụ:

```bash
# xem trạng thái
python3 rec2_app.py status

# ghi thử 1 streamer, xem log chi tiết
python3 rec2_app.py -v rec sweet_girl

# ghi vào thư mục khác
python3 rec2_app.py --recordings-dir /sdcard/CB2TE rec sweet_girl

# đóng gói lại toàn bộ file thô còn sót
python3 rec2_app.py remux
```

---

## 10. Kiểm tra sức khoẻ & chẩn đoán

### 10.1. `doctor` — "bác sĩ" của hệ thống

```bash
python3 rec2_app.py doctor              # kiểm tra + thử 1 streamer trong favorites
python3 rec2_app.py doctor --probe 3    # thử 3 streamer
python3 rec2_app.py doctor sweet_girl   # thử đúng 1 tên
python3 rec2_app.py doctor --probe 0    # chỉ kiểm tra máy, không dùng mạng
```

Kết quả gồm 3 mức: **✅ PASS**, **⚠️ WARN**, **❌ FAIL**, kèm gợi ý sửa ngay bên dưới:

```
✅ [PASS] FFmpeg            /data/data/com.termux/files/usr/bin/ffmpeg — ffmpeg version 7.1
✅ [PASS] Thư mục recordings  .../recordings — trống 21000 MB (ngưỡng 400 MB)
✅ [PASS] Proxy              socks5://10.0.0.2:1080
✅ [PASS] Live sweet_girl    6 segment | MPEG-TS | variant 3 | segment mẫu 98432 B
```

Các mục được kiểm tra: môi trường Python, ffmpeg, ffprobe, thư mục ghi, thư mục tạm,
`settings.txt`, proxy, Termux API, Telethon, `history.json`, và **probe thật** streamer
(API → playlist → variant → init segment → tải thử segment).

### 10.2. Xem log

```bash
python3 rec2_app.py -v bot            # log chi tiết ngay trên màn hình
python3 rec2_app.py bot > bot.log 2>&1 &   # ghi log ra file
tail -f bot.log                        # xem log trực tiếp
```

Các dòng đáng chú ý:

| Dòng log | Nghĩa |
|---|---|
| `Recording <room> from ...` | Đã bắt đầu ghi |
| `saved ...mp4` | Đóng gói + lưu thành công |
| `kept RAW ... instead of losing the session` | ffmpeg lỗi, **file vẫn được giữ** dạng `.ts` |
| `Room ... not recordable [geo_blocked]` | IP bị chặn vùng → cần `PROXY` |
| `kept RAW` + `muxing failed` | Cài/sửa ffmpeg rồi chạy `remux` |

### 10.3. Đóng gói lại file thô

Nếu từng thấy file `.ts`/`.m4s` trong `recordings/`:

```bash
pkg install ffmpeg -y      # đảm bảo ffmpeg đã có
python3 rec2_app.py remux
```

---

## 11. Cập nhật phiên bản mới

```bash
cd ~/cb2te
git pull                          # lấy code mới
pip install -r requirements.txt   # cập nhật thư viện (nếu có thay đổi)
python3 rec2_app.py doctor        # kiểm tra lại
```

Sau đó dừng daemon cũ (**Ctrl + C**) và chạy lại `python3 rec2_app.py bot`.

**Lưu ý an toàn:**

- `settings.txt`, `favorites.txt`, `recordings/`, `history.json`, file `.session` **không**
  bị `git pull` ghi đè (đã nằm trong `.gitignore`) → cấu hình của bạn được giữ nguyên.
- Nếu `git pull` báo có thay đổi cục bộ: `git stash` → `git pull` → `git stash pop`.
- Muốn quay lại bản cũ:
  ```bash
  git log --oneline -5        # xem các bản
  git switch --detach <mã>    # tạm quay lại bản đó
  git switch main             # quay lại mới nhất
  ```
- Cài lại sạch (giữ cấu hình): xoá thư mục code rồi `git clone` lại, sau đó copy
  `settings.txt`, `favorites.txt`, `history.json` từ bản cũ sang.

---

## 12. Khắc phục sự cố

> Mẹo: **luôn chạy `python3 rec2_app.py doctor` trước** — nó chỉ đúng mục đang lỗi.

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| Không có video nào, log im lặng | Thiếu ffmpeg / API bị chặn / geo-block | `pkg install ffmpeg`; chạy `doctor`; đặt `PROXY` |
| Có `.ts`/`.m4s` mà không có `.mp4` | ffmpeg lỗi hoặc chưa cài | `pkg install ffmpeg` → `python3 rec2_app.py remux` |
| `doctor` báo `HLS ... HTTP 403` | Token hết hạn hoặc IP bị chặn | Thử lại; nếu vẫn lỗi → dùng `PROXY` |
| `doctor` báo `geo_blocked` | Chaturbate giới hạn theo quốc gia/IP | Đặt `PROXY` ở quốc gia được phép |
| `ffmpeg: not found` | Chưa cài | `pkg install ffmpeg` |
| `ModuleNotFoundError: telethon` | Thiếu thư viện | `pip install -r requirements.txt` |
| Bot không trả lời | Sai `ADMIN_IDS`, hoặc bot trong group bị privacy | Nhắn riêng bot; `/setprivacy` → Disable; kiểm tra ID bằng `@userinfobot` |
| Bot báo lỗi gửi file vào group | Bot chưa có quyền | Group → Administrators → thêm bot, bật quyền gửi media |
| Upload kẹt ở `FloodWait` | Telegram giới hạn tần suất | Đợi vài phút; giảm `MAX_CONCURRENT` |
| Máy nóng / tụt pin nhanh | Ghi quá nhiều phòng | Đặt `MAX_CONCURRENT = 1`; cắm sạc |
| Đầy bộ nhớ | Nhiều video 48 h | Giảm `RETENTION_HOURS`, tăng `MIN_FREE_MB`, hoặc `export RECORDINGS_DIR=/sdcard/...` |
| Mất video khi Ctrl+C | Mux chưa xong | Bản mới đợi tới 120 s; đợi dòng `Daemon stopped gracefully.` |
| `database is locked` (Telethon) | Chạy 2 bot cùng lúc | Tắt hết, chỉ chạy 1 `bot`; xoá `cb2te_bot_session.session-journal` nếu còn |
| `git clone` lỗi mạng | DNS/TLS bị chặn | Thử Wi-Fi khác/4G; `git config --global http.sslBackend openssl` |

Phân tích chi tiết sự cố "không lưu phiên live" (nguyên nhân gốc + cách sửa trong code):
[`docs/2026-11-10-live-stream-not-saved-diagnosis.md`](docs/2026-11-10-live-stream-not-saved-diagnosis.md)

---

## 13. Câu hỏi thường gặp

**Có cần root không?**
Không. Toàn bộ chạy trong Termux với quyền của người dùng thường.

**Tốn bao nhiêu RAM?**
Khoảng 60–70 MB khi ghi 1 phòng; mỗi phòng thêm ~10–20 MB. Máy 3 GB RAM ghi 1–2 phòng thoải mái.

**Ghi được tối đa bao nhiêu phòng?**
Phụ thuộc CPU/mạng. Điện thoại tầm trung: **1–2**. Đặt `MAX_CONCURRENT` tương ứng.

**File lớn nhất là bao nhiêu?**
Mỗi file tối đa **1.8 GB** (để vừa giới hạn 2 GB của Telegram). Dài hơn sẽ tự chia file mới.

**Video được lưu bao lâu?**
Trên máy: `RETENTION_HOURS` giờ sau khi upload (mặc định 48). Trên Telegram: vĩnh viễn
(trừ khi bạn xoá). Máy gần đầy (<`MIN_FREE_MB`) sẽ xoá sớm file cũ nhất.

**Tắt máy / mất mạng thì sao?**
Phiên đang ghi dừng; phần đã tải vẫn được đóng gói và upload ở lần khởi động sau
(nếu còn file). Bật Termux:Boot để tự chạy lại.

**Có ghi được phòng private / ticket show không?**
Không. Hệ thống chỉ ghi phòng `public`.

**Không muốn dùng Telegram thì sao?**
Vẫn dùng được `python3 rec2_app.py rec <user>` để ghi ra máy; phần bot/upload chỉ cần
khi bạn muốn nhận video qua Telegram.

**`rec2-depseek.py` là gì?**
Bản script cũ (legacy, chạy độc lập). **Không cần dùng** — bản chính thức là `rec2_app.py`.
Giữ lại để tham khảo.

---

## 14. Cấu trúc dự án & kiểm thử

```text
cb2te/
├── bot/
│   ├── handlers.py          # Xử lý lệnh Telegram (/status, /rec, /stop...)
│   ├── telegram_bot.py      # Khởi tạo Telethon client (+ proxy dict)
│   └── uploader.py          # Hàng đợi upload MTProto, tiến độ, 2 GB
├── core/
│   ├── android.py           # wake-lock, giải phóng RAM, thư mục ghi
│   ├── config.py            # Đọc settings.txt / favorites.txt
│   ├── doctor.py            # Chẩn đoán PASS/WARN/FAIL
│   ├── muxer.py             # ffmpeg stream-copy (3 chiến lược + timeout)
│   ├── net.py               # Header trình duyệt, proxy HTTP/SOCKS5
│   ├── recorder.py          # Probe phòng + ghi HLS (TS & fMP4), re-probe
│   └── task_manager.py      # Điều phối đa luồng, giới hạn đồng thời
├── storage/
│   ├── cleaner.py           # Xoá sau 48 h + xả khẩn cấp khi đầy đĩa
│   └── history.py           # history.json (lịch sử file đã upload)
├── tests/                   # 120 bài kiểm thử tự động
├── docs/                    # Thiết kế + báo cáo phân tích sự cố
├── install_termux.sh        # Trình cài đặt tự động cho Termux
├── requirements.txt         # Thư viện Python cần thiết
├── rec2_app.py              # Chương trình chính (bot/status/rec/doctor/remux)
├── rec2-depseek.py          # Script cũ (legacy, không cần dùng)
├── settings.example.txt     # Mẫu cấu hình
└── favorites.example.txt    # Mẫu danh sách theo dõi
```

Chạy kiểm thử (không bắt buộc khi sử dụng):

```bash
python3 -m unittest discover -s tests -v     # 120 test
```

---

<p align="center">
  Làm bằng ☕ cho Termux · Báo lỗi/đề xuất: mở Issue trên GitHub
</p>
