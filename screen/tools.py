"""Tool catalogue — single source of truth for names, agent-facing descriptions, schemas, handlers and billing."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentkit import AgentInfo, Catalogue, ToolSpec

from . import engine
from .matcher import LIST_KEYS

try:
    CODE_VERSION = version("sanctions-screener")
except PackageNotFoundError:
    CODE_VERSION = "0.0.0"

NAME_SCREENED = "name-screened"  # $0.01 per name screened
AUDIT_REPORT = "audit-report"  # $0.25 per report
LIST_STATUS = "list-status"  # free

NOTE = (
    " Response: result plus data_confidence, estimated_items[], sources[] (official list pages), data_version (snapshot id), "
    "warnings[] (e.g. a list snapshot older than 7 days) and a disclaimer. Lists: ofac_sdn, ofac_cons, un, eu, uk, uae. "
    "Names are never logged; matching is deterministic (no AI) and runs offline against the bundled snapshot."
)
LISTS_DESC = "Subset of lists to screen (default all): ofac_sdn, ofac_cons, un, eu, uk, uae."
DOB_DESC = "Date of birth 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Year match adds, mismatch subtracts; omit if unknown."
NAT_DESC = "Nationality / country of incorporation as ISO 3166-1 alpha-2 ('AE', 'IR', 'GB'). Omit if unknown."
TYPE_DESC = (
    "individual | entity | vessel | aircraft | any (default). Disagreement with the listed type costs 20 points."
)
ID_DESC = "Passport / national ID / registration number. An exact match on a listed number is reported as 'exact' (score 100)."
MIN_DESC = (
    "Minimum score (0–100) for a hit to be returned; default 85. 80 widens the net for onboarding, 92 for alert review."
)
Lists = list[Literal["ofac_sdn", "ofac_cons", "un", "eu", "uk", "uae"]]
EntityType = Literal["individual", "entity", "vessel", "aircraft", "any"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_dob(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    import re

    if not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", v):
        raise ValueError("dob must be YYYY, YYYY-MM or YYYY-MM-DD")
    if not 1850 <= int(v[:4]) <= 2100:
        raise ValueError("dob year out of range")
    return v


def _check_nat(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    if len(v) != 2 or not v.isalpha():
        raise ValueError("nationality must be an ISO 3166-1 alpha-2 code")
    return v.upper()


class Subject(_Base):
    name: str = Field(..., min_length=1, max_length=300, description="Full name, Latin or Arabic script.")
    dob: str | None = Field(None, max_length=10, description=DOB_DESC)
    nationality: str | None = Field(None, max_length=2, description=NAT_DESC)
    entity_type: EntityType = Field("any", description=TYPE_DESC)
    id_number: str | None = Field(None, max_length=64, description=ID_DESC)
    reference: str | None = Field(
        None, max_length=120, description="Your own row id / customer reference, echoed back."
    )

    _dob = field_validator("dob")(classmethod(lambda cls, v: _check_dob(v)))
    _nat = field_validator("nationality")(classmethod(lambda cls, v: _check_nat(v)))


class ScreenIn(_Base):
    name: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="Full name to screen, Latin or Arabic script (e.g. 'Mohamed Al Rashid' or 'محمد الراشد').",
    )
    dob: str | None = Field(None, max_length=10, description=DOB_DESC)
    nationality: str | None = Field(None, max_length=2, description=NAT_DESC)
    entity_type: EntityType = Field("any", description=TYPE_DESC)
    id_number: str | None = Field(None, max_length=64, description=ID_DESC)
    lists: Lists | None = Field(None, max_length=6, description=LISTS_DESC)
    min_score: float = Field(85, ge=0, le=100, description=MIN_DESC)

    _dob = field_validator("dob")(classmethod(lambda cls, v: _check_dob(v)))
    _nat = field_validator("nationality")(classmethod(lambda cls, v: _check_nat(v)))


class BatchIn(_Base):
    subjects: list[Subject] = Field(
        ...,
        min_length=1,
        max_length=engine.MAX_BATCH,
        description=f"Up to {engine.MAX_BATCH} subjects (name + optional dob / nationality / entity_type / id_number / reference). Identical rows are screened once.",
    )
    lists: Lists | None = Field(None, max_length=6, description=LISTS_DESC)
    min_score: float = Field(85, ge=0, le=100, description=MIN_DESC)
    default_nationality: str | None = Field(
        None, max_length=2, description="ISO2 applied to rows without a nationality."
    )

    _nat = field_validator("default_nationality")(classmethod(lambda cls, v: _check_nat(v)))


class AuditIn(_Base):
    subjects: list[Subject] = Field(
        ...,
        min_length=1,
        max_length=engine.MAX_AUDIT,
        description=f"1 to {engine.MAX_AUDIT} subjects to include in one report.",
    )
    lists: Lists | None = Field(None, max_length=6, description=LISTS_DESC)
    min_score: float = Field(85, ge=0, le=100, description=MIN_DESC)
    prepared_by: str | None = Field(
        None, max_length=120, description="Name / role of the compliance officer, printed on the report."
    )
    case_reference: str | None = Field(
        None, max_length=120, description="Your case / client file reference, printed on the report."
    )


class StatusIn(_Base):
    pass


class ExplainIn(_Base):
    name_a: str = Field(..., min_length=1, max_length=300, description="First name to compare (Latin or Arabic).")
    name_b: str = Field(..., min_length=1, max_length=300, description="Second name to compare (Latin or Arabic).")


INFO = AgentInfo(
    name="sanctions-screener",
    title="GCC Sanctions & Terror-List Screener",
    instructions=(
        "Screen customer, counterparty and beneficial-owner names against OFAC (SDN + Consolidated), the UN Security Council "
        "Consolidated List, the EU and UK sanctions lists and the UAE Local Terrorist List — from a bundled, versioned snapshot "
        "of the official files, with Arabic-aware deterministic matching. screen_name(name, dob?, nationality?, entity_type?, "
        "id_number?) returns scored hits (0–100) with match_strength exact/strong/possible/weak, matched_on explanations and a "
        "decision_hint: clear (no hit above min_score), review (possible hits) or likely_match (exact/strong). Pass dob and "
        "nationality whenever you have them — they are what separates a common name from a real match. screen_batch for up to "
        "500 subjects; audit_report for a printable HTML record of the check (keep it on the client file); list_status to see "
        "how fresh each list is; explain_match to see why two names score the way they do. Arabic input works directly "
        "(e.g. 'محمد حسن آخوند'). Treat every hit as something a human must verify against the source list entry."
        + NOTE
    ),
    description=(
        "Sanctions & terror-list screening for AI agents and compliance workflows: OFAC SDN/Consolidated, UN, EU, UK and the "
        "UAE Local Terrorist List, Arabic-aware fuzzy matching with explainable scores, DOB/nationality/ID disambiguation, "
        "batch screening and HTML audit reports. $0.01 per name. Offline snapshot refreshed weekly; no logging; no AI calls."
    ),
    version=CODE_VERSION,
    env_prefix="SCREEN",
)

TOOLS = [
    ToolSpec(
        "screen_name",
        "Screen one name against sanctions and terror lists",
        "Screen a person, company, vessel or aircraft name against OFAC SDN + Consolidated, UN, EU, UK and the UAE Local "
        "Terrorist List. Returns up to 20 hits sorted by score with list, listed_name, aliases_matched, score, match_strength "
        "(exact / strong / possible / weak), matched_on (which alias, which normalisation, DOB / nationality / type agreement), "
        "programs, listed_on, DOBs, nationalities, id_numbers, reference and source_url, plus decision_hint. Example: name "
        "'Mohamed Hussein Al-Tikriti', dob '1966', nationality 'IQ', entity_type 'individual'." + NOTE,
        ScreenIn,
        lambda b: engine.screen_name(b.name, b.dob, b.nationality, b.entity_type, b.id_number, b.lists, b.min_score),
        NAME_SCREENED,
        truncate_keys=("hits",),
    ),
    ToolSpec(
        "screen_batch",
        "Screen up to 500 names in one call",
        "Each subject may carry dob, nationality, entity_type, id_number and your reference. Identical rows are screened "
        "once (charged once). Returns per-subject decision_hint, hit_count, top_score and the top 5 hits, plus summary "
        "counts (clear / review / likely_match). Charged per unique name. Example: subjects [{name:'Ali Kony', reference:'C-17'}, "
        "{name:'شركة الأمل للتجارة', entity_type:'entity'}], default_nationality 'AE'." + NOTE,
        BatchIn,
        lambda b: engine.screen_batch(
            [s.model_dump() for s in b.subjects], b.lists, b.min_score, b.default_nationality
        ),
        NAME_SCREENED,
        truncate_keys=("results",),
        charge_count=lambda env: env.result["billable_names"],
    ),
    ToolSpec(
        "audit_report",
        "Produce an HTML audit report of a screening",
        "Screens 1–50 subjects and returns a self-contained HTML report (result.html): timestamp, each list with its "
        "version / fetched-at / SHA-256, the inputs, every hit with its explanation, and an explicit no-match statement for "
        "clear subjects — suitable for the client's AML file (open in a browser, print to PDF). One charge per report. "
        "Example: subjects [{name:'Qusay Saddam Hussein', dob:'1966'}], case_reference 'KYC-2026-0412', prepared_by 'MLRO'."
        + NOTE,
        AuditIn,
        lambda b: engine.audit_report(
            [s.model_dump() for s in b.subjects], b.lists, b.min_score, b.prepared_by, b.case_reference
        ),
        AUDIT_REPORT,
    ),
    ToolSpec(
        "list_status",
        "Freshness and provenance of every bundled list",
        "Per list: entries (individuals / entities / vessels / aircraft), aliases, fetched_at, days_since_fetch (stale flag "
        "when older than 7 days), official source URL and page, SHA-256 of the downloaded file, licence/terms, and any "
        "failures of the last refresh. Free. Call it before relying on a 'clear' result in a regulated process." + NOTE,
        StatusIn,
        lambda b: engine.list_status(),
        None,
    ),
    ToolSpec(
        "explain_match",
        "Show why two names do or don't match",
        "Normalises both names step by step (Unicode, Arabic letter folding, honorifics/particles dropped, merged 'Abdul…' "
        "forms split, romanisation variants folded) and returns every score component (token_sort, token_set, Jaro-Winkler, "
        "subset, or the cross-script skeleton) and the resulting score / strength. Free. Example: name_a 'Abd al-Rahman "
        "Al Shaikh', name_b 'عبدالرحمن الشيخ'." + NOTE,
        ExplainIn,
        lambda b: engine.explain_match(b.name_a, b.name_b),
        None,
    ),
]

assert set(LIST_KEYS) == {"ofac_sdn", "ofac_cons", "un", "eu", "uk", "uae"}

CATALOGUE = Catalogue(
    info=INFO, tools=TOOLS, data_version=lambda: engine.index().data_version, health_extra=engine.health_extra
)
