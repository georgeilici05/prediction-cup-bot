# Predictions Cup trading project

This repository contains three separate strategies:

- `parity_scanner.py` — read-only Republican/Democratic party-pair scanner for Senate, Governor, House, and national-control markets.
- `multi_opportunity_executor.py` — settlement-arbitrage allocator.
- `live_recycler.py` — protected early-exit recycler. It requires explicit live caps and records only positions it creates.

## GitHub Actions setup

1. Create a **private** GitHub repository and push this folder to it.
2. In the repository, open **Settings → Secrets and variables → Actions**.
3. Add a repository secret named `SUSQ_API_KEY`. Paste the API key there; never put it in a tracked file.
4. In **Settings → Actions → General**, allow workflows **Read and write permissions**. The workflow needs this only to save `work/live_recycler_state.json`, its non-secret position ledger.
5. Enable the `Live recycler` workflow. It requests a run every five minutes and can also be started manually from the Actions tab.

GitHub's scheduled workflows are best-effort rather than precise timers. Review the first several workflow runs and the platform's Orders page before relying on it.

## Risk controls

- Total cap: 40,000 SUSQies across active bot positions.
- Per-contest cap: 2,000 SUSQies across active bot positions.
- Existing holdings are excluded from new entries.
- The process stops after a partial fill instead of continuing to place orders.
- `.env` is ignored. Do not commit it or paste its key into an issue, log, or README.
