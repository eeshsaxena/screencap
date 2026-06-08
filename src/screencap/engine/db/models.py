"""SQLAlchemy models for the recording database."""

import io

import sqlalchemy as sa
from PIL import Image

from screencap.engine.db import Base


# https://groups.google.com/g/sqlalchemy/c/wlr7sShU6-k
class ForceFloat(sa.TypeDecorator):
    """Custom SQLAlchemy type decorator for floating-point numbers."""

    impl = sa.Numeric(10, 2, asdecimal=False)
    cache_ok = True

    def process_result_value(
        self,
        value: int | float | str | None,
        dialect: str,
    ) -> float | None:
        """Convert the result value to float."""
        if value is not None:
            value = float(value)
        return value


class Recording(Base):
    """Class representing a recording in the database."""

    __tablename__ = "recording"

    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(ForceFloat)
    monitor_width = sa.Column(sa.Integer)
    monitor_height = sa.Column(sa.Integer)
    pixel_ratio = sa.Column(ForceFloat, default=1.0)
    double_click_interval_seconds = sa.Column(sa.Numeric(asdecimal=False))
    double_click_distance_pixels = sa.Column(sa.Numeric(asdecimal=False))
    platform = sa.Column(sa.String)
    task_description = sa.Column(sa.String)
    video_start_time = sa.Column(ForceFloat)
    config = sa.Column(sa.JSON)

    # U1 unified-pipeline ledger: the number of chunks this recording is
    # EXPECTED to produce, frozen once at the final rotation. The terminal
    # finalize/sentinel gate keys off THIS frozen count, never a live glob
    # of files on disk — eviction shrinks the on-disk file set but must not
    # shrink the completeness target. NULL on recordings that pre-date U1
    # (and on recordings whose final rotation hasn't happened yet); callers
    # treat NULL as "not yet frozen". See ``screencap.pipeline_state``.
    chunks_expected = sa.Column(sa.Integer, nullable=True)

    original_recording_id = sa.Column(sa.ForeignKey("recording.id"))
    original_recording = sa.orm.relationship(
        "Recording",
        back_populates="copies",
        remote_side=[id],
    )
    copies = sa.orm.relationship(
        "Recording", back_populates="original_recording", cascade="all, delete-orphan"
    )

    action_events = sa.orm.relationship(
        "ActionEvent",
        back_populates="recording",
        order_by="ActionEvent.timestamp",
        cascade="all, delete-orphan",
    )
    screenshots = sa.orm.relationship(
        "Screenshot",
        back_populates="recording",
        order_by="Screenshot.timestamp",
        cascade="all, delete-orphan",
    )
    window_events = sa.orm.relationship(
        "WindowEvent",
        back_populates="recording",
        order_by="WindowEvent.timestamp",
        cascade="all, delete-orphan",
    )
    audio_info = sa.orm.relationship(
        "AudioInfo", back_populates="recording", cascade="all, delete-orphan",
        order_by="AudioInfo.timestamp",
    )
    network_events = sa.orm.relationship(
        "NetworkEvent",
        back_populates="recording",
        order_by="NetworkEvent.timestamp_ns",
        cascade="all, delete-orphan",
    )
    network_event_meta = sa.orm.relationship(
        "NetworkEventMeta",
        back_populates="recording",
        uselist=False,
        cascade="all, delete-orphan",
    )
    network_health = sa.orm.relationship(
        "NetworkHealth",
        back_populates="recording",
        order_by="NetworkHealth.timestamp_ns",
        cascade="all, delete-orphan",
    )


class PipelineChunkState(Base):
    """U1 durable per-chunk state ledger (one row per EXPECTED chunk).

    On-disk replacement for ``ChunkProcessor._chunk_results`` — the
    in-memory ``dict[int, ChunkStatus]`` that the four prior incidents in
    ``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``
    showed could not be trusted across a crash. The ledger re-establishes
    those five prevention rules ON DISK so the terminal stage can
    reconstruct correct state after an interruption.

    **Closed set, never appended on completion.** One row per expected
    chunk index is seeded ``PENDING`` at chunk rotation, BEFORE any
    processing. A query over this closed set cannot exhibit survivorship
    bias — a chunk that rotated but never finished shows up as ``PENDING``,
    not as a missing entry (Bug 2 fix).

    **Per-concern tri-state columns.** ``lifecycle`` is the coarse state
    machine; the four ``*_state`` columns track each concern independently
    so "disabled" can never be conflated with "succeeded" (Bug 4 fix):

      - ``stages_state``  -- destination-agnostic stages (transcribe /
        export / manifest): PENDING | DONE | FAILED.
      - ``scrub_state``   -- cloud-bound privacy transform:
        PENDING | DONE | SKIPPED | FAILED.
      - ``upload_state``  -- UPLOADED | SKIPPED | FAILED | PENDING. Only
        ``UPLOADED`` permits eviction; ``SKIPPED`` is "uploads
        intentionally off" and is NEVER eviction-eligible.
      - ``evict_state``   -- NONE | EVICT_PENDING | EVICTED. The explicit
        ``EVICT_PENDING`` lets an interrupted eviction resume: a crash
        before it is committed leaves the file present + ``UPLOADED``
        (safe); a crash after resumes from ``EVICT_PENDING``.

    Lifecycle: PENDING -> STAGED -> (SCRUBBED -> UPLOADED | LOCAL_DONE |
    SKIPPED); UPLOADED -> (eviction) -> EVICTED. FAILED is reachable from
    STAGED/SCRUBBED and is never evicted / never counted complete.

    Cross-process: this table lives in the local-only ``recording.db``
    (R8 — never uploaded). The engine writer seeds ``PENDING`` rows during
    recording (alongside screenshots/events); a separate terminal-stage
    process advances states. Writes go through
    ``screencap.pipeline_state.PipelineLedger`` with ``busy_timeout=10000``
    and one ``BEGIN IMMEDIATE`` transaction per state change, mirroring
    ``privacy/scrub_worker.py`` so a concurrent WAL checkpoint cannot tear
    a transition.
    """

    __tablename__ = "pipeline_chunk_state"
    __table_args__ = (
        sa.UniqueConstraint(
            "recording_id", "chunk_index",
            name="recording_chunk",
        ),
        sa.Index(
            "ix_pipeline_chunk_state_recording_chunk",
            "recording_id", "chunk_index",
        ),
    )

    id = sa.Column(sa.Integer, primary_key=True)
    recording_id = sa.Column(
        sa.ForeignKey("recording.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index = sa.Column(sa.Integer, nullable=False)

    # Coarse lifecycle state (see class docstring state diagram). Plain
    # TEXT mirroring the NetworkEvent.kind / NetworkHealth.event pattern;
    # the application layer (``pipeline_state``) is the single source of
    # truth for allowed values and legal transitions.
    lifecycle = sa.Column(sa.Text, nullable=False, default="pending")

    # Per-concern tri-state columns — each settled independently so the
    # gate cannot mistake "disabled" for "succeeded".
    stages_state = sa.Column(sa.Text, nullable=False, default="pending")
    scrub_state = sa.Column(sa.Text, nullable=False, default="pending")
    upload_state = sa.Column(sa.Text, nullable=False, default="pending")
    evict_state = sa.Column(sa.Text, nullable=False, default="none")

    # Optional free-text diagnostic for the last FAILED transition
    # (exception type/message). Local-only; never uploaded.
    detail = sa.Column(sa.Text, nullable=True)
    updated_at = sa.Column(ForceFloat, nullable=True)


class ActionEvent(Base):
    """Class representing an action event in the database."""

    __tablename__ = "action_event"

    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String)
    timestamp = sa.Column(ForceFloat)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    screenshot_timestamp = sa.Column(ForceFloat)
    window_event_timestamp = sa.Column(ForceFloat)
    mouse_x = sa.Column(sa.Numeric(asdecimal=False))
    mouse_y = sa.Column(sa.Numeric(asdecimal=False))
    mouse_dx = sa.Column(sa.Numeric(asdecimal=False))
    mouse_dy = sa.Column(sa.Numeric(asdecimal=False))
    mouse_pressure = sa.Column(sa.Numeric(asdecimal=False), nullable=True)
    modifier_flags = sa.Column(sa.Integer, nullable=True)
    scroll_phase = sa.Column(sa.Integer, nullable=True)
    momentum_phase = sa.Column(sa.Integer, nullable=True)
    is_continuous = sa.Column(sa.Boolean, nullable=True)
    active_segment_description = sa.Column(sa.String)
    _available_segment_descriptions = sa.Column(
        "available_segment_descriptions",
        sa.String,
    )
    mouse_button_name = sa.Column(sa.String)
    mouse_pressed = sa.Column(sa.Boolean)
    key_name = sa.Column(sa.String)
    key_char = sa.Column(sa.String)
    key_vk = sa.Column(sa.String)
    canonical_key_name = sa.Column(sa.String)
    canonical_key_char = sa.Column(sa.String)
    canonical_key_vk = sa.Column(sa.String)
    parent_id = sa.Column(sa.Integer, sa.ForeignKey("action_event.id"))
    element_state = sa.Column(sa.JSON)
    disabled = sa.Column(sa.Boolean, default=False)

    children = sa.orm.relationship("ActionEvent")

    recording = sa.orm.relationship("Recording", back_populates="action_events")

    def __str__(self) -> str:
        """Return a string representation of the action event."""
        attr_names = [
            "name",
            "mouse_x",
            "mouse_y",
            "mouse_dx",
            "mouse_dy",
            "mouse_button_name",
            "mouse_pressed",
            "key_name",
            "key_char",
            "element_state",
        ]
        attrs = [getattr(self, attr_name) for attr_name in attr_names]
        attrs = [int(attr) if isinstance(attr, float) else attr for attr in attrs]
        attrs = [
            f"{attr_name}=`{attr}`"
            for attr_name, attr in zip(attr_names, attrs)
            if attr
        ]
        rval = " ".join(attrs)
        return rval


class WindowEvent(Base):
    """Class representing a window event in the database."""

    __tablename__ = "window_event"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    timestamp = sa.Column(ForceFloat)
    state = sa.Column(sa.JSON)
    title = sa.Column(sa.String)
    left = sa.Column(sa.Integer)
    top = sa.Column(sa.Integer)
    width = sa.Column(sa.Integer)
    height = sa.Column(sa.Integer)
    window_id = sa.Column(sa.String)
    app_bundle_id = sa.Column(sa.String)
    app_version = sa.Column(sa.String)
    browser_url = sa.Column(sa.String)
    app_name = sa.Column(sa.String)

    recording = sa.orm.relationship("Recording", back_populates="window_events")


class Screenshot(Base):
    """Class representing a screenshot in the database."""

    __tablename__ = "screenshot"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    timestamp = sa.Column(ForceFloat)
    png_data = sa.Column(sa.LargeBinary)
    png_diff_data = sa.Column(sa.LargeBinary, nullable=True)
    png_diff_mask_data = sa.Column(sa.LargeBinary, nullable=True)
    image_path = sa.Column(sa.String, nullable=True)

    recording = sa.orm.relationship("Recording", back_populates="screenshots")

    def __init__(
        self,
        *args: tuple,
        image: Image.Image | None = None,
        **kwargs: dict,
    ) -> None:
        """Initialize."""
        super().__init__(*args, **kwargs)
        self._image = image

    @sa.orm.reconstructor
    def initialize_instance_attributes(self) -> None:
        """Initialize attributes for both new and loaded objects."""
        self.prev = None
        self._image = None

    @property
    def image(self) -> Image.Image:
        """Get the image associated with the screenshot."""
        if not self._image:
            if self.png_data:
                self._image = self.convert_binary_to_png(self.png_data)
        return self._image

    @classmethod
    def take_screenshot(cls) -> "Screenshot":
        """Capture a screenshot."""
        from screencap.engine import utils

        image = utils.take_screenshot()
        screenshot = Screenshot(image=image)
        return screenshot

    def convert_binary_to_png(self, image_binary: bytes) -> Image.Image:
        """Convert a binary image to a PNG image."""
        buffer = io.BytesIO(image_binary)
        return Image.open(buffer)

    def convert_png_to_binary(self, image: Image.Image) -> bytes:
        """Convert a PNG image to binary image data."""
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


class AudioInfo(Base):
    """Class representing the audio from a recording in the database."""

    __tablename__ = "audio_info"

    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(ForceFloat)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    sample_rate = sa.Column(sa.Integer)
    words_with_timestamps = sa.Column(sa.Text)

    recording = sa.orm.relationship("Recording", back_populates="audio_info")


class PerformanceStat(Base):
    """Class representing a performance statistic in the database."""

    __tablename__ = "performance_stat"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(ForceFloat)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    event_type = sa.Column(sa.String)
    start_time = sa.Column(sa.Integer)
    end_time = sa.Column(sa.Integer)
    window_id = sa.Column(sa.String)


class WindowGeometry(Base):
    """Per-screenshot window geometry for selective masking.

    Stores the complete on-screen window list at each screenshot
    timestamp so scrub-time masking has accurate bounds.
    """

    __tablename__ = "window_geometry"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    recording_timestamp = sa.Column(ForceFloat)
    screenshot_timestamp = sa.Column(ForceFloat, index=True)
    window_list_json = sa.Column(sa.Text)


class MemoryStat(Base):
    """Class representing a memory usage statistic in the database."""

    __tablename__ = "memory_stat"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.Integer)
    recording_id = sa.Column(sa.ForeignKey("recording.id"))
    memory_usage_bytes = sa.Column(ForceFloat)
    timestamp = sa.Column(ForceFloat)


# Allowed values for NetworkEvent.kind. The DB column stores the SHORT form
# (no "network." prefix); the Pydantic class's `type` field uses the dotted
# EventType enum value. CRUD translates between them.
NETWORK_EVENT_KINDS = (
    "request", "response", "ws_upgrade", "ws_frame", "drop_burst", "tunneled",
)


class NetworkEvent(Base):
    """A single network event captured by the system proxy.

    V1 schema is metadata-only - body bytes are hashed-and-discarded by
    the addon, never stored. The `kind` column stores the SHORT form
    (request / response / ws_upgrade / ws_frame / drop_burst) for compact
    SQL; the Pydantic event class's `type` field uses the dotted form
    (network.request etc.).

    `headers_json` holds the JSON-encoded `list[list[str, str]]` ordered
    name/value pairs. For `kind="ws_upgrade"`, this column holds the
    response (101 Switching Protocols) headers; the request headers go
    in `details_json["request_headers"]`.

    `details_json` is a kind-dependent JSON payload:
      - `drop_burst`: {"dropped_count": int, "hosts_affected": [...],
                       "source": "addon" | "reader"}
      - `ws_upgrade`: {"request_headers": [[name, value], ...]}
      - other kinds: NULL

    V1.5 body-encryption columns (NULL on V1 rows; populated when the
    addon captures a body for hosts in `capture_bodies_for`):
      - `body_ciphertext`: AES-256-GCM ciphertext of the body bytes
      - `body_nonce`: 12-byte random nonce used for the AES-GCM encryption
      - `body_aad`: associated data bound to the ciphertext (recording_id +
                    flow_id + event_type + ts_ns canonical-JSON form, see
                    `crypto.aad_bytes`)

    AAD-non-null-when-ciphertext-present invariant:
    `CheckConstraint("body_ciphertext IS NULL OR body_aad IS NOT NULL")`
    applies to V1.5+ rows. V1 rows have NULL ciphertext + NULL AAD, which
    the constraint allows. The constraint cannot be added to existing
    SQLite tables without a table rewrite, so it only applies to fresh
    V1.5+ recordings (V1 recording.db files migrated by `_migrate_schema`
    pick up the new columns but not the constraint - the application-layer
    invariant in `crud.insert_network_event` always passing AAD when
    ciphertext is present is the load-bearing guarantee).
    """

    __tablename__ = "network_event"
    __table_args__ = (
        sa.CheckConstraint(
            "kind IN ('request', 'response', 'ws_upgrade', 'ws_frame', "
            "'drop_burst', 'tunneled')",
            name="ck_network_event_kind",
        ),
        sa.CheckConstraint(
            "body_ciphertext IS NULL OR body_aad IS NOT NULL",
            name="ck_network_event_aad_present",
        ),
    )

    id = sa.Column(sa.Integer, primary_key=True)
    recording_id = sa.Column(
        sa.ForeignKey("recording.id", ondelete="CASCADE"), nullable=False
    )

    kind = sa.Column(sa.Text, nullable=False)
    flow_id = sa.Column(sa.Text, nullable=True)

    method = sa.Column(sa.Text, nullable=True)
    url = sa.Column(sa.Text, nullable=True)
    host = sa.Column(sa.Text, nullable=True)
    status = sa.Column(sa.Integer, nullable=True)

    # JSON-encoded list[list[str, str]] - ordered name/value pairs, NOT a dict
    # (multi-value headers like duplicate Set-Cookie cannot survive a JSON dict).
    headers_json = sa.Column(sa.Text, nullable=True)

    body_size = sa.Column(sa.Integer, nullable=True)
    body_sha256 = sa.Column(sa.LargeBinary(32), nullable=True)

    content_type = sa.Column(sa.Text, nullable=True)
    direction = sa.Column(sa.Text, nullable=True)
    frame_type = sa.Column(sa.Text, nullable=True)
    http_version = sa.Column(sa.Text, nullable=True)

    # Kind-dependent JSON payload (see class docstring).
    details_json = sa.Column(sa.Text, nullable=True)

    # V1.5 body-encryption columns (see class docstring for the AAD invariant).
    body_ciphertext = sa.Column(sa.LargeBinary, nullable=True)
    body_nonce = sa.Column(sa.LargeBinary(12), nullable=True)
    body_aad = sa.Column(sa.LargeBinary, nullable=True)

    timestamp = sa.Column(ForceFloat, nullable=False)
    timestamp_ns = sa.Column(sa.BigInteger, nullable=False, index=True)

    recording = sa.orm.relationship("Recording", back_populates="network_events")


class NetworkEventMeta(Base):
    """Per-recording KEK-wrapped DEK metadata for V1.5 body encryption.

    One row per recording (UNIQUE on `recording_id`). Absence of a row on
    a given recording means no encrypted bodies were captured for that
    recording (V1 recordings, or V1.5 recordings that recorded only
    metadata).

    The DEK (data-encryption key) is generated per-recording, AES-GCM-
    wrapped with the long-lived KEK (key-encryption key) stored in
    macOS Keychain, and persisted here. At export time, the KEK is
    looked up, the DEK is unwrapped, and individual `NetworkEvent` body
    ciphertexts are decrypted with the DEK + per-row nonce + AAD.

    V1.5 explicitly omits a `kek_version` column - KEK rotation is
    deferred to V2 and uses rewrap-in-place against the unsuffixed `kek`
    Keychain account; rotated DEKs are NOT supported at the schema level
    in V1.5.
    """

    __tablename__ = "network_event_meta"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_id = sa.Column(
        sa.ForeignKey("recording.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    dek_wrapped = sa.Column(sa.LargeBinary, nullable=False)
    dek_nonce = sa.Column(sa.LargeBinary(12), nullable=False)
    created_at = sa.Column(ForceFloat, nullable=False)

    recording = sa.orm.relationship("Recording", back_populates="network_event_meta")


# Allowed values for NetworkHealth.event. Failure-only, low-frequency
# proxy lifecycle observability. Readers in chunk_processor mark chunks
# whose time window overlaps a proxy_crashed or network_writer_failed
# row as NETWORK_INCOMPLETE; proxy_started is informational only;
# kek_unavailable is the chunk-processor fail-soft signal that bodies
# could not be decrypted at export time.
NETWORK_HEALTH_EVENTS = (
    "proxy_started",
    "proxy_crashed",
    "kek_unavailable",
    "network_writer_failed",
)


class NetworkHealth(Base):
    """Proxy / network-writer lifecycle and failure events (V1.75).

    Failure-only, low-frequency writes. The chunk processor reads this
    table at export time to mark chunks whose time window overlaps a
    ``proxy_crashed`` or ``network_writer_failed`` row as
    ``NETWORK_INCOMPLETE``. ``proxy_started`` is informational and does
    NOT trigger NETWORK_INCOMPLETE; ``kek_unavailable`` is the
    chunk-processor fail-soft signal when bodies could not be decrypted
    at export time.

    Writes use direct DB inserts (raw sqlite3 or unbuffered SQLAlchemy
    commit). The daemon EventBus is intentionally NOT used — see
    ``docs/solutions/runtime-errors/eventbus-late-listener-replay-2026-05-12.md``
    for the late-listener race the DB-only approach side-steps.

    ``details`` is an optional free-text payload (typically a serialised
    JSON dict — exit_code + log_tail for proxy_crashed; exception type +
    message for network_writer_failed). Stored as TEXT so a >1KB stack
    trace fits without truncation.
    """

    __tablename__ = "network_health"
    __table_args__ = (
        sa.CheckConstraint(
            "event IN ({})".format(
                ", ".join(f"'{e}'" for e in NETWORK_HEALTH_EVENTS)
            ),
            name="ck_network_health_event",
        ),
        sa.Index(
            "ix_network_health_recording_ts",
            "recording_id", "timestamp_ns",
        ),
    )

    id = sa.Column(sa.Integer, primary_key=True)
    recording_id = sa.Column(
        sa.ForeignKey("recording.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Plain TEXT + CheckConstraint mirrors the NetworkEvent.kind pattern
    # above; the constraint is the single source of truth for allowed
    # values and stays in sync with NETWORK_HEALTH_EVENTS by construction.
    event = sa.Column(sa.Text, nullable=False)
    timestamp_ns = sa.Column(sa.BigInteger, nullable=False)
    details = sa.Column(sa.Text, nullable=True)

    recording = sa.orm.relationship("Recording", back_populates="network_health")
