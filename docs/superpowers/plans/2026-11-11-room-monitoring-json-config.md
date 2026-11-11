# Room Monitoring, JSON Config & Admin Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chuyển đổi định dạng cấu hình sang JSON (`settings.json`, `favorites.json`), đổi thư mục tải về `~/storage/downloads/CB2TE`, đổi tên file thực thi chính sang `1st.py`, bổ sung chu kỳ quét 5 phút định kỳ báo trạng thái (kèm resolution) qua Telegram, và thêm các lệnh điều khiển chi tiết cho từng room.

**Architecture:**
- `core/config.py`: Mở rộng để đọc/ghi `settings.json` và `favorites.json`, đồng thời hỗ trợ backward compatibility đọc `settings.txt` và `favorites.txt` nếu chưa có JSON.
- `core/android.py`: Cập nhật `get_recordings_dir` ưu tiên `~/storage/downloads/CB2TE` (fallback `~/storage/downloads` hoặc `./recordings`).
- `core/recorder.py`: Trích xuất độ phân giải (`1080p`, `720p`, v.v.) vào `StreamInfo.resolution_label` khi parse playlist variant.
- `core/task_manager.py`: Thêm cơ chế theo dõi chuyển đổi trạng thái (`last_status` & `last_resolution`) và timer quét 5 phút. Khi phát hiện thay đổi (`offline` -> `online (1080p)`, `private`, `error`), kích hoạt callback `on_status_change(room_id, old_status, new_status, info)`.
- `bot/handlers.py` & `bot/telegram_bot.py`: Bổ sung xử lý notification tới `admin_ids`, lệnh `/status <room>`, `/rec <room>`, `/stop <room>`, `/upload <room>`, `/add <room>`, `/del <room>`.
- `1st.py`: Entrypoint chính thay thế cho `rec2_app.py` (giữ `rec2_app.py` làm wrapper chuyển tiếp).

**Tech Stack:** Python 3 (asyncio, Telethon, urllib, unittest).

---

## Global Constraints
- Tất cả unit test hiện tại (120 test) phải tiếp tục pass 100%.
- Tương thích hoàn toàn trên môi trường Termux / Android.
- Không gây chặn (non-blocking) chu kỳ recording / muxing hiện tại.

---

### Task 1: Cấu hình JSON (`settings.json`, `favorites.json`) và thư mục `~/storage/downloads/CB2TE`

**Files:**
- Modify: `core/config.py`
- Modify: `core/android.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_android.py`

- [ ] **Step 1: Viết test cho load/save settings.json & favorites.json**
- [ ] **Step 2: Cập nhật `core/config.py` hỗ trợ JSON**
- [ ] **Step 3: Cập nhật `core/android.py` trỏ vào `~/storage/downloads/CB2TE`**
- [ ] **Step 4: Chạy test kiểm chứng**

---

### Task 2: Trích xuất Độ phân giải (Resolution Label) trong `probe_room`

**Files:**
- Modify: `core/recorder.py`
- Modify: `tests/test_recorder.py`

- [ ] **Step 1: Thêm trường `resolution_label` vào `StreamInfo`**
- [ ] **Step 2: Cập nhật `probe_room` & `probe_hls` trích xuất nhãn phân giải (vd: 1080p, 720p, 480p)**
- [ ] **Step 3: Chạy test xác nhận**

---

### Task 3: Quét định kỳ 5 phút và Thông báo đổi trạng thái Model

**Files:**
- Modify: `core/task_manager.py`
- Modify: `tests/test_task_manager.py`

- [ ] **Step 1: Thêm `on_status_change` callback và tracker trạng thái trước đó**
- [ ] **Step 2: Bổ sung chu kỳ kiểm tra định kỳ 5 phút (300 giây)**
- [ ] **Step 3: Viết test mô phỏng thay đổi trạng thái (Offline -> Online 1080p, Online -> Private)**

---

### Task 4: Bot Telegram: Lệnh chi tiết cho từng room & Gửi thông báo tự động

**Files:**
- Modify: `bot/handlers.py`
- Modify: `bot/telegram_bot.py`
- Modify: `tests/test_bot_handlers.py`

- [ ] **Step 1: Thêm lệnh `/status <room>` hiển thị chi tiết (độ phân giải, bitrate, trạng thái ghi, link)**
- [ ] **Step 2: Thêm lệnh `/upload <room>` kích hoạt upload file tồn của room**
- [ ] **Step 3: Kết nối `on_status_change` gửi tin nhắn cho `admin_ids`**
- [ ] **Step 4: Cập nhật test bot handlers**

---

### Task 5: Đổi tên file chính thành `1st.py` và cập nhật README / Scripts

**Files:**
- Create: `1st.py`
- Modify: `rec2_app.py` (biến thành bridge/alias gọi `1st.py`)
- Modify: `install_termux.sh`, `README.md`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Tạo `1st.py` từ `rec2_app.py`, cấu hình mặc định đọc `settings.json` và `favorites.json`**
- [ ] **Step 2: Giữ `rec2_app.py` tương thích ngược**
- [ ] **Step 3: Chạy toàn bộ test suite `python3 -m unittest discover -s tests`**

