"""
Application constants - centralized constant definitions.

This module contains all magic numbers and hardcoded values used throughout
the application, making them easier to maintain and modify.

@author: Cyicek
"""

# =============================================================================
# Version Detection Constants
# =============================================================================

# B42 version boundary (entity system was introduced in B42)
VERSION_B42_MIN = 200
# AnimalTrailer feature introduced
VERSION_ANIMAL_TRAILER = 212
# Player data fields version boundaries
VERSION_PLAYER_228 = 228
VERSION_AUTO_DRINK = 239
# Vehicle data version boundaries
VERSION_VEHICLE_116 = 116  # condition field
VERSION_VEHICLE_118 = 118  # wheel_friction field
VERSION_VEHICLE_119 = 119  # suspension field
VERSION_VEHICLE_173 = 173  # additional vehicle fields
VERSION_VEHICLE_181 = 181  # media_item field
VERSION_VEHICLE_184 = 184  # read_books_count
VERSION_VEHICLE_189 = 189  # known_media field
VERSION_VEHICLE_222 = 222  # time_to_sneeze field

# Version validation bounds
VERSION_MIN_VALID = 1
VERSION_MAX_VALID = 1000

# =============================================================================
# Timeout Constants (in milliseconds)
# =============================================================================

# Thread cleanup timeouts
THREAD_TIMEOUT_SHORT = 500      # For quick operations
THREAD_TIMEOUT_DEFAULT = 800    # Standard thread join timeout
THREAD_TIMEOUT_LONG = 1500      # For complex cleanup operations

# Timer debounce delays (ms)
DEBOUNCE_RENDER = 50            # Render debounce
DEBOUNCE_MIN_INTERVAL = 16      # Minimum interval between events (~60fps)
DEBOUNCE_SELECTION = 100        # Selection update debounce
DEBOUNCE_REFRESH = 160          # Refresh debounce
DEBOUNCE_FEATURE = 100          # Feature refresh debounce
DEBOUNCE_GRID = 160             # Grid refresh debounce
DEBOUNCE_MAP = 200              # Map refresh debounce
DEBOUNCE_STYLE = 160            # Style update debounce
DEBOUNCE_STYLE_LONG = 180       # Longer style update debounce
DEBOUNCE_UNIT_GRID = 220        # Unit grid debounce
DEBOUNCE_LAYER_TOGGLE = 2000    # Layer toggle cooldown

# Notification durations (ms)
NOTIFICATION_DURATION_SHORT = 1200
NOTIFICATION_DURATION_DEFAULT = 2600
NOTIFICATION_DURATION_MEDIUM = 2800
NOTIFICATION_DURATION_LONG = 3200
NOTIFICATION_DURATION_VERY_LONG = 3600
NOTIFICATION_DURATION_EXTRA_LONG = 4000

# UI update intervals
UI_TIMER_INTERVAL = 100         # UI update poll interval (ms)
RIPPLE_TIMER_INTERVAL = 30      # Ripple animation interval (ms)

# =============================================================================
# Buffer and Cache Size Constants
# =============================================================================

# Signature validation sample size (64 KB)
SIG_SAMPLE_BYTES = 64 * 1024

# Map bin index signature sample
MAP_BIN_SIG_SAMPLE_BYTES = 64 * 1024

# Map bin index version
MAP_BIN_INDEX_VERSION = 11

# Chunk file parsing limits
MAX_ENTRIES_DEFAULT = 5000
MAX_ENTRIES_LARGE = 500000

# SQLite batch operations
SQLITE_BATCH_SIZE = 900

# Inventory parser limits
INVENTORY_DETAIL_LIMIT = 200

# Chunk dimensions
B41_CHUNK_SIZE = 10             # 10×10 = 100 GridSquares
B42_CHUNK_SIZE = 8              # 8×8 = 64 GridSquares
CELL_TILE_SIZE = 300            # Map cells are 300x300 tiles in worldmap data

# String length limits
MAX_STRING_LENGTH = 2048
MAX_STRING_LENGTH_SMALL = 512

# Image cache limits
MAX_IMAGE_CACHE_MEMORY_MB = 50

# Map tile cache limits
TILE_CACHE_MAX_MEMORY_MB = 500
TILE_CACHE_MAX_DISK_MB = 500

# Overview cache
MAX_OVERVIEW_PIXELS = 64_000_000  # ≈256 MB ARGB32
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# =============================================================================
# UI Size Constants
# =============================================================================

# Dialog minimum sizes
DIALOG_MIN_WIDTH_SMALL = 320
DIALOG_MIN_WIDTH_DEFAULT = 420
DIALOG_MIN_WIDTH_MEDIUM = 520
DIALOG_MIN_WIDTH_LARGE = 640
DIALOG_MIN_WIDTH_EXTRA_LARGE = 900

DIALOG_MIN_HEIGHT_SMALL = 220
DIALOG_MIN_HEIGHT_DEFAULT = 240
DIALOG_MIN_HEIGHT_MEDIUM = 320
DIALOG_MIN_HEIGHT_LARGE = 420
DIALOG_MIN_HEIGHT_EXTRA_LARGE = 520
DIALOG_MIN_HEIGHT_MAP = 620

# Table and widget sizes
TABLE_MIN_HEIGHT_DEFAULT = 120
TABLE_MIN_HEIGHT_LARGE = 200
SPLITTER_LEFT_DEFAULT = 660
SPLITTER_RIGHT_DEFAULT = 440

# Combo box maximum widths
COMBO_MAX_WIDTH_SMALL = 80
COMBO_MAX_WIDTH_DEFAULT = 100
COMBO_MAX_WIDTH_MEDIUM = 120
COMBO_MAX_WIDTH_LARGE = 140
COMBO_MAX_WIDTH_EXTRA_LARGE = 160

# Color and alpha values
ALPHA_FULL = 255
ALPHA_HIGH = 230
ALPHA_MEDIUM_HIGH = 220
ALPHA_MEDIUM = 200
ALPHA_LOW_MEDIUM = 190
ALPHA_LOW = 160
ALPHA_VERY_LOW = 140
ALPHA_MINIMUM = 90

# Grid threshold
LAYOUT_THRESHOLD_DEFAULT = 860

# Population icon merging
POP_ICON_MERGE_CELL_TARGET = 5          # Reduced from 10 for more detail at distance
POP_ICON_MERGE_DENSE_THRESHOLD = 2500
POP_ICON_MERGE_MAX_FACTOR = 4           # Reduced from 8 to limit max merging

# =============================================================================
# File Size Constants
# =============================================================================

# Large file thresholds (bytes)
FILE_SIZE_LARGE_BYTES = 256 * 1024 * 1024      # 256 MB
FILE_SIZE_WARNING_BYTES = 64 * 1024 * 1024     # 64 MB

# Container size limits
CONTAINER_SIZE_THRESHOLD = 1000                # Elements
LARGE_OBJECT_MIN_SIZE_MB = 1.0                 # MB

# =============================================================================
# Validation Constants
# =============================================================================

# Range limits
MAX_HEALTH = 100.0
MAX_HEALTH_INPUT = 150.0
MAX_EGGS_TODAY = 1000
MAX_GENOME_COUNT = 1024
MAX_ANIMALS_COUNT = 4096
MAX_TRACKS_COUNT = 4096
MAX_DIMENSION = 5000                           # Width/height validation
MAX_ZOMBIES_ESTIMATE = 10000

# Value bounds
MIN_BRIGHTNESS = 0
MAX_BRIGHTNESS = 200
MIN_CONTRAST = 0.5
MAX_CONTRAST = 2.0
MIN_SATURATION = 0.5
MAX_SATURATION = 2.0
MIN_GLOW_INTENSITY = 0.0
MAX_GLOW_INTENSITY = 1.0

# Page size limits
PAGE_SIZE_MIN = 20
PAGE_SIZE_MAX = 5000
PAGE_SIZE_DEFAULT = 100

# Content limits
CONTENT_TOTAL_MIN = 1000
CONTENT_TOTAL_MAX = 5000000
CONTENT_PER_CHUNK_MIN = 100
CONTENT_PER_CHUNK_MAX = 200000

# =============================================================================
# Rendering Constants
# =============================================================================

# Rendering thresholds
PIXEL_COUNT_THRESHOLD_SIMPLE = 2_500_000       # For simple rendering
GRID_CELLS_THRESHOLD_SIMPLE = 400              # Cell count threshold

# Tile size bounds
TILE_SIZE_MIN = 192
TILE_SIZE_MAX = 1024
TILE_SIZE_BASE = 256

# Zoom calculation
ZOOM_RATIO_BASE = 1.0
ZOOM_RATIO_SCALE = 100.0

# Animation phases
ANIMATION_PHASE_MODULO = 2
ANIMATION_PHASE_COUNT = 3

# Map render timing
RENDER_DEBOUNCE_MS = 50
INTERACTION_TIMER_MS = 160

# Population rendering limits
MAX_POINTS_SMALL_AREA = 2500
MAX_POINTS_LARGE_AREA = 6000
AREA_THRESHOLD_HIGH = 200000

# Color adjustment
LIGHTEN_FACTOR_DEFAULT = 115
LIGHTEN_FACTOR_HIGH = 155
DARKEN_FACTOR_DEFAULT = 160
DARKEN_FACTOR_HIGH = 185
DARKEN_FACTOR_EXTREME = 190

# =============================================================================
# Mod Toggle Debouncer Constants
# =============================================================================

DEBOUNCE_DELAY_MS = 150
DEBOUNCE_MAX_DELAY_MS = 300

# =============================================================================
# Cache Prewarm Constants
# =============================================================================

PREWARM_DELAY_MIN_SECONDS = 1
PREWARM_DELAY_MAX_SECONDS = 300
PREWARM_DELAY_DEFAULT = 10

# =============================================================================
# Thread Pool Constants
# =============================================================================

MAX_WORKERS_DEFAULT = 4
MAX_WORKERS_IO_BOUND = 8

# =============================================================================
# Scanning and Parsing Constants
# =============================================================================

# Scan chunk limits
SCAN_MAX_CHUNKS_DEFAULT = 16
SCAN_MAX_OBJECTS_DEFAULT = 5000
SCAN_SAMPLE_SIZE = 256

# Scan timing thresholds
SCAN_SLOW_THRESHOLD_MS = 1000.0                # 1 second
SCAN_VERY_SLOW_THRESHOLD_S = 1.0               # 1 second

# Rare signature detection
RARE_SIGNATURE_THRESHOLD = 3
MAX_RATIO_DEFAULT = 0.22
MAX_RATIO_SMALL = 0.30
MIN_KEEP_DEFAULT = 30

# Probability thresholds
RARE_SIG_MAX_COUNT = 2000
RARE_SIG_MAX_RATIO = 0.5

# =============================================================================
# QFluentWidgets Style Constants
# =============================================================================

# Button width clamps
BUTTON_WIDTH_DEFAULT = 200
BUTTON_WIDTH_WIDE = 240

# Icon sizes
ICON_SIZE_SMALL = 11
ICON_SIZE_DEFAULT = 12

# Border radius
BORDER_RADIUS_DEFAULT = 6
BORDER_RADIUS_LARGE = 8

# =============================================================================
# ByteBuffer Reader Constants
# =============================================================================

U8_SIGN_BIT = 127
U8_NEGATIVE_OFFSET = 256

# =============================================================================
# Map Content Index Constants
# =============================================================================

# Header validation
HEADER_SIZE_MIN = 4
HEADER_SIZE_MAX = 16
RECORD_SIZE_MIN = 32
RECORD_SIZE_MAX = 512

# Pattern analysis
PATTERN_WIDTH_DEFAULT = 4
ASCII_PRINTABLE_MIN = 32
ASCII_PRINTABLE_MAX = 126

# =============================================================================
# Performance Profiling Constants
# =============================================================================

PERF_ITERATIONS_DEFAULT = 10
PERF_WARMUP_DEFAULT = 2
MEMORY_THRESHOLD_MB = 100                      # For recommendations

# =============================================================================
# Multiprocessing Constants
# =============================================================================

PROCESS_JOIN_TIMEOUT = 10                      # seconds
QUEUE_WAIT_TIMEOUT = 1.0                       # seconds
UI_QUEUE_WAIT_TIME = 0.2                       # seconds
CHILD_PROCESS_WAIT_TIME = 0.1                  # seconds

# =============================================================================
# CSV and Data Export Constants
# =============================================================================

CSV_BATCH_SIZE = 256
CSV_STRING_LENGTH_MAX = 8000

# =============================================================================
# Test Constants
# =============================================================================

# Test timeouts
TEST_SINGLE_SHOT_DEFAULT = 500                 # ms
TEST_SINGLE_SHOT_LONG = 1000                   # ms
TEST_SINGLE_SHOT_VERY_LONG = 2000              # ms

# Test sleep intervals
TEST_SLEEP_SHORT = 0.1                         # seconds
TEST_SLEEP_DEFAULT = 0.5                       # seconds
TEST_SLEEP_LONG = 1.0                          # seconds
