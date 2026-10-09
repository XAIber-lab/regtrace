# Circular 285 update 15 to 16: Stage 4 run summary

The frozen primary and independent GPT-5.6 Sol calls completed successfully on
9 August 2026. The run reused five existing Stage 3 annotations and supplied
117 framework rows, 117 General Prototype rows, and 152 Circular-285-related
controls.

## Output

- Primary change-level contextualizations: 5
- Primary prototype action records: 6
- Primary control action records: 3
- Unique modified prototype rows after merge: 5
- Unique modified control rows after merge: 3
- Additions, deprecations, or forced-review rows: 0

## Stability

- Relevance decisions matched for all five changes.
- All six primary prototype actions had an exact action/target match.
- Two of the three primary control targets matched exactly.
- Overall change-qualified exact-action Jaccard: 0.80.
- Mean per-change framework-mapping Jaccard: 0.84.

The unmatched control action for the final ICT-security change is the first
manual-review priority. The agreement figures do not establish semantic or
legal correctness.

## Cost

The primary call cost USD 0.737900. Prompt caching reduced the independent
call to USD 0.203707. Total incremental API cost was USD 0.941607. The
author's manual review time was not measured and is not included.

## Manual post-hoc audit

The manual audit was performed by the author as the sole reviewer. It was not
independently adjudicated. The authoritative
EBA/GL/2014/12 text was resolved for this audit but was not added retrospectively
to the model bundle.

- Change relevance: 5 correct of 5.
- Changes needing artifact action: 3; 2 had sufficient apparent coverage and
  1 was seriously incomplete.
- One change, the broad incorporation of EBA/GL/2014/12, was judged seriously
  incomplete because the output did not cover the wider operative guideline.
- Action dispositions: 4 accept, 5 revise, 0 reject.
- Targets: 7 correct, 2 plausible, 0 incorrect.
- Mappings: 7 correct, 2 partly correct, 0 incorrect.
- Wording: 6 grounded, 3 partly inferred, 0 unsupported.
- Legal references: 4 complete, 5 incomplete, 0 incorrect.

These figures are descriptive diagnostics for this selected pair. They do not
constitute a domain-expert gold standard or a general estimate of downstream
precision.
