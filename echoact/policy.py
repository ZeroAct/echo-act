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
