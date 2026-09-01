# Iteration three final results

`final-results.json` is the stable, read-only entry point assembled from the committed baseline-v3 report and source run summaries. It reports 24 active cells: `variant=full` resolved 10/12 and `variant=fuzzy` resolved 10/12. The unresolved cases are T-R2 and T-S4 for each variant.

Labels are intentionally explicit: the original experiment specification called E3=fuzzy and E4=full, while the executed frozen baseline-v3 plan labels E3=full and E4=fuzzy. Public summaries use the variant names and retain both labels here.

Canonical baseline-v1 remains full 9/12 and fuzzy 8/12. Hashes and source paths are recorded in the manifest; the frozen ontology SHA256 is unchanged.
