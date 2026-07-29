"""Tests for the drug-specific DDI / prohibited comedication engine.

These pin the clinical errors that would actually reach a regulator:

  * a CYP2D6 drug being handed the copy-pasted "CYP3A4 inhibitors or inducers"
    exclusion (the exact defect this engine replaces),
  * a grapefruit prohibition on a drug with no CYP3A4 involvement,
  * a flat washout that treats rifampicin like a reversible inhibitor,
  * an inducer prohibition invented for CYP2D6, which is not inducible,
  * paracetamol being offered as free rescue analgesia to a glucuronidated drug.
"""
from __future__ import annotations

import json

from services.ddi_screening import (
    _CATALOGUE,
    IMPRACTICAL_WASHOUT_DAYS,
    MECHANISM_BASED_INHIBITOR_WASHOUT_DAYS,
    REVERSIBLE_INHIBITOR_WASHOUT_DAYS,
    STRONG_INDUCER_WASHOUT_DAYS,
    ddi_directive,
    screen_interactions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _keys(rows):
    return {r["key"] for r in rows}


def _row(result, key):
    for bucket in ("prohibited", "restricted", "permitted"):
        for r in result[bucket]:
            if r["key"] == key:
                return r
    return None


CYP2D6_LABEL = {
    "drug_interactions": (
        "Metoprolol is metabolized predominantly by CYP2D6. Concomitant administration "
        "of quinidine, a potent CYP2D6 inhibitor, increased metoprolol plasma "
        "concentrations several-fold in extensive metabolizers."
    ),
    "clinical_pharmacology": (
        "Metoprolol undergoes oxidative metabolism; poor metabolizers of debrisoquine "
        "exhibit higher plasma concentrations."
    ),
}

CYP3A4_LABEL = {
    "drug_interactions": (
        "Co-administration with ketoconazole, a strong CYP3A4 inhibitor, increased the "
        "AUC of the drug 4.5-fold. Grapefruit juice should be avoided."
    ),
}

PGP_LABEL = {
    "drug_interactions": (
        "The drug is a substrate of P-glycoprotein (P-gp). Co-administration with "
        "P-gp inhibitors increases exposure."
    ),
}

UGT_LABEL = {
    "clinical_pharmacology": (
        "The drug is eliminated primarily by direct glucuronidation via UGT1A1; the "
        "glucuronide metabolite is excreted in bile."
    ),
}


# ---------------------------------------------------------------------------
# Catalogue integrity
# ---------------------------------------------------------------------------


def test_catalogue_is_well_formed():
    keys = [p.key for p in _CATALOGUE]
    assert len(keys) == len(set(keys)), "duplicate pathway keys"
    for pw in _CATALOGUE:
        assert pw.kind in ("enzyme", "transporter"), pw.key
        assert pw.label and pw.mechanism and pw.victim_risk
        assert len(pw.mechanism) > 60, f"{pw.key} mechanism too thin to justify a row"
        assert pw.perpetrators, f"{pw.key} has no named agents - a site pharmacist "
        for p in pw.perpetrators:
            assert p.role in ("inhibitor", "inducer"), p.name
            assert p.strength in ("strong", "moderate"), p.name
            assert p.mechanism in ("reversible", "mechanism_based", "induction"), p.name
            if p.role == "inducer":
                assert p.mechanism == "induction", f"{p.name} inducer must use induction"
            assert p.t_half_hours is None or p.t_half_hours > 0


def test_every_pathway_names_concrete_agents():
    """Exclusion criteria have to be actionable by a site pharmacist, so every
    pathway must name real agents, not just describe a class."""
    for pw in _CATALOGUE:
        names = [p.name for p in pw.perpetrators]
        assert len(names) >= 3, f"{pw.key} lists too few agents to be actionable"
        assert all(n.strip() for n in names)


def test_all_required_pathways_are_covered():
    keys = {p.key for p in _CATALOGUE}
    for required in (
        "cyp1a2", "cyp2b6", "cyp2c8", "cyp2c9", "cyp2c19", "cyp2d6", "cyp3a4",
        "ugt", "pgp", "bcrp", "oatp", "oct_mate",
    ):
        assert required in keys, f"missing pathway {required}"


# ---------------------------------------------------------------------------
# THE headline defect: drug-specificity
# ---------------------------------------------------------------------------


def test_cyp2d6_drug_gets_no_cyp3a4_clause_and_no_grapefruit():
    """The static frontend preset prohibited 'CYP3A4 inhibitors or inducers' and
    grapefruit for EVERY drug. For a CYP2D6 substrate both are wrong: they
    exclude agents that cannot touch the analyte while permitting quinidine."""
    out = screen_interactions(CYP2D6_LABEL)
    assert "cyp2d6" in out["pathway_keys"]
    assert "cyp3a4" not in out["pathway_keys"]

    all_rows = out["prohibited"] + out["restricted"]
    assert not any(r["pathway"] == "cyp3a4" for r in all_rows), (
        "a CYP2D6 drug must not carry a CYP3A4 prohibition")
    assert "grapefruit" not in _keys(all_rows), (
        "grapefruit only matters to a CYP3A4 (or intestinal transporter) substrate")

    # ...and the agents that DO matter are named.
    strong_inh = _row(out, "cyp2d6_strong_inhibitors")
    assert strong_inh is not None and strong_inh["status"] == "prohibited"
    assert "quinidine" in strong_inh["examples"]
    assert "paroxetine" in strong_inh["examples"]


def test_cyp3a4_drug_does_get_grapefruit_prohibited():
    out = screen_interactions(CYP3A4_LABEL)
    assert "cyp3a4" in out["pathway_keys"]
    grapefruit = _row(out, "grapefruit")
    assert grapefruit is not None
    assert grapefruit["status"] == "prohibited"
    assert "enterocyte" in grapefruit["washout_basis"].lower()


def test_pgp_drug_gets_transporter_rules_not_cyp_rules():
    out = screen_interactions(PGP_LABEL)
    assert out["pathway_keys"] == ["pgp"]
    rows = out["prohibited"] + out["restricted"]
    assert any(r["pathway"] == "pgp" for r in rows)
    assert not any(r["pathway"] == "cyp3a4" for r in rows)
    pgp_inh = _row(out, "pgp_strong_inhibitors")
    assert "verapamil" in pgp_inh["examples"]


def test_tobacco_and_caffeine_only_appear_for_cyp1a2():
    cyp1a2 = screen_interactions({
        "clinical_pharmacology": "The drug is metabolized by CYP1A2 to an inactive metabolite."
    })
    assert _row(cyp1a2, "tobacco") is not None
    assert _row(cyp1a2, "xanthines") is not None

    not_cyp1a2 = screen_interactions(CYP2D6_LABEL)
    assert _row(not_cyp1a2, "tobacco") is None, (
        "a non-CYP1A2 substrate gains nothing from a smoking exclusion")
    assert _row(not_cyp1a2, "xanthines") is None


# ---------------------------------------------------------------------------
# Washout asymmetry - the number a pharmacologist would check first
# ---------------------------------------------------------------------------


def test_inducer_washout_is_much_longer_than_reversible_inhibitor_washout():
    """Enzyme re-synthesis, not perpetrator clearance, governs de-induction. A
    flat washout applied to both is the classic protocol error."""
    out = screen_interactions(CYP3A4_LABEL)
    inducers = _row(out, "cyp3a4_strong_inducers")
    inhibitors = _row(out, "cyp3a4_strong_inhibitors")
    assert inducers["washout_days"] == STRONG_INDUCER_WASHOUT_DAYS
    assert inducers["washout_days"] >= 2 * inhibitors["washout_days"], (
        "induction must carry a substantially longer window than inhibition")
    assert "rifampicin (rifampin)" in inducers["examples"]
    assert "de-induction" in inducers["washout_basis"].lower()


def test_mechanism_based_inhibition_beats_the_reversible_floor():
    """A class containing a suicide inhibitor cannot use the reversible floor:
    clearing clarithromycin in a day leaves the enzyme pool still inactivated."""
    out = screen_interactions(CYP3A4_LABEL)
    strong = _row(out, "cyp3a4_strong_inhibitors")
    assert strong["washout_days"] == MECHANISM_BASED_INHIBITOR_WASHOUT_DAYS
    assert strong["washout_days"] > REVERSIBLE_INHIBITOR_WASHOUT_DAYS
    basis = strong["washout_basis"].lower()
    assert "synthesis" in basis or "re-synthesis" in basis


def test_long_lived_agents_get_their_own_longer_window_not_the_class_window():
    """Norfluoxetine and terbinafine outlast the class window by weeks; a real
    protocol names them separately rather than inflating the whole class."""
    out = screen_interactions(CYP2D6_LABEL)
    strong = _row(out, "cyp2d6_strong_inhibitors")
    extended = {e["name"]: e["washout_days"] for e in strong["extended_washout"]}
    assert "fluoxetine" in extended, "fluoxetine's active metabolite needs a longer window"
    assert extended["fluoxetine"] > strong["washout_days"]
    assert "terbinafine" in extended
    # And the class window itself stays workable for the site.
    assert strong["washout_days"] <= 14


def test_unwashoutable_agents_become_history_exclusions():
    """Amiodarone (t1/2 ~58 days) cannot be washed out inside a BE study; the
    engine must say 'exclude on history', not print a 290-day washout."""
    out = screen_interactions({
        "drug_interactions": "The drug is a substrate of P-glycoprotein (P-gp)."
    })
    names = {h["name"] for h in out["history_exclusion_agents"]}
    assert "amiodarone" in names
    entry = next(h for h in out["history_exclusion_agents"] if h["name"] == "amiodarone")
    assert entry["estimated_washout_days"] > IMPRACTICAL_WASHOUT_DAYS
    assert "history" in entry["action"].lower()
    # It must NOT appear as a routine washout on any class row.
    for r in out["prohibited"] + out["restricted"]:
        assert "amiodarone" not in r["examples"]


def test_max_washout_ignores_unwashoutable_agents():
    out = screen_interactions(PGP_LABEL)
    assert out["max_washout_days"] <= IMPRACTICAL_WASHOUT_DAYS
    assert out["screening_history_window_days"] >= out["max_washout_days"]


def test_summary_and_directive_agree_on_the_screening_history_window():
    """Both strings are injected into the SAME prompt, so if they state different
    numbers the model has to arbitrate between two 'locked' facts - which defeats
    the point of computing them in Python at all.

    They genuinely differ: for a CYP2D6 drug the longest CLASS window is short,
    but terbinafine (t1/2 ~300 h) has to be detectable at screening ~63 days back.
    The summary must not quietly report the class window as the history window.
    """
    out = screen_interactions(CYP2D6_LABEL)
    window = out["screening_history_window_days"]
    assert window > out["max_washout_days"], (
        "CYP2D6 has slow-clearing inhibitors, so this case must exercise the gap")

    summary = out["summary"]
    directive = ddi_directive(out)
    assert f"{window:.0f} days" in summary, (
        "the summary must state the real history window, not the class window")
    assert f"{window:.0f} days" in directive
    # The class window must not be passed off as the history requirement.
    assert "which is also the minimum period" not in summary

    # When there is no gap, the simple wording is used and is still correct.
    plain = screen_interactions(None)
    assert plain["screening_history_window_days"] == plain["max_washout_days"]
    assert f"{plain['max_washout_days']:.0f} days" in plain["summary"]


# ---------------------------------------------------------------------------
# Honest pharmacology - things the engine must NOT claim
# ---------------------------------------------------------------------------


def test_cyp2d6_gets_no_inducer_prohibition_because_it_is_not_inducible():
    out = screen_interactions(CYP2D6_LABEL)
    rows = out["prohibited"] + out["restricted"]
    assert not any(
        r["pathway"] == "cyp2d6" and "inducer" in r["key"] for r in rows
    ), "CYP2D6 is not appreciably inducible - claiming an inducer class is wrong"
    caveat = next(p["caveat"] for p in out["pathways"] if p["key"] == "cyp2d6")
    assert "not appreciably inducible" in caveat


def test_uptake_transporters_get_no_inducer_prohibition():
    out = screen_interactions({
        "clinical_pharmacology": "Hepatic uptake is mediated by OATP1B1 (SLCO1B1)."
    })
    rows = out["prohibited"] + out["restricted"]
    assert not any(r["pathway"] == "oatp" and "inducer" in r["key"] for r in rows)


def test_rifampicin_dual_role_is_recorded_for_oatp():
    """Rifampicin INHIBITS OATP1B1 acutely but INDUCES CYP3A4 chronically - the
    single most common conflation in a protocol's interaction section."""
    out = screen_interactions({
        "drug_interactions": "Co-administration with a single dose of rifampin, an OATP1B1 "
                             "inhibitor, increased exposure 2-fold."
    })
    oatp = _row(out, "oatp_strong_inhibitors")
    assert oatp is not None
    assert any("rifampicin" in name for name in oatp["examples"])
    pathway = next(p for p in out["pathways"] if p["key"] == "oatp")
    assert "not clinically inducible" in pathway["caveat"]


# ---------------------------------------------------------------------------
# Prohibited vs restricted vs permitted
# ---------------------------------------------------------------------------


def test_strong_is_prohibited_and_moderate_is_restricted():
    out = screen_interactions(CYP3A4_LABEL)
    assert _row(out, "cyp3a4_strong_inhibitors")["status"] == "prohibited"
    moderate = _row(out, "cyp3a4_moderate_inhibitors")
    assert moderate["status"] == "restricted"
    assert moderate["conditions"], "a restricted class must state its documented washout condition"


def test_nti_escalates_moderate_perpetrators_to_prohibited():
    """For a narrow-therapeutic-index victim the acceptance limits are tightened,
    so even a 2-fold moderate interaction is unacceptable."""
    normal = screen_interactions(CYP3A4_LABEL, is_nti=False)
    nti = screen_interactions(CYP3A4_LABEL, is_nti=True)
    assert _row(normal, "cyp3a4_moderate_inhibitors")["status"] == "restricted"
    escalated = _row(nti, "cyp3a4_moderate_inhibitors")
    assert escalated["status"] == "prohibited"
    assert escalated["escalated_by_nti"] is True


def test_paracetamol_is_permitted_rescue_by_default():
    out = screen_interactions(CYP2D6_LABEL)
    para = _row(out, "paracetamol_rescue")
    assert para["status"] == "permitted"
    assert "2 g" in para["conditions"]


def test_paracetamol_is_not_free_rescue_for_a_glucuronidated_drug():
    """Paracetamol is itself glucuronidated at gram doses, so for a UGT substrate
    it stops being a neutral rescue analgesic."""
    out = screen_interactions(UGT_LABEL)
    assert "ugt" in out["pathway_keys"]
    para = _row(out, "paracetamol_rescue")
    assert para["status"] == "restricted"
    assert "glucuroni" in para["rationale"].lower()


def test_paracetamol_is_not_permitted_when_it_is_the_analyte():
    out = screen_interactions({"pharmacokinetics": "Absorption is rapid."},
                              drug_inn="paracetamol")
    para = _row(out, "paracetamol_rescue")
    assert para["status"] == "restricted"
    assert "analyte" in para["rationale"].lower()


def test_patient_study_does_not_blanket_prohibit_background_therapy():
    """Patients cannot stop standard of care for a PK study; the pathway-specific
    classes still bind, but the blanket prescription ban must not."""
    hv = screen_interactions(CYP3A4_LABEL, healthy_volunteers=True)
    patients = screen_interactions(CYP3A4_LABEL, healthy_volunteers=False)
    assert _row(hv, "prescription_medication")["status"] == "prohibited"
    rx = _row(patients, "prescription_medication")
    assert rx["status"] == "restricted"
    assert "unchanged" in rx["conditions"].lower()
    # The class that actually protects the measurement is untouched.
    assert _row(patients, "cyp3a4_strong_inhibitors")["status"] == "prohibited"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_each_pathway_cites_the_label_section_that_triggered_it():
    out = screen_interactions(CYP3A4_LABEL)
    pathway = out["pathways"][0]
    ev = pathway["evidence"][0]
    assert "DRUG INTERACTIONS" in ev["source"]
    assert "34073-7" in ev["source"], "cite the SPL LOINC code the section came from"
    assert ev["cue"]
    assert "ketoconazole" in ev["quote"].lower()


def test_drug_interactions_section_outranks_clinical_pharmacology():
    """A named interacting agent in DRUG INTERACTIONS is stronger evidence than a
    passing in-vitro mention in Clinical Pharmacology, so it is cited first."""
    label = {
        "drug_interactions": "Concomitant ketoconazole increased CYP3A4 substrate AUC.",
        "clinical_pharmacology": "In vitro, the drug is metabolized by CYP3A4.",
    }
    out = screen_interactions(label)
    first = out["pathways"][0]["evidence"][0]
    assert "DRUG INTERACTIONS" in first["source"]


def test_pathway_rows_carry_the_same_evidence_as_the_pathway():
    out = screen_interactions(CYP3A4_LABEL)
    row = _row(out, "cyp3a4_strong_inhibitors")
    assert "DRUG INTERACTIONS" in row["evidence"]["source"]
    assert row["evidence"]["quote"]


def test_universal_rules_are_marked_as_convention_not_as_label_evidence():
    out = screen_interactions(CYP3A4_LABEL)
    alcohol = _row(out, "alcohol")
    assert "convention" in alcohol["evidence"]["source"].lower()
    assert alcohol["evidence"]["quote"] == ""


# ---------------------------------------------------------------------------
# Unknown pathway - conservative but flagged
# ---------------------------------------------------------------------------


def test_unknown_pathway_falls_back_conservatively_and_discloses_the_assumption():
    out = screen_interactions({"pharmacokinetics": "Absorption is rapid and complete."})
    assert out["pathway_keys"] == []
    generic = _row(out, "generic_perpetrators")
    assert generic is not None
    assert generic["washout_days"] == STRONG_INDUCER_WASHOUT_DAYS
    assert generic["assumption"], "a fallback must be disclosed, not presented as fact"
    assert out["unresolved"]


def test_generic_fallback_disappears_once_a_pathway_is_known():
    out = screen_interactions(CYP2D6_LABEL)
    assert _row(out, "generic_perpetrators") is None, (
        "a generic pan-pathway clause is exactly the defect this engine replaces")


def test_no_label_does_not_crash():
    out = screen_interactions(None)
    assert out["prohibited"], "universal restrictions still apply"
    assert out["summary"]


# ---------------------------------------------------------------------------
# Design awareness and PK-driven windows
# ---------------------------------------------------------------------------


def test_summary_is_design_aware():
    xo = screen_interactions(CYP3A4_LABEL, design_key="crossover")["summary"].lower()
    par = screen_interactions(CYP3A4_LABEL, design_key="parallel")["summary"].lower()
    assert "between periods" in xo and "own control" in xo
    assert "arm" in par
    assert "between periods" not in par


def test_post_dose_window_uses_the_study_drug_half_life():
    pk = [{"parameter": "t_half", "value": 12.0, "unit": "h", "source_pmid": "1"}]
    out = screen_interactions(CYP3A4_LABEL, pk)
    assert out["study_drug_t_half_hours"] == 12.0
    assert out["post_dose_restriction_hours"] == 60  # 5 x 12 h
    assert _row(out, "post_dose_window") is not None


def test_half_life_units_are_normalised_not_assumed():
    """A days/hours mix-up here would silently change every downstream window -
    this repo has already been bitten by a unit bug once."""
    days = screen_interactions(CYP3A4_LABEL,
                               [{"parameter": "t_half", "value": 2.0, "unit": "days"}])
    assert days["study_drug_t_half_hours"] == 48.0
    minutes = screen_interactions(CYP3A4_LABEL,
                                  [{"parameter": "t_half", "value": 90.0, "unit": "min"}])
    assert minutes["study_drug_t_half_hours"] == 1.5
    junk = screen_interactions(CYP3A4_LABEL,
                               [{"parameter": "t_half", "value": 4.0, "unit": "furlongs"}])
    assert junk["study_drug_t_half_hours"] is None
    other = screen_interactions(CYP3A4_LABEL,
                                [{"parameter": "Cmax", "value": 10.0, "unit": "ng/mL"}])
    assert other["study_drug_t_half_hours"] is None


# ---------------------------------------------------------------------------
# The label sections this engine depends on must actually be fetched
# ---------------------------------------------------------------------------


def test_spl_parser_extracts_the_interaction_sections_the_engine_reads():
    """The engine is only drug-specific because literature_service now fetches
    DRUG INTERACTIONS (34073-7) and CONTRAINDICATIONS (34070-3). If those LOINC
    codes are ever dropped from _SPL_SECTIONS the engine silently degrades to a
    generic clause, so pin the whole chain: SPL XML -> sections -> screening."""
    from services.literature_service import _parse_spl_sections

    xml = (
        '<document xmlns="urn:hl7-org:v3">'
        '<component><section><code code="34073-7"/><text>Concomitant use with '
        'ketoconazole, a strong CYP3A4 inhibitor, increased AUC 4-fold.</text>'
        '</section></component>'
        '<component><section><code code="34070-3"/><text>Contraindicated with strong '
        'CYP3A4 inducers such as rifampin.</text></section></component>'
        '<component><section><code code="43682-4"/><text>Terminal half-life is 8 hours.'
        '</text></section></component>'
        '</document>'
    )
    sections = _parse_spl_sections(xml)
    assert "drug_interactions" in sections
    assert "contraindications" in sections

    out = screen_interactions(sections)
    assert out["pathway_keys"] == ["cyp3a4"]
    assert "34073-7" in out["pathways"][0]["evidence"][0]["source"]


# ---------------------------------------------------------------------------
# Serialisation and the prompt directive
# ---------------------------------------------------------------------------


def test_result_round_trips_through_json_for_population_config():
    """It is persisted inside population_config (a JSONB column), so it must
    survive json.dumps -> json.loads unchanged (no tuples, no dataclasses)."""
    out = screen_interactions(
        CYP3A4_LABEL,
        [{"parameter": "t_half", "value": 8.0, "unit": "h"}],
        drug_inn="midazolam",
        design_key="crossover",
        is_nti=True,
    )
    assert json.loads(json.dumps(out)) == out


def test_empty_result_round_trips_too():
    out = screen_interactions(None)
    assert json.loads(json.dumps(out)) == out


def test_directive_locks_the_list_for_the_prompt():
    out = screen_interactions(CYP3A4_LABEL)
    directive = ddi_directive(out)
    assert "ALREADY DETERMINED" in directive
    assert "PROHIBITED" in directive and "RESTRICTED" in directive
    # Every prohibited class must actually reach the prompt.
    for r in out["prohibited"]:
        assert r["label"] in directive
    # The washout numbers must be stated, not left to the model.
    assert f"{STRONG_INDUCER_WASHOUT_DAYS:g} days" in directive
    assert ddi_directive({}) == ""


def test_directive_warns_against_the_generic_cyp3a4_clause():
    out = screen_interactions(CYP2D6_LABEL)
    directive = ddi_directive(out)
    assert "CYP3A4" in directive, "the model must be explicitly told not to add it"
    assert "unless CYP3A4 appears in the list below" in directive
    # ...and CYP3A4 must not be present as an actual prohibited class.
    assert "inhibitors of CYP3A4/5" not in directive


def test_directive_surfaces_extended_and_history_exclusions():
    out = screen_interactions(CYP2D6_LABEL)
    directive = ddi_directive(out)
    assert "LONGER individual washout" in directive
    assert "fluoxetine" in directive

    pgp = ddi_directive(screen_interactions(PGP_LABEL))
    assert "NOT washable" in pgp
    assert "amiodarone" in pgp


def test_directive_is_ascii_safe_for_windows_console():
    """This repo has been bitten by cp1252 encode errors on non-ASCII output, so
    every string the ENGINE authors (as opposed to quotes lifted from a label)
    must be plain ASCII."""
    directive = ddi_directive(screen_interactions(CYP3A4_LABEL))
    directive.encode("ascii")  # raises UnicodeEncodeError on failure

    for pw in _CATALOGUE:
        for text in (pw.label, pw.mechanism, pw.victim_risk, pw.caveat):
            text.encode("ascii")
        for p in pw.perpetrators:
            p.name.encode("ascii")
            p.note.encode("ascii")

    # And the emitted rows for every pathway, exercised through a label that
    # mentions all of them at once.
    every = {"drug_interactions": " ".join([
        "CYP3A4", "CYP2D6", "CYP2C19", "CYP2C9", "CYP2C8", "CYP1A2", "CYP2B6",
        "glucuronidation", "P-glycoprotein", "BCRP", "OATP1B1", "OCT2",
    ])}
    json.dumps(screen_interactions(every), ensure_ascii=False).encode("ascii")
