# Iteration 3 Candidate Revision Diagnosis

This internal record treats baseline-v2 as immutable diagnostic evidence. The revised candidate is post-hoc and aggregate-error-analysis-informed; it is not an unseen-test claim.

## Verified structural findings

- Refinement is unconditionally enabled for full and fuzzy tasks. Full prompts must submit the same rich eleven-slot baseline.
- The requirement tool schema is about 8.2 KiB and remains in every model request after approval. The approved RequirementBaseline is roughly 6–11 KiB and remains a system message.
- Skill selection is descriptive metadata only; it does not change evidence order, candidate generation, scoring, reranking, or stopping.
- Approval is irreversible. Test, diff, or repository evidence cannot reopen a mistaken hypothesis.
- One full task that previously resolved failed both its target and a regression under baseline-v2. Another fuzzy task passed its target but failed two regressions. These outcomes support protecting complete prompts and adding post-patch regression reflection; they do not justify case-specific behavior.
- Frozen task images are used, but Agent commands do not bootstrap their existing testbed/conda environment. Raw traces contain unavailable pytest/dependency failures.
- Input tokens increased from 582,009 to 1,071,043 (+489,034, 1.84x). Persistent schemas and baseline messages dominate the structural increase; model steps changed only slightly.
- Concurrent evaluator adapters each change process cwd. Five preserved infra attempts are consistent with a shared cwd lifecycle race. Evaluator execution requires a narrow serialization boundary; Agent/model execution does not.

## Revision decision

Replace the mandatory fixed form with Adaptive Evidence-Grounded ReqRefine: a task-text-only fail-open router, baseline-equivalent fast path, temporary compact refinement phase, evidence IDs, executable skill policies, ranked hypotheses, one post-patch revise opportunity, task-image environment bootstrap, and evaluator-only serialization. Preserve baseline-v1, baseline-v2, and all prior evidence byte-for-byte.
