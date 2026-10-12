# HƯỚNG DẪN SỬ DỤNG THREADS STANDALONE — TIẾNG VIỆT

> **Phiên bản tài liệu:** 2026-10-12. **Đối tượng:** operator dùng CLI cục bộ trên Windows.
>
> **Đọc trước:** Micro-slice S1–S6 cung cấp quy trình offline để operator nhập permalink, lưu shortlist riêng theo account, tự viết draft và duyệt target. S2 search/conversation chỉ dùng fixture synthetic; **không có tìm kiếm/thu thập Threads thật bằng browser**. S4 handoff có thể mở browser thật tới đúng URL sau xác nhận `OPEN`, nhưng chưa chạy live UAT. Không có browser auto-fill hoặc auto-send. Không hiểu tài liệu/test synthetic là sự cho phép của Meta hay bằng chứng đã qua App Review.
>
> Kế hoạch: [GitHub issue #47](https://github.com/pumni/threads-standalone/issues/47), S5: [issue #55](https://github.com/pumni/threads-standalone/issues/55), S6: [issue #58](https://github.com/pumni/threads-standalone/issues/58). Release gate: [runbook](docs/BROWSER_MVP_RELEASE_READINESS.md). Giới hạn live/UAT: [issue #18](https://github.com/pumni/threads-standalone/issues/18) và [#46](https://github.com/pumni/threads-standalone/issues/46).

## 1. Threads Standalone dùng để làm gì?

Threads Standalone là công cụ chạy **cục bộ trên máy tính**. Workflow S1–S6 tập trung vào permalink do operator cung cấp, queue riêng theo account, shortlist/draft riêng tư, exact-target browser handoff có xác nhận và journal outcome local. Các lệnh API/Nurture/mutation đã có là luồng riêng, có thể gọi API hoặc tạo remote effect khi operator chạy với quyền phù hợp.

**Nguyên tắc:** Mỗi tài khoản phải thuộc sở hữu khách hàng hoặc được ủy quyền hợp pháp; người dùng chịu trách nhiệm kiểm tra nội dung và đích gửi. Không tạo tương tác giả, spam hoặc dùng phần mềm vượt challenge/CAPTCHA. Các hoạt động đọc/ghi tự động trên website chỉ được kích hoạt khi nền tảng cho phép hành vi tương ứng.

### Trạng thái hiện tại sau Micro-slice 6

| Chức năng | Trạng thái |
| --- | --- |
| Tạo local account, đặt alias, tách Chromium profile theo UUID | **Có; synthetic-tested**. Ví dụ trong tài liệu chỉ dùng alias synthetic |
| S1: mở exact thread và giữ Chromium cho operator | **Có; fake-browser/synthetic-tested; chưa live UAT**. Actor luôn `UNKNOWN_IDENTITY` |
| S2: search/conversation adapter | **Fixture synthetic-only**; chưa có live selector hoặc browser-search CLI |
| S3: `manual-list` rồi `manual-review` | **CLI offline đã nối**; danh sách canonical/dedup trước khi chọn; draft/context chỉ in trong private TTY |
| S4: `manual-handoff`, `manual-status`, `manual-record` | **Có; synthetic-tested**; exact target/SHA, xác nhận `OPEN`/`RECORD`, journal `UNVERIFIED`, không auto-fill/send |
| S5: integrated CLI flow, installed-wheel smoke, docs/runbook | **Synthetic-only**; không mở Chromium thật hoặc gọi network/API trong test |
| S6: `opportunity-import`, `opportunity-list`, `opportunity-select` | **Local queue theo account; synthetic-only**; context do operator nhập, S3 review dùng exact target, không đọc Threads |
| Queue maintenance: `opportunity-skip`, `opportunity-remove` | **Local-only**; remove yêu cầu `DELETE` và bị chặn khi S4 handoff còn `HANDOFF_UNRESOLVED` |
| Đọc/ghi Threads bằng official API và Nurture | **Lệnh riêng đã có**; có thể truy cập API hoặc tạo remote effect tùy lệnh, credentials và quyền |
| Browser tự điền/gửi reply, tự động engagement | **Không có trong S1–S6**; không hứa khả năng hoặc quyền sử dụng |

S1–S6 **synthetic/local-only, chưa production-ready**. Queue operations không khởi chạy browser, không import Playwright runtime, không gọi API và không tạo remote effect. Meta Terms/compliance permission cho automated browser reads/actions, App Review cho capability API cần review riêng. Live Threads UAT, kiểm tra root/profile/account thật và các issue #18/#46 chưa được thực hiện trong checkpoint này.

### Trạng thái Micro-slice 2 — synthetic-only

- Adapter tìm kiếm và đọc hội thoại chỉ nhận dữ liệu fixture/fake engine. Chưa có selector Threads thật và không có lệnh `browser search` hay `browser scan`.
- Search yêu cầu query do operator cung cấp (1–120 ký tự), tối đa 5 batch, 100 candidate đầu vào và 20 kết quả; excerpt tối đa 500 ký tự. Đọc hội thoại nhận đúng permalink, tối đa 100 candidate và trả tối đa 20 observation.
- Kết quả hội thoại chỉ ghi `parent_ref` khi parent xuất hiện và khớp rõ trong fixture. Thiếu parent hoặc ngữ cảnh chưa hoàn chỉnh được đánh dấu `PARTIAL`/`UNKNOWN`.
- Helper permalink chạy offline và hiện được CLI S3 dùng trong `manual-list`/`manual-review`; chỉ nhận URL `https://www.threads.com` hoặc path tương đối đúng dạng post, chuẩn hóa query tracking trong allowlist, đánh dấu `source=MANUAL_URL` và context `UNKNOWN`.
- S2 chỉ synthetic-tested; **chưa live-verified**. Không dùng adapter fixture để suy ra Threads thật cho phép hoặc hỗ trợ tự động thu thập dữ liệu.

### Trạng thái Micro-slice 3 — offline manual review

- Có lệnh `threads-local recruitment manual-review` để đọc tối đa 20 permalink operator nhập trong file local, chuẩn hóa bằng helper S2, bỏ trùng theo URL canonical và hiển thị shortlist theo thứ tự nhập.
- Lệnh riêng `threads-local recruitment manual-list` hiển thị shortlist canonical đánh số từ 1 và ánh xạ URL trùng trên `stderr` của terminal riêng tư. Có thể xem danh sách trước khi tạo draft hoặc chọn mục. Dedup giữ nguyên hoa/thường của post ID; khác biệt username và query tracking được chuẩn hóa theo S2.
- Operator phải chỉ định `--select` (đánh số từ 1) trong `manual-review`. Author chỉ được suy ra từ permalink và luôn là **chưa xác minh**; URL không tạo nội dung bài, điểm liên quan, remote ID hay trạng thái API/Nurture.
- Draft do operator tự viết/chỉnh sửa trong file UTF-8, tối đa 500 ký tự; không có AI. Context tùy chọn cũng do operator cung cấp và luôn có mức đầy đủ `UNKNOWN`. Review hiển thị hash SHA-256 và `NOT_SENT`.
- CLI S3 chỉ chạy trong terminal riêng tư tương tác; từ chối CI và luồng có stdout/stderr/stdin chuyển hướng. Shortlist, draft, context và URL chỉ được in ở `stderr` terminal sau khi kiểm tra TTY. `manual-list`/`manual-review` không gọi API/browser và không ghi queue, receipt, journal hay Nurture state.
- S3 synthetic-tested; **chưa live-verified**. `manual-list` và `manual-review` offline không đồng nghĩa với quyền lấy dữ liệu Threads. S1 browser handoff là thao tác network riêng, chỉ dùng sau phê duyệt live riêng theo #18/#46 và operator phải tự xác minh actor/target.

### Trạng thái Micro-slice 4–5 — synthetic-only

- `manual-handoff` recheck account, selection, canonical target và SHA của draft S3; yêu cầu `--confirm-target`, `--interactive`, rồi literal `OPEN`. Sau `OPEN`, journal ghi `HANDOFF_UNRESOLVED` bền vững **trước khi** browser khởi động. Persistence lỗi hoặc lock bận thì browser không mở.
- Handoff chỉ điều hướng đến một target đã duyệt. Không mở composer, tự điền draft hoặc nhấn Send. Browser đóng bình thường, crash, Ctrl+C hay EOF không chứng minh đã/ chưa gửi. `UNKNOWN_IDENTITY` luôn được hiển thị vì phần mềm không xác thực actor đang đăng nhập.
- `manual-status` chỉ đọc journal local. `manual-record` yêu cầu attempt ID đang pending, SHA không đổi và nhập đúng `RECORD`; các outcome `ABORTED_NOT_SENT`, `AMBIGUOUS`, `OPERATOR_REPORTED_SENT_UNVERIFIED` đều mang `UNVERIFIED`. Chỉ `ABORTED_NOT_SENT` mới cho phép handoff sau; hai outcome còn lại chặn thử lại.
- S5 thêm integrated CLI synthetic test chạy với fake browser engine và temporary root; wheel smoke kiểm tra import từ site-packages ngoài checkout. **Không có live Threads login, browser, API, gửi reply hay dữ liệu khách hàng trong test.** Kết quả synthetic không phải live UAT hoặc xác nhận quyền nền tảng.

### Trạng thái Micro-slice 6 — private opportunity queue, offline-only

- `opportunity-import ALIAS --targets-file PATH` lưu 1–20 permalink operator chọn vào queue JSON v1 riêng theo account dưới `THREADS_LOCAL_DATA_ROOT`. Có thể thêm một file context chung cho batch bằng `--context-file`; `--context-completeness UNKNOWN|PARTIAL|COMPLETE` là xác nhận của operator, không phải kết luận tự động. Không có file URL hợp lệ (0 dòng), quá 20 dòng, URL sai host/path, UTF-8 sai hoặc file quá lớn sẽ bị từ chối.
- URL canonical dùng S2 `normalize_manual_permalink`; tracking query trong allowlist được loại bỏ. Import trùng trong file hoặc đã có trong queue không tạo item thứ hai. Tối đa 20 item active/account; queue giữ tối đa 40 item gồm cả `SKIPPED`, nên hãy xóa item cũ khi cần thêm chỗ.
- `opportunity-list ALIAS` hiển thị ID, state, canonical target, context provenance/completeness và nội dung context trên private TTY. `opportunity-select ALIAS --id ID` chọn một item chính xác; có thể thay context bằng `--context-file` và xác nhận lại completeness. Thêm `--draft-file PATH --confirm-target URL` để chạy cùng S3 review, in preview/hash ở private TTY. Draft vẫn do operator quản lý trong file và không được lưu vào queue.
- State ban đầu là `NEEDS_CONTEXT`; state `READY` chỉ khi operator cung cấp context và chọn completeness `COMPLETE`. `opportunity-skip ALIAS --id ID [--reason-file PATH]` chuyển sang `SKIPPED` và giải phóng active capacity. `opportunity-remove ALIAS --id ID` yêu cầu gõ `DELETE`; lệnh không xóa S4 journal và dừng nếu target còn `HANDOFF_UNRESOLVED`.
- Queue file được kiểm tra schema/path, ghi atomic và khóa theo account. Tất cả queue commands yêu cầu private interactive TTY, chỉ in dữ liệu riêng tư ở `stderr`, không gọi network/API, không khởi động browser/Playwright và không gửi reply. URL không chứng minh tác giả; identity vẫn `UNKNOWN_IDENTITY`/`UNVERIFIED`.

## 2. Yêu cầu máy tính

- Windows, PowerShell; Python **3.14**, [uv](https://docs.astral.sh/uv/), Git.
- Chromium được cài theo phiên bản Playwright của dự án. Không dùng profile Chrome cá nhân hoặc copy cookie từ máy khác.
- Máy cần đủ dung lượng cho các browser profile tách biệt; mỗi profile có cookies riêng.
- **Không chạy bước tạo root, đăng nhập, đọc API hay thao tác trên account thật trước khi operator phê duyệt riêng phạm vi live UAT** theo issue #18/#46.
- Repository công khai; **không đặt token, cookie, tài khoản thật, dữ liệu khách hàng, screenshots riêng tư hoặc đường dẫn dữ liệu thật vào repo/issue/CI**.

## 3. Cài đặt phần mềm (môi trường thử nghiệm được phê duyệt)

Chỉ chạy trong **checkout dùng cho phát triển hoặc môi trường test riêng đã được cho phép**, không đè lên root cũ:

```powershell
git clone https://github.com/pumni/threads-standalone.git
cd threads-standalone

uv sync --locked
uv run --locked playwright install chromium
uv run --locked threads-local --help
```

Cài Playwright Chromium có thể download browser và ghi vào cache của máy. Các lệnh trên không tự đăng nhập Threads. Nếu dùng mã nguồn chưa merge của Codex, hãy kiểm tra đúng branch, CI và nguồn wheel trước khi triển khai máy khách.

**Không dùng lại root/profile từ sản phẩm cũ.** Root mới phải riêng tư, nằm ngoài Git repo, đã được chọn và duyệt. Ví dụ bên dưới chỉ là **placeholder**, không phải đường dẫn cần chạy nguyên văn:

```powershell
# Thay <PRIVATE_APPROVED_ROOT> bằng vị trí riêng tư đã được phê duyệt.
$env:THREADS_LOCAL_DATA_ROOT = "C:\\<PRIVATE_APPROVED_ROOT>"
```

Không paste giá trị root thật hay thông tin nhận dạng vào GitHub/ChatGPT/Codex.

## 4. Quản lý hai tài khoản

**Chỉ sau GO riêng cho việc tạo local state.** Alias là tên cục bộ, không nhất thiết là username trên Threads:

```powershell
uv run --locked threads-local account add test-a
uv run --locked threads-local account add test-b
uv run --locked threads-local account list
```

Phần mềm cấp UUID riêng cho mỗi alias và sử dụng:

- `<data-root>/accounts/`: metadata account (không lưu mật khẩu).
- `<data-root>/profiles/<uuid>/`: Chromium profile/cookies riêng.
- `<data-root>/locks/<uuid>.lock`: khóa tránh hai tác vụ dùng cùng một account.
- Nurture receipts/journal/insights, nếu chạy những chức năng được phép, cũng nằm ở root riêng.

Không đưa cả hai alias vào một profile, không mở hai browser cùng một profile đồng thời; khi thấy `ACCOUNT_BUSY`, đóng/hoàn tất phiên đang hoạt động thay vì xóa file lock thủ công.

## 5. Đăng nhập Threads thủ công bằng Chromium

**Chỉ sau GO riêng cho browser login.** Lệnh hiện có:

```powershell
uv run --locked threads-local account login test-a
```

1. Chromium được mở bằng profile gắn với `test-a`.
2. Trong Chromium, người dùng tự mở `https://www.threads.com`, nhập thông tin đăng nhập trực tiếp vào trang chính thức, hoàn thành xác minh/challenge nếu xuất hiện.
3. Quay về PowerShell và nhấn Enter để kết thúc phiên; trình duyệt được đóng, dữ liệu session nằm trong profile cục bộ.
4. Lặp lại riêng với `test-b`. **Phải tự xác nhận đúng account đang đăng nhập**. Một profile lưu cookie không chứng minh identity đã được kiểm tra.

Không nhập mật khẩu/token ở CLI của ứng dụng, không export cookie, không chuyển profile giữa máy hoặc alias.

## 6. Các thao tác browser đã tồn tại

**Chỉ sau GO cho phép đọc live qua browser**:

```powershell
uv run --locked threads-local feed test-a --limit 10
uv run --locked threads-local profile test-a synthetic_user
uv run --locked threads-local thread test-a "/@synthetic_user/post/SYNTHETIC_POST_ID"
uv run --locked threads-local browser open-thread test-a "/@synthetic_user/post/SYNTHETIC_POST_ID" --interactive
```

- `feed` lấy danh sách permalink hạn chế, **không phải nội dung bài viết đầy đủ**. Giới hạn tối đa hiện tại là 20.
- `profile` và `thread` xác minh cấu trúc target ở mức giới hạn, **không đồng nghĩa xác minh danh tính account đang đăng nhập**.
- `account login`, `feed`, `profile`, và `thread` đóng Chromium sau khi hoàn tất. Lệnh handoff bên dưới giữ cửa sổ mở trong lúc operator review.
- `browser open-thread ... --interactive` giữ Chromium và account lock sau khi đã xác minh đúng permalink/author. Nhấn Enter trong terminal để kết thúc phiên; lệnh không điền nội dung hoặc gửi action. Terminal luôn báo `identity=UNKNOWN_IDENTITY`, vì profile path không chứng minh actor đang đăng nhập.
- Lệnh mới chỉ được synthetic-tested; chưa thực hiện live Threads UAT. Chỉ mở target live sau khi có phê duyệt riêng cho phạm vi đó.
- Lệnh `thread` không hỗ trợ toàn bộ URL tùy ý; ví dụ dùng dạng tham chiếu `/@username/post/id`.

Không chạy quét feed/search liên tục hoặc sử dụng browser tự thu thập dữ liệu khi chưa xác minh việc đó được Meta cho phép.

## 7. Làm việc với các token tester và recruitment API (tùy chọn)

Không suy ra rằng browser handoff thủ công được miễn khỏi điều khoản nền tảng hoặc review compliance. Phải xác minh riêng điều kiện áp dụng trước mọi browser network operation. Nếu đơn vị vận hành đã có token tester được cấp quyền hợp lệ, một số thao tác API có thể chạy theo đúng scope đã được xác minh; không suy diễn mọi endpoint đều khả dụng.

**Chỉ sau GO riêng cho binding token và API reads**, dùng tên biến môi trường (không ghi giá trị token vào lệnh hoặc Git):

```powershell
uv run --locked threads-local account credential set-env test-a THREADS_PLATFORM_THREADS_TOKEN_TEST_A
uv run --locked threads-local api quota test-a
uv run --locked threads-local api search test-a "CV" --mode keyword --type recent --limit 10
```

Token thật được thiết lập riêng trong môi trường bảo mật của máy operator, **không lấy từ cookie của browser**. Xác nhận token thuộc đúng account và có scope cần thiết trước mỗi loại API call. Nếu không có quyền thì bỏ qua nhánh API này.

Các lệnh tuyển dụng liên quan:

```powershell
# Chỉ kiểm tra dữ liệu local; không gọi API hoặc ghi receipt
uv run --locked python scripts/run_recruitment.py --account test-a --validate

# Chỉ chạy khi đã cho phép API reads; nội dung shortlist được xem tại terminal riêng tư
uv run --locked python scripts/run_recruitment.py --account test-a --shortlist

# Quan sát, ghi Nurture receipt/state: cần quyền ghi riêng
uv run --locked python scripts/run_recruitment.py --account test-a --observe
```

`--shortlist` là danh sách ứng viên tạm thời, không tự tạo draft hoặc gửi reply. `--observe` có ghi local state, dù không gửi Threads reply. Không dùng `--apply-reply-file`, `--apply-post-file`, `threads-local reply` hoặc `threads-local post` nếu không được cấp quyền cụ thể cho account, target và nội dung.

## 8. Quy trình browser-first MVP — sáu bước

### 1. Chuẩn bị root và account synthetic

Chỉ dùng root mới nằm ngoài checkout và account alias synthetic. Không dùng profile, cookie, account thật, dữ liệu khách hàng hoặc thư mục của sản phẩm cũ trong phát triển, kiểm thử hay UAT chưa được phê duyệt:

```powershell
$sampleId = [Guid]::NewGuid().ToString('N')
$sampleRoot = Join-Path $env:TEMP "threads-standalone-synthetic-$sampleId"
$env:THREADS_LOCAL_DATA_ROOT = Join-Path $sampleRoot 'data'
$inputRoot = Join-Path $sampleRoot 'inputs'
New-Item -ItemType Directory -Path $inputRoot | Out-Null
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$targetsFile = Join-Path $inputRoot 'targets.txt'
$draftFile = Join-Path $inputRoot 'draft.txt'
[System.IO.File]::WriteAllText($targetsFile, "https://www.threads.com/@synthetic/post/fake_01`nhttps://www.threads.com/@synthetic/post/fake_02?utm_source=synthetic`n", $utf8NoBom)
[System.IO.File]::WriteAllText($draftFile, 'Synthetic operator-authored draft.', $utf8NoBom)
uv run --locked threads-local account add synthetic
uv run --locked threads-local account list
```

Nếu alias đã tồn tại trong root synthetic, dùng root temporary khác hoặc alias synthetic mới; không xóa journal/profile để làm sạch một attempt chưa resolve.

### 2. Nhập permalink và xem shortlist trước khi chọn

Operator tạo file UTF-8 riêng tư, mỗi dòng một permalink. Ví dụ bên dưới là chuỗi synthetic để dùng trong lệnh offline, không phải post thật:

PowerShell block ở bước 1 đã tạo file synthetic `targets.txt` dưới `%TEMP%`, không nằm trong repo. Cấu trúc nội dung:

```text
https://www.threads.com/@synthetic/post/fake_01
https://www.threads.com/@synthetic/post/fake_02?utm_source=synthetic
```

File nhận 1–20 URL không rỗng, tối đa 64 KiB tổng, tối đa 2048 byte mỗi URL, UTF-8 không BOM và URL đúng host/path. Query tracking được allowlist loại khi canonicalize. Post ID giữ nguyên phân biệt hoa/thường. URL trùng được gộp theo thứ tự đầu tiên; dữ liệu sai hoặc mơ hồ làm lệnh dừng.

Trong PowerShell riêng tư, không pipe/redirect stdin, stdout, stderr và không chạy trong CI:

```powershell
uv run --locked threads-local recruitment manual-list synthetic `
  --targets-file $targetsFile
```

Lệnh chỉ đọc file và local account, không gọi browser/API, không ghi state. Shortlist canonical một-based và mapping dòng trùng in lên `stderr` terminal riêng tư. Operator chọn mục từ danh sách vừa xem.

### 3. Tự viết draft và duyệt đúng target

Draft trong file UTF-8 do operator tự viết/chỉnh sửa, tối đa 500 ký tự. Context tùy chọn do operator cung cấp, tối đa 4000 ký tự/16 KiB; app không lấy context từ Threads. Không đặt target file, draft, context trong repository, GitHub, CI hay thư mục chia sẻ.

```powershell
uv run --locked threads-local recruitment manual-review synthetic `
  --targets-file $targetsFile `
  --select 2 `
  --confirm-target "https://www.threads.com/@synthetic/post/fake_02" `
  --draft-file $draftFile
```

Bỏ `--context-file` nếu không có context. `--select` bắt buộc là số một-based trong shortlist; `--confirm-target` phải khớp chính xác permalink canonical ở mục đó. Output riêng tư gồm full draft, canonical target, author chỉ suy ra từ permalink (chưa xác minh), actor `UNKNOWN_IDENTITY`, context `OPERATOR_SUPPLIED`/`NONE` với completeness `UNKNOWN`, trạng thái `NOT_SENT` và SHA-256.

SHA này gắn với account UUID + canonical target URL + draft cuối. Context không thuộc hash, và software không xác thực actor nên identity cũng không thuộc hash. Thay account, target hoặc draft yêu cầu review lại. `manual-list`/`manual-review` chỉ chạy offline: không search, mở browser, gọi API, gửi reply hoặc ghi queue/draft/state.

#### S6: Lưu shortlist và tiếp tục review sau khi chạy lại CLI

S6 thêm queue cục bộ riêng theo account. Dùng file/alias synthetic hiện có trong ví dụ và một private PowerShell terminal:

```powershell
$contextFile = Join-Path $inputRoot 'context.txt'
[System.IO.File]::WriteAllText($contextFile, 'Synthetic operator-observed context.', $utf8NoBom)
uv run --locked threads-local recruitment opportunity-import synthetic `
  --targets-file $targetsFile `
  --context-file $contextFile
uv run --locked threads-local recruitment opportunity-list synthetic
$opportunityId = Read-Host 'Nhập ID chính xác từ opportunity-list'
uv run --locked threads-local recruitment opportunity-select synthetic `
  --id $opportunityId `
  --confirm-target 'https://www.threads.com/@synthetic/post/fake_02' `
  --context-file $contextFile `
  --context-completeness COMPLETE `
  --draft-file $draftFile
```

Import và list canonicalize/deduplicate offline, không tìm post. Khi list, chọn đúng ID; `--confirm-target` phải đúng URL canonical đang duyệt. Context là dữ liệu do operator quan sát/nhập. `COMPLETE` chỉ là operator assertion; app không kiểm tra độ đầy đủ hay danh tính tác giả. Draft chỉ đọc để tạo S3 review, không được lưu trong queue. SHA vẫn bind account UUID + exact canonical target + draft; context không nằm trong SHA như semantics S3 hiện có.

Queue tối đa 20 item active/account và giữ tối đa 40 item tổng cộng. `opportunity-skip` đánh dấu `SKIPPED`; `opportunity-remove` yêu cầu gõ `DELETE`, giữ nguyên mọi journal S4, và không xóa target khi outcome còn `HANDOFF_UNRESOLVED`. Mọi queue command cần TTY riêng tư, không pipe/redirect. S6 không gọi API, mở browser, khởi động Playwright hoặc tạo remote effect. Nếu sau này có GO riêng cho S4, vẫn dùng lệnh S4 hiện có với URL exact và SHA vừa review; queue select tự nó không phải handoff.

### 4. Yêu cầu exact browser handoff sau phê duyệt riêng

> **Không chạy lệnh này trong test, sample dry run, CI hoặc checkpoint synthetic.** Lệnh mở headed Chromium và điều hướng qua mạng tới target thật nếu operator đưa URL thật. Chỉ dùng sau phê duyệt live riêng cho account, target và nội dung, và chỉ khi hoạt động đó được nền tảng cho phép.

Không chạy command handoff với target synthetic trong ví dụ, test hoặc sample dry run. Khi có phê duyệt live riêng, operator tự tạo command từ private files và dùng `threads-local recruitment manual-handoff ALIAS` cùng `--targets-file`, `--select`, `--confirm-target` (exact canonical permalink), `--draft-file`, `--approve-sha` (đúng SHA đã review), và `--interactive`. Chỉ literal `OPEN` rồi Enter mới tiếp tục. Lệnh recheck alias/account UUID, selection, canonical URL, full draft và SHA. Sau `OPEN`, trước khi khởi động Chromium, journal riêng ghi bền vững `HANDOFF_UNRESOLVED` và in attempt ID. Nếu journal lỗi, corrupt, lock bận hoặc approval thay đổi thì browser không mở. Journal không lưu raw URL/draft/context.

### 5. Operator tự xác minh account và target trong browser

Browser mở đúng thread qua S1 và kiểm tra target/author theo locator hiện có. Phần mềm không thể khẳng định account nào đang đăng nhập; màn hình luôn ghi `UNKNOWN_IDENTITY`. Operator phải tự kiểm tra profile/account và exact target. Nếu và chỉ nếu có quyền riêng, operator tự thao tác bằng native UI chính thức. Không có auto-fill, composer automation hoặc click Send.

Đóng browser bình thường, crash, Ctrl+C, mất mạng hoặc không có attestation **không xác định** đã gửi hay chưa. Attempt vẫn pending; không chạy lại handoff để thử. `HANDOFF_CLOSED_ACTION_UNVERIFIED` chỉ nói browser đã đóng.

### 6. Xem status và resolve đúng attempt bằng `RECORD`

Status chỉ đọc journal local, không kiểm tra Threads/API và không phải receipt:

```powershell
uv run --locked threads-local recruitment manual-status synthetic `
  --targets-file $targetsFile `
  --select 2 `
  --confirm-target "https://www.threads.com/@synthetic/post/fake_02"
```

Nếu thấy `HANDOFF_UNRESOLVED`, lấy attempt ID hiện hành và SHA vẫn khớp, rồi record khai báo đúng sự thật:

```powershell
$attemptId = Read-Host "Nhập attempt_id hiện đang pending từ manual-status"
$approvalSha = Read-Host "Nhập SHA-256 đã duyệt"
uv run --locked threads-local recruitment manual-record synthetic `
  --targets-file $targetsFile `
  --select 2 `
  --confirm-target "https://www.threads.com/@synthetic/post/fake_02" `
  --draft-file $draftFile `
  --approve-sha $approvalSha `
  --attempt-id $attemptId `
  --outcome ABORTED_NOT_SENT
```

`manual-record` chỉ ghi sau exact `RECORD`, attempt ID hiện tại, account/target chính xác và cùng SHA. Outcome chọn được là `ABORTED_NOT_SENT`, `AMBIGUOUS`, `OPERATOR_REPORTED_SENT_UNVERIFIED`; tất cả luôn `UNVERIFIED`, không phải official receipt/API result. Chỉ khai `ABORTED_NOT_SENT` khi biết chắc chưa gửi; đây là outcome duy nhất cho phép handoff sau. Nếu có thể đã gửi nhưng không biết, chọn `AMBIGUOUS`; nếu operator tự khai đã gửi, chọn `OPERATOR_REPORTED_SENT_UNVERIFIED`. Hai trạng thái này chặn mọi handoff sau. Attempt cũ không resolve được attempt mới. Browser close không bao giờ tự tạo kết quả gửi.

### API-driven Nurture

`scripts/run_recruitment.py --shortlist/--observe` và `threads-local nurture` là luồng riêng dựa trên Threads API, với quyền/token và semantics state riêng. Không dùng chúng như thể là reader của browser S2: search/conversation adapters ở S2 hiện chỉ dùng fixture synthetic. `--observe` có thể ghi Nurture state/receipt; `--apply` và các lệnh post/reply có thể tạo remote effect, cần quyền riêng theo account, target và nội dung.

## 9. Dừng, khôi phục và xử lý lỗi

| Tình huống | Cách xử lý |
| --- | --- |
| Sai account hoặc không chứng minh được identity | **STOP**, không soạn/gửi; mở lại đúng profile, người dùng tự kiểm tra |
| Mất session / challenge / CAPTCHA | **STOP**; operator tự giải quyết qua giao diện chính thức |
| Giao diện thay đổi / không tìm được target | **STOP**; không dùng locator đoán, không chọn kết quả gần đúng |
| `NO_RECORD` | Chỉ có nghĩa journal local chưa có bản ghi cho account/target đó; không chứng minh điều gì đã/ chưa xảy ra online |
| `HANDOFF_UNRESOLVED` | **STOP**, không handoff lại. Chỉ resolve đúng attempt hiện hành với cùng SHA và literal `RECORD` |
| Browser đóng / crash / Ctrl+C / EOF / mất mạng sau `OPEN` | Không suy luận gửi hay chưa gửi. Giữ journal; đọc `manual-status`, không retry; chỉ khai outcome operator biết chắc |
| `AMBIGUOUS` hoặc `OPERATOR_REPORTED_SENT_UNVERIFIED` | **STOP**, không thử gửi/handoff lại; hai outcome này chặn attempt mới |
| `MANUAL_OUTCOME_INVALID` hoặc journal corrupt/unreadable | **STOP**; giữ nguyên toàn bộ root, không xóa hoặc sửa tay journal |
| `MANUAL_OUTCOME_BUSY` / `ACCOUNT_BUSY` | Chờ foreground process hiện tại; không xóa lock file và không mở phiên song song |
| Attempt ID cũ / `MANUAL_OUTCOME_ATTEMPT_MISMATCH` | Đọc status riêng tư, chỉ dùng attempt đang pending; attempt `ABORTED_NOT_SENT` cũ không resolve attempt mới |
| Target khác bản preview | Hủy approval, đọc lại nội dung và duyệt lại |
| Không biết reply đã gửi thành công hay chưa | Đánh dấu **AMBIGUOUS/UNVERIFIED**, kiểm tra thủ công, không gửi lặp |
| Nghi lộ token/cookie | Dừng thao tác, thu hồi session/token theo quy trình chính thức và thông báo quản trị |

Không xóa outcome journal hoặc account profile trong bước cleanup. Backup root phải được bảo vệ như dữ liệu riêng tư vì browser profile có thể chứa cookies/session. Khi rollback code, dừng process và giữ nguyên data root; nếu version cũ không được xác nhận tương thích với namespace journal thì dừng để review, không xóa/hạ journal để ép CLI chạy tiếp. Xem [release readiness runbook](docs/BROWSER_MVP_RELEASE_READINESS.md) để biết quy trình evidence và release gates.

## 10. Kiểm thử trước khi bàn giao cho khách hàng

**Trong môi trường synthetic, tuyệt đối không dùng token/account/profile thật**:

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
uv build
```

CI wheel smoke phải chạy ngoài checkout, không `PYTHONPATH`, với wheel artifact `0.1.0` và integrated flow trên fake browser. Cũng phải PASS hosted Windows/Ubuntu QA và full-history Gitleaks trước khi chấp nhận merge. S5 test **không** cài Chromium hoặc khởi chạy real browser.

S5 local result trên Windows/Python 3.14.7: locked Ruff, format, Pyright, full synthetic pytest, build và installed-wheel smoke **PASS**; source suite `1266 passed, 6 skipped`, wheel suite `1260 passed, 12 skipped`. Skip gồm kiểm tra symlink bị hạn chế quyền Windows và test chỉ có trong source checkout; xem `pytest` summary/runbook để đối chiếu. Hosted Windows/Ubuntu và full-history Gitleaks chỉ PASS khi status trên đúng Draft PR head xác nhận. Live Threads UAT là **NOT RUN**. Local-root/security approval, account owner/actor/target proof, terms/compliance permission, Meta App Review nếu dùng gated API capability, provenance/license và issues #18/#46 vẫn **PENDING**. Không gắn nhãn production-ready trước khi các gate tương ứng được giải quyết.

## 11. Trạng thái App Review và đường nâng cấp

**Hiện tại:** S1–S6 chỉ đạt synthetic/local readiness, chưa production-ready. Tài liệu này không cấp quyền tự động thu thập dữ liệu hoặc điều khiển website. Cần xác minh riêng điều khoản nền tảng và phê duyệt compliance trước automated browser reads/actions; official API capability cần App Review/permission tương ứng. Live UAT và issues #18/#46 vẫn là gate riêng. Lịch sử chuyển mã nguồn và quyết định license công khai cũng chưa được giải quyết: xem [PROVENANCE_DECISION.md](docs/PROVENANCE_DECISION.md); không suy ra quyền tái sử dụng từ việc repository public.

Tài liệu tham khảo: [Meta Terms](https://www.facebook.com/terms/) · [Playwright persistent context](https://playwright.dev/python/docs/api/class-browsertype) · [Playwright locators](https://playwright.dev/python/docs/locators) · [README dự án](README.md).

---

**Lưu ý cho khách hàng:** Không nên đánh giá sản phẩm là “auto-reply” trước khi quy trình gửi thực sự được triển khai, kiểm thử và cho phép sử dụng. Trong phiên bản hiện tại, các thao tác xuất bản và trả lời có thể thực hiện qua official API với quyền hợp lệ, nhưng không có chức năng gửi bằng browser.
