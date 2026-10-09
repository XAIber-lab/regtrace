# RegTrace: Regulatory Changes Tracker

This project contains the implementation used to practically validate the methodology developed in the master’s thesis *A Methodology for Tracking and Managing Cybersecurity Regulatory Changes in Rapidly Evolving Sectors: The Case of the Financial Sector*. The methodology addresses the evolution of regulatory and standard sources in cybersecurity contextualisation artifacts.

Its objective is not to reconstruct a contextualisation prototype whenever a regulatory source changes. Given a source document version, a target document version, and an existing prototype containing mappings to the source version, the implementation supports a traceable update process. It identifies candidate changes, annotates their relevance, relates them to existing controls, and produces candidate updates for human validation.

Circular 285 of the Bank of Italy is used as a validation case. The Italian National Framework for Cybersecurity and Data Protection 2.1 provides the operational reference framework; neither is intended to limit the methodology to a single source or framework.

Version 2.1 is the project target. The retained Stage 4 studies operate on legacy version-2.0 prototypes and therefore use the matching version-2.0 core for identifier validation; migration of those existing artifacts to version 2.1 is outside the evaluated scope.

## Workflow

### Proof of concept

Run `proof_of_concept\server.py --port <PORT>` and open the prototype on `localhost:PORT`.
Install `requirements.txt` in the virtual environment first. The interface uses
Python's standard library and plain HTML, CSS, and JavaScript. It presents the
SCA worked example from thesis Section 4.6: all three Stage 3 annotations,
one focused Stage 4 prototype-reference addition, and one new control. Evidence
navigation, local review decisions and an export with SHA-256 hashes are available.
The interface includes a comparison selector and a New comparison workflow. New runs
and reviews are stored in `outputs/proof_of_concept/`. Use `--port 8766` if needed.
For the focused update view, open `http://127.0.0.1:8765/?demo=sca`.
The selected output is retained
in [`reproducibility_artifacts/sca_case_study_20261004`](reproducibility_artifacts/sca_case_study_20261004).

### 1. Document normalization

The source and target document versions are converted from PDF into comparable Markdown representations. The process performs soft normalization, produces a line-to-page map, and can apply approved low-risk cleanup rules. The resulting Markdown preserves the textual evidence used in later stages and supports traceability back to the source pages.

### 2. Syntactic diff generation

The normalized document versions are compared through a unified, numbered diff. This identifies observable textual and structural differences and groups them into comparable change units. The diff is the input to the semantic analysis stage; it does not by itself establish the regulatory relevance of a difference.

### 3. Semantic change annotation

The numbered differences are transformed into structured candidate change annotations using the taxonomy and JSON schema. For each relevant change, the annotation can record the affected provision, the type and relevance of the change, taxonomy labels, and supporting rationale. The language-model component is used as decision support: its outputs are candidate annotations, not autonomous final decisions.

### 4. Contextualization prototype update

Stage 4 uses one constrained action-generation call with the semantic annotations, framework and selected workbook rows. The prompt favours minimal edits to controls addressing the same obligation, concise new controls in the input catalogue's style, canonical prototype descriptions and explicit evidence boundaries. Complete control wording and changed passages are supplied. Candidate actions are validated against the output schema, assigned confidence scores and exported to review workbooks. These workbooks preserve baseline cells and record proposals for human review.

Human review remains responsible for validating interpretations and final modifications throughout the workflow.

## Validation inputs and outputs

| Artifact | Role |
| --- | --- |
| Source and target document versions | Regulatory, supervisory, or standard sources whose evolution is analysed. |
| Existing contextualisation prototype | An artifact containing mappings derived from the source version, potentially alongside mappings from other sources. |
| Reference framework and taxonomy | Structured control references used to support classification and impact matching. |
| Prompts and response schemas | Task instructions and constraints for structured candidate annotations. |

The resulting artifacts include normalized Markdown documents, line-to-page maps, numbered diffs, schema-validated change annotations, evaluation reports, candidate contextualisation actions, and updated workbook versions. Together, they preserve the links between source provisions, identified changes, affected controls, proposed actions, and subsequent review decisions.

## Reproducibility artifacts

[`reproducibility_artifacts/`](reproducibility_artifacts/) retains the SCA worked example and the frozen Circular 285 update-15-to-16 evaluation, with their inputs, outputs, API metadata and SHA-256 manifests.

## Current implementation scope

The implementation follows a text-first approach. It supports the extraction, comparison, annotation, and contextualisation of textual regulatory provisions. Tables, images, and other layout-dependent content may contain relevant information, but they are not treated as structured comparison objects in the current validation.

The reliability of the process also depends on the validity of its inputs. Regulatory documents should be verified at document level, rather than relying only on filenames or external labels, before they are used for comparison.

## Project contents

| Location | Role in the validation |
| --- | --- |
| `src/` | Python implementation of the four workflow stages, evaluation, and shared utilities. |
| `prompts/` | Instructions for annotation, baseline comparison, noise reduction, and contextualisation tasks. |
| `schemas/` | JSON schemas for structured model outputs and an example of approved document-cleanup rules. |
| `reference/` | Taxonomy and the two framework-core workbooks used by the implementation. |
| `reproducibility_artifacts/` | Versioned Stage 4 executions, evidence, candidates, review records and integrity manifests. |

## Source files

| File | Contribution to the validation |
| --- | --- |
| `src/pdf_to_md.py` | Implements document normalization by converting PDF sources into Markdown and recording the source page for each output line. |
| `src/diff.py` | Implements syntactic diff generation between normalized document versions. |
| `src/query.py` | Obtains structured candidate annotations from the selected LLM provider and validates them against the selected schema. |
| `src/pipeline.py` | Coordinates document normalization, syntactic diff generation, and semantic change annotation. |
| `src/evaluation_circ285.py` | Evaluates Circular 285 annotations against the locations affected by each update and, where available, page-level evidence. |
| `src/pipeline_with_evaluation_circ285.py` | Coordinates the Circular 285 annotation and evaluation sequence. |
| `src/contextualization_update.py` | Implements contextualization prototype update by generating candidate actions and applying proposed actions to the prototype and controls workbooks. |
| `src/pipeline_with_contextualization_update.py` | Coordinates the complete workflow, including contextualisation. |
| `src/contextualization_confidence.py` | Calculates confidence and consistency evidence for candidate contextualisation actions. |
| `src/pipeline_helpers.py` | Provides shared file validation, command execution, and provider-selection functions. |
| `src/project_paths.py` | Defines the project locations used by the implementation. |

The project separates prompts, schemas, reference material, and generated artifacts so that every validation run can be examined in relation to the methodological steps that produced it.
