"""Record states, routes, statuses and methods (Final MVP Specification, Sections 6-8)."""

# --- Record states (Section 8, "Record states") ---------------------------
IMPORTED = "IMPORTED"
INVALID = "INVALID"
QUEUED = "QUEUED"
MATCHING = "MATCHING"
FOOTPRINT_READY = "FOOTPRINT_READY"
NEEDS_IMAGERY = "NEEDS_IMAGERY"
NO_IMAGERY_AVAILABLE = "NO_IMAGERY_AVAILABLE"
SEGMENTING = "SEGMENTING"
POLYGONIZING = "POLYGONIZING"
VALIDATING = "VALIDATING"
REVIEW = "REVIEW"
MANUAL = "MANUAL"
AUTO_ACCEPTED = "AUTO_ACCEPTED"
APPROVED = "APPROVED"
APPROVED_MANUAL = "APPROVED_MANUAL"
REJECTED = "REJECTED"
EXPORTED = "EXPORTED"
FAILED = "FAILED"

ALL_STATES = (
    IMPORTED, INVALID, QUEUED, MATCHING, FOOTPRINT_READY, NEEDS_IMAGERY,
    NO_IMAGERY_AVAILABLE, SEGMENTING, POLYGONIZING, VALIDATING, REVIEW, MANUAL,
    AUTO_ACCEPTED, APPROVED, APPROVED_MANUAL, REJECTED, EXPORTED, FAILED,
)

# Allowed transitions. The pipeline transitions come straight from the spec's
# state table; the operator-triggered re-entries at the bottom support the
# /resegment and /select-candidate endpoints.
TRANSITIONS = {
    IMPORTED: {QUEUED},
    INVALID: set(),
    QUEUED: {MATCHING},
    MATCHING: {FOOTPRINT_READY, NEEDS_IMAGERY, NO_IMAGERY_AVAILABLE, FAILED},
    FOOTPRINT_READY: {SEGMENTING, VALIDATING},
    NEEDS_IMAGERY: {SEGMENTING, NO_IMAGERY_AVAILABLE},
    NO_IMAGERY_AVAILABLE: {REVIEW, MANUAL},
    SEGMENTING: {POLYGONIZING, FAILED},
    POLYGONIZING: {VALIDATING, FAILED},
    VALIDATING: {AUTO_ACCEPTED, REVIEW, MANUAL},
    REVIEW: {APPROVED, REJECTED, MANUAL},
    MANUAL: {APPROVED_MANUAL},
    AUTO_ACCEPTED: {EXPORTED},
    APPROVED: {EXPORTED},
    APPROVED_MANUAL: {EXPORTED},
    FAILED: {QUEUED, MANUAL},
    REJECTED: set(),
    EXPORTED: set(),
}
# Operator-triggered reprocessing (click-to-fix / pick other candidate).
REPROCESS_FROM = {REVIEW, MANUAL, NO_IMAGERY_AVAILABLE, FAILED, AUTO_ACCEPTED}
for _s in REPROCESS_FROM:
    TRANSITIONS[_s] = set(TRANSITIONS[_s]) | {MATCHING, SEGMENTING}

TERMINAL_WORKER_STATES = {REVIEW, MANUAL, AUTO_ACCEPTED, FAILED, INVALID}

# --- Identity decision (S4) -------------------------------------------------
IDENTITY_CLEAR = "IDENTITY_CLEAR"
IDENTITY_AMBIGUOUS = "IDENTITY_AMBIGUOUS"
NO_CANDIDATE = "NO_CANDIDATE"

# --- Routes (Section 6, Routing) ---------------------------------------------
ROUTE_AUTO_ACCEPT = "AUTO_ACCEPT"
ROUTE_REVIEW = "REVIEW"
ROUTE_MANUAL = "MANUAL"
ROUTE_ORDER = {ROUTE_MANUAL: 0, ROUTE_REVIEW: 1, ROUTE_AUTO_ACCEPT: 2}

# --- Buildings layer status (Section 7) --------------------------------------
STATUS_PROPOSED = "PROPOSED"
STATUS_EDITED = "EDITED"
STATUS_APPROVED = "APPROVED"
STATUS_APPROVED_MANUAL = "APPROVED_MANUAL"
STATUS_REJECTED = "REJECTED"
STATUS_AUTO_ACCEPTED = "AUTO_ACCEPTED"
BUILDING_STATUSES = (STATUS_PROPOSED, STATUS_EDITED, STATUS_APPROVED,
                     STATUS_APPROVED_MANUAL, STATUS_REJECTED, STATUS_AUTO_ACCEPTED)
EXPORTABLE_STATUSES = (STATUS_APPROVED, STATUS_APPROVED_MANUAL)

# --- Detection methods (Documentation 4.3) -----------------------------------
METHOD_HISTORIC = "historic-match"
METHOD_FOOTPRINT = "footprint"
METHOD_FOOTPRINT_SAM = "footprint+SAM"
METHOD_IMAGERY_SAM = "imagery+SAM"
METHOD_CLICK_TO_FIX = "click-to-fix"
METHOD_MANUAL = "manual"

# --- Review actions ------------------------------------------------------------
ACTION_APPROVE = "APPROVE"
ACTION_EDIT = "EDIT"
ACTION_CLICK_TO_FIX = "CLICK_TO_FIX"
ACTION_PICK_CANDIDATE = "PICK_CANDIDATE"
ACTION_DRAW_MANUAL = "DRAW_MANUAL"
ACTION_REJECT = "REJECT"
ACTION_LOAD_PROPOSAL = "LOAD_PROPOSAL"
ACTION_EXPORT = "EXPORT"
ACTION_SHIFT = "SHIFT"              # whole outline moved onto the roof (offset footprint)
ACTION_SPLIT = "SPLIT"              # merged footprint cut; the part with the survey point kept
ACTION_DRAW_BOX = "DRAW_BOX"        # building drawn as a 3-click rectangle
ACTION_LEARN = "LEARN"              # approved polygons added to the trusted reference
ACTION_ADD_IMAGERY = "ADD_IMAGERY"  # client imagery registered (with its permission record)

# Outline-changing actions: a record touched by any of these was not "approved as proposed".
OUTLINE_FIX_ACTIONS = (ACTION_EDIT, ACTION_SHIFT, ACTION_SPLIT, ACTION_CLICK_TO_FIX)
DRAW_ACTIONS = (ACTION_DRAW_MANUAL, ACTION_DRAW_BOX)

REJECT_REASONS = (
    "WRONG_LOCATION",
    "BUILDING_NOT_VISIBLE",
    "BUILDING_DEMOLISHED",
    "DUPLICATE_RECORD",
    "INSUFFICIENT_INFORMATION",
    "OUT_OF_SCOPE",
    "OTHER",
)

# --- Corrected coordinate strategies (S10) ------------------------------------
STRATEGY_CENTROID_IF_INSIDE = "centroid_if_inside"
STRATEGY_REPRESENTATIVE_POINT = "representative_point"
STRATEGY_SNAP_TO_BOUNDARY = "snap_to_boundary"
STRATEGIES = (STRATEGY_CENTROID_IF_INSIDE, STRATEGY_REPRESENTATIVE_POINT,
              STRATEGY_SNAP_TO_BOUNDARY)

# --- Soft flags / hard gates (S9) ----------------------------------------------
FLAG_FAR_FROM_SURVEY = "far_from_survey_point"
FLAG_LOW_FOOTPRINT_AGREEMENT = "low_footprint_agreement"
FLAG_OVERLAPS_APPROVED = "overlaps_approved_building"
FLAG_IMPLAUSIBLE_AREA = "implausible_area_for_category"
FLAG_HEIGHT_MISMATCH = "height_mismatch"
FLAG_SLIVER = "sliver_or_spike"
FLAG_IDENTITY_AMBIGUOUS = "identity_ambiguous"
FLAG_NO_CANDIDATE_PATH = "no_candidate_path"
FLAG_SAM_REFINEMENT_FAILED = "sam_refinement_failed"
FLAG_LOW_STABILITY = "low_segmentation_stability"
FLAG_CANDIDATE_ACROSS_ROAD = "candidate_across_road"
FLAG_NAME_FOUND_FAR = "name_found_far"
FLAG_FOUND_BY_NAME = "found_by_name"
FLAG_IMAGERY_TOO_COARSE = "imagery_too_coarse"

GATE_INVALID_GEOMETRY = "invalid_geometry"
GATE_MULTIPART = "multipart_geometry"
GATE_AREA = "area_out_of_range"
GATE_CROP_EDGE = "touches_crop_edge"
GATE_CRS = "outside_search_area"

FLAG_TEXT = {
    FLAG_FAR_FROM_SURVEY: "Polygon is farther from the survey point than the search radius",
    FLAG_LOW_FOOTPRINT_AGREEMENT: "Segmented polygon disagrees with the chosen footprint",
    FLAG_OVERLAPS_APPROVED: "Overlaps an already approved building by more than 10%",
    FLAG_IMPLAUSIBLE_AREA: "Area is implausible for the declared category/floors",
    FLAG_HEIGHT_MISMATCH: "Reference height is inconsistent with the declared height",
    FLAG_SLIVER: "Polygon has very thin parts (sliver or spike)",
    FLAG_IDENTITY_AMBIGUOUS: "Building identity is ambiguous - check the other candidates",
    FLAG_NO_CANDIDATE_PATH: "No footprint candidate - proposal comes from automatic masks",
    FLAG_SAM_REFINEMENT_FAILED: "Imagery refinement failed; footprint geometry kept",
    FLAG_LOW_STABILITY: "Segmentation is unstable across prompt variants",
    FLAG_CANDIDATE_ACROSS_ROAD: "Chosen building is across a road from the survey point",
    FLAG_NAME_FOUND_FAR: "A building with this name was found further away - see the candidate list",
    FLAG_FOUND_BY_NAME: "Proposal found by its name, away from the survey point - check the location",
    FLAG_IMAGERY_TOO_COARSE: "Imagery is too coarse for this building's size; footprint geometry kept",
    GATE_INVALID_GEOMETRY: "Geometry is invalid after repair",
    GATE_MULTIPART: "Geometry has more than one part",
    GATE_AREA: "Area is outside the allowed range",
    GATE_CROP_EDGE: "Polygon touches the imagery crop edge",
    GATE_CRS: "Polygon lies outside the record's search area",
}
