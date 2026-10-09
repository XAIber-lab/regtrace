# Stage 4 evaluation case-selection protocol

Selection frozen before the new contextualization model call on 9 August 2026.

## Objective

Select one consecutive Circular 285 comparison that limits API and reviewer
cost while containing at least one operationally meaningful cybersecurity
change suitable for downstream contextualization evaluation.

## Candidate screening

Candidate directories were restricted to consecutive comparisons (`n` to
`n+1`) with an existing normalized unified diff and semantic-annotation JSON.
They were ranked by normalized diff line count. The six smallest usable pairs
were:

| Pair | Diff lines | Diff size (KB) | Semantic annotations |
|---|---:|---:|---:|
| 42 to 43 | 298 | 20.6 | 8 |
| 46 to 47 | 308 | 18.5 | 3 |
| 45 to 46 | 559 | 18.6 | 5 |
| 15 to 16 | 709 | 48.5 | 5 |
| 22 to 23 | 984 | 75.7 | 4 |
| 30 to 31 | 1,113 | 32.5 | 1 |

## Selected pair

**Circular 285, update 15 to update 16**

The pair is consecutive and is the fourth-smallest usable consecutive diff in
the retained corpus. It was selected over the three smaller pairs because its
five pre-existing semantic annotations include a coherent cybersecurity episode:

1. creation of a section on internet-payment security;
2. replacement of ECB recommendations with EBA internet-payment-security
   guidelines;
3. a scope change for non-EU bank branches providing internet payment services;
4. new compliance obligations for banks offering internet payment services; and
5. an explicit update to ICT security controls and monitoring.

The selection is based on diff size and the subject matter of the already
available Stage 3 annotations, not on any newly generated Stage 4 result.

## Frozen run design

- Source annotations: `reproducibility_artifacts/stage4_circ285_evaluation/prepared/source_annotations.json`
- Framework core: `reference/Framework_v2.0_core_ITA.xlsx` (matching the legacy version-2.0 prototype identifiers)
- Existing prototype rows: `contextualization_prototype_rows` in the frozen input bundle
- Existing controls: `existing_control_rows` in the frozen input bundle
- Model: `gpt-5.6-sol`
- Reasoning effort: `medium`
- Output language: Italian
- Independent second Stage 4 query: enabled for self-consistency assessment
- Maximum controls supplied to the model: 180

Version 2.1 is the project target framework. This frozen run used existing version-2.0 prototype artifacts and the matching version-2.0 core so that identifier validation did not mix framework generations; migration to version 2.1 was outside the evaluated scope.

The input bundle must be prepared and inspected before either paid query. The
run is to be stopped if the expected artifact scope is not recovered.

## Pre-call inspection

The no-API preparation step completed successfully and recovered:

- 5 semantic changes;
- 117 framework-core rows;
- 117 existing General Prototype rows; and
- 152 existing Circular-285-related controls.

The prepared JSON bundle is 315.8 KB. This remained within the expected
scope, so the frozen two-query design was retained.

## Executed run

Both planned calls completed successfully with `gpt-5.6-sol` at medium
reasoning effort. Existing Stage 3 annotations were reused, so this experiment
incurred only Stage 4 model cost.

| Call | Input | Cached input | Cache write | Output | Cost (USD) |
|---|---:|---:|---:|---:|---:|
| Primary | 94,367 | 0 | 94,364 | 4,937 | 0.737900 |
| Independent | 94,367 | 94,364 | 0 | 5,217 | 0.203707 |
| **Total** |  |  |  |  | **0.941607** |

The primary output contains five change-level contextualizations, six prototype
action records, and three control action records. Workbook merging resolved
these to five unique modified prototype rows and three modified control rows.

## Pre-review stability evidence

- Cybersecurity-relevance agreement: 5 of 5 changes.
- Exact prototype-action agreement: 6 of 6 primary records; Jaccard 1.00.
- Exact control-action agreement: two shared targets, one primary-only target,
  and one independent-only target; Jaccard 0.50.
- Overall change-qualified exact-action Jaccard: 0.80.
- Mean per-change framework-mapping Jaccard: 0.84.

The principal disagreement concerns the control target for
`change_005_security_controls_and_monitoring_update`: the primary call targeted
control row 149 / ID 148, whereas the independent call targeted row 133 / ID
132. This is a reviewer-priority case. These agreement measures concern output
stability only and are not treated as evidence that the proposed mappings or
control wording are correct.
