# Báo cáo Phase 2 – phiên làm việc không giám sát (2026-10-09)

## Tóm tắt

- Toàn bộ phần **không cần token** đã xong và có test: catalog tổng hợp, chính sách, golden set (60 câu nháp), client chỉ-đọc, fixture đơn hàng.
- **Các script seed và report đã viết xong nhưng CHƯA chạy với Saleor.** Lý do: chưa có token. Ngoài ra **Saleor stack đang tắt** (7 container `Exited` từ khoảng 3 giờ trước phiên). Mình **không** khởi động lại stack.
- `uv run pytest`: **61 passed, 28 skipped**. 28 test bị skip là các test kiểm tra document GraphQL với SDL của Saleor; khi trỏ tới SDL thì **89/89 passed**.
- Không có lệnh phá hủy nào được chạy. Không ghi gì vào Saleor, không git add/commit, không sudo, không thêm dependency ngoài danh sách đã duyệt.

## Từng hạng mục

### Đã xong và đã kiểm chứng

| Hạng mục | Kết quả | Kiểm chứng |
|---|---|---|
| A. Môi trường Python (từ phiên trước) | Python 3.12.15 qua uv; runtime deps: httpx, pydantic, graphql-core; dev: pytest | `uv run python --version` |
| D1. Catalog | `data/seed/catalog.vi.json`: 250 sản phẩm, 6 danh mục, 25 sản phẩm tồn kho 0 (10%), giá 69.000đ–1.869.000đ (làm tròn 1.000đ), tồn kho ≤ 50 | `tests/test_catalog_determinism.py`: hai lần chạy (hai tiến trình với `PYTHONHASHSEED` khác nhau) cho ra file giống hệt từng byte, và giống file đã ghi; schema pydantic từ chối giá lẻ và size sai thứ tự |
| Thiết kế catalog | 1 sản phẩm = 1 SKU; màu sắc và chất liệu là dropdown một giá trị; **kích cỡ là MULTISELECT các size có sẵn**, không có tồn kho theo size (đã ghi trong README) | schema + test |
| E. Chính sách | `data/policies/doi-tra.md` (7 điều), `van-chuyen.md` (6 điều), `bao-hanh.md` (6 điều), có số ngày, phí và điều kiện cụ thể | `tests/test_policies.py`: các điều đánh số liên tục; **phí ship 30.000đ, freeship từ 500.000đ và thời gian giao khớp với `app/shop.py`**, cũng là nguồn mà `seed_channel.py` dùng để cấu hình |
| Fixture đơn hàng | `data/seed/orders.vi.json`: 30 đơn (10 FULFILLED, 9 UNFULFILLED, 4 PARTIALLY_FULFILLED, 4 CANCELED, 3 UNCONFIRMED), 20 khách `@example.com`, số đơn = `pending` | `tests/test_orders_fixture.py`: tất định; SKU và giá khớp catalog; trạng thái khớp với số lượng đã fulfill, theo đúng quy tắc của `orderBulkCreate` |
| F. Golden set | `eval/golden/golden.vi.json`: 60 câu, **tất cả `draft_needs_human_review`**. Gồm 22 product_search (2 câu cố ý không khớp sản phẩm nào, 1 câu hỏi sản phẩm hết hàng), 12 policy, 8 order_status, 6 create_ticket, 6 out_of_scope, 6 smalltalk. Kiểu viết: 31 có dấu, 16 không dấu, 13 teencode. Split: 41 dev, 19 test | `validate_golden` → **OK**. `tests/test_golden.py`: SKU đúng được tính lại từ catalog; validator bắt được nhãn bị lệch, SKU không tồn tại, điều khoản không tồn tại, sai trạng thái đơn; `--require-order-numbers` báo lỗi khi số đơn còn `pending` |
| Hash tập test | **Chưa ghi**, đúng theo yêu cầu. Giá trị hiện tại chỉ để tham khảo: `de1b714e…09943` | sẽ thay đổi sau khi review và sync số đơn |
| G. Client chỉ-đọc | `app/saleor/readonly_client.py`: allowlist 6 query (lớp chính), parse bằng graphql-core (lớp hai) | `tests/test_readonly_client.py` (12 test): query trong allowlist được gửi; **mutation, subscription, document nhiều operation, tên operation lạ, chuỗi GraphQL thô, query vô danh và biến sai đều bị chặn trước khi có request mạng nào** (fake transport ghi nhận 0 request) |
| Kiểm tra document GraphQL | 6 query runtime và 22 document của seeder đều hợp lệ với SDL của Saleor 3.23 | `tests/test_documents_against_schema.py` (cần `SALEOR_SCHEMA_SDL`) |

### Đã viết, chưa chạy với Saleor

| Script | Chức năng | Đã test bằng Saleor giả lập |
|---|---|---|
| `scripts/seed_channel.py` (C) | Channel `vn` (VND, VN), warehouse `shopmate-vn-hcm`, zone "ShopMate – Việt Nam", 2 phương thức giao hàng | chạy lần 2 không phát sinh mutation nào; `default-channel` không bị đổi; channel `vn` có sẵn nhưng sai tiền tệ thì báo lỗi, không sửa |
| `scripts/seed_catalog.py` (D) | Tạo attribute, product type, category và 250 sản phẩm theo khóa externalReference = SKU; có `--reset-vn` | chạy lần 2 tạo 0 sản phẩm và số sản phẩm trong `vn` giữ nguyên; reset chỉ xóa sản phẩm ShopMate, không đụng đơn hàng và dữ liệu demo; không xác nhận thì từ chối |
| `scripts/seed_orders.py` (H) | `orderBulkCreate` chỉ-thêm; đọc số đơn bằng client runtime chỉ-đọc rồi ghi vào `orders.vi.json` | chạy lần 2 tạo 0 đơn, 30 đơn đã tồn tại; đơn có sản phẩm đã bị xóa vẫn được tính là đã tồn tại; không bao giờ ghi đè số đơn đã ghi |
| `scripts/report_vn.py` (I) | Đếm theo channel `vn`; so drift catalog (tên, giá, tồn kho, thuộc tính, danh mục) và đơn hàng (theo externalReference: trạng thái, số đơn, dòng hàng) | không drift sau khi seed; phát hiện đổi tồn kho, đổi tên, mất sản phẩm, đổi trạng thái, mất đơn; sản phẩm bị xóa trong đơn chỉ là **cảnh báo** |
| `eval/golden/sync_order_numbers.py` | Điền số đơn vào golden set sau bước H, giữ nguyên chỉnh sửa của người review | test giữ `notes` và `status` do người sửa |

Không có Saleor thật nên chưa xác nhận được: đủ quyền hay chưa; địa chỉ VN có qua validation không; `orderBulkCreate` có chấp nhận đủ 5 trạng thái không; có xung đột giữa zone VN mới và các zone demo không; `productVariant(sku:)` có đọc được bằng token runtime không.

### Bị chặn

- **Không có hạng mục nào thất bại hai lần.**
- Toàn bộ phần chạy với Saleor (C, D, H, I, các số đếm, hash) bị chặn vì chưa có token. Ngoài ra **stack đang tắt**.

## Kết quả test

```
uv run pytest
61 passed, 28 skipped in 1.11s

SALEOR_SCHEMA_SDL=<bản copy schema.graphql> uv run pytest
89 passed in 2.02s
```

Phân bổ: catalog 5, fixture đơn 5, chính sách 8, golden 15, client chỉ-đọc 12, seeder và report 16, document so với SDL 28.

## Các quyết định (chi tiết trong `docs/DECISIONS-phase2.md`)

1. **D1** Stack đang tắt nên mình không khởi động lại. Mình `docker cp` SDL và source từ container đã dừng (chỉ-đọc) để kiểm tra query.
2. **D2** Không commit SDL của Saleor vì là code bên thứ ba. Test so với SDL chỉ chạy khi có biến `SALEOR_SCHEMA_SDL`.
3. **D3** Seeder chỉ có `MANAGE_ORDERS_IMPORT`. Idempotency dựa vào lỗi `UNIQUE`; số đơn đọc bằng token runtime.
4. **D4** `report_vn` đọc catalog bằng token seeder, vì đọc tồn kho chính xác cần `MANAGE_PRODUCTS`.
5. **D5** Tồn kho ≤ 50, vì `quantityAvailable` công khai bị giới hạn ở 50.
6. **D6** Allowlist runtime có 6 query. Lưu ý `userEmail` là dữ liệu cá nhân, không được để lộ ở tầng tool sau này.
7. **D7** Thêm `scripts/generate_orders.py` để sinh fixture đơn một cách tất định.
8. **D8** Chỉ dùng 5 trạng thái; bỏ RETURNED, EXPIRED, DRAFT.
9. **D9** `stockUpdatePolicy: SKIP`: đơn import không trừ tồn kho.
10. **D10** Dòng có lỗi `UNIQUE` được coi là "đã tồn tại", kể cả khi có lỗi khác.
11. **D11** Không bao giờ ghi đè số đơn đã ghi; nếu lệch thì báo xung đột.
12. **D12** Địa chỉ dùng tên tỉnh theo i18naddress; không có số điện thoại và mã bưu chính (tránh số thật của người khác).
13. **D13** Gửi `productSku` trên từng dòng đơn, để vẫn nhận ra SKU khi sản phẩm bị xóa.
14. **D14** 6 danh mục (thêm "Đồ thể thao"), SKU dạng `SM-<NAM|NU|GIAY|TUI|PK|TT>-NNNN`.
15. **D15** Đánh dấu sở hữu bằng externalReference hoặc slug `shopmate-`; không tái sử dụng object không phải của ShopMate.
16. **D16** Seeder chỉ tạo hoặc bỏ qua, không cập nhật. Thay đổi trong JSON được `report_vn` báo là drift.
17. **D17** `--reset-vn` chỉ xóa sản phẩm ShopMate trong `vn`, cần cụm xác nhận `xoa-san-pham-vn`, không đụng đơn hàng. **Chưa chạy.**
18. **D18** Hai phương thức giao hàng: tiêu chuẩn 30.000đ (đơn ≤ 499.999đ) và miễn phí (đơn từ 500.000đ).
19. **D19** Mô tả sản phẩm lưu dạng một đoạn EditorJS.
20. **D20** Bộ lọc golden có thêm `kind` (loại sản phẩm) và `keyword` (kiểu dáng).
21. **D21** Câu hỏi về đơn dùng placeholder `{order_number}`, có script sync và cờ strict.
22. **D22** Một file golden với trường `author`; script draft từ chối ghi đè.
23. **D23** Chia split theo từng intent bằng sha256(id): 41 dev / 19 test.
24. **D24** Chưa ghi hash tập test.
25. **D25** Có 2 câu cố ý không khớp và 1 câu hỏi sản phẩm hết hàng.
26. **D26** `.env.example` có 2 biến token mới. (Cập nhật sau: `check_saleor.py` nay đọc `SALEOR_RUNTIME_TOKEN`, tên biến cũ không còn được dùng; xem D30.)
27. **D27** Test dùng Saleor giả lập: chứng minh logic của script, không chứng minh hành vi hay quyền của Saleor.

## Việc bạn cần làm khi quay lại (theo thứ tự)

1. **Bật lại Saleor stack** (đang tắt):
   ```bash
   cd ~/shopmate-workspace/saleor-platform && docker compose up -d
   ```
   Chờ đến khi http://localhost:9000 mở được.
2. **Tạo 2 App trong Dashboard**: vào Extensions (hoặc Apps), Add extension, Provide details manually.
   - `shopmate-seeder`: `MANAGE_CHANNELS`, `MANAGE_PRODUCTS`, `MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES`, `MANAGE_SHIPPING`, `MANAGE_ORDERS_IMPORT`.
   - `shopmate-runtime`: chỉ `MANAGE_ORDERS`.
   - Với mỗi app, bấm Create token rồi copy.
3. **Dán token** vào `shopmate/.env` ở hai dòng `SALEOR_SEEDER_TOKEN=` và `SALEOR_RUNTIME_TOKEN=`. Đừng dán vào chat.
4. **Nhắn mình "xong"**. Mình sẽ chạy từng bước và kiểm chứng sau mỗi bước:
   ```bash
   cd ~/shopmate-workspace/shopmate
   uv run python -m scripts.seed_channel      # C
   uv run python -m scripts.seed_catalog      # D: kỳ vọng 250 created
   uv run python -m scripts.report_vn --catalog-only   # đếm lần 1 trong vn
   uv run python -m scripts.seed_catalog      # D lần 2: kỳ vọng 0 created, 250 already existed
   uv run python -m scripts.seed_orders       # H: 30 created, ghi số đơn
   uv run python -m scripts.seed_orders       # H lần 2: 0 created, 30 already existed
   uv run python -m scripts.report_vn         # I: kỳ vọng "products in channel vn: 250", "orders in channel vn: 30", OK
   uv run python -m eval.golden.sync_order_numbers
   uv run python -m eval.golden.validate_golden --require-order-numbers
   ```
   Nếu bước nào báo `PermissionDenied`, mình chỉ đề xuất thêm đúng quyền bị thiếu và hỏi bạn trước.
5. **Review golden set** `eval/golden/golden.vi.json`: sửa hoặc xóa từng câu, đổi `status` thành `human_reviewed` cho câu đã duyệt, và tự viết khoảng 40 câu mới (`author: "human"`, `status: "human_reviewed"`). Chạy `uv run python -m eval.golden.validate_golden` sau khi sửa.
6. **Đóng băng tập test**: sau khi review xong, chạy validator và ghi giá trị `test-split sha256` vào `docs/SETUP.md` (mục "Golden set").

## Những điểm dễ vỡ

- Seeder **không cập nhật** dữ liệu đã tồn tại. Sửa giá hoặc tồn kho trong JSON thì phải `--reset-vn` (có hỏi trước) rồi seed lại, hoặc chấp nhận drift.
- Đơn hàng **không xóa được** qua API. Nếu đơn bị seed sai thì chỉ hủy được; fixture phải được thêm mới bằng externalReference mới.
- Bộ quyền của hai app được suy ra từ source; chưa thử thật.
- Hash tập test thay đổi mỗi khi sửa câu trong split test, kể cả khi sync số đơn. Nên chỉ đóng băng sau cùng.
- Image Saleor dùng tag trôi `3.23`. Khi image đổi, schema có thể đổi; nên chạy lại test so với SDL.
- `catalog.vi.json` sinh lại từ code. Mọi thay đổi trong `generate_catalog.py` làm đổi file và làm test tất định fail; khi đó cần chủ động generate lại, kiểm tra drift, và có thể cần seed lại.
