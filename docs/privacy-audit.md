# Privacy & dependency audit — GCC Sanctions & Terror-List Screener (31 Oct 2026)

This agent screens names against bundled copies of public government lists. "No data leaves our machines except what the
product must send" therefore means:

| what leaves | to whom | when |
|---|---|---|
| **nothing** at request time | — | `screen_name`, `screen_batch`, `audit_report`, `list_status`, `explain_match` never open a socket; the lists are files in the image / bind mount |
| HTTPS GET of the six official list files (User-Agent `NoahsArch-SanctionsScreener/1.0`) | OFAC, UN, European Commission, FCDO, UAE EOCN | only when `screen refresh` runs (weekly cron, `docker compose run --rm screen-refresh`); no customer data is in those requests |
| metering (`Actor.charge`, dataset rows) | Apify | Actor image only, when a customer runs it there |

Names being screened travel only from the caller to the server (via the Cloudflare Worker, which size-checks but never
reads bodies, and the Tunnel). They are not written to logs, metrics, disk or the dataset other than the caller's own
Apify dataset for an ordinary run. `tests/test_contract.py::test_no_request_logging` plants a marker name and a marker
reference in `screen_name` and `screen_batch` and asserts neither appears in any log record or metric snapshot.
uvicorn access logging is off (agentkit.rest). The MCP server uses `--allowed-host` DNS-rebinding protection.

## Dependencies

| dependency | version | network | telemetry |
|---|---|---|---|
| rapidfuzz | 3.14.6 | none (C++ string metrics) | none — 0 keyword hits in Python/METADATA, 0 `http`/`socket`/`getaddrinfo` strings in the .so |
| xlrd | 2.0.2 | none (BIFF .xls reader; refresh job only) | none — 0 hits |
| pyyaml | 6.0.3 | none | none — 0 hits |
| pydantic, mcp, fastapi, uvicorn | 2.14.2 / 2.3.0 / 0.162.0 / 0.54.0 | serve only | none (audited for the earlier agents; unchanged policy: `DO_NOT_TRACK=1` set in the image anyway) |
| stdlib urllib / xml.etree | 3.12 | refresh job only | — |
| apify (Actor image only) | 4.x | Apify API (`charge`, dataset) | platform metering |

Wheel audit method (same as the other agents): `pip download --no-deps` of the exact locked version, unzip, grep for
`telemetry|analytics|sentry|segment.io|mixpanel|posthog|amplitude|google-analytics|urlopen|requests.(get|post)|http.client|socket.connect|httpx`
in `*.py` and `METADATA`, and `strings` of every `.so` for `http://`, `https://`, `getaddrinfo`, `socket`. All zero.

`defusedxml` was deliberately not added: the XML is parsed only by the offline refresh job from files fetched over HTTPS
from the issuing authorities, CPython 3.12's Expat has billion-laughs / quadratic-blowup protection, and no DTDs or external
entities are resolved. OpenSanctions (CC-BY-NC) and every other aggregator are excluded by design; only primary sources.

## Sources, licences and terms

| list | authority | file | terms |
|---|---|---|---|
| OFAC SDN | US Treasury, Office of Foreign Assets Control | `SDN_ADVANCED.XML` via sanctionslistservice.ofac.treas.gov | US Government work, public domain (17 U.S.C. §105); OFAC asks that users attribute and note the list is updated frequently |
| OFAC Consolidated (non-SDN) | OFAC | `CONS_ADVANCED.XML` | as above |
| UN Security Council Consolidated List | UN Security Council | `scsanctions.un.org/resources/xml/en/consolidated.xml` | published for implementation by all Member States; UN website terms of use (non-commercial restrictions apply to UN *site content*, the sanctions list is an operative document States must apply — we republish match results, not the list) |
| EU Financial Sanctions File | European Commission (DG FISMA, FSD) | `webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw` | Commission reuse policy (Decision 2011/833/EU): free reuse with source acknowledgement; the public token is the one documented on data.europa.eu |
| UK Sanctions List | FCDO (single UK list since 28 Jan 2026; the OFSI Consolidated List and its `ofsistorage` download were retired — the blob now returns 409 PublicAccessNotPermitted) | `sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml` | Open Government Licence v3.0 (attribution) |
| UAE Local Terrorist List | UAE Cabinet, published by the Executive Office for Control & Non-Proliferation (EOCN) | the "Download Excel File" link on `uaeiec.gov.ae/en-us/un-page?p=1` (`LS 130923 2026.xls`, 5 sheets; the two "رفع الإدراج" de-listing sheets are excluded) | official public disclosure list; EOCN website terms and conditions; Cabinet Resolution 74/2020 obliges UAE FIs and DNFBPs to screen against it |

What we republish: for each hit, the listed name, aliases, DOB(s), nationalities, ID numbers, programme/regime, listing
date, reference number and a link to the official entry/list — i.e. the information the issuing authority publishes so
that it *is* screened against. We do not redistribute the raw files; `data/version.yaml` records URL, fetch time and SHA-256
so a reviewer can re-download and verify the snapshot.

## Content and safety notes

- Matching is deterministic (rapidfuzz + rule tables); no model, no hosted LLM, no probabilistic inference beyond scores.
- Every response carries `data_confidence`, `estimated_items` (naming the heuristics used), `sources`, `data_version`,
  `warnings` (stale snapshot > 7 days, refresh failures) and the disclaimer ("screening result, not a legal determination…").
- Input caps: name ≤ 300 chars, batch ≤ 500 subjects, report ≤ 50 subjects; Pydantic `extra="forbid"` everywhere.
- Container: non-root, read-only root filesystem, `cap_drop ALL`, `no-new-privileges`, 512 MB limit (observed ~210 MB RSS);
  the refresh container is the only one that can reach the internet and it has a 1 GB limit (the 128 MB OFAC XML is
  stream-parsed in two passes, ~210 MB peak).
