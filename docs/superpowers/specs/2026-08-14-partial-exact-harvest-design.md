# Partial Exact Harvest Design

## Scope

Recover deterministic crawl results from one already exported legacy remote job without making a remote call. Keep the remote job `MANUAL_REQUIRED` when any linked durable crawl identity remains ambiguous. Do not change model, scheduler, finalization, P1-04, P1-05, or the 2026-08-13 golden run.

## Global Gates

The operation accepts only the exact durable remote job and identity. It requires `post_attempted`, accepted priority evidence, exact `EXPORTED` evidence for the immutable result path, valid current-task provenance, exact mall provenance, and unique non-empty current-batch execution numbers. Failure of any global gate rejects the operation before the first business-state mutation.

## Classification

The adapter filters foreign batch rows before identity classification. It evaluates every SKU candidate column using the existing exact and allowlisted canonical URL rules. Candidate columns producing the same row-to-durable-identity vector form one equivalence class.

- `EXACT_MATCH`: every valid candidate class maps exactly one legal export row to the durable backend identity and the parsed result is identical. Persist `SUCCESS`, including `price=0`.
- `EXACT_ABSENT`: no valid candidate class maps a row to the durable backend identity and no unresolved row or candidate disagreement can represent it. Persist `NO_RESULT` with `EXPORTED_NO_MATCHING_ROW` and a null price.
- `AMBIGUOUS`: candidate classes disagree, exact rows conflict, provenance cannot be proved, or multiple linked local `vCommodityId` rows share one backend identity. Only affected crawl rows remain manual.

Foreign provenance rows never participate. A sibling SKU sharing a URL never supplies a price or prevents an independently exact absence determination.

## Persistence

The recovery command performs no `/add`, priority, crawl, export, or other remote call. It acquires the normal process lock and fenced daily lease, skips immutable `SUCCESS` and `NO_RESULT` rows, and persists each unresolved crawl independently. A crash may leave a prefix completed; rerunning classifies only unresolved rows and never increments `harvest_attempts`.

After row persistence, the remote job becomes `COMPLETED` only when all linked crawl rows are `SUCCESS` or `NO_RESULT`. Otherwise it remains `MANUAL_REQUIRED`, with exact export/result evidence retained. Formal finalization remains blocked by any remaining remote or crawl manual state.

## Production Limits

Jobs 3, 4, and 5 may be fully recovered after fresh evidence validation. Job 1 may recover stable exact matches and one stable exact absence while leaving the SKU-column disagreement manual. Job 6 may recover only unique backend identities; the five `AF0620700` fan-out rows remain manual until their mapping is independently established.

