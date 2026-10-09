# Reproducibility artifacts

This directory retains the two Stage 4 artifact sets reported in the thesis. It separates the end-to-end SCA illustration from the small-sample Circular 285 evaluation.

Both retained studies operate on legacy version-2.0 prototype identifiers and use the matching version-2.0 core. Version 2.1 is the project target, but migration of these existing prototypes to version 2.1 is outside the evaluated scope.

## `sca_case_study_20261004/`

This is the SCA worked example cited in Section 4.6. The selected Stage 4 call used `gpt-5.6-sol` at medium reasoning effort and produced one prototype reference addition and one new control for `change_003_interface_change_deadline` (Article 30(4a)).

- `example/` retains the two input PDFs, normalized Markdown documents, line-to-page maps, numbered diff, three Stage 3 annotations, focused Stage 4 input bundle, prompt, schema, output and API metadata.
- `example/inputs/` contains the framework core, payment-services prototype and controls workbook used by the selected call.

## `stage4_circ285_evaluation/`

This is the frozen Circular 285 update-15-to-16 evaluation reported in the results chapter.

- `SELECTION_PROTOCOL.md` records the selection rule, frozen inputs, model configuration, token usage, cost, and stability metrics.
- `input_pdfs/` retains the two compared Circular 285 versions.
- `prepared/` retains the Stage 3 source annotations, input bundle, and schemas inspected before the paid calls.
- `run/` retains the identical executed bundle, primary and independent outputs, API metadata, and generated review workbooks.
- `RUN_SUMMARY.md` summarizes the numbers of changes, proposed actions and review outcomes.

The primary run processed 5 semantic changes and produced 6 prototype actions and 3 control actions. Four actions were accepted unchanged, five required revision, and none was rejected.

The authoritative EBA/GL/2014/12 text was resolved for the post-hoc audit but was not supplied to either model call. That boundary is preserved in the retained input bundle.
