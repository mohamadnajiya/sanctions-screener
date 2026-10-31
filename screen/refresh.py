"""Refresh job: download the official lists, normalise them into compact JSONL under data/lists/ and rewrite
data/version.yaml (source URL, fetched-at UTC, SHA-256, counts per list).

This module is the ONLY place in the package that touches the network, and it is only run by `screen refresh`
(or scripts/fetch_lists.py) — never at request time. Each list is parsed independently; a list that fails to
download or parse is reported and the previously bundled snapshot of that list is kept.

XML parsing uses the stdlib ElementTree (ruff S314 is ignored for this module in pyproject): the inputs are the
official government files fetched over HTTPS by this job, CPython 3.12 bundles an Expat with billion-laughs/quadratic-blowup
protection, and no DTD/external entities are resolved. Nothing here runs on the request path.

Record shape (one JSON object per line, keys kept short because ~35k of them live in memory):
  id        "<list>:<entry id>"           list      ofac_sdn | ofac_cons | un | eu | uk | uae
  type      individual | entity | vessel | aircraft | unknown
  name      primary (Latin where available) listed name
  aliases   [[text, weak], ...]  weak=1 for low-quality / "weak" aliases (never upgrade a hit to 'exact')
  dobs      ["1948-12-10", "1971", "1962/1965", ...]      nats  ["IQ", ...] ISO2 (or verbatim if unmapped)
  ids       [[doc type, number], ...]     programs  ["SDGT", ...]     listed  "YYYY-MM-DD" or null
  url       source page for this entry (or the list page)     ref  UN/EU/UK reference number or resolution
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml

USER_AGENT = "NoahsArch-SanctionsScreener/1.0 (+https://noahsarch.tech; list refresh job)"
TIMEOUT_S = 300
MAX_BYTES = 400 * 1024 * 1024

SOURCES: dict[str, dict[str, str]] = {
    "ofac_sdn": {
        "title": "OFAC Specially Designated Nationals (SDN) List",
        "url": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML",
        "page": "https://ofac.treasury.gov/specially-designated-nationals-and-blocked-persons-list-sdn-human-readable-lists",
        "entry_url": "https://sanctionssearch.ofac.treas.gov/Details.aspx?id={id}",
        "authority": "U.S. Department of the Treasury, Office of Foreign Assets Control",
        "terms": "U.S. Government work; public domain (17 U.S.C. §105). Attribution to OFAC requested.",
    },
    "ofac_cons": {
        "title": "OFAC Consolidated (non-SDN) Sanctions List",
        "url": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_ADVANCED.XML",
        "page": "https://ofac.treasury.gov/consolidated-sanctions-list-non-sdn-lists",
        "entry_url": "https://sanctionssearch.ofac.treas.gov/Details.aspx?id={id}",
        "authority": "U.S. Department of the Treasury, Office of Foreign Assets Control",
        "terms": "U.S. Government work; public domain (17 U.S.C. §105).",
    },
    "un": {
        "title": "UN Security Council Consolidated List",
        "url": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
        "page": "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list",
        "entry_url": "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list",
        "authority": "United Nations Security Council",
        "terms": "Published by the UN for implementation by all Member States; UN terms of use (https://www.un.org/en/about-us/terms-of-use).",
    },
    "eu": {
        "title": "EU Consolidated Financial Sanctions List",
        "url": "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
        "page": "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions",
        "entry_url": "",
        "authority": "European Commission, DG FISMA (Financial Sanctions Database)",
        "terms": "Commission reuse policy (Decision 2011/833/EU) — free reuse with attribution; public token 'dG9rZW4tMjAxNw' documented on data.europa.eu.",
    },
    "uk": {
        "title": "UK Sanctions List (FCDO; single UK list since 28 Jan 2026, replaces the OFSI Consolidated List)",
        "url": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml",
        "page": "https://www.gov.uk/government/publications/the-uk-sanctions-list",
        "entry_url": "https://search-uk-sanctions-list.service.gov.uk/",
        "authority": "UK Foreign, Commonwealth & Development Office (OFSI consolidated list retired 28 January 2026)",
        "terms": "Open Government Licence v3.0 (attribution).",
    },
    "uae": {
        "title": "UAE Local Terrorist List (Cabinet designations under Cabinet Resolution 74/2020, UNSCR 1373)",
        "url": "https://www.uaeiec.gov.ae/API/Upload/DownloadFile?FileID=0433bfdb-8a3d-44db-9015-90cbbf48f6f6",
        "page": "https://www.uaeiec.gov.ae/en-us/un-page?p=1",
        "entry_url": "https://www.uaeiec.gov.ae/en-us/un-page?p=1",
        "authority": "UAE Executive Office for Control and Non-Proliferation (EOCN), publishing UAE Cabinet resolutions",
        "terms": "Official public disclosure list (EOCN website terms: https://www.uaeiec.gov.ae/en-us/terms-and-conditions). Published as an .xls workbook (Excel link on the list page).",
    },
}

# ---------------------------------------------------------------- helpers


def _countries() -> dict[str, str]:
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "data" / "countries.yaml", here / "_data" / "countries.yaml"):
        if cand.exists():
            return yaml.safe_load(cand.read_text(encoding="utf-8"))["countries"]
    return {}


_C = _countries()
_C_LOWER = {k.casefold(): v for k, v in _C.items()}


def iso2(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().strip(".").strip()
    if not n or n in ("-", "na", "n/a"):
        return None
    if len(n) == 2 and n.isupper():
        return n
    hit = _C.get(n) or _C_LOWER.get(n.casefold())
    if hit:
        return hit
    # "lebanese / قطري"-style adjectives or compound values: try each part
    # compound values ("Saudi Arabia / Kuwait", "الحالية: السويد السابقة: ليبيريا" = current: Sweden, former: Liberia):
    # take the first country named, by position in the string
    found: list[tuple[int, str]] = []
    for cand in re.split(r"[/,;:]| - ", n) + n.split():
        c = cand.strip()
        if c and (hit := _C.get(c) or _C_LOWER.get(c.casefold())):
            found.append((n.find(c), hit))
    if found:
        return min(found)[1]
    return n


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()


def download(url: str, dest: Path) -> tuple[int, str]:
    """Stream `url` to `dest`; return (bytes, sha256). Honors HTTPS_PROXY / SSL_CERT_FILE from the environment."""
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https source {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})  # noqa: S310 - scheme checked above
    h = hashlib.sha256()
    n = 0
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r, dest.open("wb") as f:  # noqa: S310 - https only, SOURCES table
        while chunk := r.read(1 << 20):
            n += len(chunk)
            if n > MAX_BYTES:
                raise RuntimeError(f"{url}: larger than {MAX_BYTES} bytes")
            h.update(chunk)
            f.write(chunk)
    return n, h.hexdigest()


def _rec(
    list_key: str,
    entry_id: str,
    typ: str,
    name: str,
    aliases: Iterable[tuple[str, int]],
    *,
    dobs: Iterable[str] = (),
    nats: Iterable[str | None] = (),
    ids: Iterable[tuple[str, str]] = (),
    programs: Iterable[str] = (),
    listed: str | None = None,
    url: str = "",
    ref: str | None = None,
) -> dict[str, Any] | None:
    name = _clean(name)
    seen = {name.casefold()} if name else set()
    al: list[list[Any]] = []
    for text, weak in aliases:
        t = _clean(text)
        if not t or t.casefold() in seen:
            continue
        seen.add(t.casefold())
        al.append([t, 1 if weak else 0])
    if not name:
        if not al:
            return None
        name = al[0][0]
        al = al[1:]
    d = sorted({x for x in (_clean(x) for x in dobs) if x})
    nn = sorted({x for x in (iso2(x) for x in nats) if x})
    ii = []
    seen_ids = set()
    for typ_, num in ids:
        num = _clean(num)
        if num and num.casefold() not in seen_ids and len(num) <= 64:
            seen_ids.add(num.casefold())
            ii.append([_clean(typ_)[:40], num])
    pr = sorted({x for x in (_clean(x) for x in programs) if x})
    rec: dict[str, Any] = {"id": f"{list_key}:{entry_id}", "list": list_key, "type": typ, "name": name, "aliases": al}
    if d:
        rec["dobs"] = d
    if nn:
        rec["nats"] = nn
    if ii:
        rec["ids"] = ii[:20]
    if pr:
        rec["programs"] = pr
    if listed:
        rec["listed"] = listed
    if url:
        rec["url"] = url
    if ref:
        rec["ref"] = _clean(ref)[:120]
    return rec


# ---------------------------------------------------------------- OFAC advanced XML (SDN and Consolidated)

_NS = "{https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML}"
_NAME_ORDER = {
    "First Name": 0,
    "Middle Name": 1,
    "Patronymic": 2,
    "Matronymic": 3,
    "Last Name": 4,
    "Maiden Name": 5,
    "Nickname": 6,
}
_PARTY_TYPE = {"1": "vessel", "2": "aircraft", "3": "entity", "4": "individual"}


def _t(tag: str) -> str:
    return _NS + tag


def _ymd(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    y, m, d = (el.findtext(_t(k)) for k in ("Year", "Month", "Day"))
    if not y:
        return None
    if m and d:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    if m:
        return f"{int(y):04d}-{int(m):02d}"
    return f"{int(y):04d}"


def _date_period(dp: ET.Element) -> str | None:
    start, end = dp.find(_t("Start")), dp.find(_t("End"))
    a = _ymd(start.find(_t("From"))) if start is not None else None
    b = _ymd(end.find(_t("To"))) if end is not None else None
    if a and b and a != b:
        return f"{a[:4]}/{b[:4]}" if a[:4] != b[:4] else a[:4]
    return a or b


def parse_ofac(path: Path, list_key: str) -> Iterator[dict[str, Any]]:
    countries: dict[str, str] = {}
    feature_types: dict[str, str] = {}
    name_part_types: dict[str, str] = {}
    doc_types: dict[str, str] = {}
    list_names: dict[str, str] = {}
    locations: dict[str, str] = {}  # LocationID -> country ISO2
    docs: dict[str, list[tuple[str, str]]] = {}  # IdentityID -> [(type, number)]
    entries: dict[str, dict[str, Any]] = {}  # ProfileID -> {programs, listed, list}
    entry_url = SOURCES[list_key]["entry_url"]

    # pass 1: reference tables, locations, ID documents and sanctions entries (everything except the parties)
    for _ev, el in ET.iterparse(str(path), events=("end",)):
        tag = el.tag
        if tag == _t("Country"):
            countries[el.get("ID", "")] = el.get("ISO2") or (el.text or "")
        elif tag == _t("FeatureType"):
            feature_types[el.get("ID", "")] = el.text or ""
        elif tag == _t("NamePartType"):
            name_part_types[el.get("ID", "")] = el.text or ""
        elif tag == _t("IDRegDocType"):
            doc_types[el.get("ID", "")] = el.text or ""
        elif tag == _t("List"):
            list_names[el.get("ID", "")] = el.text or ""
        elif tag == _t("Location"):
            lc = el.find(_t("LocationCountry"))
            if lc is not None:
                locations[el.get("ID", "")] = countries.get(lc.get("CountryID", ""), "")
            el.clear()
        elif tag == _t("IDRegDocument"):
            num = el.findtext(_t("IDRegistrationNo"))
            if num:
                docs.setdefault(el.get("IdentityID", ""), []).append(
                    (doc_types.get(el.get("IDRegDocTypeID", ""), "ID"), num)
                )
            el.clear()
        elif tag == _t("SanctionsEntry"):
            progs = [
                _clean(m.findtext(_t("Comment")))
                for m in el.findall(_t("SanctionsMeasure"))
                if m.get("SanctionsTypeID") == "1" and m.findtext(_t("Comment"))
            ]
            ev = el.find(_t("EntryEvent"))
            listed = _ymd(ev.find(_t("Date"))) if ev is not None else None
            entries[el.get("ProfileID", "")] = {
                "programs": progs,
                "listed": listed,
                "list": list_names.get(el.get("ListID", ""), ""),
            }
            el.clear()
        elif tag in (
            _t("DistinctParty"),
            _t("ReferenceValueSets"),
            _t("Locations"),
            _t("IDRegDocuments"),
            _t("SanctionsEntries"),
        ):
            el.clear()

    # pass 2: stream the parties (keeps peak memory around 100 MB for the 130 MB SDN file)
    def parties() -> Iterator[ET.Element]:
        for _ev, el in ET.iterparse(str(path), events=("end",)):
            if el.tag == _t("DistinctParty"):
                yield el
            elif el.tag in (_t("ReferenceValueSets"), _t("Locations"), _t("IDRegDocuments"), _t("SanctionsEntries")):
                el.clear()

    for party in parties():
        prof = party.find(_t("Profile"))
        if prof is None:
            continue
        pid = prof.get("ID", "")
        typ = _PARTY_TYPE.get(prof.get("PartySubTypeID", ""), "unknown")
        name = ""
        aliases: list[tuple[str, int]] = []
        ids: list[tuple[str, str]] = []
        for ident in prof.findall(_t("Identity")):
            group_types = {
                g.get("ID", ""): name_part_types.get(g.get("NamePartTypeID", ""), "")
                for mg in ident.findall(f"{_t('NamePartGroups')}/{_t('MasterNamePartGroup')}")
                for g in mg.findall(_t("NamePartGroup"))
            }
            for alias in ident.findall(_t("Alias")):
                weak = 1 if alias.get("LowQuality") == "true" else 0
                primary_alias = alias.get("Primary") == "true"
                for dn in alias.findall(_t("DocumentedName")):
                    parts = []
                    for p in dn.findall(_t("DocumentedNamePart")):
                        v = p.find(_t("NamePartValue"))
                        if v is None or not v.text:
                            continue
                        order = _NAME_ORDER.get(group_types.get(v.get("NamePartGroupID", ""), ""), 9)
                        parts.append((order, v.text))
                    parts.sort(key=lambda x: x[0])
                    full = _clean(" ".join(t for _, t in parts))
                    if not full:
                        continue
                    if primary_alias and dn.get("DocNameStatusID") == "1" and not name:
                        name = full
                    else:
                        aliases.append((full, weak))
            if ident.get("Primary") == "true":
                ids.extend(docs.get(ident.get("ID", ""), []))
        dobs: list[str] = []
        nats: list[str] = []
        for feat in prof.findall(_t("Feature")):
            ft = feature_types.get(feat.get("FeatureTypeID", ""), "")
            for fv in feat.findall(_t("FeatureVersion")):
                if ft == "Birthdate":
                    dp = fv.find(_t("DatePeriod"))
                    if dp is not None and (d := _date_period(dp)):
                        dobs.append(d)
                elif ft in ("Nationality Country", "Citizenship Country", "Nationality of Registration"):
                    loc = fv.find(_t("VersionLocation"))
                    if loc is not None and (c := locations.get(loc.get("LocationID", ""))):
                        nats.append(c)
        meta = entries.get(pid, {})
        programs = list(meta.get("programs", []))
        sub = meta.get("list", "")
        if list_key == "ofac_cons" and sub:
            programs.append(f"[{sub}]")
        rec = _rec(
            list_key,
            pid,
            typ,
            name,
            aliases,
            dobs=dobs,
            nats=nats,
            ids=ids,
            programs=programs,
            listed=meta.get("listed"),
            url=entry_url.format(id=pid),
        )
        if rec:
            yield rec
        party.clear()


# ---------------------------------------------------------------- UN consolidated list


def _un_dob(el: ET.Element) -> str | None:
    kind = (el.findtext("TYPE_OF_DATE") or "").upper()
    date, year = el.findtext("DATE"), el.findtext("YEAR")
    fy, ty = el.findtext("FROM_YEAR"), el.findtext("TO_YEAR")
    if kind == "BETWEEN" and fy and ty:
        return f"{fy}/{ty}"
    if date:
        return date[:10]
    if year:
        return year
    return None


def parse_un(path: Path) -> Iterator[dict[str, Any]]:
    root = ET.parse(str(path)).getroot()  # noqa: S314 - official UN file we just downloaded, no DTD/entities
    url = SOURCES["un"]["entry_url"]
    for kind, typ in (("INDIVIDUALS/INDIVIDUAL", "individual"), ("ENTITIES/ENTITY", "entity")):
        for el in root.findall(kind):
            name = " ".join(
                _clean(el.findtext(k))
                for k in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")
                if el.findtext(k)
            )
            aliases: list[tuple[str, int]] = []
            if orig := el.findtext("NAME_ORIGINAL_SCRIPT"):
                aliases.append((orig, 0))
            for a in el.findall(f"{typ.upper()}_ALIAS"):
                text = a.findtext("ALIAS_NAME")
                if not text:
                    continue
                weak = 1 if (a.findtext("QUALITY") or "").lower().startswith("low") else 0
                for piece in re.split(r";|\ba\.k\.a\.\s", text):
                    if _clean(piece):
                        aliases.append((piece, weak))
            dobs = [d for d in (_un_dob(x) for x in el.findall("INDIVIDUAL_DATE_OF_BIRTH")) if d]
            nats = [v.text for v in el.findall("NATIONALITY/VALUE") if v.text]
            ids = []
            for doc in el.findall("INDIVIDUAL_DOCUMENT"):
                num = doc.findtext("NUMBER")
                if num:
                    ids.append((doc.findtext("TYPE_OF_DOCUMENT") or "ID", num))
            rec = _rec(
                "un",
                el.findtext("DATAID") or "",
                typ,
                name,
                aliases,
                dobs=dobs,
                nats=nats,
                ids=ids,
                programs=[el.findtext("UN_LIST_TYPE") or ""],
                listed=(el.findtext("LISTED_ON") or "")[:10] or None,
                url=url,
                ref=el.findtext("REFERENCE_NUMBER"),
            )
            if rec:
                yield rec


# ---------------------------------------------------------------- EU financial sanctions file

_EU = "{http://eu.europa.ec/fpi/fsd/export}"


def parse_eu(path: Path) -> Iterator[dict[str, Any]]:
    for _ev, el in ET.iterparse(str(path), events=("end",)):
        if el.tag != _EU + "sanctionEntity":
            continue
        st = el.find(_EU + "subjectType")
        typ = {"person": "individual", "enterprise": "entity"}.get(
            st.get("code", "") if st is not None else "", "unknown"
        )
        names = el.findall(_EU + "nameAlias")
        name = ""
        aliases: list[tuple[str, int]] = []
        for na in names:
            whole = _clean(
                na.get("wholeName") or " ".join(na.get(k) or "" for k in ("firstName", "middleName", "lastName"))
            )
            if not whole:
                continue
            weak = 0 if na.get("strong", "true") == "true" else 1
            if not name and not weak:
                name = whole
            else:
                aliases.append((whole, weak))
        dobs = []
        for b in el.findall(_EU + "birthdate"):
            if b.get("birthdate"):
                dobs.append(b.get("birthdate")[:10])
            elif b.get("year"):
                dobs.append(b.get("year"))
        nats = [
            c.get("countryIso2Code")
            for c in el.findall(_EU + "citizenship")
            if c.get("countryIso2Code") not in (None, "", "00")
        ]
        ids = [
            (i.get("identificationTypeDescription") or i.get("identificationTypeCode") or "ID", i.get("number"))
            for i in el.findall(_EU + "identification")
            if i.get("number")
        ]
        regs = el.findall(_EU + "regulation")
        programs = [r.get("programme") for r in regs if r.get("programme")]
        dates = sorted(r.get("publicationDate") for r in regs if r.get("publicationDate"))
        url = ""
        for r in regs:
            if (u := r.findtext(_EU + "publicationUrl")) and u.startswith("http"):
                url = u
                break
        ref = el.get("euReferenceNumber") or ""
        if el.get("unitedNationId"):
            ref += f" (UN {el.get('unitedNationId')})"
        rec = _rec(
            "eu",
            el.get("logicalId", ""),
            typ,
            name,
            aliases,
            dobs=dobs,
            nats=nats,
            ids=ids,
            programs=programs,
            listed=dates[0] if dates else None,
            url=url or SOURCES["eu"]["page"],
            ref=ref,
        )
        el.clear()
        if rec:
            yield rec


# ---------------------------------------------------------------- UK sanctions list


def _uk_date(s: str | None) -> str | None:
    s = _clean(s)
    m = re.fullmatch(r"(\d\d|dd)/(\d\d|mm)/(\d{4})", s)
    if not m:
        return s or None
    d, mo, y = m.groups()
    if d == "dd" or mo == "mm":
        return f"{y}-{mo}" if mo != "mm" else y
    return f"{y}-{mo}-{d}"


def parse_uk(path: Path) -> Iterator[dict[str, Any]]:
    for _ev, el in ET.iterparse(str(path), events=("end",)):
        if el.tag != "Designation":
            continue
        typ = {"Individual": "individual", "Entity": "entity", "Ship": "vessel"}.get(
            el.findtext("IndividualEntityShip") or "", "unknown"
        )
        name = ""
        aliases: list[tuple[str, int]] = []
        for n in el.findall("Names/Name"):
            full = _clean(" ".join(n.findtext(f"Name{i}") or "" for i in range(1, 7)))
            if not full:
                continue
            weak = 1 if (n.findtext("AliasStrength") or "").lower() == "weak" else 0
            if (n.findtext("NameType") or "") == "Primary Name" and not name:
                name = full
            else:
                aliases.append((full, weak))
        for n in el.findall("NonLatinNames/NonLatinName"):
            if t := n.findtext("NameNonLatinScript"):
                aliases.append((t, 0))
        dobs = [d for d in (_uk_date(x.text) for x in el.findall("IndividualDetails/Individual/DOBs/DOB")) if d]
        nats = [x.text for x in el.findall("IndividualDetails/Individual/Nationalities/Nationality") if x.text]
        ids: list[tuple[str, str]] = []
        for x in el.findall("IndividualDetails/Individual/PassportDetails/Passport/PassportNumber"):
            if x.text:
                ids.append(("Passport", x.text))
        for x in el.findall(
            "IndividualDetails/Individual/NationalIdentifierDetails/NationalIdentifier/NationalIdentifierNumber"
        ):
            if x.text:
                ids.append(("National ID", x.text))
        for x in el.findall("EntityDetails/Entity/BusinessRegistrationNumbers/BusinessRegistrationNumber"):
            if x.text:
                ids.append(("Business registration", x.text))
        for x in el.findall("ShipDetails/Ship/IMONumbers/IMONumber"):
            if x.text:
                ids.append(("IMO", x.text))
        programs = [el.findtext("RegimeName") or ""]
        if src := el.findtext("DesignationSource"):
            programs.append(f"source: {src}")
        uid = el.findtext("UniqueID") or ""
        rec = _rec(
            "uk",
            uid,
            typ,
            name,
            aliases,
            dobs=dobs,
            nats=nats,
            ids=ids,
            programs=programs,
            listed=_uk_date(el.findtext("DateDesignated")),
            url=SOURCES["uk"]["entry_url"],
            ref=f"{uid}" + (f" (UN {u})" if (u := el.findtext("UNReferenceNumber")) else ""),
        )
        el.clear()
        if rec:
            yield rec


# ---------------------------------------------------------------- UAE Local Terrorist List (.xls)

_UAE_RES = re.compile(r"رقم\s*\(?\s*(\d+)\s*\)?\s*لسنة\s*(\d{4})")


def _uae_ref(text: str) -> str:
    m = _UAE_RES.search(text or "")
    return f"UAE Cabinet Resolution No. {m.group(1)} of {m.group(2)}" if m else _clean(text)


def _uae_dob(v: Any, wb: Any) -> str | None:
    if isinstance(v, float | int):
        if 1800 < v < 3000:
            return f"{int(v):04d}"
        if v > 3000:
            try:
                import xlrd

                return xlrd.xldate.xldate_as_datetime(v, wb.datemode).date().isoformat()
            except Exception:  # noqa: BLE001
                return None
        return None
    s = _clean(str(v))
    if re.fullmatch(r"\d{4}", s):
        return s
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _cell(sh: Any, r: int, c: int) -> str:
    v = sh.cell_value(r, c) if c < sh.ncols else ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = _clean(str(v))
    return "" if s in ("-", "–", "—", "nan") else s


def parse_uae(path: Path) -> Iterator[dict[str, Any]]:
    import xlrd  # noqa: PLC0415 - refresh-only dependency

    wb = xlrd.open_workbook(str(path))
    url = SOURCES["uae"]["entry_url"]
    sheets = {s.name: s for s in wb.sheets()}

    def find(*keys: str) -> Any:
        for name, sh in sheets.items():
            if all(k in name for k in keys):
                return sh
        raise RuntimeError(f"UAE workbook: sheet with {keys} not found; sheets = {list(sheets)}")

    ind = find("الأفراد")
    orgs = find("التنظيمات")
    ents = find("الكيانات")
    if any("رفع" in s.name for s in (ind, orgs, ents)):
        raise RuntimeError("UAE workbook: picked a de-listing sheet by mistake")

    def header_row(sh: Any) -> int:
        for r in range(min(sh.nrows, 8)):
            if _cell(sh, r, 0) == "#":
                return r
        raise RuntimeError(f"UAE workbook: header row not found in sheet {sh.name}")

    # individuals: # | class | nationality | family AR | family LAT | full AR | full LAT | DOB | POB | known name | street | city | country | doc type | doc no | issuer | issued | expires | other
    h = header_row(ind)
    for r in range(h + 1, ind.nrows):
        num = _cell(ind, r, 0)
        if not num.isdigit():
            continue
        full_lat, full_ar, known = _cell(ind, r, 6), _cell(ind, r, 5), _cell(ind, r, 9)
        aliases = [(full_ar, 0), (known, 0)]
        dob = _uae_dob(ind.cell_value(r, 7), wb)
        ids = [(_cell(ind, r, 13) or "Document", _cell(ind, r, 14))] if _cell(ind, r, 14) else []
        other = _cell(ind, r, 18)
        rec = _rec(
            "uae",
            f"i{num}",
            "individual",
            full_lat or full_ar,
            aliases,
            dobs=[dob] if dob else [],
            nats=[_cell(ind, r, 2)],
            ids=ids,
            programs=["UAE Local Terrorist List (individuals)"],
            url=url,
            ref=_uae_ref(other),
        )
        if rec:
            yield rec
    # organisations: # | class | full LAT | known AR | other
    h = header_row(orgs)
    for r in range(h + 1, orgs.nrows):
        num = _cell(orgs, r, 0)
        if not num.isdigit():
            continue
        rec = _rec(
            "uae",
            f"o{num}",
            "entity",
            _cell(orgs, r, 2) or _cell(orgs, r, 3),
            [(_cell(orgs, r, 3), 0)],
            programs=["UAE Local Terrorist List (organisations)"],
            url=url,
            ref=_uae_ref(_cell(orgs, r, 4)),
        )
        if rec:
            yield rec
    # entities: # | class | full AR | full LAT | known AR | licence no | licence exp | HQ | notes | other
    h = header_row(ents)
    for r in range(h + 1, ents.nrows):
        num = _cell(ents, r, 0)
        if not num.isdigit():
            continue
        lic = _cell(ents, r, 5)
        rec = _rec(
            "uae",
            f"e{num}",
            "entity",
            _cell(ents, r, 3) or _cell(ents, r, 2) or _cell(ents, r, 4),
            [(_cell(ents, r, 2), 0), (_cell(ents, r, 4), 0)],
            ids=[("Licence", lic)] if lic else [],
            programs=["UAE Local Terrorist List (entities)"],
            url=url,
            ref=_uae_ref(_cell(ents, r, 9)),
        )
        if rec:
            yield rec


PARSERS = {
    "ofac_sdn": lambda p: parse_ofac(p, "ofac_sdn"),
    "ofac_cons": lambda p: parse_ofac(p, "ofac_cons"),
    "un": parse_un,
    "eu": parse_eu,
    "uk": parse_uk,
    "uae": parse_uae,
}

# ---------------------------------------------------------------- driver


def _counts(recs: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {"entries": len(recs), "aliases": sum(len(r["aliases"]) for r in recs)}
    for r in recs:
        out[r["type"]] = out.get(r["type"], 0) + 1
    return out


def refresh(
    data_dir: Path, lists: Iterable[str] | None = None, *, keep_raw: Path | None = None, log=print
) -> dict[str, Any]:
    """Download + parse each list into data_dir/lists/<key>.jsonl and rewrite data_dir/version.yaml.
    Returns the new version document. Lists that fail keep their previous file and are reported under `failed`."""
    lists_dir = data_dir / "lists"
    lists_dir.mkdir(parents=True, exist_ok=True)
    vpath = data_dir / "version.yaml"
    old = yaml.safe_load(vpath.read_text(encoding="utf-8")) if vpath.exists() else {}
    old_lists = dict(old.get("lists") or {})
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    new_lists: dict[str, Any] = {}
    failed: dict[str, str] = {}
    keys = list(lists) if lists else list(SOURCES)
    with tempfile.TemporaryDirectory(prefix="screen-refresh-") as tmp:
        for key in keys:
            src = SOURCES[key]
            raw = Path(tmp) / f"{key}.raw"
            try:
                log(f"[{key}] downloading {src['url']}")
                nbytes, sha = download(src["url"], raw)
                log(f"[{key}] {nbytes:,} bytes, sha256 {sha[:16]}…; parsing")
                recs = list(PARSERS[key](raw))
                if len(recs) < 10:
                    raise RuntimeError(f"only {len(recs)} entries parsed — refusing to overwrite the bundled list")
                out = lists_dir / f"{key}.jsonl"
                with out.open("w", encoding="utf-8") as f:
                    for r in recs:
                        f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
                if keep_raw:
                    keep_raw.mkdir(parents=True, exist_ok=True)
                    raw.replace(keep_raw / f"{key}{'.xls' if key == 'uae' else '.xml'}")
                new_lists[key] = {
                    "title": src["title"],
                    "authority": src["authority"],
                    "source_url": src["url"],
                    "page_url": src["page"],
                    "fetched_at": now.isoformat().replace("+00:00", "Z"),
                    "sha256": sha,
                    "bytes": nbytes,
                    "counts": _counts(recs),
                    "terms": src["terms"],
                }
                log(f"[{key}] {new_lists[key]['counts']}")
            except Exception as e:  # noqa: BLE001 - report and keep going with the other lists
                failed[key] = f"{type(e).__name__}: {e}"
                log(f"[{key}] FAILED: {failed[key]}")
                if key in old_lists:
                    new_lists[key] = old_lists[key]
        for key, meta in old_lists.items():
            new_lists.setdefault(key, meta)
    doc = {
        "data_version": now.strftime("%Y.%m.%d") + "-1",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "lists": {k: new_lists[k] for k in SOURCES if k in new_lists},
        "failed": failed,
        "notes": "Official public lists only; OpenSanctions and other aggregators are never used. "
        "Re-run `screen refresh` (weekly cron) to update; the request path never fetches.",
    }
    if old.get("data_version", "").startswith(now.strftime("%Y.%m.%d")):
        n = int(old["data_version"].rsplit("-", 1)[-1]) + 1
        doc["data_version"] = f"{now.strftime('%Y.%m.%d')}-{n}"
    vpath.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=160), encoding="utf-8")
    return doc


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Download the official sanctions lists into data/lists/*.jsonl")
    ap.add_argument(
        "--data-dir", default=os.environ.get("SCREEN_DATA_DIR") or str(Path(__file__).resolve().parents[1] / "data")
    )
    ap.add_argument("--only", nargs="*", choices=list(SOURCES), help="refresh only these lists")
    ap.add_argument("--keep-raw", help="directory to keep the raw downloads in (default: discard)")
    ns = ap.parse_args(argv)
    doc = refresh(Path(ns.data_dir), ns.only, keep_raw=Path(ns.keep_raw) if ns.keep_raw else None)
    print(json.dumps({"data_version": doc["data_version"], "failed": doc["failed"]}, ensure_ascii=False))
    return 1 if doc["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
