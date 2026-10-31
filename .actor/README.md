# GCC Sanctions & Terror-List Screener — OFAC · UN · EU · UK · UAE Local Terrorist List, Arabic-aware

**Give it a name. Get back whether it is on a sanctions or terror list — and why.** The screener checks a person,
company, vessel or aircraft against the official lists UAE and GCC businesses are required to screen against, from a
bundled, versioned snapshot of the primary sources, with deterministic matching built for Arabic names:

- **Six lists, one call** — OFAC SDN and OFAC Consolidated (non-SDN), the UN Security Council Consolidated List, the
  EU Financial Sanctions File, the UK Sanctions List (FCDO) and the **UAE Local Terrorist List** (Cabinet designations
  published by the EOCN). About 34,000 entries and 107,000 names and aliases, each hit linked to its source entry.
- **Arabic-aware matching** — Arabic input works directly (`محمد حسن آخوند`) against the Arabic aliases in the UN, UK,
  OFAC and UAE lists; Latin input is folded through 124 romanisation-variant groups (Mohamed / Mohammad / Muhammad /
  Mohd, Hussein / Husain / Hossein, Abd al-Rahman / Abdulrahman / Abdurrahman…), honorifics and particles (Sheikh,
  Haji, al-, bin, ibn, bint) are dropped, name parts may be reordered, and a consonant-skeleton heuristic bridges the
  two scripts when a list has no Arabic alias.
- **Disambiguation that compliance teams actually use** — pass `dob`, `nationality`, `entity_type` and `id_number`:
  a date-of-birth mismatch costs 15 points, a passport-number match is reported as *exact*, and a company name never
  "matches" an individual at full score.
- **Explainable** — every hit carries a 0–100 `score`, `match_strength` (exact / strong / possible / weak), and
  `matched_on` (which alias, which normalisation, which identifiers agreed); every response says which snapshot it
  used and whether any list is older than a week.
- **Audit-ready** — `audit_report` produces a self-contained HTML record (lists, versions, hashes, inputs, hits,
  explicit no-match statements) for the client file. Print it to PDF.

**$0.01 per name screened**, $0.25 per audit report, list status free. No AI, no third-party aggregators (OpenSanctions
is never used), nothing stored, names never logged.

## Who it is for

UAE DNFBPs and their software: real-estate brokers and developers, auditors and accountants, company-service providers,
dealers in precious metals and stones, lawyers and notaries, plus fintechs, exchange houses and any workflow that must
check names before onboarding or a transaction — and the AI agents that now do that work. It gives you the *screening*
step with a defensible record; your MLRO still decides.

## Three ways to use it

| | |
|---|---|
| **Run it** (Apify) | paste up to 5,000 names → one dataset row per name with `decision_hint`, score and top hits. |
| **Call it from an AI agent** | MCP server at `/mcp` with five tools — "screen this beneficial owner, DOB 1966, Iraqi" just works. |
| **REST** | `POST /v1/tools/screen_name {"name": "Qusay Saddam Hussein", "dob": "1966", "nationality": "IQ"}` |

## Tools

| tool | what | price |
|---|---|---|
| `screen_name` | one name (+ dob, nationality, entity_type, id_number, lists, min_score) → up to 20 scored hits + `decision_hint` | 1 name |
| `screen_batch` | up to 500 subjects, duplicates screened once, per-subject verdicts + summary counts | per unique name |
| `audit_report` | 1–50 subjects → self-contained HTML report for the AML file | 1 report |
| `list_status` | per list: counts, fetched-at, days since fetch, source URL, SHA-256, terms | **free** |
| `explain_match` | two names → every normalisation step and score component (try it with Arabic vs Latin) | **free** |

## Example

```json
// screen_name {"name": "Qusay Saddam Hussein", "dob": "1966", "nationality": "IQ", "entity_type": "individual", "lists": ["un", "uae"]}
{"result": {
  "normalised": {"text": "qusay saddam husayn", "script": "latin", "steps": ["NFKC + casefold", "romanisation variants: hussein→husayn"]},
  "hits": [{
    "list": "un", "list_title": "UN Security Council Consolidated List", "list_entry_id": "6908049",
    "listed_name": "QUSAY SADDAM HUSSEIN AL-TIKRITI", "entity_type": "individual",
    "score": 96.8, "match_strength": "strong",
    "matched_on": ["query name parts all present in a longer listed name", "romanisation variants folded: hussein→husayn",
                   "dob year match 1966", "nationality match IQ", "type match (individual)"],
    "programs": ["Iraq"], "listed_on": "2003-06-27", "dobs": ["1965", "1966"], "nationalities": ["IQ"],
    "reference": "IQi.002", "source_url": "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list"}],
  "hit_count": 1, "decision_hint": "likely_match", "billable_names": 1},
 "data_confidence": "high",
 "estimated_items": ["romanisation-variant folding (data/name_variants.yaml) is a heuristic table, not an authority"],
 "sources": ["https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list", "https://www.uaeiec.gov.ae/en-us/un-page?p=1"],
 "data_version": "2026.10.31-1", "warnings": [],
 "disclaimer": "Screening result, not a legal determination. A hit means a listed name is similar to the name you gave; verify ..."}
```

`decision_hint` is `clear` (nothing at or above `min_score`), `review` (possible hits) or `likely_match` (an exact or
strong hit). Same query with `dob: "1980"` → `clear`, because the listed DOBs are 1965/1966.

## How matching works (deterministic, no ML)

1. **Normalise** both sides: Unicode NFKC, casefold, diacritics; for Arabic remove tashkeel/tatweel and fold
   أإآ→ا, ة→ه, ى→ي, ؤ→و, ئ→ي and Persian/Urdu letter forms; strip `ال`/`al-`/`el-`, drop `bin/ibn/bint/ben/ould` and
   honorifics (Sheikh, Haji, Mullah, Dr…), split merged forms (`Abdulrahman` → `abd rahman`, `عبدالرحمن` → `عبد رحمن`),
   fold romanisation variants to one canonical token, drop legal suffixes (LLC, FZE, DMCC, GmbH…).
2. **Retrieve** candidates through inverted indexes on canonical tokens and consonant skeletons (IDF-weighted), so a
   query touches ~1,500 aliases, not 107,000.
3. **Score** with rapidfuzz `token_sort_ratio` / `token_set_ratio` and Jaro-Winkler on the canonical strings; subset
   matches (two given parts inside a four-part listed name) cap at 90; cross-script skeleton matches cap at 90 and are
   flagged as heuristic.
4. **Adjust** per entry: DOB (+5 exact, +3 year, +1 ±1 year, −15 mismatch), nationality (+3 / −5), entity type (−20),
   weak alias (−3), ID number exact match → 100.

Measured on the full corpus: p95 ≈ 30 ms per `screen_name` (common two-part Arabic names are the slow case), ~210 MB RSS.

## Honest limits

- **It screens names, it does not decide.** Every response carries a disclaimer; a hit must be verified against the
  source entry (`source_url`) and the customer's documents; a *clear* means "no match above your threshold in this
  snapshot", not "not sanctioned anywhere". Follow your AML/CFT obligations (UAE Federal Decree-Law 10/2025, Cabinet
  Resolution 74/2020) on confirmed matches.
- **Snapshot, not live.** Lists are refreshed by a weekly job (`screen refresh`) and bundled; `list_status` and the
  `warnings` field tell you the age. Between refreshes new designations are missing — run the refresh daily if your
  regulator expects that.
- **Heuristics are heuristics.** The romanisation table was compiled from common transliteration practice and the
  spellings in the lists; the cross-script skeleton is lossy. Both are reported in `estimated_items` and never produce
  an *exact* strength.
- **Near-spellings are "strong".** `Ahmad Alkhald` scores 96 against `Ahmed Khalid`; raise `min_score` to 92–95 when
  you only want names that are really the same.
- **Not covered:** PEP lists, adverse media, OFAC's sectoral directives beyond the names on the Consolidated list,
  GCC-national lists other than the UAE's (Saudi, Qatar and Bahrain publish no machine-readable public list), and
  ownership/control analysis (the 50 % rule). The UAE list covers the Cabinet's local designations; UN designations
  that the UAE adopts are screened through the UN list.

## Connect

- **Apify:** run from the Console (input form) or Standby URL + `/mcp` with `Authorization: Bearer <your Apify token>`.
- **Self-hosted:** `claude mcp add sanctions-screener --transport http https://screen.noahsarch.tech/mcp --header "X-API-Key: <key>"`
- **REST:** `POST https://screen.noahsarch.tech/v1/tools/screen_name`; OpenAPI at `/openapi.json`; `list_status` and
  `explain_match` need no key.

## Sources

[OFAC Sanctions List Service](https://sanctionslistservice.ofac.treas.gov/) (SDN_ADVANCED / CONS_ADVANCED XML) ·
[UN Security Council Consolidated List](https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list) ·
[EU Financial Sanctions File](https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions) ·
[UK Sanctions List](https://www.gov.uk/government/publications/the-uk-sanctions-list) ·
[UAE EOCN — Local Terrorist List](https://www.uaeiec.gov.ae/en-us/un-page?p=1). Built on
[rapidfuzz](https://github.com/rapidfuzz/RapidFuzz). Each `data/version.yaml` records the URL, fetch time and SHA-256 of
every file in the snapshot.

---

## بالعربية

**فاحص قوائم العقوبات والإرهاب لدول الخليج** — أدخل اسم شخص أو شركة بالعربية أو باللاتينية، ويفحصه فوراً في قوائم مكتب
مراقبة الأصول الأجنبية الأمريكي (OFAC)، ومجلس الأمن الدولي، والاتحاد الأوروبي، والمملكة المتحدة، **وقائمة الإرهاب المحلية
الإماراتية** الصادرة بقرارات مجلس الوزراء والمنشورة عبر المكتب التنفيذي للرقابة وحظر الانتشار. المطابقة حتمية ومُفسَّرة:
توحيد الحروف العربية وإزالة التشكيل، تجاهل الألقاب وأدوات النسب (الشيخ، الحاج، ال، بن، بنت)، معالجة اختلافات الكتابة اللاتينية
للأسماء العربية (محمد/Mohamed/Muhammad)، وترتيب أجزاء الاسم، مع ترجيح تاريخ الميلاد والجنسية ورقم الهوية. كل نتيجة تحمل درجة
من 0 إلى 100 وتفسيراً وقوة المطابقة ورابط المصدر الرسمي، ويمكن إصدار تقرير تدقيق HTML لملف مكافحة غسل الأموال.
مناسب لوسطاء العقارات ومدققي الحسابات ومزودي خدمات الشركات وتجار المعادن والأحجار الكريمة والمحامين (DNFBPs) في الإمارات.
0.01 دولار لكل اسم. لا ذكاء اصطناعي، لا تخزين، ولا تسجيل للأسماء. النتيجة أداة فحص وليست قراراً قانونياً؛ تحقّق من أي مطابقة
مع المصدر والتزم بواجباتك في مكافحة غسل الأموال وتمويل الإرهاب.
