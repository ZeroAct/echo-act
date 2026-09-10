"""Section 4's operating policy, as constants.

Every number the requirements fix lives here and nowhere else, so that a
policy change is one edit and a reviewer can check the document against one
file.  Nothing here is read from disk: these are the *defaults* that
``echoact.config.settings`` starts from and that safety limits clamp against.

Units follow Section 4.1's closing rule: memory in GiB (2**30), storage in
GB (10**9), transfer in bytes, CPU as a percentage of total logical capacity.
"""

from __future__ import annotations

from typing import Final

GIB: Final = 1 << 30
GB: Final = 1_000_000_000

# ---------------------------------------------------------------- input ---
MAX_INPUT_CODEPOINTS: Final = 50_000  # F-03
MAX_IMPORT_FILE_BYTES: Final = 2_000_000  # F-03
MAX_REQUEST_BODY_BYTES: Final = 2_000_000  # 4.1, includes upload metadata

# ------------------------------------------------------------ segmenting ---
# F-81.  A segment is capped by code points and by estimated audio seconds;
# the first segment of a job is capped harder so playback can start sooner.
SEGMENT_MAX_CODEPOINTS: Final = 200
SEGMENT_MAX_SECONDS: Final = 20.0
FIRST_SEGMENT_MAX_SECONDS: Final = 8.0
SEGMENT_MIN_CODEPOINTS: Final = 20

# Estimating audio length from source length.
#
# F-81 splits on an estimated duration and F-88 quotes one to a caller, so
# the numbers have to come from the engine rather than from intuition.  These
# are measured against Supertonic 3 at tempo 1.00, two threads, voice F1:
# Korean 6.0 code points per second of audio (range 3.7 to 6.4), English 13.3
# (range 7.2 to 14.0).  Short sentences sit at the low end because onset and
# trailing silence are a larger fraction of them, so using the median
# over-estimates a short segment's length -- which is the safe direction for
# a cap.  Tempo divides duration almost exactly: 0.70x measured 1.43x the
# 1.00x length and 1.50x measured 0.67x, so ``seconds / tempo`` is the model.
ESTIMATE_CODEPOINTS_PER_SECOND_KO: Final = 6.0
ESTIMATE_CODEPOINTS_PER_SECOND_EN: Final = 13.3
#: Seconds of computation per second of audio, at the Section 8.2 baseline.
#: Median 0.24, worst case 0.34; the worst case is quoted so an estimate is
#: not optimistic about a machine that is busier than the one measured.
ESTIMATE_REAL_TIME_FACTOR: Final = 0.35

# ----------------------------------------------------------- engine call ---
# Fixed arguments for every synthesis call.  Both exist to keep a segment's
# duration exactly known rather than inferred:
#
# * The engine re-chunks long text on its own (120 code points for Korean)
#   and would then decide its own internal boundaries.  A large limit makes
#   one of our segments exactly one engine call.
# * The engine's own inter-chunk silence is inert once we chunk first, which
#   A.5 measured, so F-82 makes inter-segment silence the app's to insert.
#   Asking for zero states that intent rather than relying on the accident.
ENGINE_MAX_CHUNK_CODEPOINTS: Final = 100_000
ENGINE_SILENCE_DURATION_S: Final = 0.0
#: Diffusion steps.  A.5 measured RTF 0.067 at the fastest setting and 0.207
#: at this one; the margin over playback is fivefold either way, so quality
#: wins.
ENGINE_TOTAL_STEPS: Final = 8

# F-08's speaking styles, as the preset the requirement describes: a tempo
# multiplier applied on top of the user's own tempo, and the inter-segment
# pause the app inserts per F-82.  A style never changes which characters are
# spoken.  The product of the two tempi is clamped to F-07's range, so a
# style can never take tempo outside what the user is told is possible.
STYLE_TEMPO_MULTIPLIER: Final = {
    "natural": 1.00,
    "calm": 0.92,
    "bright": 1.08,
    "narration": 0.97,
}
STYLE_SEGMENT_GAP_MS: Final = {
    "natural": 250,
    "calm": 400,
    "bright": 180,
    "narration": 320,
}
#: Extra pause where the source text itself had a paragraph break.
PARAGRAPH_EXTRA_GAP_MS: Final = 350

# ---------------------------------------------------------------- voice ---
TEMPO_MIN: Final = 0.70  # F-07
TEMPO_MAX: Final = 1.50
TEMPO_DEFAULT: Final = 1.00

# ------------------------------------------------------------- resources ---
CPU_PERCENT_MIN: Final = 10  # F-20
CPU_PERCENT_MAX: Final = 70
CPU_PERCENT_DEFAULT: Final = 20

MEMORY_DEFAULT_FRACTION: Final = 0.25  # F-21
MEMORY_DEFAULT_MIN_BYTES: Final = 2 * GIB
MEMORY_DEFAULT_MAX_BYTES: Final = 6 * GIB
MEMORY_CEILING_BYTES: Final = 32 * GIB
MEMORY_CEILING_FRACTION_OF_TOTAL: Final = 0.5
MEMORY_FLOOR_BYTES: Final = 2 * GIB  # F-23: below this, do not load

HEADROOM_FRACTION: Final = 0.10  # N-04: greater of 10% of RAM or 1 GiB
HEADROOM_MIN_BYTES: Final = 1 * GIB
RESOURCE_SAMPLE_INTERVAL_S: Final = 0.5

# --------------------------------------------------------------- jobs -----
MAX_CONCURRENT_GENERATION: Final = 1  # F-47, whole app
WORKER_RELEASE_DEADLINE_S: Final = 5.0  # N-22
WORKER_TERMINATE_GRACE_S: Final = 1.0  # ask, then kill

# One-off results.  4.1: external jobs stay retrievable for one hour after a
# terminal state, or until the app exits, whichever comes first.
ONEOFF_RESULT_TTL_S: Final = 3600.0
IDEMPOTENCY_TTL_S: Final = 3600.0  # 4.1, survives a normal restart

# F-88 / 4.1 bounded wait.
BOUNDED_WAIT_DEFAULT_S: Final = 10.0
BOUNDED_WAIT_CEILING_S: Final = 60.0

# --------------------------------------------------------------- limits ---
#: What a caller is told to wait for a result that is not finished yet.
#: Section 4 fixes no figure, so this borrows the busy hint rather than
#: introducing a second unexplained number.
NOT_READY_RETRY_AFTER_S: Final = 3.0

# F-74's scheduled backup: once daily, made up only once when missed.
SCHEDULED_BACKUP_INTERVAL_S: Final = 86_400.0
#: How long a failed scheduled backup waits before trying again.  Not a
#: requirement; without it a frequent tick would retry a failing backup
#: continuously, which N-28 would not forgive.
SCHEDULED_BACKUP_RETRY_S: Final = 3_600.0

RATE_GENERATION_PER_MIN: Final = 10  # 4.1, per client
RATE_OTHER_PER_MIN: Final = 120
AUTH_FAILURES_PER_MIN: Final = 10
AUTH_LOCKOUT_S: Final = 60.0

LIST_PAGE_DEFAULT: Final = 20
LIST_PAGE_MAX: Final = 100

# -------------------------------------------------------------- storage ---
RETENTION_DEFAULT_BYTES: Final = 5 * GB
RETENTION_MIN_BYTES: Final = 1 * GB
RETENTION_MAX_BYTES: Final = 100 * GB
LOW_SPACE_WARNING_BYTES: Final = 1 * GB

LOG_RETENTION_DAYS: Final = 7
LOG_MAX_BYTES: Final = 100 * 1_000_000

BACKUP_KEEP_SCHEDULED: Final = 7
BACKUP_RESTORE_MAX_BYTES: Final = 100 * GB
BACKUP_RESTORE_MAX_ITEMS: Final = 100_000

# ------------------------------------------------------------- service ---
REST_HOST: Final = "127.0.0.1"  # N-17: loopback only, not configurable
REST_PORT_DEFAULT: Final = 8765
REST_ENABLED_DEFAULT: Final = True  # F-46
MCP_ENABLED_DEFAULT: Final = False  # F-46

CREDENTIAL_DAYS_DEFAULT: Final = 90  # 4.1
CREDENTIAL_DAYS_MIN: Final = 1
CREDENTIAL_DAYS_MAX: Final = 365
CREDENTIAL_EXPIRY_WARNING_DAYS: Final = 7

# Hosts a request may claim.  N-17 evaluates this before authentication.
ALLOWED_HOSTS: Final = frozenset(
    {"127.0.0.1", "localhost", "[::1]", "::1"}
)

# ------------------------------------------------------------ interface ---
API_VERSION: Final = "v1"
API_PREFIX: Final = "/api/v1"
# The MCP revision this build implements, per F-58.
MCP_PROTOCOL_REVISION: Final = "2026-07-28"

# ----------------------------------------------------------------- misc ---
VOICE_PRESET_MAX: Final = 100  # 4.1
PLAYBACK_VOLUME_DEFAULT: Final = 1.0
OS_NOTIFICATIONS_DEFAULT: Final = False
AUTOPLAY_DEFAULT: Final = True  # F-83
FOLLOW_DEFAULT: Final = True  # F-30
AUTOSAVE_DOCUMENTS_DEFAULT: Final = False  # 4.1
RETAIN_HISTORY_DEFAULT: Final = False  # 4.1
SCHEDULED_BACKUP_DEFAULT: Final = False  # 4.1

# N-12: the on-screen highlight must track the audio clock this closely.
HIGHLIGHT_SYNC_BUDGET_MS: Final = 300
