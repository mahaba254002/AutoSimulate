# Research portfolio

## Project summary

Alpha Research investigates how a structured, auditable experimentation system can support quantitative researchers working with large financial metadata catalogues. It combines explicit hypotheses, typed expression templates, controlled transformations, bounded evaluation and recorded researcher feedback.

The work is relevant to computational finance, data engineering, human-in-the-loop systems and sequential decision-making. Its current evidence is implemented software and automated tests. It does not contain a published empirical claim that the system produces superior investment returns.

## Demonstrable contributions

1. **Formal representation.** Parse expressions into an AST, validate supported operator roles, distinguish time-trend slope from regression residual, and retain traceable transformations and parent lineage.
2. **Evidence-aware data selection.** Resolve fields in a specific region/delay/universe; retain descriptions, category provenance and reported coverage; avoid inferring undocumented field meaning from identifiers.
3. **Controlled redevelopment.** Separate component diagnostics from structural revisions and nearby-window follow-ups. Compare against an available parent under matching saved settings without inventing unavailable correlation.
4. **Failure-aware experimentation.** Persist quota reservations before network submission; handle partial downloads, platform cooldown, missing metrics, monitoring cancellation and uncertain outcomes conservatively.
5. **Researcher oversight.** Review plans before execution, apply explicit acceptance criteria, pause dispatch after qualifying candidates and record human validation.
6. **Inspectable learning.** Persist versioned bandit decisions, rewards and feedback. Evaluate this policy independently before claiming learning improves performance.

ACE supplies part of the remote-client functionality; that code is not claimed as an original research contribution. See the third-party notices.

## Proposed evaluation

**Question:** Does documented, hypothesis-controlled candidate generation improve reproducibility and research efficiency compared with unconstrained field replacement under an equal simulation budget?

Compare manual templates, compatibility-ranked substitutions, controlled redevelopment and a simple random-compatible baseline. Hold settings, available metadata and total attempt budget fixed. Register hypotheses and evaluation criteria before examining outcomes. Record generated candidates, rejection reasons, missing checks, API failures and human decisions.

Useful outcomes include valid-expression rate, accepted-run rate, attempts per qualifying candidate, duplicate frequency, metadata retrieval cost, failure recovery and reviewer agreement on semantic compatibility. Financial outcomes require independent holdout periods, uncertainty estimates, transaction-cost assumptions and controls for multiple testing. A high in-sample Sharpe ratio alone is insufficient evidence.

For learning evaluation, compare UCB selection against uniform selection over the same documented candidate families. Report sample size and confidence intervals; distinguish observed feedback from extrapolated benefit. Do not reuse validation data to tune the final holdout.

## Evidence available in the repository

- Source for the parser, catalogue, planning, execution, safety and learning modules.
- Tests using mock remote services and disposable database schemas.
- A deterministic offline preview that requires no platform account or paid model.
- A setup guide, architecture document, publication policy and automated CI workflow.

Private account records and investment results are deliberately excluded. Any future benchmark release should use licensed or synthetic inputs, a reproducible protocol and a separate results report stating its limitations.

## Use in an application

An applicant can present this repository as a technical portfolio, explain their own contribution and demonstrate the offline preview and tests. Personal academic qualifications, employment, awards, authorship allocation and measured research results should be documented separately and accurately. This document is a project research statement, not a substitute for those records or a guarantee of admission or funding.
