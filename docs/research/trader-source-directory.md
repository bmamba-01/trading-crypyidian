# Danh mục 132 nguồn trader, analyst và tác giả kỹ thuật

Ngày research: **07/10/2026**. [CSV đầy đủ](./trader-source-registry.csv) lưu nguồn bằng chứng, trạng thái hoạt động, phương pháp, vai trò và URL bổ sung; [thiết kế bot](../superpowers/specs/2026-10-07-independent-binance-futures-bot-design.md) mô tả cách dùng cho AI.

Danh mục gồm **132 cá nhân/pseudonym khác nhau** sau khi gộp 63 hồ sơ crypto với 71 hồ sơ global và loại trùng Peter Brandt, Lyn Alden. Trong đó **116 hồ sơ verified_channel**, **16 discovered_candidate**. Verified nghĩa là bằng chứng tác giả/kênh, không phải audit tài khoản hoặc pháp danh của mọi pseudonym. **0 hồ sơ có lợi nhuận kiểm toán được xác minh trong research này.**

Danh mục đáp ứng phạm vi tìm 100–200 nguồn đáng nghiên cứu; chưa đủ bằng chứng để gọi là bảng xếp hạng 132 trader có lợi nhuận tốt nhất. Vai trò gồm người giao dịch, quant, nhà phân tích technical/macro/on-chain và tác giả/giảng viên. Nhóm giáo dục và macro phục vụ phương pháp/ngữ cảnh, không tự biến thành nguồn gọi lệnh futures.

## Cách đọc và ưu tiên

- verified_channel: có bằng chứng kênh/tác giả từ trang gốc hoặc bài ký tên; activity_status nói riêng về độ mới.
- discovered_candidate: tìm thấy hồ sơ, tên khách mời hoặc giới thiệu; cần kiểm tra liên kết định danh/kênh riêng trước ingestion.
- priority: ưu tiên đọc phương pháp do research đề xuất, không phải rank lợi nhuận.
- not_admitted, not_assessed: chưa bật connector, chưa kết luận quyền embedding/training và chưa review đủ mẫu.
- ID person-xxx chỉ ổn định trong snapshot v1. Khi triển khai, cấp UUID bất biến; alias/link mới cập nhật cùng canonical person.
- Nhiều người dùng chung tổ chức/kênh. Domain hint chỉ giúp triage, không đủ xác định origin independence.

Không chọn nguồn dựa trên follower count. Nguồn riêng có timestamp, setup, điều kiện xác nhận và invalidation rõ được ưu tiên lấy mẫu ex-ante. Tác phẩm lịch sử/long-term macro có thể giúp học phương pháp nhưng không dùng như forecast 15m hiện tại.

## Pool 20 nguồn đọc trước

Đây là đề xuất về độ liên quan phương pháp, chưa là tập đã cấp quyền ingest. Mỗi người có evidence trong bảng dưới.

| Nhóm | Người | Phương pháp muốn đối chiếu |
| --- | --- | --- |
| Crypto chart | CryptoCred, Peter Brandt, RektProof, Trader Mayne, Cheds | Cấu trúc giá, range, pattern, invalidation và sizing |
| Crypto derivatives/on-chain | James Check, Charles Edwards, Skew, Keith Alan, Josh Olszewicz | Regime, flow, volume, Ichimoku và khác biệt spot/perp |
| Systematic/validation | Robert Carver, Ernest P. Chan, Kevin Davey, Andrea Unger, Marcos López de Prado | Trend, portfolio, cost, chọn tham số và chống overfit |
| Macro/pattern/process | Joseph Wang, Lyn Alden, John Bollinger, Thomas N. Bulkowski, Brett Steenbarger | Liquidity, bands, thống kê chart, nhật ký và review |

Tài liệu nguồn ban đầu: [crypto notes](./crypto-traders-notes.md), [global notes](./global-traders-notes.md). Research đọc nguồn chính thức và bài gốc để kiểm tra attribution; chưa đọc mọi sách/khóa học/paywall hoặc mọi bài trong lịch sử từng kênh.

## Toàn bộ danh mục

Tên và vai trò giữ theo snapshot research. Link “bằng chứng” có thể là bio, author archive, bài tự xuất bản hoặc kênh chung; không phải mọi link là feed cập nhật hàng ngày. Hồ sơ cùng kênh chỉ tách người khi nguồn có attribution; không coi họ là các phiếu độc lập.

| ID | Người | Vai trò | Kênh tham khảo | Bằng chứng | Trạng thái |
| --- | --- | --- | --- | --- | --- |
| person-001 | Adam H. Grimes | practitioner_author_educator | [website_or_blog](https://www.adamhgrimes.com/) | [nguồn](https://www.adamhgrimes.com/) | verified_channel |
| person-002 | Adrian Zduńczyk (CryptoBirb) | technical analyst; educator | [website](https://www.linkedin.com/company/thebirbnest) | [nguồn](https://www.linkedin.com/company/thebirbnest) | discovered_candidate |
| person-003 | Al Brooks | practitioner_educator_author | [website_or_blog](https://www.brookstradingcourse.com/) | [nguồn](https://www.brookstradingcourse.com/) | verified_channel |
| person-004 | Alessio Rastani | technical trader; educator | [website](https://leadingtrader.com/alessio-rastani/) | [nguồn](https://leadingtrader.com/alessio-rastani/) | verified_channel |
| person-005 | Alexander Elder | practitioner_author_educator | [website_or_blog](https://www.elder.com/) | [nguồn](https://www.elder.com/) | verified_channel |
| person-006 | Alfonso Peccatiello | former_institutional_portfolio_manager | [website_or_blog](https://themacrocompass.org/) | [nguồn](https://themacrocompass.org/) | verified_channel |
| person-007 | Andrea Unger | practitioner_educator | [website_or_blog](https://ungeracademy.com/blog) | [nguồn](https://ungeracademy.com/about) | verified_channel |
| person-008 | Andreas F. Clenow | practitioner_author | [website_or_blog](https://www.followingthetrend.com/) | [nguồn](https://www.followingthetrend.com/) | verified_channel |
| person-009 | Anthony Crudele | practitioner_host | [website_or_blog](https://anthonycrudele.com/) | [nguồn](https://anthonycrudele.com/) | verified_channel |
| person-010 | Arthur Hayes | derivatives trader; macro writer | [Substack](https://cryptohayes.substack.com/) | [nguồn](https://cryptohayes.substack.com/) | discovered_candidate |
| person-011 | Arthur Hill | technical_strategist | [website_or_blog](https://articles.stockcharts.com/author/arthur-hill/) | [nguồn](https://articles.stockcharts.com/article/mag7-etf-takes-lead-with-breakout-and-follow-through/) | verified_channel |
| person-012 | Ashraf Laidi | FX_strategist_author | [website_or_blog](https://www.ashraflaidi.com/) | [nguồn](https://www.ashraflaidi.com/) | verified_channel |
| person-013 | Avi Felman | crypto portfolio manager | [podcast](https://rss.com/podcasts/technicalroundup/) | [nguồn](https://rss.com/podcasts/technicalroundup/) | discovered_candidate |
| person-014 | Benjamin Cowen | quantitative analyst; educator | [website](https://www.benjamincowen.com/) | [nguồn](https://www.benjamincowen.com/) | verified_channel |
| person-015 | Bob Loukas | position trader; educator | [YouTube](https://www.youtube.com/watch?v=izftI6f-ImQ) | [nguồn](https://thefinancialtap.com/) | verified_channel |
| person-016 | Boris Schlossberg | practitioner_author_educator | [website_or_blog](https://www.bktraders.com/) | [nguồn](https://www.bktraders.com/) | verified_channel |
| person-017 | Brent Donnelly | institutional_practitioner_author | [website_or_blog](https://www.spectramarkets.com/) | [nguồn](https://www.spectramarkets.com/about/) | verified_channel |
| person-018 | Brent Johnson | investment_practitioner_macro_researcher | [website_or_blog](https://research.santiagocapital.com/) | [nguồn](https://research.santiagocapital.com/about) | verified_channel |
| person-019 | Brett Steenbarger | trader_performance_coach_author | [website_or_blog](https://traderfeed.blogspot.com/) | [nguồn](https://traderfeed.blogspot.com/?m=1) | verified_channel |
| person-020 | Brian Shannon | practitioner_author_educator | [website_or_blog](https://alphatrends.net/) | [nguồn](https://alphatrends.net/about/) | verified_channel |
| person-021 | CBS | pseudonymous trader; educator | [website](https://www.tradermayne.com/home) | [nguồn](https://www.tradermayne.com/home) | discovered_candidate |
| person-022 | Cem Karsan | derivatives_practitioner | [website_or_blog](https://www.kaivolatility.com/) | [nguồn](https://www.kaivolatility.com/) | verified_channel |
| person-023 | Cesar Alvarez | quant_researcher_practitioner | [website_or_blog](https://alvarezquanttrading.com/) | [nguồn](https://alvarezquanttrading.com/bio/) | verified_channel |
| person-024 | Charles Edwards | quant trader; fund manager | [website](https://www.capriolepro.com/team) | [nguồn](https://www.capriolepro.com/team) | verified_channel |
| person-025 | Cheds (BigCheds) | technical trader; educator | [Bluesky](https://bsky.app/profile/chedstrading.bsky.social) | [nguồn](https://bsky.app/profile/chedstrading.bsky.social) | verified_channel |
| person-026 | Chris Inks | technical trader; educator | [website](https://www.texaswestcapital.com/trader-education) | [nguồn](https://www.texaswestcapital.com/trader-education) | verified_channel |
| person-027 | Chris Tate | practitioner_author_educator | [website_or_blog](https://www.tradinggame.com.au/) | [nguồn](https://www.tradinggame.com.au/) | verified_channel |
| person-028 | CL | pseudonymous trader; investor | [podcast](https://rss.com/podcasts/technicalroundup/) | [nguồn](https://rss.com/podcasts/technicalroundup/) | discovered_candidate |
| person-029 | Corey Hoffstein | systematic_practitioner_researcher | [website_or_blog](https://blog.thinknewfound.com/author/choffstein/) | [nguồn](https://blog.thinknewfound.com/author/choffstein/) | verified_channel |
| person-030 | CrediBULL Crypto | technical trader; analyst | [website](https://linktr.ee/crediblecrypto) | [nguồn](https://linktr.ee/crediblecrypto) | verified_channel |
| person-031 | CryptoCon | model analyst; educator | [Substack](https://cryptocon.substack.com/) | [nguồn](https://cryptocon.substack.com/p/quick-update-july-2026) | verified_channel |
| person-032 | CryptoCred | technical trader; educator | [website](https://linktr.ee/cryptocred) | [nguồn](https://linktr.ee/cryptocred) | verified_channel |
| person-033 | CryptoPatel | pseudonymous technical analyst | [TradingView](https://in.tradingview.com/u/CryptoPatel/page-3/) | [nguồn](https://in.tradingview.com/u/CryptoPatel/page-3/) | verified_channel |
| person-034 | Daan Crypto Trades | technical trader | [TradingView](https://www.tradingview.com/u/DaanCryptoTrades/) | [nguồn](https://www.tradingview.com/u/DaanCryptoTrades/) | discovered_candidate |
| person-035 | Dan Gambardello | macro analyst; educator | [website](https://cryptocapitalventure.com/) | [nguồn](https://cryptocapitalventure.com/) | verified_channel |
| person-036 | Dan Zanger | practitioner_educator | [website_or_blog](https://www.chartpattern.com/) | [nguồn](https://www.chartpattern.com/about.cfm) | verified_channel |
| person-037 | Darius Dale | macro_strategist_researcher | [website_or_blog](https://42macro.com/) | [nguồn](https://42macro.com/our-founder) | verified_channel |
| person-038 | Dave the Wave | technical analyst | [Substack](https://substack.com/@davethewave) | [nguồn](https://substack.com/@davethewave) | verified_channel |
| person-039 | David Bergstrom | quant_practitioner_software_author | [website_or_blog](https://www.buildalpha.com/) | [nguồn](https://www.buildalpha.com/about/) | verified_channel |
| person-040 | David Keller | technical_strategist | [website_or_blog](https://articles.stockcharts.com/author/david-keller/) | [nguồn](https://articles.stockcharts.com/article/bull-flags-are-everywhere-but-are-they-confirmed/) | verified_channel |
| person-041 | David Puell | on-chain analyst | [research publication](https://charts.checkonchain.com/webassets/docs/Cointime%20Economics.pdf) | [nguồn](https://charts.checkonchain.com/webassets/docs/Cointime%20Economics.pdf) | verified_channel |
| person-042 | DonAlt | technical trader; educator | [YouTube](https://www.youtube.com/@TechnicalRoundup) | [nguồn](https://rss.com/podcasts/technicalroundup/) | verified_channel |
| person-043 | Dylan LeClair | Bitcoin analyst | [website](https://www.linkedin.com/posts/dylanleclair_bitcoin-bitcoin-activity-6834955278929149952-QN_w) | [nguồn](https://www.linkedin.com/posts/dylanleclair_bitcoin-bitcoin-activity-6834955278929149952-QN_w) | verified_channel |
| person-044 | Ed Seykota | systematic_practitioner_author | [website_or_blog](https://www.seykota.com/) | [nguồn](https://www.seykota.com/) | verified_channel |
| person-045 | Eric Krown | technical/quant analyst; educator | [YouTube](https://www.youtube.com/c/KrownsCryptoCave) | [nguồn](https://www.youtube.com/c/KrownsCryptoCave) | discovered_candidate |
| person-046 | Ernest P. Chan | quant_practitioner_author | [website_or_blog](https://epchan.com/) | [nguồn](https://epchan.com/) | verified_channel |
| person-047 | FieryTrading | pseudonymous technical analyst; educator | [website](https://www.fierytrading.com/) | [nguồn](https://www.tradingview.com/script/9bRuNVK3-FieryTrend-Indicator/) | verified_channel |
| person-048 | Filbfilb | technical trader | [website](https://dev.decentrader.com/landing-svg/) | [nguồn](https://dev.decentrader.com/landing-svg/) | verified_channel |
| person-049 | Galen Woods | retail_practitioner_educator | [website_or_blog](https://www.tradingsetupsreview.com/) | [nguồn](https://www.tradingsetupsreview.com/how-to-identify-demand-and-supply-using-price-action/) | verified_channel |
| person-050 | Gareth Soloway | trader; educator | [website](https://verifiedinvesting.com/pages/profiles/gareth-soloway) | [nguồn](https://verifiedinvesting.com/pages/profiles/gareth-soloway) | verified_channel |
| person-051 | Gary Antonacci | investment_practitioner_author | [website_or_blog](https://www.optimalmomentum.com/) | [nguồn](https://www.optimalmomentum.com/) | verified_channel |
| person-052 | Hsaka | pseudonymous trader | [X](https://x.com/HsakaTrades) | [nguồn](https://x.com/HsakaTrades/status/1822061700331323900) | discovered_candidate |
| person-053 | Jacob Canfield | technical analyst; educator | [website](https://jacobcanfield.com/cn/index.html) | [nguồn](https://jacobcanfield.com/cn/index.html) | verified_channel |
| person-054 | James Check (Checkmate) | on-chain analyst; educator | [website](https://newsletter.checkonchain.com/) | [nguồn](https://www.checkonchain.com/about) | verified_channel |
| person-055 | Jason Pizzino | investor; educator | [website](https://linktr.ee/jasonpizzino) | [nguồn](https://linktr.ee/jasonpizzino) | verified_channel |
| person-056 | Jason Shapiro | practitioner_educator | [website_or_blog](https://www.crowdedmarketreport.com/) | [nguồn](https://www.crowdedmarketreport.com/) | verified_channel |
| person-057 | Jerry Parker | institutional_practitioner | [website_or_blog](https://chesapeakecapital.com/) | [nguồn](https://chesapeakecapital.com/about/team/jerry-parker/) | verified_channel |
| person-058 | Jim Bianco | macro_strategist_researcher | [website_or_blog](https://www.biancoresearch.com/) | [nguồn](https://www.biancoresearch.com/) | verified_channel |
| person-059 | Jim Dalton | practitioner_author_educator | [website_or_blog](https://jimdaltontrading.com/) | [nguồn](https://jimdaltontrading.com/about/) | verified_channel |
| person-060 | Joe Rabil | technical_strategist | [website_or_blog](https://articles.stockcharts.com/author/joe-rabil/) | [nguồn](https://articles.stockcharts.com/article/the-3-trade-setups-every-trader-should-know/) | verified_channel |
| person-061 | John Bollinger | technical_author_practitioner | [website_or_blog](https://www.bollingerbands.com/) | [nguồn](https://www.bollingerbands.com/) | verified_channel |
| person-062 | John Ehlers | technical_author_researcher | [website_or_blog](https://www.mesasoftware.com/TechnicalArticles.htm) | [nguồn](https://www.mesasoftware.com/TechnicalArticles.htm) | verified_channel |
| person-063 | Joseph Wang | former_central_bank_trader_author | [website_or_blog](https://fedguy.com/) | [nguồn](https://fedguy.com/about/) | verified_channel |
| person-064 | Josh Olszewicz (CarpeNoctom) | technical trader; analyst | [website](https://bravenewcoin.com/insights/cryptocurrency-trading-tips) | [nguồn](https://bravenewcoin.com/insights/cryptocurrency-trading-tips) | verified_channel |
| person-065 | Julius de Kempenaer | technical_researcher | [website_or_blog](https://articles.stockcharts.com/author/julius-de-kempenaer/) | [nguồn](https://articles.stockcharts.com/article/bonds-are-the-real-risk-right-now-not-stocks/) | verified_channel |
| person-066 | Kathy Lien | practitioner_author_educator | [website_or_blog](https://www.bktraders.com/) | [nguồn](https://www.bktraders.com/) | verified_channel |
| person-067 | Keith Alan | order-flow analyst | [website](https://materialindicators.com/author/keithadmin/) | [nguồn](https://materialindicators.com/author/keithadmin/) | verified_channel |
| person-068 | Kevin Davey | practitioner_educator | [website_or_blog](https://kjtradingsystems.com/free.html) | [nguồn](https://kjtradingsystems.com/about.html) | verified_channel |
| person-069 | Kevin Svenson | technical analyst; educator | [website](https://linktr.ee/kevinsvenson) | [nguồn](https://linktr.ee/kevinsvenson) | verified_channel |
| person-070 | Ki Young Ju | on-chain analyst | [X](https://x.com/ki_young_ju) | [nguồn](https://consensus.cryptoquant.com/analyst/ki_young_ju?asset=btc) | discovered_candidate |
| person-071 | Koroush AK | trader; educator | [website](https://www.koroushak.io/framework) | [nguồn](https://www.koroushak.io/framework) | verified_channel |
| person-072 | Krillin | pseudonymous trader; educator | [website](https://www.tradermayne.com/home) | [nguồn](https://www.tradermayne.com/home) | discovered_candidate |
| person-073 | Kristjan Kullamägi | practitioner_educator | [website_or_blog](https://qullamaggie.com/) | [nguồn](https://qullamaggie.com/) | verified_channel |
| person-074 | Lance Breitstein | prop_practitioner_educator | [website_or_blog](https://www.smbtraining.com/blog/) | [nguồn](https://www.smbtraining.com/blog/smb-trading-summit-nyc-2026) | verified_channel |
| person-075 | Larry Connors | practitioner_author_educator | [website_or_blog](https://www.tradingmarkets.com/) | [nguồn](https://store.tradingmarkets.com/products/ai-for-traders-manual) | verified_channel |
| person-076 | Larry Williams | practitioner_author_educator | [website_or_blog](https://www.ireallytrade.com/) | [nguồn](https://www.ireallytrade.com/halloffame/) | verified_channel |
| person-077 | Ledger Status | trader; on-chain analyst | [podcast](https://rss.com/podcasts/technicalroundup/) | [nguồn](https://rss.com/podcasts/technicalroundup/) | discovered_candidate |
| person-078 | Linda Bradford Raschke | practitioner_author | [website_or_blog](https://lindaraschke.net/) | [nguồn](https://lindaraschke.net/) | verified_channel |
| person-079 | Lomah | pseudonymous trader; educator | [website](https://www.tradermayne.com/home) | [nguồn](https://www.tradermayne.com/home) | discovered_candidate |
| person-080 | Louise Bedford | technical_author_educator | [website_or_blog](https://www.tradinggame.com.au/) | [nguồn](https://www.tradinggame.com.au/) | verified_channel |
| person-081 | Luke Gromen | macro_researcher_author | [website_or_blog](https://fftt-llc.com/) | [nguồn](https://fftt-llc.com/about/) | verified_channel |
| person-082 | Lyn Alden | investment analyst  /  macro_researcher_author | [website  /  website_or_blog](https://www.lynalden.com/) | [nguồn](https://www.lynalden.com/) | verified_channel |
| person-083 | MagicPoopCannon | pseudonymous technical analyst | [TradingView](https://www.tradingview.com/u/MagicPoopCannon/) | [nguồn](https://www.tradingview.com/u/MagicPoopCannon/) | verified_channel |
| person-084 | Marc Chandler | institutional_FX_strategist_author | [website_or_blog](https://www.marctomarket.com/) | [nguồn](https://www.marctomarket.com/p/about-marc-chandler.html) | verified_channel |
| person-085 | Marcos López de Prado | institutional_quant_researcher_author | [website_or_blog](https://www.quantresearch.org/) | [nguồn](https://www.duffield.cornell.edu/people/marcos-lopez-de-prado/) | verified_channel |
| person-086 | Mark Minervini | practitioner_author_educator | [website_or_blog](https://www.minervini.com/) | [nguồn](https://www.minervini.com/) | verified_channel |
| person-087 | Martin Pring | technical_author_strategist | [website_or_blog](https://articles.stockcharts.com/author/martin-pring/) | [nguồn](https://articles.stockcharts.com/article/two-inter-asset-relationships-worth-monitoring-if-you-want-to-stay-bullish/) | verified_channel |
| person-088 | Mary Ellen McGonagle | investment_researcher | [website_or_blog](https://articles.stockcharts.com/author/mary-ellen-mcgonagle/) | [nguồn](https://articles.stockcharts.com/article/the-danger-of-falling-in-love-with-market-leaders/) | verified_channel |
| person-089 | Max Boonen | market maker; derivatives practitioner | [podcast](https://rss.com/podcasts/technicalroundup/) | [nguồn](https://rss.com/podcasts/technicalroundup/) | discovered_candidate |
| person-090 | Meb Faber | portfolio_practitioner_author_host | [website_or_blog](https://mebfaber.com/) | [nguồn](https://mebfaber.com/about/) | verified_channel |
| person-091 | Michael Covel | author_interviewer_educator | [website_or_blog](https://www.covel.com/) | [nguồn](https://www.covel.com/) | verified_channel |
| person-092 | Michael Halls-Moore | quant_developer_author | [website_or_blog](https://quantstart.com/) | [nguồn](https://quantstart.com/articles/Should-You-Build-Your-Own-Backtester/) | verified_channel |
| person-093 | Michael Harris | practitioner_author | [website_or_blog](https://www.priceactionlab.com/Blog/) | [nguồn](https://www.priceactionlab.com/Blog/about/) | verified_channel |
| person-094 | Michael Howell | macro_practitioner_author | [website_or_blog](https://capitalwars.substack.com/) | [nguồn](https://capitalwars.substack.com/p/a-new-fed-chair-the-names-bond) | verified_channel |
| person-095 | Michael Pizzino | futures trader; educator | [website](https://linktr.ee/michaelpizzinotrades) | [nguồn](https://linktr.ee/michaelpizzinotrades) | verified_channel |
| person-096 | Michaël van de Poppe | trader; macro analyst | [website](https://www.michaelvandepoppe.com/) | [nguồn](https://www.michaelvandepoppe.com/) | verified_channel |
| person-097 | Mike Bellafiore | prop_practitioner_author_educator | [website_or_blog](https://www.smbtraining.com/blog/) | [nguồn](https://www.smbtraining.com/blog/smb-trading-summit-nyc-2026) | verified_channel |
| person-098 | Milan Vaishnav | technical_strategist | [website_or_blog](https://articles.stockcharts.com/author/milan-vaishnav/) | [nguồn](https://articles.stockcharts.com/article/week-ahead-nifty-must-reclaim-key-level-to-extend-technical-rebound-leadership-slowly-changing-hands/) | verified_channel |
| person-099 | Nicholas Merten | analyst; educator | [website](https://www.datadashpro.com/) | [nguồn](https://www.datadashpro.com/) | verified_channel |
| person-100 | Nick Radge | practitioner_author | [website_or_blog](https://www.thechartist.com.au/) | [nguồn](https://www.thechartist.com.au/team/) | verified_channel |
| person-101 | Noelle Acheson | crypto macro analyst | [website](https://www.cryptoismacro.com/) | [nguồn](https://www.cryptoismacro.com/) | verified_channel |
| person-102 | Paul Sampson | technical analyst | [website](https://www.datadashpro.com/) | [nguồn](https://www.datadashpro.com/) | verified_channel |
| person-103 | Perry J. Kaufman | systematic_author_practitioner | [website_or_blog](https://kaufmansignals.com/) | [nguồn](https://kaufmansignals.com/) | verified_channel |
| person-104 | Peter Brandt | trader; educator  /  practitioner_author | [website  /  website_or_blog](https://www.peterlbrandt.com/) | [nguồn](https://www.peterlbrandt.com/home-1/) | verified_channel |
| person-105 | Philip Swift | on-chain analyst; trader | [website](https://www.decentrader.com/market_updates/what_next_after_the_major_downturn/) | [nguồn](https://www.decentrader.com/market_updates/what_next_after_the_major_downturn/) | verified_channel |
| person-106 | Pierre | pseudonymous trader; educator | [website](https://www.tradermayne.com/home) | [nguồn](https://www.tradermayne.com/home) | discovered_candidate |
| person-107 | PlanB | model analyst; educator | [website](https://www.planbtc.com/) | [nguồn](https://www.planbtc.com/) | verified_channel |
| person-108 | Ray Dalio | macro_practitioner_author | [website_or_blog](https://www.principles.com/) | [nguồn](https://www.principles.com/) | verified_channel |
| person-109 | Rayner Teo | practitioner_educator | [website_or_blog](https://www.tradingwithrayner.com/) | [nguồn](https://www.tradingwithrayner.com/about-rayner-teo/) | verified_channel |
| person-110 | Rekt Capital | analyst; educator | [website](https://www.rektcapital.co.uk/) | [nguồn](https://www.rektcapital.co.uk/) | verified_channel |
| person-111 | RektProof | price action trader; educator | [website](https://rektproof.com/) | [nguồn](https://www.tradingview.com/u/Rektproof/) | verified_channel |
| person-112 | RLinda | pseudonymous technical analyst | [TradingView](https://www.tradingview.com/u/RLinda/) | [nguồn](https://www.tradingview.com/u/RLinda/) | verified_channel |
| person-113 | Robert Carver | practitioner_author | [website_or_blog](https://www.systematicmoney.org/) | [nguồn](https://www.systematicmoney.org/bio) | verified_channel |
| person-114 | Scott Melker | trader; educator | [website](https://www.linkedin.com/in/scottmelker) | [nguồn](https://www.linkedin.com/in/scottmelker) | verified_channel |
| person-115 | Seth Freudberg | options_practitioner_educator | [website_or_blog](https://www.smbtraining.com/blog/) | [nguồn](https://www.smbtraining.com/blog/smb-trading-summit-nyc-2026) | verified_channel |
| person-116 | Skew (52kskew) | order-flow trader; developer | [Medium](https://52kskew.medium.com/) | [nguồn](https://52kskew.medium.com/) | verified_channel |
| person-117 | Steve Burns | trading_author_educator | [website_or_blog](https://www.newtraderu.com/category/interview/) | [nguồn](https://www.newtraderu.com/category/interview/) | verified_channel |
| person-118 | Steve Nison | technical_author_educator | [website_or_blog](https://candlecharts.com/) | [nguồn](https://candlecharts.com/about-steve-nison/) | verified_channel |
| person-119 | Steven Spencer | prop_practitioner_educator | [website_or_blog](https://www.smbtraining.com/blog/) | [nguồn](https://www.smbtraining.com/blog/smb-trading-summit-nyc-2026) | verified_channel |
| person-120 | Thomas N. Bulkowski | practitioner_technical_author | [website_or_blog](https://www.thepatternsite.com/) | [nguồn](https://www.thepatternsite.com/about.html) | verified_channel |
| person-121 | Tom Basso | practitioner_author | [website_or_blog](https://enjoytheride.world/) | [nguồn](https://www.linkedin.com/in/tom-basso-7786a01a3) | verified_channel |
| person-122 | Tom Crown | technical trader; educator | [website](https://tomcrown.io/) | [nguồn](https://tomcrown.io/) | verified_channel |
| person-123 | Tom Hougaard | practitioner_author_educator | [website_or_blog](https://tradertom.com/) | [nguồn](https://tradertom.com/) | verified_channel |
| person-124 | Tone Vays | derivatives trader; educator | [website](https://www.tonevays.com/) | [nguồn](https://www.tonevays.com/) | verified_channel |
| person-125 | Trader Mayne | trader; educator | [website](https://www.tradermayne.com/home) | [nguồn](https://www.tradermayne.com/home) | verified_channel |
| person-126 | Trader XO | trader | [X](https://x.com/Trader_XO/with_replies) | [nguồn](https://x.com/Trader_XO/with_replies) | discovered_candidate |
| person-127 | TraderSZ | trader; educator | [website](https://www.tradersz.com/) | [nguồn](https://www.tradersz.com/) | verified_channel |
| person-128 | UB | pseudonymous trader; educator | [website](https://www.tradermayne.com/home) | [nguồn](https://www.tradermayne.com/home) | discovered_candidate |
| person-129 | Wendy O (CryptoWendyO) | analyst; educator | [website](https://rumble.com/c/cryptowendyo/about) | [nguồn](https://rumble.com/c/cryptowendyo/about) | verified_channel |
| person-130 | Will Clemente | on-chain analyst | [Substack](https://reflexivityresearch.substack.com/p/letter-from-will-clemente) | [nguồn](https://reflexivityresearch.substack.com/p/letter-from-will-clemente) | verified_channel |
| person-131 | Willy Woo | on-chain analyst | [website](https://woobull.com/author/willywoo/) | [nguồn](https://woobull.com/author/willywoo/) | verified_channel |
| person-132 | Yves Hilpisch | quant_author_educator | [GitHub](https://github.com/yhilpisch/py4fi2nd) | [nguồn](https://github.com/yhilpisch/py4fi2nd) | verified_channel |

## Đưa nguồn vào kho AI

Pipeline tham chiếu: discovery → kiểm tra attribution → review mẫu → kiểm tra quyền → MethodCard/Opinion/PatternCase → shadow evaluation → chỉ đưa feature vào model sau ablation. Chi tiết connector và giới hạn nền tảng ở [execution-and-ingestion-2026.md](./execution-and-ingestion-2026.md).

Mỗi MethodCard lưu điều kiện, công thức, thị trường/timeframe, invalidation và nguồn; mỗi Opinion lưu published/received/available_at, horizon và expiry. Các bài nói lại cùng một ý tưởng được dedup theo origin. Sau này nếu đánh giá forecast, chỉ dùng mẫu đã ghi trước outcome; không chỉ chọn các bài đúng hoặc được giữ lại trên hồ sơ.

Danh mục chưa phải corpus huấn luyện. Không có thao tác đăng ký khóa học, mua API, nhắn người được liệt kê hoặc tải hàng loạt nội dung. Hoạt động năm 2026 được ghi riêng trong CSV; trường not_checked/archive/historical không có nghĩa tác giả đang cập nhật mỗi ngày.
