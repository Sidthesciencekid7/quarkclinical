"""Tests for the randomization allocation-list generator.

The list this engine produces is a regulatory record: a regulator may ask, years
after database lock, to see it regenerated from its seed and shown to match what
the pharmacy dispensed. These tests therefore pin the things that would make it
indefensible rather than merely untidy:

  * a fixed seed must reproduce the list exactly, and the module must not touch
    the global ``random`` state that any other import could perturb;
  * a block size that is not a whole multiple of the number of sequences must be
    rejected, because such a block can never be balanced;
  * every completed block must contain each sequence exactly equally often;
  * each design must carry the sequence set its statistical method requires - a
    partial replicate cannot support the NTI variability comparison, and a
    3-period set in a 4-period design would be a silent protocol error;
  * the blinded enrolment log must carry no treatment information at all.
"""
from __future__ import annotations

import json
import random

import pytest

from services.randomization import (
    ENGINE_VERSION,
    SEQUENCE_REPEATS_PER_BLOCK,
    _DESIGNS,
    _derive_seed,
    assess_blinding,
    blinded_view,
    format_unblinded_list,
    generate_allocation,
    randomization_directive,
    sequences_for_design,
)


# ---------------------------------------------------------------------------
# Design catalogue integrity
# ---------------------------------------------------------------------------


def test_design_catalogue_is_well_formed():
    seen = set()
    for d in _DESIGNS:
        assert (d.key, d.variant) not in seen, "duplicate design variant"
        seen.add((d.key, d.variant))
        assert d.key in ("crossover", "parallel", "replicate"), (
            "design_key must match the services/biostatistics.py vocabulary")
        assert d.sequences, "a design must have at least one sequence"
        for seq in d.sequences:
            assert len(seq) == d.n_periods, (
                f"{d.variant}: sequence {seq} does not span {d.n_periods} periods")
            assert set(seq) <= set(d.treatments), (
                f"{d.variant}: sequence {seq} uses an unknown treatment code")
        # Every treatment must appear somewhere, or the design cannot compare them.
        assert set("".join(d.sequences)) == set(d.treatments)
        assert len(d.rationale) > 80, f"{d.variant} rationale too thin to defend"
        assert d.regulatory_basis


def test_sequence_sets_match_the_statistical_method():
    """The sequence set IS the design. Getting it wrong invalidates the analysis."""
    # 2x2 crossover
    assert set(sequences_for_design("crossover").sequences) == {"TR", "RT"}

    # Partial replicate: the REFERENCE is replicated, which is what makes s_WR
    # (and therefore reference scaling) estimable.
    partial = sequences_for_design("replicate", is_nti=False)
    assert set(partial.sequences) == {"TRR", "RTR", "RRT"}
    assert partial.n_periods == 3
    for seq in partial.sequences:
        assert seq.count("R") == 2 and seq.count("T") == 1

    # Fully replicated: BOTH products are replicated, because the FDA NTI method
    # compares s_WT with s_WR and a partial replicate cannot supply s_WT.
    full = sequences_for_design("replicate", is_nti=True)
    assert set(full.sequences) == {"TRTR", "RTRT"}
    assert full.n_periods == 4
    for seq in full.sequences:
        assert seq.count("T") == 2 and seq.count("R") == 2

    # Parallel: one period, two arms.
    par = sequences_for_design("parallel")
    assert set(par.sequences) == {"T", "R"}
    assert par.n_periods == 1


def test_partial_replicate_cannot_be_used_for_an_nti_drug():
    """An NTI study needs s_WT as well as s_WR; the 2x3x3 gives only s_WR."""
    nti = sequences_for_design("replicate", is_nti=True)
    for seq in nti.sequences:
        assert seq.count("T") >= 2, (
            "an NTI design must replicate the TEST product too, otherwise the "
            "within-subject variability comparison is impossible")


def test_n_periods_pins_the_replicate_variant():
    assert sequences_for_design("replicate", n_periods=4).n_periods == 4
    assert set(sequences_for_design("replicate", n_periods=3).sequences) == {
        "TRR", "RTR", "RRT"}
    # 3-period NTI study keeps both products replicated.
    three_nti = sequences_for_design("replicate", n_periods=3, is_nti=True)
    assert set(three_nti.sequences) == {"TRT", "RTR"}


def test_bad_design_vocabulary_is_rejected():
    with pytest.raises(ValueError):
        sequences_for_design("2x2")            # not this project's vocabulary
    with pytest.raises(ValueError):
        sequences_for_design("crossover", n_periods=3)   # 2x2x2 has 2 periods
    with pytest.raises(ValueError):
        sequences_for_design("parallel", n_periods=2)    # parallel has 1 period
    with pytest.raises(ValueError):
        sequences_for_design("replicate", n_periods=5)


# ---------------------------------------------------------------------------
# Reproducibility - the whole point of recording a seed
# ---------------------------------------------------------------------------


def test_same_seed_reproduces_a_byte_identical_list():
    a = generate_allocation("crossover", 24, seed=20260728)
    b = generate_allocation("crossover", 24, seed=20260728)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_different_seed_produces_a_different_list():
    a = generate_allocation("crossover", 24, seed=1)
    b = generate_allocation("crossover", 24, seed=2)
    seq_a = [e["sequence"] for e in a["unblinded_allocation"]["entries"]]
    seq_b = [e["sequence"] for e in b["unblinded_allocation"]["entries"]]
    assert seq_a != seq_b


def test_seed_is_recorded_so_the_list_can_be_regenerated():
    out = generate_allocation("crossover", 24, seed=987654321)
    repro = out["reproducibility"]
    assert repro["seed"] == 987654321
    assert repro["global_random_used"] is False
    assert out["engine_version"] == ENGINE_VERSION
    # The recorded call must contain everything needed to re-run it.
    call = repro["call"]
    for key in ("design_key", "n_enrolled", "seed", "n_periods", "block_size",
                "n_spare", "subject_id_prefix", "stratify_by"):
        assert key in call
    regenerated = generate_allocation(
        call["design_key"], call["n_enrolled"], call["seed"],
        call["stratify_by"], call["n_periods"],
        block_size=call["block_size"], n_spare=call["n_spare"],
        subject_id_prefix=call["subject_id_prefix"],
    )
    assert (regenerated["unblinded_allocation"]["entries"]
            == out["unblinded_allocation"]["entries"])


def test_global_random_state_is_never_touched():
    """Using the module-level `random` would make the list irreproducible and
    would also perturb every other consumer of the global generator."""
    random.seed(4242)
    expected = [random.random() for _ in range(3)]

    random.seed(4242)
    generate_allocation("replicate", 36, seed=7, stratify_by=["sex"])
    actual = [random.random() for _ in range(3)]

    assert actual == expected


def test_stream_seed_derivation_is_stable_across_processes():
    """hash() of a str is salted by PYTHONHASHSEED; SHA-256 is not. Pinning a
    literal here fails loudly if the derivation is ever swapped for hash()."""
    assert _derive_seed(42, "ALL") == _derive_seed(42, "ALL")
    assert _derive_seed(42, "ST01") != _derive_seed(42, "ST02")
    assert _derive_seed(1, "ALL") != _derive_seed(2, "ALL")
    # Fixed expected value: sha256("42|ALL")[:16] as an int.
    assert _derive_seed(42, "ALL") == 0x08EEF5345D41C14C


# ---------------------------------------------------------------------------
# Permuted blocks - the balance guarantee
# ---------------------------------------------------------------------------


def _blocks(entries):
    """Group entries by their (stratum, block) key, preserving list order."""
    grouped: dict[str, list[dict]] = {}
    for e in entries:
        grouped.setdefault(e["block"], []).append(e)
    return grouped


@pytest.mark.parametrize(
    "design_key,is_nti,n",
    [("crossover", None, 24), ("parallel", None, 40),
     ("replicate", False, 36), ("replicate", True, 32)],
)
def test_every_completed_block_is_exactly_balanced(design_key, is_nti, n):
    out = generate_allocation(design_key, n, seed=99, is_nti=is_nti)
    block_size = out["block"]["size"]
    n_seq = out["block"]["n_sequences"]
    repeats = block_size // n_seq
    sequences = out["design"]["sequences"]

    for block_id, entries in _blocks(out["unblinded_allocation"]["entries"]).items():
        assert len(entries) == block_size, f"{block_id} is not a full block"
        for seq in sequences:
            got = sum(1 for e in entries if e["sequence"] == seq)
            assert got == repeats, (
                f"{block_id}: sequence {seq} appears {got} times, expected {repeats}")
        # Positions are 1..block_size with no gaps or repeats.
        assert sorted(e["position_in_block"] for e in entries) == list(
            range(1, block_size + 1))


@pytest.mark.parametrize(
    "design_key,is_nti,expected_sequences",
    [("crossover", None, 2), ("parallel", None, 2),
     ("replicate", False, 3), ("replicate", True, 2)],
)
def test_default_block_size_is_a_valid_multiple(design_key, is_nti, expected_sequences):
    out = generate_allocation(design_key, 30, seed=5, is_nti=is_nti)
    block = out["block"]
    assert block["n_sequences"] == expected_sequences
    assert block["size"] % block["n_sequences"] == 0, (
        "a block that is not a whole multiple of the sequence count can never "
        "contain each sequence equally often")
    assert block["size"] == expected_sequences * SEQUENCE_REPEATS_PER_BLOCK
    assert block["is_multiple_of_n_sequences"] is True


def test_block_size_that_is_not_a_multiple_is_rejected():
    """The classic implementation error: a block of 5 across 2 sequences can
    never be balanced, yet nothing in a naive implementation complains."""
    with pytest.raises(ValueError) as exc:
        generate_allocation("crossover", 24, seed=1, block_size=5)
    assert "multiple" in str(exc.value).lower()

    # 4 is fine for 2 sequences but NOT for the 3-sequence partial replicate.
    generate_allocation("crossover", 24, seed=1, block_size=4)
    with pytest.raises(ValueError):
        generate_allocation("replicate", 36, seed=1, is_nti=False, block_size=4)
    # 6 works for the partial replicate (3 sequences x 2).
    ok = generate_allocation("replicate", 36, seed=1, is_nti=False, block_size=6)
    assert ok["block"]["size"] == 6


def test_block_equal_to_sequence_count_is_allowed_but_flagged():
    out = generate_allocation("crossover", 24, seed=1, block_size=2)
    assert out["block"]["size"] == 2
    caution = next(c for c in out["cautions"]
                   if c["key"] == "block_equals_sequence_count")
    assert "determined" in caution["text"].lower(), (
        "a block as small as the sequence count is fully predictable and must be "
        "flagged")
    # The caution states the block size in words, so it belongs to the
    # restricted record only.
    assert caution["disclosable"] is False


def test_transient_imbalance_never_exceeds_the_theoretical_bound():
    out = generate_allocation("crossover", 26, seed=31)
    bal = out["balance"]
    assert bal["max_transient_imbalance_realised"] <= (
        bal["max_transient_imbalance_theoretical"])
    assert bal["max_transient_imbalance_theoretical"] == (
        out["block"]["size"] // out["block"]["n_sequences"])
    # Walk it independently rather than trusting the engine's own number.
    counts = {s: 0 for s in out["design"]["sequences"]}
    worst = 0
    for e in out["unblinded_allocation"]["entries"]:
        if e["is_replacement"]:
            continue
        counts[e["sequence"]] += 1
        worst = max(worst, max(counts.values()) - min(counts.values()))
    assert worst <= bal["max_transient_imbalance_theoretical"]


def test_full_list_is_exactly_balanced_overall():
    out = generate_allocation("replicate", 36, seed=8, is_nti=False)
    counts = out["balance"]["counts_full_list"]
    assert len(set(counts.values())) == 1, "complete blocks must balance exactly"


# ---------------------------------------------------------------------------
# Stratified randomization
# ---------------------------------------------------------------------------


def test_each_stratum_gets_its_own_independent_block_sequence():
    out = generate_allocation(
        "crossover", 48, seed=11, stratify_by=["sex", "body_size"])
    strat = out["stratification"]
    assert strat["applied"] is True
    assert strat["n_strata"] == 4          # 2 sexes x 2 BMI bands
    assert len(strat["strata"]) == 4

    entries = out["unblinded_allocation"]["entries"]
    stream_ids = {e["stratum_id"] for e in entries}
    assert len(stream_ids) == 4

    # Different strata must not share an allocation order, or they are not
    # independent streams.
    per_stream = {}
    for e in entries:
        per_stream.setdefault(e["stratum_id"], []).append(e["sequence"])
    orders = [tuple(v) for v in per_stream.values()]
    assert len(set(orders)) > 1, "strata are drawing from the same stream"

    # Every stratum is balanced on its own, not merely in aggregate.
    for sid, counts in out["balance"]["by_stratum_counts"].items():
        assert len(set(counts.values())) == 1, f"{sid} is not internally balanced"


def test_stratum_levels_are_carried_on_every_entry():
    out = generate_allocation("parallel", 40, seed=3, stratify_by=["sex"])
    levels = {tuple(sorted(e["stratum"].items()))
              for e in out["unblinded_allocation"]["entries"]}
    assert levels == {(("sex", "Male"),), (("sex", "Female"),)}


def test_over_stratification_is_refused_with_a_stated_reason():
    """More strata than the sample size can fill leaves partial blocks
    everywhere - MORE imbalance than stratifying was meant to prevent."""
    out = generate_allocation(
        "crossover", 16, seed=1,
        stratify_by=["sex", "body_size", "age", "ancestry"],
    )
    # Block size 4, 16 subjects -> at most 4 strata can each fill one block.
    assert out["stratification"]["n_strata"] <= 4
    assert out["stratification"]["dropped_factors"], "should have dropped factors"
    for d in out["stratification"]["dropped_factors"]:
        assert d["reason"], "a dropped factor must say why"
        assert d["key"]


def test_eligibility_narrowed_factors_are_considered_last():
    """Stratification slots are scarce. Spending one on a characteristic the
    entry criteria have already flattened - while dropping an unconstrained
    covariate like sex - would waste it."""
    factors = [
        # Upstream (PK-impact) order deliberately puts the narrowed factor first.
        {"key": "renal_function", "narrowed_by_eligibility": True},
        {"key": "sex", "narrowed_by_eligibility": False},
    ]
    # 8 subjects leave room for exactly one 2-level factor, so the ranking is
    # what decides which one survives.
    out = generate_allocation("crossover", 8, seed=1, stratify_by=factors)
    kept = [f["key"] for f in out["stratification"]["factors"]]
    dropped = [d["key"] for d in out["stratification"]["dropped_factors"]]
    assert kept == ["sex"]
    assert dropped == ["renal_function"]
    reason = out["stratification"]["dropped_factors"][0]["reason"].lower()
    assert "eligibility criteria" in reason


def test_dropped_factor_reason_does_not_disclose_the_block_size():
    """The reason is repeated verbatim into the AI directive, and the block
    length is withheld from the protocol body (ICH E9 sect. 2.3.2)."""
    out = generate_allocation(
        "crossover", 16, seed=1,
        stratify_by=["sex", "body_size", "age", "ancestry"],
    )
    size = out["block"]["size"]
    for d in out["stratification"]["dropped_factors"]:
        assert "block size" not in d["reason"].lower()
        assert "block size %d" % size not in d["reason"]


def test_tiny_study_falls_back_to_unstratified():
    out = generate_allocation("crossover", 6, seed=1, stratify_by=["sex", "body_size"])
    assert out["stratification"]["n_strata"] in (1, 2)
    if not out["stratification"]["applied"]:
        assert out["stratification"]["method"]


def test_consumes_the_stratification_engine_result_directly():
    """The two engines must interoperate without the caller reshaping anything."""
    from services.stratification import recommend_stratification

    strat = recommend_stratification(
        design_key="parallel",
        label={"pharmacokinetics": "Excreted unchanged in the urine; CYP2D6 substrate."},
    )
    out = generate_allocation("parallel", 60, seed=17, stratify_by=strat)
    assert out["stratification"]["applied"] is True
    used = {f["key"] for f in out["stratification"]["factors"]}
    recommended = {f["key"] for f in strat["factors"]}
    assert used <= recommended, "must not invent factors the strat engine did not rank"


def test_every_stratification_factor_has_a_randomization_stratum():
    """Hard coupling, pinned deliberately. generate_allocation() is handed the
    stratification engine's whole result, and a factor key with no stratum
    catalogue entry (and no explicit levels) raises. Without this test that
    failure would surface as a crash during live protocol generation the first
    time someone adds a factor to services/stratification.py; here it surfaces
    as a red test naming the missing key."""
    from services.randomization import _STRATUM_BY_KEY
    from services.stratification import _CATALOGUE

    missing = [f.key for f in _CATALOGUE if f.key not in _STRATUM_BY_KEY]
    assert not missing, (
        "services/stratification.py ranks these factors but "
        "services/randomization.py has no levels to randomise them into: "
        f"{missing}. Add a _STRATUM_CATALOGUE entry with a defensible cut-point.")

    # And the levels must actually partition a cohort, or the stratum is a no-op.
    for f in _CATALOGUE:
        levels = _STRATUM_BY_KEY[f.key].levels
        assert len(levels) >= 2, f"{f.key} has fewer than two levels"
        assert len(set(levels)) == len(levels), f"{f.key} has duplicate levels"
        assert _STRATUM_BY_KEY[f.key].basis, f"{f.key} cut-point has no stated basis"


def test_genotype_stratum_raises_the_operational_caution():
    out = generate_allocation(
        "parallel", 60, seed=2, stratify_by=["cyp_polymorphism"])
    assert any("genotype" in c["text"].lower() for c in out["cautions"])
    # Operational, not concealment-sensitive: the site cannot plan the screening
    # window unless the protocol says it.
    assert all(c["disclosable"] for c in out["cautions"]
               if c["key"] == "genotype_turnaround")
    factor = out["stratification"]["factors"][0]
    assert factor["ascertainment"] == "prospective_genotyping"
    assert factor["ascertainment_note"]


def test_unknown_stratification_key_is_rejected():
    with pytest.raises(ValueError):
        generate_allocation("crossover", 24, seed=1, stratify_by=["astrological_sign"])


# ---------------------------------------------------------------------------
# Replacement / spare tail
# ---------------------------------------------------------------------------


def test_spare_tail_exists_and_is_itself_balanced():
    out = generate_allocation("crossover", 24, seed=13)
    spares = [e for e in out["unblinded_allocation"]["entries"] if e["is_replacement"]]
    assert spares, "BE studies replace dropouts; the list needs a reserve tail"
    assert out["replacement"]["n_replacement_slots_per_stream"] == len(spares)
    # Reserve allocations are complete blocks, so the tail is balanced too.
    counts = {s: 0 for s in out["design"]["sequences"]}
    for e in spares:
        counts[e["sequence"]] += 1
    assert len(set(counts.values())) == 1
    assert all(e["role"] == "replacement" for e in spares)


def test_replacement_rule_preserves_sequence_balance():
    out = generate_allocation("crossover", 24, seed=13)
    rule = out["replacement"]["rule"].lower()
    assert "same" in rule and "sequence" in rule, (
        "replacing a withdrawal on a different sequence would unbalance the "
        "completed blocks")


def test_explicit_spare_count_is_honoured_and_rounded_to_whole_blocks():
    out = generate_allocation("crossover", 24, seed=13, n_spare=5)
    spares = [e for e in out["unblinded_allocation"]["entries"] if e["is_replacement"]]
    assert len(spares) >= 5
    assert len(spares) % out["block"]["size"] == 0

    none = generate_allocation("crossover", 24, seed=13, n_spare=0)
    assert not [e for e in none["unblinded_allocation"]["entries"]
                if e["is_replacement"]]


# ---------------------------------------------------------------------------
# Blinding - the data model must not let the list leak
# ---------------------------------------------------------------------------


def test_blinded_log_carries_no_treatment_information():
    out = generate_allocation("crossover", 24, seed=21, stratify_by=["sex"])
    sequences = set(out["design"]["sequences"])
    log = out["blinded_enrolment_log"]
    assert log["contains_treatment_assignments"] is False
    assert log["n_entries"] == out["unblinded_allocation"]["n_entries"]
    for e in log["entries"]:
        assert "sequence" not in e
        assert "treatment_by_period" not in e
        # Block membership is withheld too: block identity plus one leaked
        # assignment narrows the rest of that block.
        assert "block" not in e and "position_in_block" not in e
        # No field anywhere in the entry may carry a treatment code, including
        # inside the stratum sub-dict.
        flat = [v for v in e.values() if isinstance(v, str)]
        flat += [str(v) for v in e["stratum"].values()]
        for value in flat:
            assert value not in sequences


def test_unblinded_list_is_marked_restricted():
    out = generate_allocation("crossover", 24, seed=21)
    unb = out["unblinded_allocation"]
    assert unb["confidentiality"] == "RESTRICTED"
    assert unb["contains_treatment_assignments"] is True
    assert "protocol body" in unb["warning"].lower()
    assert unb["holders"]


def test_blinded_view_strips_the_list_and_the_block_size():
    out = generate_allocation("crossover", 24, seed=21)
    view = blinded_view(out)
    assert "unblinded_allocation" not in view
    assert view["blinded_enrolment_log"]["entries"]
    assert view["block"]["disclosed"] is False
    assert "size" not in view["block"]
    # The transient bound equals the repeats per block, so it must go too.
    assert "max_transient_imbalance_theoretical" not in view["balance"]
    blob = json.dumps(view)
    assert "method_detail" not in blob
    assert blinded_view({}) == {}


def test_blinded_view_does_not_carry_the_seed():
    """The seed regenerates the list entry for entry - that is the whole reason
    it is recorded. Shipping it with the blinded copy would hand a blinded
    reader the unblinded list via one function call."""
    seed = 987654321
    out = generate_allocation("crossover", 24, seed=seed, stratify_by=["sex"])
    view = blinded_view(out)

    assert str(seed) not in json.dumps(view)
    assert "seed" not in view["reproducibility"]
    assert "call" not in view["reproducibility"], (
        "the recorded call is enough to re-run the engine")
    # The audit CLAIM survives - a blinded reader is entitled to know the
    # schedule is reproducible on demand, just not to do it themselves.
    assert view["reproducibility"]["seed_recorded"] is True
    assert view["reproducibility"]["seed_disclosed"] is False
    assert "regenerated" in view["reproducibility"]["statement"]


def test_blinded_view_leaves_no_arithmetic_path_back_to_the_block_size():
    """Withholding `block.size` is worthless if the number can be divided back
    out of the slot counts, read off the repeats assumption, or simply read in a
    caution that states it in words."""
    out = generate_allocation(
        "crossover", 24, seed=42, stratify_by=["sex"], block_size=2)
    size = out["block"]["size"]
    view = blinded_view(out)

    # 1. The repeats-per-block assumption: block = n_sequences x repeats, and
    #    the sequence set is public.
    names = {a["name"] for a in view["assumptions"]}
    assert "SEQUENCE_REPEATS_PER_BLOCK" not in names

    # 2. Slot counts are rounded up to whole blocks, so they divide back to it.
    for stratum in view["stratification"]["strata"]:
        for leaky in ("primary_slots", "replacement_slots", "n_blocks"):
            assert leaky not in stratum, (
                f"{leaky} lets the block size be recovered by division")
    assert "n_replacement_slots_per_stream" not in view["replacement"]
    assert "n_replacement_slots_total" not in view["replacement"]

    # 3. A caution can state the block size in plain words.
    for c in view["cautions"]:
        assert c["disclosable"] is True
        assert "block size" not in c["text"].lower()

    # The removals must be declared, not silent.
    assert view["withheld"]["fields"] and view["withheld"]["reason"]

    # Independent sweep: no integer field anywhere in the blinded view equals
    # the block size in a slot-like context.
    def _walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from _walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from _walk(v, f"{path}[{i}]")
        else:
            yield path, node

    for path, value in _walk(view):
        if any(t in path for t in ("slots", "n_blocks", "block_size", "repeats")):
            assert not isinstance(value, int) or value != size, (
                f"{path} = {value} recovers the block size")


def test_treatment_by_period_matches_the_sequence():
    out = generate_allocation("replicate", 32, seed=6, is_nti=True)
    for e in out["unblinded_allocation"]["entries"]:
        by_period = e["treatment_by_period"]
        assert list(by_period.keys()) == ["1", "2", "3", "4"]
        assert "".join(by_period[str(p)] for p in range(1, 5)) == e["sequence"]


def test_parallel_has_a_single_period_and_arms_not_sequences():
    out = generate_allocation("parallel", 40, seed=4)
    assert out["design"]["assignment_term"] == "treatment arm"
    for e in out["unblinded_allocation"]["entries"]:
        assert e["sequence"] in ("T", "R")
        assert e["treatment_by_period"] == {"1": e["sequence"]}


# ---------------------------------------------------------------------------
# Label-grounded blinding assessment
# ---------------------------------------------------------------------------


def test_chromaturia_in_the_label_defeats_subject_masking():
    label = {"clinical_pharmacology": (
        "Rifampin may produce a reddish orange discoloration of the urine, sweat "
        "and tears."
    )}
    out = assess_blinding(label)
    keys = out["treatment_masking"]["signals_detected"]
    assert "visible_pharmacological_cue" in keys
    sig = next(s for s in out["treatment_masking"]["signals"]
               if s["key"] == "visible_pharmacological_cue")
    assert sig["evidence"], "the judgement must cite the label text it rests on"
    assert "urine" in sig["evidence"].lower()


def test_intravenous_in_pharmacology_does_not_flag_a_parenteral_product():
    """An oral drug's Clinical Pharmacology routinely mentions an IV comparison
    for absolute bioavailability; that is not the product's presentation."""
    label = {"clinical_pharmacology": (
        "Absolute bioavailability was determined against an intravenous reference "
        "dose administered by infusion."
    )}
    out = assess_blinding(label)
    assert "parenteral_presentation" not in out["treatment_masking"]["signals_detected"]

    injectable = {"dosage_and_administration": (
        "Administer 40 mg by subcutaneous injection using the prefilled syringe."
    )}
    out2 = assess_blinding(injectable)
    assert "parenteral_presentation" in out2["treatment_masking"]["signals_detected"]


def test_pd_coprimary_forces_a_double_blind():
    open_label = assess_blinding(None)["treatment_masking"]["recommended"]
    assert open_label == "open_label_dosing_with_blinded_bioanalysis"

    pd = assess_blinding(None, pd_coprimary=True)["treatment_masking"]
    assert pd["recommended"] == "double_blind"
    assert "pharmacodynamic" in pd["rationale"].lower()


def test_allocation_concealment_is_required_even_when_open_label():
    """Concealment and masking are different things; conflating them is the
    classic error that lets selection bias into an 'open-label' BE study."""
    out = generate_allocation("crossover", 24, seed=1)
    blind = out["blinding"]
    assert blind["treatment_masking"]["recommended"] == (
        "open_label_dosing_with_blinded_bioanalysis")
    assert blind["allocation_concealment"]["required"] is True
    assert blind["allocation_concealment"]["applies_even_when_open_label"] is True
    assert blind["bioanalytical_blinding"]["required"] is True
    assert blind["unblinding_procedure"]["statement"]


def test_over_encapsulation_caveat_is_stated_not_glossed():
    """Over-encapsulating to blind a BE study can alter dissolution - a real
    regulatory issue that the mitigation must not paper over."""
    label = {"how_supplied": "White, film-coated, oval tablets debossed with 'A10'."}
    out = assess_blinding(label)
    sig = next(s for s in out["treatment_masking"]["signals"]
               if s["key"] == "distinguishable_appearance")
    assert "dissolution" in sig["mitigation"].lower()


# ---------------------------------------------------------------------------
# Directive - locked facts for the prompt, with nothing leaked
# ---------------------------------------------------------------------------


def test_directive_locks_the_method_for_the_prompt():
    out = generate_allocation("crossover", 24, seed=1, stratify_by=["sex"])
    directive = randomization_directive(out)
    assert "ALREADY DETERMINED" in directive
    assert out["design"]["label"] in directive
    assert "TR / RT" in directive
    assert randomization_directive({}) == ""
    assert randomization_directive(None) == ""


def test_directive_leaks_neither_assignments_nor_the_block_size():
    out = generate_allocation("crossover", 24, seed=1)
    directive = randomization_directive(out)
    for e in out["unblinded_allocation"]["entries"][:5]:
        assert e["subject_id"] not in directive
    # The block size must not appear, and neither may anything it can be
    # recovered from: "each sequence appears N times per block" and "the counts
    # can differ by at most N" both hand it straight back.
    assert out["block"]["method_detail"] not in directive
    assert "time(s)" not in directive
    assert "differ by more than" not in directive
    assert "block size %d" % out["block"]["size"] not in directive

    # Same guarantee when a dropped stratification factor adds its own prose.
    stratified = generate_allocation(
        "crossover", 16, seed=1, stratify_by=["sex", "body_size", "age", "ancestry"])
    d2 = randomization_directive(stratified)
    assert "block size %d" % stratified["block"]["size"] not in d2
    assert "time(s)" not in d2
    assert "undisclosed length" in directive
    assert "do NOT reproduce" in directive


def test_directive_is_identical_whether_built_from_the_full_or_blinded_result():
    """The prompt context is fed the BLINDED view (the raw result carries the
    list and the seed). If the directive drew on anything restricted it would
    silently degrade there - and an empty confidentiality clause is exactly the
    failure that would let the model print allocations. Byte-identical output
    from both inputs proves the directive uses only disclosable material."""
    out = generate_allocation(
        "replicate", 36, seed=5, is_nti=False, stratify_by=["sex"],
        label={"how_supplied": "White capsules."},
    )
    from_full = randomization_directive(out)
    from_blinded = randomization_directive(blinded_view(out))
    assert from_blinded == from_full
    # Both must still carry the confidentiality clause and its exact wording.
    assert "do NOT reproduce" in from_blinded
    assert "undisclosed length" in from_blinded
    assert "ICH E9" in from_blinded


def test_directive_is_ascii_safe_for_a_windows_console():
    out = generate_allocation("replicate", 36, seed=1, is_nti=False,
                              stratify_by=["sex"],
                              label={"how_supplied": "White capsules."})
    directive = randomization_directive(out)
    directive.encode("ascii")           # raises if a non-ASCII char crept in
    format_unblinded_list(out).encode("ascii")


def test_unblinded_csv_is_marked_restricted_and_complete():
    out = generate_allocation("crossover", 12, seed=1)
    csv = format_unblinded_list(out)
    lines = csv.splitlines()
    assert lines[0].startswith("# RESTRICTED")
    header = next(ln for ln in lines if ln.startswith("subject_id"))
    assert "sequence" in header
    body = [ln for ln in lines if ln and not ln.startswith("#") and ln != header]
    assert len(body) == out["unblinded_allocation"]["n_entries"]
    assert format_unblinded_list({}) == ""


# ---------------------------------------------------------------------------
# Assumptions, validation, serialisation
# ---------------------------------------------------------------------------


def test_named_assumptions_are_surfaced_for_disclosure():
    out = generate_allocation("crossover", 24, seed=1, stratify_by=["sex"])
    names = {a["name"] for a in out["assumptions"]}
    assert {"SEQUENCE_REPEATS_PER_BLOCK", "SPARE_FRACTION",
            "STRATUM_HEADROOM_FACTOR", "MIN_BLOCKS_PER_STRATUM"} <= names
    for a in out["assumptions"]:
        assert a["why"], "an assumption the protocol discloses must say why"


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        generate_allocation("crossover", 24, seed=1.5)      # non-integer seed
    with pytest.raises(ValueError):
        generate_allocation("crossover", 24, seed=True)     # bool is not a seed
    with pytest.raises(ValueError):
        generate_allocation("crossover", 1, seed=1)         # nonsensical N
    with pytest.raises(ValueError):
        generate_allocation("crossover", 24, seed=1, n_spare=-1)
    with pytest.raises(ValueError):
        generate_allocation("crossover", 24, seed=1, subject_id_prefix="")


def test_subject_ids_are_unique_and_ordered():
    out = generate_allocation("crossover", 24, seed=1, stratify_by=["sex"])
    ids = [e["subject_id"] for e in out["unblinded_allocation"]["entries"]]
    assert len(ids) == len(set(ids)), "duplicate subject numbers"
    blinded_ids = [e["subject_id"] for e in out["blinded_enrolment_log"]["entries"]]
    assert blinded_ids == ids


def test_result_is_json_serialisable_for_population_config():
    """It is persisted inside population_config (JSONB), so it must round-trip
    with no tuples, sets, or integer dict keys surviving as such."""
    out = generate_allocation(
        "replicate", 36, seed=2026, stratify_by=["sex"], is_nti=False,
        label={"how_supplied": "Blue, film-coated tablets."},
    )
    restored = json.loads(json.dumps(out))
    assert restored == out
    assert json.loads(json.dumps(blinded_view(out))) == blinded_view(out)


def test_summary_reads_as_protocol_prose():
    out = generate_allocation("crossover", 24, seed=1, stratify_by=["sex"])
    summary = out["summary"]
    assert "permuted blocks" in summary
    assert "stratified" in summary.lower()
    assert "seed" in summary.lower(), "the audit trail belongs in the narrative"
