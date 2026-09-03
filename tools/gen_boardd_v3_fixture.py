#!/usr/bin/env python3.12
"""Generate tests/fixtures/boardd_state/goals.json from real GoalRecord instances.

The v1 fixture (2026-08-08) predates chitra.goals.v3. This script builds the
fixture directly from the installed chitra.goals.GoalRecord dataclass so the
JSON on disk can never drift from what the schema actually accepts. Run after
changing the fixture's lane set:

    python3.12 tools/gen_boardd_v3_fixture.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chitra.completion_gate import CompletionEvidence  # noqa: E402
from chitra.goals import SCHEMA, EnrolledDoneWhenItem, GoalRecord  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "boardd_state" / "goals.json"
# Deliberately frozen in the past (well past BOARDD_STALE_AFTER_SECONDS) so the
# fixture keeps exercising the stale-data banner in tests and the demo.
UPDATED_AT = "2026-08-08T16:40:00-04:00"


def lane(**kw) -> GoalRecord:
    kw.setdefault("source", "task-file:PLAN.md")
    kw.setdefault("now", "")
    kw.setdefault("last_verified", "")
    kw.setdefault("created_at", UPDATED_AT)
    kw.setdefault("updated_at", UPDATED_AT)
    return GoalRecord(**kw)


LANES = [
    lane(
        session_ref="roundtop:ramble-build",
        goal="Build the ramble skill (conversation contract, five routes, RCP v1 outbox format) and the sync step that turns flushed batches into dispatchd orders for the monitor.",
        done_when="skill merged; RCP validator merged in the chitra repo; a live flushed batch lands as a dispatchd order and the monitor's decision appears in the decision log; research route ships disabled until the dispatchd retry fix is applied on twinridge",
        status="working",
        intent="Give the operator a conversational sorting surface that files governed intents instead of doing work",
        scope="skill + outbox + validator + monitor doctrine paragraphs; boardd is a separate lane",
        now="conversation-contract simulation running with the operator; findings feed the skill draft",
        goal_version=2,
        updated_at="2026-08-08T14:20:00-04:00",
    ),
    lane(
        session_ref="twinridge:boardd-build",
        goal="Build boardd: a tailnet web dashboard reading the chitra state dir, pushing per-change updates, translating agent updates to plain technical English at render time.",
        done_when="SSE server deployed on twinridge with a tool-registry entry; identical-wording rulings; three degraded states; 15-lane condensed mode; capped Needs-you zone and change rail; translation cache with raw fallback; JSON state endpoint; artifact board retired after N clean days",
        status="held",
        intent="Move fleet visibility out of chat onto a glanceable board built for the operator",
        scope="pure reader; never writes fleet state; never spawns sessions",
        hold_reason="awaiting operator acceptance of the v3 visual mockups and go on the build plan",
        now="v3 mockups published with Linear and Vercel derived visual language",
        open_asks=("Accept the dashboard direction?", "Go on the five-step build?"),
        goal_version=1,
        updated_at="2026-08-08T15:30:00-04:00",
    ),
    lane(
        session_ref="tophand:chitra-deploy",
        goal="Land chitra PR 27 (blocked-order retry, config-crash survival, unit env drift guard, README fixes) and deploy the fixed daemons to twinridge.",
        done_when="PR 27 merged green; new version deployed to twinridge; a lock-blocked order observed retrying from deferred/ on the live host",
        status="working",
        intent="Close the silent-loss hole before Ramble's research route ships",
        scope="the four PR-27 defects only",
        now="merge watcher on CI; checks were queued at last look; deploy step prepared behind the merge",
        goal_version=1,
        updated_at="2026-08-08T16:10:00-04:00",
    ),
    lane(
        session_ref="tophand:atlas-ingest",
        goal="Give Atlas an incremental ingestion trigger so harvested records load without a manual full-pipeline run.",
        done_when="a harvest completion triggers ingestion automatically; record_id dedup holds; post-load counts match the harvest manifest",
        status="idle",
        intent="Stop harvests from piling up unloaded; harvest currently retrieves but nothing calls the loader",
        scope="ingestion trigger only; harvest pipeline itself is out of scope",
        now="no active session since the compute-graph root cause closed; the known gap is that nothing invokes ingestion after harvest",
        goal_version=1,
        updated_at="2026-08-08T11:05:00-04:00",
    ),
    lane(
        session_ref="twinridge:c912-scenarios",
        goal="Produce the v2 scenario pack through the scenario pipeline with reviewer notes resolved.",
        done_when="all injects drafted in template voice; legal review notes resolved; pack delivered to the competition folder",
        status="completion-disputed",
        intent="Competition-ready scenario pack",
        scope="the v2 pack only; the pipeline itself is out of scope",
        now="agent claims the pack is delivered; the registered validator did not confirm a passing receipt",
        goal_version=1,
        updated_at="2026-08-08T12:40:00-04:00",
    ),
    lane(
        session_ref="tophand:harvest-elec",
        goal="Build a scored corpus of 120 full-text sources on election infrastructure security via the source-harvest pipeline.",
        done_when="120 sources at quality 4 or better indexed; audit pass complete; validation pass over the whole kept set; final report written",
        status="working",
        intent="Primary-source base for the fall research push",
        scope="the 120-source target and its audit and validation passes",
        now="collectors fetched 87 of 120 candidates; 14 paywalled DOIs queued for the escape ladder; HTML extraction quality flagged low on 9 records",
        goal_version=1,
        updated_at="2026-08-08T16:31:00-04:00",
    ),
    lane(
        session_ref="twinridge:ws-paper",
        goal="Revise the workshop paper until every reviewer 2 comment has a written disposition.",
        done_when="all reviewer 2 comments dispositioned; sections 3 and 5 rewritten; gaps pass rerun clean; voice-fix pass done",
        enrolled_done_when="all reviewer 2 comments dispositioned; all reviewer 3 optional comments dispositioned; sections 3 and 5 rewritten; gaps pass rerun clean; voice-fix pass done",
        status="turn-finished-unverified",
        intent="Resubmission-ready draft",
        scope="reviewer 2's comments; reviewer 3's are optional and were dropped",
        now="agent reports section 5 rewrite complete; completion audit queued",
        goal_version=2,
        updated_at="2026-08-08T16:22:00-04:00",
        enrolled_done_when_items=(
            EnrolledDoneWhenItem(
                id="ws-paper-r2-dispositioned",
                text="all reviewer 2 comments dispositioned",
                validator="suite",
                required_receipt="r2-dispositioned.json",
            ),
            EnrolledDoneWhenItem(
                id="ws-paper-sections-rewritten",
                text="sections 3 and 5 rewritten",
                validator="suite",
                required_receipt="sections-rewritten.json",
            ),
        ),
        completion_proofs=(
            CompletionEvidence(
                kind="artifact",
                citation="tests/fixtures/boardd_state/ws-paper/r2-dispositioned.json",
                done_when_item_id="ws-paper-r2-dispositioned",
                receipt_name="r2-dispositioned.json",
                validator="suite",
                validator_result="pass",
            ),
        ),
    ),
    lane(
        session_ref="trailhead:feeds-service",
        goal="Keep the Feeds daily digest generating and delivered on schedule.",
        done_when="seven consecutive on-schedule digests after the repair",
        status="done-pending-verification",
        intent="Reliable morning digest",
        scope="the seven-clean-day proof window only",
        now="repair landed; three of seven clean days elapsed",
        goal_version=1,
        updated_at="2026-08-08T14:55:00-04:00",
    ),
    lane(
        session_ref="renegade:embed-bench",
        goal="Compare three embedding models on the corpus sample before the Atlas corpus rebuild.",
        done_when="all three models run on the same sample; retrieval quality table produced; recommendation memo written",
        status="done-pending-close",
        intent="Pick the rebuild embedder on evidence, not vibes",
        scope="the three-model comparison only",
        now="all three models ran; the recommendation memo is written and awaiting the operator's close",
        goal_version=1,
        updated_at="2026-08-08T15:47:00-04:00",
    ),
    lane(
        session_ref="tophand:wiki-backfill",
        goal="Backfill the wiki's project pages for the six projects that shipped since July with durable facts and decision records.",
        done_when="six project pages updated; each cites its digest entries; lint clean",
        status="blocked",
        intent="Stop re-deriving project state from scratch in new sessions",
        scope="the six named projects only",
        now="two pages done; blocked because the wiki write path rejects one page over a taxonomy naming collision",
        open_asks=("Rename the colliding page 'atlas-compute' to 'atlas-compute-graph', or merge into the existing page?",),
        goal_version=1,
        updated_at="2026-08-08T15:58:00-04:00",
    ),
]


def main() -> None:
    payload = {
        "schema": SCHEMA,
        "updated_at": UPDATED_AT,
        "note": "SIMULATION roster. Lanes mirror real open Polyphony work; states are frozen mock snapshots, not live.",
        "goals": [record.to_dict() for record in LANES],
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(LANES)} lanes to {OUT}")


if __name__ == "__main__":
    main()
