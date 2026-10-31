from __future__ import annotations

import json
from pathlib import Path

import pytest

from screen import matcher
from screen.matcher import LIST_KEYS, compare, index, screen
from screen.normalize import data_dir, normalise


def score(a: str, b: str) -> float:
    return compare(normalise(a), normalise(b))["score"]


def test_all_six_lists_are_bundled_and_indexed():
    ix = index()
    for k in LIST_KEYS:
        assert ix.list_counts[k]["entries"] > 100 if k != "uae" else ix.list_counts[k]["entries"] > 200, k
    assert len(ix.entries) > 30_000
    assert len(ix.aliases) > 90_000


def test_name_only_scores():
    assert score("Mohamed Hussein", "Muhammad Husain") == 100.0
    assert score("Hussein Ali Mohamed", "Mohamed Ali Hussein") == 100.0  # reorder
    assert score("John Smith", "Ahmed Khalid") < 60
    assert score("Jane Doe", "Bank Melli Iran") < 50
    assert 80 <= score("Usama bin Ladin", "Usama bin Muhammad bin Awad bin Ladin") <= 90  # subset, capped
    assert score("Hussein", "Saddam Hussein Al-Tikriti") < 85  # one-word queries are penalised


def test_reordering_is_strong_not_exact():
    r = screen("Hussein Al-Tikriti Saddam", lists=["un"])
    top = r["hits"][0]
    assert top["listed_name"] == "SADDAM HUSSEIN AL-TIKRITI"
    assert top["score"] == 100.0 and top["match_strength"] == "strong"
    assert "different order" in top["matched_on"][0]


def test_exact_hit_on_un_entity_and_on_each_list():
    expectations = {
        "un": ("Al-Qaida", "entity"),
        "ofac_sdn": ("AEROCARIBBEAN AIRLINES", "entity"),
        "ofac_cons": ("Mohammed Abu Teir", "individual"),
        "eu": ("Saddam Hussein Al-Tikriti", "individual"),
        "uk": ("Haji Khairullah Haji Sattar Money Exchange", "entity"),
        "uae": ("Khalifa Mohd T Al Subaey", "individual"),
    }
    for lst, (name, typ) in expectations.items():
        r = screen(name, lists=[lst], entity_type=typ)
        assert r["decision_hint"] == "likely_match", (lst, name)
        top = r["hits"][0]
        assert top["list"] == lst and top["match_strength"] == "exact" and top["score"] == 100.0, (lst, top)
        assert top["source_url"].startswith("http")


def test_arabic_input_hits_arabic_aliases_directly():
    r = screen("محمد حسن آخوند")
    top = r["hits"][0]
    assert top["match_strength"] == "exact" and top["list"] in ("un", "uk")
    assert top["aliases_matched"] == ["محمد حسن آخوند"]
    r2 = screen("خليفة محمد تركي السبيعي", lists=["uae"])
    assert r2["hits"][0]["match_strength"] == "exact"


def test_arabic_input_reaches_latin_only_entries_via_skeleton_as_possible():
    r = screen("عبد الملك محمد يوسف عبد السلام", lists=["ofac_sdn"], min_score=85)
    assert r["hits"], "cross-script hit expected"
    top = r["hits"][0]
    assert top["score"] <= 90 and top["match_strength"] == "possible" and r["cross_script_used"]
    assert any("cross-script" in m for m in top["matched_on"])


def test_dob_disambiguation():
    with_dob = screen("Qusay Saddam Hussein", dob="1966", lists=["un"])
    wrong_dob = screen("Qusay Saddam Hussein", dob="1980", lists=["un"])
    assert with_dob["decision_hint"] == "likely_match"
    assert any("dob year match 1966" in m for m in with_dob["hits"][0]["matched_on"])
    assert wrong_dob["decision_hint"] == "clear"
    exact = screen("Abdulmalik Mohammad Yousef Abdelsalam", dob="1989-07-13", nationality="JO", lists=["uae"])
    assert exact["hits"][0]["score"] == 100.0 and any("dob exact match" in m for m in exact["hits"][0]["matched_on"])


def test_nationality_adjusts_score():
    jo = screen("Abdulmalik Mohammad Yousef Abdelsalam", nationality="JO", lists=["uae"])["hits"][0]
    eg = screen("Abdulmalik Mohammad Yousef Abdelsalam", nationality="EG", lists=["uae"])["hits"][0]
    assert jo["score"] > eg["score"]
    assert any("nationality mismatch" in m for m in eg["matched_on"])


def test_entity_vs_individual_type_penalty():
    as_entity = screen("Saddam Hussein Al-Tikriti", entity_type="entity", lists=["un"], min_score=70)
    as_person = screen("Saddam Hussein Al-Tikriti", entity_type="individual", lists=["un"])
    assert as_person["hits"][0]["score"] == 100.0
    assert as_entity["hits"][0]["score"] == 80.0 and as_entity["hits"][0]["match_strength"] != "exact"
    assert any("type mismatch" in m for m in as_entity["hits"][0]["matched_on"])


def test_id_number_exact_match_surfaces_entry():
    r = screen("Somebody Else", id_number="685868", lists=["uae"])  # passport number of a UAE-listed individual
    assert r["hits"] and r["hits"][0]["match_strength"] == "exact" and r["hits"][0]["score"] == 100.0
    assert any("ID number exact match" in m for m in r["hits"][0]["matched_on"])


def test_no_false_positive_for_clearly_different_names():
    assert screen("John Smith")["decision_hint"] == "clear"
    assert screen("Maria Fernanda Oliveira Santos", nationality="BR")["decision_hint"] == "clear"


def test_min_score_and_max_hits_are_respected():
    wide = screen("Mohamed Ali", min_score=80)
    narrow = screen("Mohamed Ali", min_score=99)
    assert len(wide["hits"]) <= matcher.MAX_HITS
    assert all(h["score"] >= 80 for h in wide["hits"])
    assert all(h["score"] >= 99 for h in narrow["hits"])
    assert wide["hits"] == sorted(wide["hits"], key=lambda h: -h["score"])


def test_list_filter_restricts_results():
    r = screen("Saddam Hussein Al-Tikriti", lists=["uae"])
    assert r["hit_count"] == 0 and r["lists_screened"] == ["uae"]


def test_weak_alias_never_exact():
    ix = index()
    weak = next(a for a in ix.aliases if a.weak and len(a.norm.tokens) >= 2)
    e = ix.entries[weak.entry]
    r = screen(weak.text, lists=[e.list], min_score=50)
    hit = next(h for h in r["hits"] if h["list_entry_id"] == e.id.split(":", 1)[1])
    assert hit["match_strength"] != "exact" and any("weak alias" in m for m in hit["matched_on"])


def test_every_bundled_entry_cites_a_source_url_and_version_is_consistent():
    import yaml

    d = data_dir()
    v = yaml.safe_load((d / "version.yaml").read_text(encoding="utf-8"))
    assert v["data_version"] == index().data_version
    for k in LIST_KEYS:
        meta = v["lists"][k]
        assert (
            meta["source_url"].startswith("https://") and len(meta["sha256"]) == 64 and meta["fetched_at"].endswith("Z")
        )
        n = 0
        with (d / "lists" / f"{k}.jsonl").open(encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                assert r["url"].startswith("http") and r["name"] and r["list"] == k
                n += 1
        assert n == meta["counts"]["entries"] == index().list_counts[k]["entries"]


def test_memory_footprint_under_300mb():
    """Peak RSS of a fresh process that loads every list and answers one query (the pytest process itself also holds
    MCP/FastAPI and a second index, so it is measured in a subprocess)."""
    import subprocess
    import sys

    code = (
        "from screen.matcher import index, screen; index(); screen('Mohamed Ali Hussein');"
        "import re; st = open('/proc/self/status').read(); "
        "print(int(re.search(r'VmHWM:\\s+(\\d+)', st).group(1)) // 1024)"  # peak RSS of this process (ru_maxrss survives exec on Linux)
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120)  # noqa: S603
    rss_mb = int(out.stdout.strip().splitlines()[-1])
    print(f"\npeak RSS with all lists loaded: {rss_mb} MB")
    assert rss_mb < 300, rss_mb


def test_refresh_parsers_on_tiny_fixtures(tmp_path: Path):
    """The XML/XLS parsers are exercised on the bundled snapshot indirectly; here: UK date and UAE helpers."""
    from screen import refresh

    assert refresh._uk_date("dd/mm/1945") == "1945"
    assert refresh._uk_date("07/08/2024") == "2024-08-07"
    assert refresh.iso2("Iran (Islamic Republic of)") == "IR"
    assert refresh.iso2("لبناني") == "LB"
    assert refresh.iso2("الحالية: السويد السابقة: ليبيريا") == "SE"
    assert (
        refresh._uae_ref("مدرج بموجب قرار مجلس الوزراء رقم (18) لسنة 2017") == "UAE Cabinet Resolution No. 18 of 2017"
    )


@pytest.mark.parametrize("name", ["Hizballah", "Islamic Revolutionary Guard Corps", "Bank Melli Iran"])
def test_well_known_entities_are_found(name):
    r = screen(name, entity_type="entity")
    assert r["decision_hint"] == "likely_match" and r["hits"][0]["score"] >= 92
