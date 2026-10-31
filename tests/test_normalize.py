from __future__ import annotations

import pytest

from screen.normalize import canonical_id, data_dir, normalise, skeleton_arabic, skeleton_latin, tables


def norm(s: str) -> str:
    return normalise(s).text


def test_romanisation_groups_fold_to_one_canonical():
    assert (
        norm("Mohamed") == norm("Mohammad") == norm("Muhammad") == norm("Mohammed") == norm("Muhamed") == norm("Mohd")
    )
    assert norm("Ahmed") == norm("Ahmad")
    assert norm("Hussein") == norm("Husain") == norm("Hussain") == norm("Hossein")
    assert norm("Youssef") == norm("Yusuf") == norm("Yousef")
    assert norm("Khaled") == norm("Khalid")
    assert norm("Saeed") == norm("Said") == norm("Sa'id")
    assert norm("Omar") == norm("Umar")
    assert norm("Osama") == norm("Usama")
    assert norm("Qasim") == norm("Kassem") == norm("Qassem")


def test_variant_table_is_declared_heuristic_and_large_enough():
    t = tables()
    assert t.n_groups >= 80
    text = (data_dir() / "name_variants.yaml").read_text(encoding="utf-8")
    assert "HEURISTIC" in text


def test_arabic_letter_folding_and_tashkeel():
    assert norm("أحمد") == norm("احمد") == norm("آحمد")
    assert norm("فاطمة") == norm("فاطمه")
    assert norm("مصطفى") == norm("مصطفي")
    assert norm("مُحَمَّد") == norm("محمد")
    assert norm("محمـــد") == norm("محمد")  # tatweel


def test_arabic_prefixes_particles_and_abd_forms():
    assert norm("عبد الرحمن") == norm("عبدالرحمن") == norm("عَبْدُ الرَّحْمَنِ")
    assert norm("محمد بن سلمان") == norm("محمد سلمان")
    assert norm("الشيخ خالد") == norm("خالد")
    assert norm("عبد الله") == norm("عبدالله")


def test_latin_particles_honorifics_and_merged_abd_forms():
    assert norm("Abd al-Rahman") == norm("Abdulrahman") == norm("Abdel Rahman") == norm("Abdurrahman") == "abd rahman"
    assert norm("Dr. Khalid bin Mohammed Al Saud") == norm("Khalid Mohamed Saud")
    assert norm("Sheikh Hamad") == norm("Hamad")
    assert norm("Abdallah") == norm("Abd Allah") == norm("Abdullah")


def test_legal_suffixes_dropped_for_entities():
    assert norm("ANGLO-CARIBBEAN CO., LTD.") == "anglo caribbean"
    assert norm("Al Amal General Trading LLC") == norm(
        "Amal"
    )  # 'general' + 'trading' + 'llc' are legal/generic suffixes


def test_diacritics_and_punctuation():
    assert norm("Qoussaï Al-Tikriti") == "qoussai tikriti"
    assert norm("O'Brien-Smith") == "obrien smith"
    assert normalise("   ").tokens == ()


def test_reordering_is_visible_in_sorted_text_only():
    a, b = normalise("Hussein Ali Mohamed"), normalise("Mohamed Ali Hussein")
    assert a.text != b.text
    assert a.sorted_text == b.sorted_text


def test_skeleton_is_shared_between_scripts():
    pairs = [
        ("Mohammed", "محمد"),
        ("Hussein", "حسين"),
        ("Khalid", "خالد"),
        ("Yusuf", "يوسف"),
        ("Abdullah", "عبدالله"),
        ("Khalifa", "خليفة"),
        ("Qassem", "قاسم"),
        ("Tariq", "طارق"),
    ]
    for lat, ar in pairs:
        assert skeleton_latin(normalise(lat).tokens[0]) == skeleton_arabic(normalise(ar).tokens[0]), (lat, ar)


def test_mixed_script_input_is_handled():
    n = normalise("Mohamed Hussein / محمد حسين")
    assert n.script == "mixed"
    assert "muhammad" in n.tokens and "محمد" in n.tokens


def test_steps_are_recorded_and_explain_variants():
    n = normalise("Mohamed Al-Hussein")
    assert any("romanisation" in s for s in n.steps)
    assert "mohamed→muhammad" in n.variants_applied
    assert normalise("Mohamed", explain=False).steps == ()


def test_canonical_id():
    assert canonical_id("P 0458-1926") == "P04581926"
    assert canonical_id("٤٧٥٣٣٦") == "475336"


@pytest.mark.parametrize("name", ["Al", "bin", "LLC", "الشيخ"])
def test_particle_only_input_keeps_something(name):
    assert normalise(name).tokens  # never empty for a non-blank input
