#!/usr/bin/env python3.12
"""Generate src/boardd/data/translations-seed.json from a mock state dir.

Reads the mock state dir, enumerates every translatable line (goal, now,
hold_reason, intent, each done-when clause including enrolled-only clauses,
each open ask, each digest event text), looks each up in TRANSLATIONS below,
and writes the hash-keyed seed cache. Fails loudly if any line has no
translation, so the demo never silently shows raw agent-speak.

Usage: python3.12 tools/gen_boardd_seed.py <state_dir>
(e.g. <state_dir> = tests/fixtures/boardd_state)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from boardd.state import split_conditions  # noqa: E402
from boardd.translate import TranslationCache  # noqa: E402

TRANSLATIONS: dict[str, str] = {
    # ---- roundtop:ramble-build
    "Build the ramble skill (conversation contract, five routes, RCP v1 outbox format) and the sync step that turns flushed batches into dispatchd orders for the monitor.":
        "Build the Ramble skill — the conversation contract, the five sorting routes, and the version 1 outbox record format — plus the sync step that turns flushed batches into dispatch orders for the fleet monitor.",
    "conversation-contract simulation running with the operator; findings feed the skill draft":
        "A simulation of the conversation contract is running with the operator. What it finds feeds the skill draft.",
    "skill merged": "The skill is merged.",
    "RCP validator merged in the chitra repo":
        "The outbox record validator is merged in the chitra repository.",
    "a live flushed batch lands as a dispatchd order and the monitor's decision appears in the decision log":
        "A real flushed batch arrives as a dispatch order, and the monitor's decision shows up in the decision log.",
    "research route ships disabled until the dispatchd retry fix is applied on twinridge":
        "The research route ships switched off until the dispatch retry fix lands on twinridge.",
    "Give the operator a conversational sorting surface that files governed intents instead of doing work":
        "Give the operator a conversational sorting surface that files governed intents instead of doing the work itself.",

    # ---- twinridge:boardd-build
    "Build boardd: a tailnet web dashboard reading the chitra state dir, pushing per-change updates, translating agent updates to plain technical English at render time.":
        "Build boardd: a private-network web dashboard that reads the fleet state directory, pushes each change to the page, and rewrites agent updates into plain technical English as it renders them.",
    "v3 mockups published with Linear and Vercel derived visual language":
        "Version 3 mockups are published, using a visual language derived from Linear and Vercel.",
    "awaiting operator acceptance of the v3 visual mockups and go on the build plan":
        "Waiting for the operator to accept the version 3 mockups and approve the build plan.",
    "SSE server deployed on twinridge with a tool-registry entry":
        "The push-update server is deployed on twinridge and registered in the tool registry.",
    "identical-wording rulings":
        "A decision request uses identical wording everywhere it appears.",
    "three degraded states":
        "The page shows three honest connection states: live, delayed, disconnected.",
    "15-lane condensed mode": "A condensed layout handles fifteen lanes.",
    "capped Needs-you zone and change rail":
        "The needs-you zone and the change list both have size caps.",
    "phone filter": "The phone view has a lane filter.",
    "translation cache with raw fallback":
        "Translations are cached by line, and the raw line shows when no translation exists.",
    "JSON state endpoint": "A JSON endpoint serves the full board state.",
    "artifact board retired after N clean days":
        "The old artifact board is retired after enough clean days.",
    "Move fleet visibility out of chat onto a glanceable board built for the operator":
        "Move fleet visibility out of chat and onto a glanceable board built for the operator.",
    "Accept the dashboard direction?": "Do you accept the dashboard direction?",
    "Go on the five-step build?": "Do you approve the five-step build plan?",

    # ---- tophand:chitra-deploy
    "Land chitra PR 27 (blocked-order retry, config-crash survival, unit env drift guard, README fixes) and deploy the fixed daemons to twinridge.":
        "Land chitra pull request 27 — retrying blocked orders, surviving a bad config, guarding against service environment drift, and README fixes — then deploy the fixed daemons to twinridge.",
    "merge watcher on CI; checks were queued at last look; deploy step prepared behind the merge":
        "A watcher is waiting on the merge. The test checks were queued at last look. The deploy step is prepared to run once the merge lands.",
    "PR 27 merged green": "Pull request 27 is merged with green checks.",
    "new version deployed to twinridge": "The new version is deployed to twinridge.",
    "a lock-blocked order observed retrying from deferred/ on the live host":
        "On the live host, an order blocked by a lock is seen retrying from the deferred queue.",
    "Close the silent-loss hole before Ramble's research route ships":
        "Close the silent-loss hole before Ramble's research route ships.",

    # ---- tophand:atlas-ingest
    "Give Atlas an incremental ingestion trigger so harvested records load without a manual full-pipeline run.":
        "Give Atlas a trigger so that finished harvests load into the corpus without someone running the full pipeline by hand.",
    "no active session since the compute-graph root cause closed; the known gap is that nothing invokes ingestion after harvest":
        "No session has been active since the compute-graph root cause closed. The known gap stands: nothing starts the loader after a harvest finishes.",
    "a harvest completion triggers ingestion automatically":
        "Finishing a harvest starts the load automatically.",
    "record_id dedup holds": "No two loaded records share a record id.",
    "post-load counts match the harvest manifest":
        "After loading, the record count matches the harvest manifest.",
    "Stop harvests from piling up unloaded; harvest currently retrieves but nothing calls the loader":
        "Stop harvests from piling up unloaded. Today the harvest retrieves records, but nothing calls the loader.",

    # ---- twinridge:c912-scenarios
    "Produce the v2 scenario pack through the scenario pipeline with reviewer notes resolved.":
        "Produce the version 2 scenario pack through the scenario pipeline, with every reviewer note resolved.",
    "four injects drafted; inject 4 names a real vendor and needs a keep-with-disclaimer or fictionalize call at review":
        "Four injects are drafted. Inject 4 names a real vendor; at review you decide whether to keep the name with a disclaimer or make it fictional.",
    "waiting on operator time for inject review":
        "Waiting for your time to review the injects.",
    "agent claims the pack is delivered; the registered validator did not confirm a passing receipt":
        "The agent claims the pack is delivered. The registered validator did not confirm a passing result.",
    "all injects drafted in template voice": "Every inject is drafted in the template voice.",
    "legal review notes resolved": "Every legal-review note is resolved.",
    "pack delivered to the competition folder": "The pack is delivered to the competition folder.",
    "Competition-ready scenario pack": "A competition-ready scenario pack.",

    # ---- tophand:harvest-elec
    "Build a scored corpus of 120 full-text sources on election infrastructure security via the source-harvest pipeline.":
        "Build a scored collection of 120 full-text sources on election infrastructure security, using the source-harvest pipeline.",
    "collectors fetched 87 of 120 candidates; 14 paywalled DOIs queued for the escape ladder; HTML extraction quality flagged low on 9 records":
        "Collectors have fetched 87 of the 120 candidate sources. Fourteen paywalled journal articles are queued for the paywall-escape steps. Nine web-page records were flagged for low text-extraction quality.",
    "120 sources at quality 4 or better indexed":
        "120 sources scoring quality 4 or better are indexed.",
    "audit pass complete": "The audit pass is complete.",
    "validation pass over the whole kept set":
        "The validation pass has run over every kept source.",
    "final report written": "The final report is written.",
    "Primary-source base for the fall research push":
        "A primary-source base for the fall research push.",

    # ---- twinridge:ws-paper
    "Revise the workshop paper until every reviewer 2 comment has a written disposition.":
        "Revise the workshop paper until every one of reviewer 2's comments has a written response.",
    "agent reports section 5 rewrite complete; completion audit queued":
        "The agent reports the section 5 rewrite is complete. The completion audit is queued.",
    "all reviewer 2 comments dispositioned":
        "Every reviewer 2 comment has a written response.",
    "all reviewer 3 optional comments dispositioned":
        "Every reviewer 3 optional comment has a written response.",
    "sections 3 and 5 rewritten": "Sections 3 and 5 are rewritten.",
    "gaps pass rerun clean": "The gaps check reruns clean.",
    "voice-fix pass done": "The voice-fix editing pass is done.",
    "Resubmission-ready draft": "A resubmission-ready draft.",

    # ---- trailhead:feeds-service
    "Keep the Feeds daily digest generating and delivered on schedule.":
        "Keep the Feeds daily digest generating and arriving on schedule.",
    "repair landed; three of seven clean days elapsed":
        "The repair landed. Three of the seven required clean days have passed.",
    "seven consecutive on-schedule digests after the repair":
        "Seven digests in a row arrive on schedule after the repair.",
    "Reliable morning digest": "A reliable morning digest.",

    # ---- renegade:embed-bench
    "Compare three embedding models on the corpus sample before the Atlas corpus rebuild.":
        "Compare three embedding models on the corpus sample before the Atlas corpus is rebuilt.",
    "first model finished its sample run; results file verified":
        "The first model finished its sample run, and the results file is present and checked.",
    "rate-limit window until 18:00; second model queued":
        "Inside a rate-limit window until 18:00, with the second model queued. This is scheduled, not stuck.",
    "all three models run on the same sample": "All three models have run on the same sample.",
    "retrieval quality table produced": "The retrieval-quality comparison table is produced.",
    "recommendation memo written": "The recommendation memo is written.",
    "all three models ran; the recommendation memo is written and awaiting the operator's close":
        "All three models ran. The recommendation memo is written and is waiting for your close.",
    "Pick the rebuild embedder on evidence, not vibes":
        "Pick the rebuild's embedding model on evidence, not vibes.",

    # ---- tophand:wiki-backfill
    "Backfill the wiki's project pages for the six projects that shipped since July with durable facts and decision records.":
        "Fill in the wiki's project pages for the six projects shipped since July, with durable facts and decision records.",
    "two pages done; blocked because the wiki write path rejects one page over a taxonomy naming collision":
        "Two pages are done. The lane is blocked: the wiki's write path rejects one page because its name collides with an existing page.",
    "six project pages updated": "All six project pages are updated.",
    "each cites its digest entries": "Each page cites its digest entries.",
    "lint clean": "The wiki's style check passes clean.",
    "Stop re-deriving project state from scratch in new sessions":
        "Stop new sessions from re-deriving project state from scratch.",
    "Rename the colliding page 'atlas-compute' to 'atlas-compute-graph', or merge into the existing page?":
        "Rename the colliding page 'atlas-compute' to 'atlas-compute-graph', or merge it into the existing page?",

    # ---- digest events
    "Collector wave 3 finished: 87 of 120 candidates fetched full-text. Nine HTML records scored below the extraction floor and moved to quality review.":
        "Collector wave 3 finished: 87 of 120 candidate sources fetched in full text. Nine web-page records scored below the extraction floor and moved to quality review.",
    "Agent reports the section 5 rewrite is complete. Completion audit queued; not yet verified.":
        "The agent reports the section 5 rewrite is complete. The completion audit is queued.",
    "PR 27 marked ready for review; CI checks queued (two test matrices and the review gate).":
        "Pull request 27 was marked ready for review. Three checks are queued: two test runs and the review gate.",
    "Wiki write rejected the third page: taxonomy naming collision on 'atlas-compute'. Lane blocked on the operator's naming call.":
        "The wiki rejected the third page: the name 'atlas-compute' collides with an existing page. The lane is blocked on your naming call.",
    "First model's sample run finished; results file present and checked. Lane holds for the rate-limit window until 18:00.":
        "The first model finished its sample run; the results file is present and checked. The lane holds for a rate-limit window until 18:00.",
    "Visual redesign of the mockup deck completed and published; lane holds for the operator's direction and build decisions.":
        "The mockup deck's visual redesign is complete and published. The lane holds for your direction and build decisions.",
    "Third consecutive on-schedule digest since the repair. Four more clean days close the lane.":
        "The third on-schedule digest in a row since the repair. Four more clean days close the lane.",
    "Conversation-contract simulation environment stood up; operator round one started.":
        "The conversation-contract simulation environment is up, and round one with the operator has started.",
    "No change since yesterday. Injects drafted; review not started. The inject 4 vendor-name question waits for review time.":
        "No change since yesterday. The injects are drafted and review has not started. The inject 4 vendor-name question waits for review time.",
    "No activity. Known gap stands: harvests retrieve but nothing calls the loader.":
        "No activity. The known gap stands: harvests retrieve records, but nothing calls the loader.",
}


def translatable_lines(state_dir: Path) -> list[str]:
    goals = json.loads((state_dir / "goals.json").read_text())
    digest = json.loads((state_dir / "sweep-digest.json").read_text())
    lines: list[str] = []
    for g in goals["goals"]:
        for field in ("goal", "now", "hold_reason", "intent"):
            if g.get(field):
                lines.append(g[field])
        for clause_src in ("done_when", "enrolled_done_when"):
            if g.get(clause_src) and g[clause_src].strip().lower() != "same as done_when":
                lines.extend(split_conditions(g[clause_src]))
        lines.extend(g.get("open_asks", []))
    for ev in digest["events"]:
        lines.append(ev["text"])
    return lines


def main() -> None:
    state_dir = Path(sys.argv[1])
    out_path = Path(__file__).resolve().parents[1] / "src" / "boardd" / "data" / "translations-seed.json"
    seed: dict[str, dict] = {}
    missing = []
    for line in translatable_lines(state_dir):
        line = line.strip()
        if line not in TRANSLATIONS:
            missing.append(line)
            continue
        seed[TranslationCache.key(line)] = {"raw": line, "text": TRANSLATIONS[line]}
    if missing:
        sys.exit("No translation for:\n  " + "\n  ".join(missing))
    out_path.write_text(json.dumps(seed, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(seed)} entries to {out_path}")


if __name__ == "__main__":
    main()
