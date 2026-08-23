"""Event-based detectors and the bounded response ladder (DESIGN-v3 §4)."""

from .detectors import (
    DETECTOR_VERSION,
    Finding,
    detect_document_dithering,
    detect_drift,
    detect_excessive_testing,
    detect_false_done,
    detect_unnecessary_steps,
)
from .ladder import (
    LADDER_STAGES,
    ConsumptionProof,
    IncidentRecord,
    IncidentStore,
    LadderDecision,
    ResponseLadder,
)
from .rescue import (
    BRIEF_SCHEMA,
    BUNDLE_SCHEMA,
    RescueBundle,
    collect_rescue_bundle,
    generate_relaunch_brief,
    write_checkpoint_receipt,
    write_rescue_bundle,
)

__all__ = [
    "BRIEF_SCHEMA",
    "BUNDLE_SCHEMA",
    "DETECTOR_VERSION",
    "LADDER_STAGES",
    "ConsumptionProof",
    "Finding",
    "IncidentRecord",
    "IncidentStore",
    "LadderDecision",
    "RescueBundle",
    "ResponseLadder",
    "collect_rescue_bundle",
    "detect_document_dithering",
    "detect_drift",
    "detect_excessive_testing",
    "detect_false_done",
    "detect_unnecessary_steps",
    "generate_relaunch_brief",
    "write_checkpoint_receipt",
    "write_rescue_bundle",
]
