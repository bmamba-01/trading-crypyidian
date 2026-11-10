# WP-03A Task 1A — Hợp đồng parser order offline

**Ngày truy cập nguồn:** 2026-11-02. **Phạm vi:** tài liệu công khai và dữ liệu dựng; không gọi endpoint Binance, không credential, không kiểm chứng Demo. Ghi chú này bổ sung field/status/nullability/event contract còn mở trong audit cùng ngày; không thay thế capability matrix.

## Nguồn chính thức đã mở lại

- [Trade REST](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade): New Order, Query Order, New Algo Order, Query Algo Order; kiểm tra tên/kiểu response và ví dụ tại từng mục.
- [User Data Streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams): danh sách status, định nghĩa vòng đời Algo và thời gian `E`/`T`.
- [orderTradeUpdate schema](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/~schemas#ordertradeupdate) và [algoUpdate schema](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/~schemas#algoupdate): đã mở giao diện trình duyệt, bấm **Toggle properties** của `o` ở từng schema và đọc nested fields trực tiếp. Bản text của web chỉ hiện `o: object`; không suy từ REST, COIN-M hoặc SDK. Download schema YAML trực tiếp bị CloudFront trả 202/WAF rỗng nên không dùng làm bằng chứng. Giao diện schema hiện đầy đủ các field được dùng dưới đây.
- [Change Log](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log): mục 2025-11-06, hiệu lực 2025-12-09, conditional migration và `-4120`.
- [General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info): millisecond timestamp, 503 ambiguity và caveat Testnet.

## Điều đã xác minh và quyết định nội bộ

Chỉ `MARKET`/`LIMIT` thuộc regular type trong lát cắt này. `STOP`, `STOP_MARKET`, `TAKE_PROFIT`, `TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET` thuộc conditional Algo. REST vẫn có enum/ví dụ cũ; lấy migration và mô tả New Algo Order làm quy tắc routing. `-4120` là lỗi hợp đồng routing; classifier trả `ROUTING_MISMATCH` + `SubmissionOutcome.UNKNOWN`, không retry và không cung cấp bằng chứng absent/release.

REST regular có `orderId` int64, `clientOrderId`, `symbol`, `status`, `type`, `executedQty` chuỗi và `updateTime` int64. `updateTime` xuất hiện trong Query Order với ý nghĩa thời gian cập nhật; không thay bằng giờ local khi thiếu. Danh sách regular status đã xác minh ở User Data Streams là `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `EXPIRED`, `EXPIRED_IN_MATCH`. REST status hiện khai báo chuỗi, không có danh sách response enum đầy đủ riêng; vì vậy parser chỉ chuẩn hóa sáu status đã chứng minh, giá trị khác kể cả `REJECTED` giữ raw và chiếu `UNKNOWN`. Đây là lựa chọn bảo thủ, không khẳng định Binance không bao giờ trả status khác.

REST Algo dùng `algoType=CONDITIONAL`, `orderType`, `algoId` int64, `clientAlgoId`, `algoStatus`, `triggerPrice` chuỗi. Query Algo Order ghi `actualOrderId` là **chuỗi**, rỗng khi chưa trigger. `actualType` chỉ có sau trigger, `actualQty` chỉ có khi fill/partial fill; cả hai optional. Algo status riêng gồm `NEW`, `TRIGGERING`, `TRIGGERED`, `FINISHED`, `CANCELED`, `REJECTED`, `EXPIRED`. `FINISHED` có thể là fill hoặc cancel trong matching engine; DTO Algo luôn chiếu sang regular state `UNKNOWN`, kể cả khi có `actualQty`. Phải query/reconcile regular order để xác nhận fill.

Trang response không đánh dấu đầy đủ các field là required. Parser áp dụng **required facts nội bộ** để từ chối payload không đủ nhận diện, không tuyên bố các required facts này là lời hứa field luôn hiện của Binance:

| Surface | Required facts nội bộ | Optional facts được giữ riêng |
| --- | --- | --- |
| Regular REST projection | `symbol`, `orderId`, `clientOrderId`, `type`, `status` | `executedQty`, `origQty`, `updateTime`; thiếu giữ `None`, JSON null không được tự coercion |
| Algo REST projection | `symbol`, `algoId`, `clientAlgoId`, `algoType`, `orderType`, `algoStatus`, `triggerPrice` | `actualOrderId`, `actualType`, `actualQty`, `updateTime`; ID thiếu/rỗng giữ `None`, explicit null không có chứng cứ nên từ chối |
| Order event | `e=ORDER_TRADE_UPDATE`, `E`, `T`, `o` object; `o.s/i/c/o/X/z/q/T/x/t/l/L` | `o.ot` được kiểm tra nếu có; `o.T` là order trade time, không lẫn transaction time top-level |
| Algo event | `e=ALGO_UPDATE`, `E`, `T`, `o` object; `o.s/aid/caid/at/o/X/tp` | `o.ai` → linked order ID; `o.act/aq` → actual type/quantity; không suy REST `updateTime` từ envelope |

Nested schema Algo còn nêu `ap`, `q`, `S`, `ps`, `f`, `p`, `V`, `wt`, `pm`, `cp`, `pP`, `R`, `tt`, `gtd`, `rm`, `ia`. Các field ngoài projection không tạo quyền thành công. Schema ghi `act` là chuỗi và mô tả actual order type nhưng example là `0`; parser chỉ nhận actual `MARKET`/`LIMIT`, từ chối `0` để giữ UNKNOWN. `icebergQuantity` trong ví dụ REST là literal string `"null"`; parser không dùng field này và không coi đó là chứng cứ mọi field nullable. Không đóng băng nullability còn chưa được docs nói rõ.

JSON object được đọc trước khi chuẩn hóa; key trùng, NaN/Infinity, kiểu sai và ID/số lượng sai đều bị từ chối. Các field projection dùng đúng casing; không fallback field top-level hay alias REST khi đọc event. Unknown status giữ raw, unknown execution type cho event chiếu UNKNOWN. Số lượng mâu thuẫn với status, hoặc terminal thiếu executed quantity, không được chiếu thành kết quả đã xác nhận.

## Giới hạn nghiệm thu

Fixture là dữ liệu dựng có metadata nguồn/ngày/surface, không phải capture từ tài khoản. ACK tối thiểu là input parser bảo thủ có thể xử lý, không chứng minh response thật luôn rút gọn. Các DTO/parser không tự giữ hoặc release reservation; caller phải giữ UNKNOWN khi parse fail. Existing adapter chỉ được nối decoder nghiêm ngặt, family/type guard và classifier `-4120`; chưa chuyển toàn bộ normalization inline sang DTO mới. Chưa có stream consumer, Algo transport, reducer, authoritative reconciliation, reservation integration hoặc E2E của parser mới. Mọi năng lực Demo theo endpoint vẫn UNKNOWN.

Không thêm khái niệm giao dịch/AI mới: thuật ngữ conditional Algo/Algo Service và các thuật ngữ sử dụng đã có trong `docs/research/glossary-vi.md`; không sửa glossary dùng chung trong lát cắt này.
