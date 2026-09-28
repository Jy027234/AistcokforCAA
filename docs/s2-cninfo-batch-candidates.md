# CNINFO S2 batch candidate collection

Use `tools/collect_s2_cninfo_batch.py` to collect only an explicitly selected
subset of C-sector manufacturing stocks from `configs/real-pool-csrc.yaml`.
Both `--stocks` and `--periods` are required, so an invocation cannot default
to the full configured pool. Financial-sector rows are excluded.

Example for one issuer and one report period:

```powershell
python tools/collect_s2_cninfo_batch.py `
  --stocks 300502 `
  --periods 2026-06-30 `
  --output data/s2-pdf-review/cninfo-batch-300502-2026H1.json
```

The collector uses the guarded and rate-limited `CninfoClient`, follows every
page of the regular-report index within its recorded query window, archives
the selected official PDF, and runs the general CNINFO candidate extractor.
The config short name must match the official index entry. The legal company
name is read from the PDF cover and checked against that short name; when the
cover layout is ambiguous, pass a checked name explicitly with
`--company-full-name 300502=FULL_NAME`. The supplied name must occur on the
official cover.

The FetchGuard proxy-network allowlist is empty by default. Only after
verifying the active local proxy configuration, an operator may pass its
explicit fake-IP ranges for this process, for example:

```powershell
python tools/collect_s2_cninfo_batch.py `
  --stocks 300502 `
  --periods 2026-06-30 `
  --trusted-proxy-network 198.18.0.0/15 `
  --trusted-proxy-network 2001:2::/64 `
  --output data/s2-pdf-review/cninfo-batch-300502-2026H1.json
```

This is the explicit, process-scoped exception described in
[ADR-002](adr/ADR-002-transparent-proxy-dns.md); it does not change FetchGuard
defaults. Declaring a proxy range weakens IP-destination verification, so keep
the configured range narrow and verified. Host allowlisting, protocol and port
checks, response limits, redirect checks, and request rate limits remain active.

To resume the same batch from a previous output, keep the same selections and
output path and add `--resume`. The regular-report index is queried again; a
candidate is reused only when its announcement URL and ID still match and its
archived PDF receipt, bytes, hash, and required fields verify. Failed
security-periods remain in the result with an error, and later pairs continue.

The output is `cninfo-s2-batch-candidates-v1`, with announcement ID, date,
URL, index-page receipts, PDF receipt/hash, extracted value, page, row, unit,
and source cells. It remains `pitEligible: false` with `formalFactCount: 0`.
The regular-report index does not establish a complete cross-category
correction chain, and the collector does not sign or promote any field. Human
review and the existing S2 admission policy remain separate.

## Isolated live pilot (2026-09-28)

The active Mihomo configuration had `enhanced-mode: fake-ip`, IPv6 enabled,
and `fake-ip-range6: 2001:2::0/64`. System DNS returned `www.cninfo.com.cn`
as `198.18.1.105` and `2001:2::159`; `push2.eastmoney.com`, `github.com`, and
`pypi.org` returned `2001:2::164`, `2001:2::31`, and `2001:2::99`. These
observations matched the configured fake-IP ranges. An exact `/128` trial
allowed the query host but correctly stopped at the official PDF host when
`static.cninfo.com.cn` resolved to `2001:2::165`. After verifying the active
`/64`, the final isolated continuation explicitly trusted
`198.18.0.0/15` and `2001:2::/64` for that process only.

The continuation reused the already archived, hash-verified official index
page and fetched only the linked PDF. The report index was complete
(`has_more_false`): announcement `1225499406`, published `2026-08-25`,
[official PDF](https://static.cninfo.com.cn/finalpage/2026-08-25/1225499406.PDF).
The PDF archive hash is
`sha256:bc164fb2026947ef377e0f242cb80566b30afcb06d202e47b20222fd45c93d3e`
(934,935 bytes); its cover matched `成都新易盛通信技术股份有限公司`. The
extractor returned five unreviewed candidates:

| Field | PDF page | Displayed row | Value (CNY) |
| --- | ---: | --- | ---: |
| `parent_equity` | 38 | 归属于母公司所有者权益合计 | 24,296,767,571.68 |
| `revenue` | 41 | 其中：营业收入 | 20,909,746,962.20 |
| `net_profit_consolidated` | 41 | 五、净利润（净亏损以“—”号填列） | 7,566,314,696.14 |
| `net_profit_attributable` | 41 | 1.归属于母公司股东的净利润（净亏损以“—”号填列） | 7,529,168,039.39 |
| `operating_cashflow` | 44 | 经营活动产生的现金流量净额 | 1,616,408,524.15 |

The machine-readable evidence is in
`data/s2-pdf-review/cninfo-batch-300502-2026H1-final64-pilot-20260928.json`.
It remains `pitEligible: false` with `formalFactCount: 0`; this one-report
pilot does not establish the cross-category correction chain or replace human
disposition under the existing S2 admission policy.
