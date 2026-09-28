# S2 沪深交易所公告来源小样本实测（2026-09-28）

本轮只验证官方站点的可达性、按证券和日期发现报告、PDF 原文一致性，以及更正公告发现能力。所有请求通过 `FetchGuard` 的精确域名白名单、重定向复核、体积上限和限速；本机透明代理的伪 IP 仅在探针进程内按当时 DNS 实测值声明。响应及请求体哈希、收据保存在被 Git 忽略的 `data/s2-pdf-review/`；没有改动正式事实库。

| 来源 | 本轮实测 | 尚未证明 |
| --- | --- | --- |
| 深交所 | [上市公司公告](https://www.szse.cn/disclosure/listed/notice/index.html?stock=300502)和[定期报告](https://www.szse.cn/disclosure/listed/fixed/index.html?stock=300502)页面均返回 200；现行 `POST /api/search/content` 的 `fixed_disc` 查询按代码和日期分别命中新易盛 2026 半年报、格力电器 2025 年报全文，带发布日期、文档 ID、PDF 地址。`listedNotice_disc` 查询新易盛 2026-08-25 至 2026-09-28 返回 20 条，全部在首页；标题中未命中“更正/修订”。 | 更长历史范围的稳定性、所有证券与报告期覆盖、正文层面更正关系、压力下限流情况。 |
| 上交所 | [上市公司公告](https://www.sse.com.cn/disclosure/listedinfo/announcement/index.shtml?productId=600276)、[定期报告预约](https://www.sse.com.cn/disclosure/listedinfo/periodic/)页面及官网脚本返回 200；探索性 `query.sse.com.cn` 请求返回 200、空列表。 | 探索请求没有官网脚本支持的确切参数；因此 600276 的 2025 年报按代码检索、分页、对应 PDF 下载和更正链均未验证。 |

深交所[新易盛 2026 半年报官方 PDF](https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-08-25/7208175f-a232-40d5-84a3-8a187e783752.PDF)返回 200、934,935 字节，SHA-256 为 `bc164fb2026947ef377e0f242cb80566b30afcb06d202e47b20222fd45c93d3e`，与巨潮公告 `1225499406` 的归档字节完全相同。PDF 的 134 页中，S2 所需的归母权益、营业收入、合并净利润、归母净利润和经营现金流五个值均与现有候选提取结果一致。深交所列表的 `docpubtime` 为日期级精度；不能据此推断公告发布时刻。列表给出的 PDF URL 使用 HTTP，本轮下载用同域 HTTPS，并分别保留来源 URL 与实际抓取 URL。

第二个样本是格力电器 `000651` 的 2025 年报：2026-04-29 窄窗查询返回全文、一季报和摘要共 3 条；全文文档 ID 为 `073709c5-c0bf-4d8f-803d-d88af2e25a18`。[深交所 PDF](http://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-29/f9937e4b-3994-4100-8200-7fd8a000ecfa.PDF)下载返回 200、2,350,012 字节，SHA-256 为 `7cc972f2d199e2ec3797cc5da479e8268afa59b77875770de3bcd3648242fb8c`，与巨潮公告 `1225250396` 的归档字节完全相同。PDF 的 219 页中，S2 所需的营业收入、合并净利润、归母净利润和经营现金流四个值与候选一致。

深交所正式查询证据见本机 `szse-fixed-search-evidence.json`（请求体 SHA-256 `b4ef1217…04b610`、响应 SHA-256 `062dd362…592ca`、收据 `rcpt_69f654151ba7f341e640f0a4`）、`szse-all-notices-evidence.json`（收据 `rcpt_7dc27c9bcf848af8f1e02af8`），以及 `szse-000651-2025fy-evidence.json`（查询收据 `rcpt_673896b3c02efd3a7fd36f2d`、PDF 收据 `rcpt_7ca1acaeffafde51ec3408a4`）。全类别请求设 `pageSize=100`，服务端返回的规范值是 50；本样本 `totalSize=20`，因此没有多页可验证。上交所证据见 `sse-html-probe.json`、`sse-script-probe.json`、`sse-600276-announcement-index-probe.json`。

**结论：** 深交所已证明可作为深市单份原文下载和已测试查询范围内的公告发现备源；上交所目前仅证明页面可达。少量成功请求不能推出交易所的访问限制比巨潮更少。交易所 PDF 的存在和五个候选值一致，也不等于 S2 的正式时点事实已经通过验收。接入时需给交易所建立独立来源身份与公告索引收据，校验分页、公告日期、原版及更正关系；不同站点内容哈希相同可做强交叉验证，不能把交易所 URL 或文档 ID 冒充巨潮身份。只有完成版本链和决策时点验证后才能考虑升为正式 S2 输入。
