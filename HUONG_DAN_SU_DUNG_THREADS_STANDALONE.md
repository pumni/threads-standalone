# HƯỚNG DẪN SỬ DỤNG THREADS STANDALONE — TIẾNG VIỆT

> **Phiên bản tài liệu:** 2026-10-12. **Đối tượng:** người vận hành quản lý tài khoản Threads được sở hữu/ủy quyền hợp lệ trên Windows.
>
> **Đọc trước:** Tài liệu mô tả đúng các chức năng đã có trong mã nguồn và lộ trình phát triển. Tính năng **tự động tìm kiếm Threads qua browser, đọc hội thoại, tự điền/gửi reply chưa có trong phiên bản hiện tại**. Không sử dụng tài liệu này như lời xác nhận đã vượt qua Meta App Review hoặc đã được Meta cho phép tự động hóa UI.
>
> Kế hoạch Codex: [GitHub issue #47](https://github.com/pumni/threads-standalone/issues/47). Giới hạn triển khai/live: [issue #18](https://github.com/pumni/threads-standalone/issues/18) và [#46](https://github.com/pumni/threads-standalone/issues/46).

## 1. Threads Standalone dùng để làm gì?

Threads Standalone là công cụ chạy **cục bộ trên máy tính**, giúp operator quản lý account, mở Threads trong Chromium riêng, xem một phần thông tin qua giao diện browser, dùng API chính thức **khi đã có quyền**, lập shortlist tuyển dụng, chuẩn bị nội dung, review câu trả lời và quản lý trạng thái xử lý.

**Nguyên tắc:** Mỗi tài khoản phải thuộc sở hữu khách hàng hoặc được ủy quyền hợp pháp; người dùng chịu trách nhiệm kiểm tra nội dung và đích gửi. Không tạo tương tác giả, spam hoặc dùng phần mềm vượt challenge/CAPTCHA. Các hoạt động đọc/ghi tự động trên website chỉ được kích hoạt khi nền tảng cho phép hành vi tương ứng.

### Trạng thái chức năng sau Micro-slice 1

| Chức năng | Trạng thái |
| --- | --- |
| Tạo local account, đặt bí danh, tách Chromium profile theo UUID | **Đã có mã nguồn; synthetic-tested** |
| Mở Chromium để người dùng tự đăng nhập | **Đã có mã nguồn; chưa live-verified trong repo mới** |
| Các lệnh `profile`, `thread`, `feed` (tối đa 20 permalink) | **Đã có mã nguồn; synthetic-tested**, mỗi lệnh đóng browser sau khi xong |
| Mở một thread chính xác và giữ Chromium cho operator review | **Có ở Micro-slice 1; synthetic-tested; chưa live-verified**; actor identity luôn là `UNKNOWN_IDENTITY` |
| Tìm kiếm bài qua browser và đọc hội thoại trên Threads thật | **Chưa được bật**; UI selector chưa xác minh, chỉ có adapter/fixture synthetic ở S2 |
| Chuẩn hóa permalink do người vận hành nhập | **Có helper offline ở S2**, chưa nối vào CLI hoặc hàng đợi tuyển dụng |
| Tìm kiếm/mentions/replies thông qua official Threads API | **Đã có mã nguồn**, phụ thuộc quyền token và endpoint |
| Nurture discovery, ranking, dedupe, cooldown | **Đã có mã nguồn**, hiện sử dụng **API** thay vì browser |
| Review, áp dụng trả lời/đăng bài qua API | **Đã có mã nguồn**, có thể phát sinh tác động thật; **không sử dụng khi chưa được duyệt** |
| Browser điền/gửi reply | **Chưa có**; hành vi tự động cần kiểm tra quyền Meta riêng |
| Customer demo launcher | **Chỉ synthetic, không đăng nhập và không gửi thật** |

### Trạng thái Micro-slice 2 — synthetic-only

- Adapter tìm kiếm và đọc hội thoại chỉ nhận dữ liệu fixture/fake engine. Chưa có selector Threads thật và không có lệnh `browser search` hay `browser scan`.
- Search yêu cầu query do operator cung cấp (1–120 ký tự), tối đa 5 batch, 100 candidate đầu vào và 20 kết quả; excerpt tối đa 500 ký tự. Đọc hội thoại nhận đúng permalink, tối đa 100 candidate và trả tối đa 20 observation.
- Kết quả hội thoại chỉ ghi `parent_ref` khi parent xuất hiện và khớp rõ trong fixture. Thiếu parent hoặc ngữ cảnh chưa hoàn chỉnh được đánh dấu `PARTIAL`/`UNKNOWN`.
- Helper permalink hoạt động offline, chỉ nhận URL `https://www.threads.com` hoặc path tương đối có dạng post hợp lệ; query tracking nằm trong allowlist được loại khỏi kết quả. Helper đánh dấu `source=MANUAL_URL`, `context_status=UNKNOWN` và không ghi hàng đợi/state.
- S2 chỉ synthetic-tested; **chưa live-verified**. Không dùng adapter fixture để suy ra Threads thật cho phép hoặc hỗ trợ tự động thu thập dữ liệu.

### Trạng thái Micro-slice 3 — offline manual review

- Có lệnh `threads-local recruitment manual-review` để đọc tối đa 20 permalink operator nhập trong file local, chuẩn hóa bằng helper S2, bỏ trùng theo URL canonical và hiển thị shortlist theo thứ tự nhập.
- Lệnh riêng `threads-local recruitment manual-list` hiển thị shortlist canonical đánh số từ 1 và ánh xạ URL trùng trên `stderr` của terminal riêng tư. Có thể xem danh sách trước khi tạo draft hoặc chọn mục. Dedup giữ nguyên hoa/thường của post ID; khác biệt username và query tracking được chuẩn hóa theo S2.
- Operator phải chỉ định `--select` (đánh số từ 1) trong `manual-review`. Author chỉ được suy ra từ permalink và luôn là **chưa xác minh**; URL không tạo nội dung bài, điểm liên quan, remote ID hay trạng thái API/Nurture.
- Draft do operator tự viết/chỉnh sửa trong file UTF-8, tối đa 500 ký tự; không có AI. Context tùy chọn cũng do operator cung cấp và luôn có mức đầy đủ `UNKNOWN`. Review hiển thị hash SHA-256 và `NOT_SENT`.
- CLI chỉ chạy trong terminal riêng tư tương tác; từ chối CI và luồng có stdout/stderr/stdin chuyển hướng. Shortlist, draft, context và URL chỉ được in ở `stderr` terminal sau khi kiểm tra TTY. Lệnh không gọi API/browser và không ghi queue, receipt, journal hay Nurture state.
- S3 synthetic-tested; **chưa live-verified**. S1 browser handoff là một lệnh riêng, chỉ dùng sau phê duyệt live riêng theo #18/#46 và operator phải tự xác minh actor/target.

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

**Không bắt buộc có App Review để sử dụng các chức năng browser thủ công.** Nếu đơn vị vận hành đã có token tester được cấp quyền hợp lệ, một số thao tác API có thể chạy theo đúng scope đã được xác minh; không suy diễn mọi endpoint đều khả dụng.

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

## 8. Quy trình operator cho các chức năng đã có

### Review permalink và draft offline (Micro-slice 3)

1. Chọn alias của account local đã tạo trước đó. Lệnh chỉ đọc alias/UUID; account không được chia sẻ giữa các alias.
2. Dùng nguồn permalink mà operator được phép sử dụng. Tạo file UTF-8 riêng tư, mỗi dòng một URL; có thể có dòng trống. Không đặt file này, draft hoặc context vào GitHub/repository.

   Ví dụ synthetic cho `targets.txt`:

   ```text
   https://www.threads.com/@synthetic/post/fake_01
   https://www.threads.com/@synthetic/post/fake_02?utm_source=operator
   ```

   Có 1–20 dòng không trống, tối đa 64 KiB tổng file và tối đa 2048 byte/ký tự mỗi URL. Chỉ nhận UTF-8 không BOM và permalink đúng host `www.threads.com`; query tracking được S2 cho phép sẽ bị loại khi chuẩn hóa. URL trùng được gộp ổn định và lệnh in số dòng gốc ánh xạ tới mục shortlist còn lại. URL sai hoặc dữ liệu mơ hồ làm lệnh dừng.

3. Mở PowerShell tương tác riêng tư, không chuyển hướng stdin/stdout/stderr và không chạy trong CI. Xem shortlist trước; lệnh này không cần draft:

   ```powershell
   uv run --locked threads-local recruitment manual-list test-a `
     --targets-file .\targets.txt
   ```

   Lệnh in danh sách canonical một-based và ánh xạ các dòng trùng. Chọn đúng một số thứ tự từ danh sách này; không cần đoán vị trí trước khi thấy shortlist.

4. Tự viết/chỉnh sửa draft trong `draft.txt`; tối đa 500 ký tự. Context tùy chọn trong `context.txt` tối đa 4000 ký tự/16 KiB. Hai file phải là UTF-8 không BOM, không chứa control character hoặc ký tự vô hình nguy hiểm. Draft không được AI tạo; context chỉ là nội dung operator tự quan sát/cung cấp, không được app đọc từ Threads.
5. Chạy review trong terminal riêng tư. Dùng URL canonical đúng dòng shortlist cùng `--select`; `--confirm-target` tùy chọn nhưng được khuyến nghị để lệnh dừng nếu file target đổi sau khi xem danh sách:

   ```powershell
   uv run --locked threads-local recruitment manual-review test-a `
     --targets-file .\targets.txt `
     --select 2 `
     --confirm-target "https://www.threads.com/@synthetic/post/fake_02" `
     --draft-file .\draft.txt `
     --context-file .\context.txt
   ```

   Bỏ `--context-file` nếu chưa có context. `--select` là số thứ tự **một-based** trong shortlist vừa xem; bắt buộc chọn chính xác một mục, không có chọn mặc định. Khi có `--confirm-target`, giá trị phải khớp chính xác URL canonical của mục đã chọn.

6. Review chỉ in ra `stderr` của terminal private: alias và UUID, actor `UNKNOWN_IDENTITY`, URL canonical, author chỉ từ permalink (chưa xác minh), provenance `MANUAL_URL`, context `OPERATOR_SUPPLIED`/`NONE` với completeness `UNKNOWN`, toàn văn draft, hash SHA-256 và `NOT_SENT`. Thay account, target hoặc draft làm thay hash; chỉnh file rồi chạy lại để review phiên bản mới.
7. Lệnh offline này không mở Chromium, gọi API, đăng nhập, gửi reply, hoặc ghi queue/state/journal/receipt. `NOT_SENT` không phải là quyền gửi và không chứng minh danh tính actor.
8. **Chỉ sau khi có phê duyệt live riêng theo #18/#46**, operator có thể tự chọn lệnh S1 được in trong review: `threads-local browser open-thread test-a "/@synthetic/post/fake_02" --interactive`. Đây là lệnh browser riêng; operator phải tự xác minh account và target trong UI chính thức. S1 không tự điền hay gửi reply.

### API-driven Nurture

`scripts/run_recruitment.py --shortlist/--observe` và `threads-local nurture` là luồng riêng dựa trên Threads API, với quyền/token và semantics state riêng. Không dùng chúng như thể là reader của browser S2: search/conversation adapters ở S2 hiện chỉ dùng fixture synthetic. `--observe` có thể ghi Nurture state/receipt; `--apply` và các lệnh post/reply có thể tạo remote effect, cần quyền riêng theo account, target và nội dung.

## 9. Dừng, khôi phục và xử lý lỗi

| Tình huống | Cách xử lý |
| --- | --- |
| Sai account hoặc không chứng minh được identity | **STOP**, không soạn/gửi; mở lại đúng profile, người dùng tự kiểm tra |
| Mất session / challenge / CAPTCHA | **STOP**; operator tự giải quyết qua giao diện chính thức |
| Giao diện thay đổi / không tìm được target | **STOP**; không dùng locator đoán, không chọn kết quả gần đúng |
| Browser crash / mất mạng | Đóng tác vụ, kiểm tra trạng thái; **không tự gửi lại** |
| `ACCOUNT_BUSY` | Đợi hoặc kết thúc phiên hiện có; không xóa lock tùy tiện |
| Target khác bản preview | Hủy approval, đọc lại nội dung và duyệt lại |
| Không biết reply đã gửi thành công hay chưa | Đánh dấu **AMBIGUOUS/UNVERIFIED**, kiểm tra thủ công, không gửi lặp |
| Nghi lộ token/cookie | Dừng thao tác, thu hồi session/token theo quy trình chính thức và thông báo quản trị |

Có thể dùng `Ctrl+C` để dừng foreground command; việc dừng tiến trình sau một action remote không đủ chứng minh action đó chưa thực hiện.

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

Checklist bàn giao sau khi Codex hoàn tất: đúng version/commit; cài wheel và Chromium theo quy trình được duyệt; hai profile riêng; login manual; case sai account/target/challenge/timeout; một luồng từ permalink đến draft rồi mở đúng target; không gửi tự động; không log bí mật; operator có quyền dừng. Báo cáo rõ những bước chỉ synthetic pass và những bước live UAT đã được phê duyệt/kiểm chứng.

## 11. Trạng thái App Review và đường nâng cấp

**Hiện tại:** browser-guided operator workflow là đường sử dụng khi chưa hoàn tất App Review; các API reads chỉ dùng theo quyền tester thực tế đã được xác minh. **Tương lai:** khi Meta App Review được phê duyệt, dùng adapter official API cho các capability tương ứng. Không cần đổi mô hình local account, Nurture ranking/dedupe/cooldown hoặc quy trình human review.

Tài liệu tham khảo: [Meta Terms](https://www.facebook.com/terms/) · [Playwright persistent context](https://playwright.dev/python/docs/api/class-browsertype) · [Playwright locators](https://playwright.dev/python/docs/locators) · [README dự án](README.md).

---

**Lưu ý cho khách hàng:** Không nên đánh giá sản phẩm là “auto-reply” trước khi quy trình gửi thực sự được triển khai, kiểm thử và cho phép sử dụng. Trong phiên bản hiện tại, các thao tác xuất bản và trả lời có thể thực hiện qua official API với quyền hợp lệ, nhưng không có chức năng gửi bằng browser.
