# Handoff: session A (content analysis, concern radar, fact bank, writing prompts)

Handed off 2026-10-22 under D44. Session: `local_4b63a1f7`. Everything below passes its tests and **none of it is deployed**. The coordinator's next reload deploys it, and the frontend needs a build for item 7.

## 1. Done, not deployed

All files changed with Edit and logged in SESSION_BOARD.md "变更公告".

1. **Fact ordering and rotation** (`ctn_data/fact_bank.py`)
   - New `usage(db_path, agent_id)` reads `post_attribution`.
   - `facts_for(..., agent_id=None, used=None)` sorts by:
     1. concern overlap;
     2. facts this account used `REUSE_LIMIT`=3 times in `REUSE_HOURS`=6 go last;
     3. core before news;
     4. least team use first;
     5. newest first.
   - `PAIRED`: `one_in_twenty` is offered only together with `icu_capacity` or `surge_150`.
   - `public_fact` adds `url` (`NEWS_URL` + `article_key`) for news facts.
   - **Not yet wired:** `agent_pipeline.offered_facts` still calls `facts_for` without `agent_id`. Rotation by team use, core-first and pairing work now. The per-account cap only works once `offered_facts` passes `agent_id`. See 2.1.
2. **Polarity screen** (`fact_bank.NO_LEANING`, `insert_extracted`). In auto mode, a matching extracted claim stays pending with the note "可能偏向 No 方，需人工确认".
3. **Seed** (`fact_bank_seed.json`)
   - `phased_after_access`, `public_vote` and `no_vote_locks_out` are tagged `venue_list`.
   - The venue_list rebuttal no longer lists places.
   - The seed affects new databases only. The live database needs the data change in section 3.
4. **Writing templates** (`ctn_data/agent_writing.py`, D42)
   - **Replies:** `REPLY_TEMPLATE` plus `reply_guide(rng)`.
     - 220–320 characters, one fact, reworded.
     - Structure: the cost the reader fears → a fact that meets their condition → why that settles it.
     - Openings and endings are drawn per reply. About 25% end with a question, about 35% with "voting Yes", about 35% on the fact, about 5% with a KITB jab.
     - Banned: the "Fair worry" / "Name," openers and the No-side framings (list unpublished, blank cheque, sunset clause, no end date, listing the ballot's places).
   - **Posts:** `POST_TEMPLATE` plus `post_guide(rng)`.
     - 300–480 characters, about 50% end with a question.
     - Link the fact's `url` when one is used.
     - @mention people only when MENTION lists them.
   - `REPLY_GUIDE` and `POST_GUIDE` remain as fixed versions for the read-only prompt view.
5. **Pipeline** (`ctn_data/agent_pipeline.py`; B handed it over, A has handed it back to B)
   - `REPLY_WRITE_TARGET`=320 and `WRITE_TARGET`=480 through `{limit}` in WRITE_SYSTEM.
   - `write_messages(..., rng=None)` uses `reply_guide` or `post_guide`.
   - Post FACTS carry `url`.
   - **MENTION:** for posts with `decision['mention_candidates']` (from B's `_staged_decision`), the payload gets `MENTION` and a system note, at most 2 mentions. Replies allow 0 mentions.
   - The venue rule now answers with the three core facts.
   - DECIDE_SYSTEM's target_blocked list includes `crowded`.
   - **`local_issues(body, action, recent, facts=())`:** sends the text back to the writer without a check call when it finds any of these:
     - `_CONCEDE` (D's phrases plus "I don't have it/the name…", "I haven't seen it/the…", "I can't speak to", "I won't pretend");
     - `_VENUE_LIST`;
     - a reply copying ≥`VERBATIM_LIMIT`=40 characters from a chosen fact;
     - any @ in a reply;
     - more than 2 @ in a post.
   - **`check_issues`:** notes matching "conced", "unanswered" or "feeds/echoes/repeats/amplifies the No side" now fail the text.
   - **One-line edit in B's `agent_control.py`:** `local_issues(body, action, recent, chosen)`.
6. **Attribution uses citizens only** (`ctn_data/post_attribution.py`)
   - `settle` and `capture` exclude `agent_targeting.bot_accounts` and `non_citizens`.
   - B has since recalibrated `non_citizens` to the citizens.md proxy without renaming it.
   - Rows already settled are not recomputed. See section 3.
7. **Fact bank page and phone-width fix**
   - Fact bank page: tabs sync with `?tab=`, and there is a new "事实提取" tab.
     - Changed files: `frontend/src/pages/FactBank.tsx`, `ctn_data/fact_extraction.recent` and `GET /api/agents/facts/extractions`.
     - Deployed in window 4.
   - `frontend/src/pages/contentResearch.css`: grid columns are `minmax(0,1fr)` and form items get `min-width:0` at ≤900px and ≤600px. This fixes the 375px overflow E reported. It is not deployed and needs a build.

## 2. Remaining tasks, in order

1. **Wire the per-account fact cap.** In `agent_pipeline.offered_facts(db_path, sources, strategy=None)`:
   - add an `agent_id=None` keyword and pass `agent_id=agent_id` to both `fact_bank.facts_for` calls;
   - in `agent_control._staged_decision` (B's file, a one-line Edit), pass `agent_id=agent['id']`;
   - add a test.
2. **Cross-account similarity.** `local_issues` compares a body only with the same account's recent bodies. The report found 262 cross-account near-duplicates. Pass the team's recent published bodies, not only the account's own, or add a separate check.
3. **After deployment, measure for 2 hours**
   - Run the exposure section: `.venv\Scripts\python.exe scripts\measure_kpis.py --label after-d42 --hours 2`.
   - Check the reply length median (target 220–320), the share ending with a question (≤30%) and the share with "voting Yes" (≤40%), whether originals reach Trending, and how many check failures are concedes_no.
   - D's handoff (`docs/handoff/D.md` §4.4) covers the false-block rate of the local checks.
4. **Radar follow-ups.**
   - With the citizen-only attribution, the Thompson arms (`post_attribution.arm_stats` / `enrich`) restart from small counts. Check after 24 h that `MIN_ARM_POSTS`=10 is still reachable, and lower it if no arm qualifies.
   - Consider dropping the like weight in `WEIGHTS`: likes cannot be attributed to an author and are bot-contaminated (citizens.md §1).
5. **E is waiting for these files:** ContentResearch, ResearchAutomation, FactBank and AIReview.css. Hand them over once 1–4 no longer touch them.

## 3. Live data changes (user approved 2026-10-22)

**Status:**
- **Fact bank: done.** 40 facts retired, 3 core facts tagged `venue_list`, venue_list rebuttal updated. Now 137 active facts.
- **Personas: done.** Applied through `AgentController.save`. Backup: `backups/personas-before-voice-cleanup-20261022-112735.json`. Versions after: Ethan 24, Leah 14, Maya 23, Nora 11, Simon 5.
- **Attribution recount: backup done, reset NOT yet run.**
  - Backup: `backups/post-attribution-before-recount-20261022-112750.json` (663 rows, 375 settled, 84 npc_replies).
  - Run `docs/handoff/A-attribution-recount.sql` **right after the reload** that deploys the new `post_attribution.py`. Before that, the running server would re-settle the rows with the old bot filter.

The details below are kept for reference.

- **Draft with every old and new text:** `docs/drafts/content-fixes-20261022.draft.md`.
- **Fact bank: retire 40 No-leaning or weak facts; add `venue_list` to the three core facts; update the venue_list rebuttal**
  - SQL: `C:\Users\R2616\AppData\Local\Temp\claude\C--Users-R2616-Desktop-CTN\52a9af6f-6415-48d0-b21d-b423edb43827\scratchpad\polarity_retire.sql`. It is one transaction and ends with a status count.
  - Run: `docker exec -i ctn-postgres-1 sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < polarity_retire.sql`.
  - Backup already taken: `backups/facts-polarity-screen-20261022-110023.json` (253 facts plus the rebuttal).
  - Retired facts can be restored one by one on the 事实库 page.
- **Persona voices** (Maya, Ethan, Leah, Nora; Simon's stance)
  - The sentence swaps are in the draft's table.
  - First back up `meta` key `agent_control_v1:agents` to `backups/personas-before-voice-cleanup-<ts>.json`.
  - Apply through the Agent settings UI or API so the running controller sees the change, not by raw SQL.
- **Recount the old post_attribution rows** (66 "citizen replies", about 15 under the calibrated proxy)
  - Back up the table, then set `outcome_at=NULL` for settled rows. The 10-minute loop re-settles them with the new filter.
  - This only works for rows whose replies are still in `posts`. They are, since nothing is deleted.

## 4. Tests

```
.venv\Scripts\python.exe -m unittest tests.test_agent_pipeline tests.test_agent_writing tests.test_fact_bank tests.test_fact_routes tests.test_post_attribution
```
These 83 tests pass as of the handoff.

Frontend (Node is not on PATH; use the bundled runtime):
```
C:\Users\R2616\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe frontend\node_modules\vitest\vitest.mjs run src/pages/FactBank.test.tsx
```
Run it from `frontend`. FactBank's 7 tests pass and `tsc --noEmit` is clean.

The full `test_agent_*` suite finished: 934 tests, 1 error. The error is `test_agent_review_integration...test_disabled_review_allows_emoji_faces_and_symbols_without_ai`. It is in the single-call path, which A's changes do not touch, and is probably caused by B's strict post/post/reply cycle. B has been told. The run was started before the last mention change; its output is in `C:\Users\R2616\AppData\Local\Temp\claude\C--Users-R2616-Desktop-CTN\52a9af6f-6415-48d0-b21d-b423edb43827\tasks\b2c7iu4l5.output`. Re-run it. The "Generation worker failed" log lines in it come from tests that simulate failures.

Live-DB scripts must follow board rule 6: replace `initialize_postgres` and `db.initialize` with functions that raise, then run `BEGIN READ ONLY`.
