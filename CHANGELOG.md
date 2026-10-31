# Changelog

## 0.1.0 — 2026-10-31
First release: screen_name, screen_batch (≤ 500, deduplicated), audit_report (self-contained HTML), list_status, explain_match.
Lists bundled from the official files: OFAC SDN + Consolidated (advanced XML, incl. Arabic/Cyrillic script names), UN Security
Council Consolidated List, EU Financial Sanctions File, UK Sanctions List (FCDO single list), UAE Local Terrorist List (EOCN .xls,
individuals + organisations + entities; de-listed sheets excluded). Deterministic Arabic-aware matcher (NFKC, tashkeel/letter
folding, honorific/particle removal, merged Abd-al- forms, 124 romanisation-variant groups, token-sort/token-set/Jaro-Winkler,
cross-script consonant skeleton), DOB / nationality / entity-type / ID adjustments, inverted-index candidate blocking
(p95 ≈ 30 ms, ~210 MB RSS with all 34k entries loaded). Pay-per-event `name-screened` $0.01, `audit-report` $0.25, `list-status` free.
