"""CRUD operations for the recording database."""

import json
from typing import Any

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.orm import Session as SaSession

from screencap.engine.db.models import (
    ActionEvent,
    AudioInfo,
    MemoryStat,
    PerformanceStat,
    Recording,
    Screenshot,
    WindowEvent,
    WindowGeometry,
)

BATCH_SIZE = 1  # default; recorder overrides to 50 for throughput

action_events = []
screenshots = []
window_events = []
performance_stats = []
memory_stats = []
window_geometries = []


def _insert(
    session: SaSession,
    event_data: dict[str, Any],
    table: sa.Table,
    buffer: list[dict[str, Any]] | None = None,
) -> sa.engine.Result | None:
    """Insert using Core API for improved performance (no rows are returned).

    Args:
        session (sa.orm.Session): The database session.
        event_data (dict): The event data to be inserted.
        table (sa.Table): The SQLAlchemy table to insert the data into.
        buffer (list, optional): A buffer list to store the inserted objects
            before committing. Defaults to None.

    Returns:
        sa.engine.Result | None: The SQLAlchemy Result object if a buffer is
          not provided. None if a buffer is provided.
    """
    db_obj = {column.name: None for column in table.__table__.columns}
    for key in db_obj:
        if key in event_data:
            val = event_data[key]
            db_obj[key] = val
            del event_data[key]

    # make sure all event data was saved
    assert not event_data, event_data

    if buffer is not None:
        buffer.append(db_obj)

    if buffer is None or len(buffer) >= BATCH_SIZE:
        to_insert = buffer or [db_obj]
        result = session.execute(sa.insert(table), to_insert)
        session.commit()
        if buffer:
            buffer.clear()
        # Note: this does not contain the inserted row(s)
        return result


def flush_buffers(session: SaSession) -> None:
    """Commit any partially-filled insert buffers.

    Must be called before a writer process exits so the last <BATCH_SIZE
    events are not silently dropped.
    """
    _buffer_table_pairs = [
        (action_events, ActionEvent),
        (screenshots, Screenshot),
        (window_events, WindowEvent),
        (window_geometries, WindowGeometry),
        (performance_stats, PerformanceStat),
        (memory_stats, MemoryStat),
    ]
    for buffer, table in _buffer_table_pairs:
        if buffer:
            session.execute(sa.insert(table), buffer)
    session.commit()
    # Clear only after commit succeeds — avoids silent data loss on I/O error.
    for buffer, _ in _buffer_table_pairs:
        buffer.clear()


def insert_action_event(
    session: SaSession,
    recording: Recording,
    event_timestamp: int,
    event_data: dict[str, Any],
) -> None:
    """Insert an action event into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_timestamp (int): The timestamp of the event.
        event_data (dict): The data of the event.
    """
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
    }
    _insert(session, event_data, ActionEvent, action_events)


def insert_screenshot(
    session: SaSession,
    recording: Recording,
    event_timestamp: int,
    event_data: dict[str, Any],
) -> None:
    """Insert a screenshot into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_timestamp (int): The timestamp of the event.
        event_data (dict): The data of the event.
    """
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
    }
    _insert(session, event_data, Screenshot, screenshots)


def insert_window_geometry(
    session: SaSession,
    recording: Recording,
    screenshot_timestamp: float,
    window_list_json: str,
) -> None:
    """Insert a window geometry snapshot for a screenshot.

    Args:
        session: The database session.
        recording: The recording object.
        screenshot_timestamp: The timestamp of the associated screenshot.
        window_list_json: JSON-encoded list of window geometry dicts.
    """
    data = {
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
        "screenshot_timestamp": screenshot_timestamp,
        "window_list_json": window_list_json,
    }
    _insert(session, data, WindowGeometry, window_geometries)


def insert_window_event(
    session: SaSession,
    recording: Recording,
    event_timestamp: int,
    event_data: dict[str, Any],
) -> None:
    """Insert a window event into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_timestamp (int): The timestamp of the event.
        event_data (dict): The data of the event.
    """
    event_data = {
        **event_data,
        "timestamp": event_timestamp,
        "recording_id": recording.id,
        "recording_timestamp": recording.timestamp,
    }
    _insert(session, event_data, WindowEvent, window_events)


def insert_perf_stat(
    session: SaSession,
    recording: Recording,
    event_type: str,
    start_time: float,
    end_time: float,
) -> None:
    """Insert an event performance stat into the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        event_type (str): The type of the event.
        start_time (float): The start time of the event.
        end_time (float): The end time of the event.
    """
    event_perf_stat = {
        "recording_timestamp": recording.timestamp,
        "recording_id": recording.id,
        "event_type": event_type,
        "start_time": start_time,
        "end_time": end_time,
    }
    _insert(session, event_perf_stat, PerformanceStat, performance_stats)


def insert_memory_stat(
    session: SaSession,
    recording: Recording,
    memory_usage_bytes: int,
    timestamp: int,
) -> None:
    """Insert memory stat into db.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        memory_usage_bytes (int): The memory usage in bytes.
        timestamp (int): The timestamp of the event.
    """
    memory_stat = {
        "recording_timestamp": recording.timestamp,
        "recording_id": recording.id,
        "memory_usage_bytes": memory_usage_bytes,
        "timestamp": timestamp,
    }
    _insert(session, memory_stat, MemoryStat, memory_stats)


def insert_recording(session: SaSession, recording_data: dict) -> Recording:
    """Insert the recording into to the db.

    Args:
        session (sa.orm.Session): The database session.
        recording_data (dict): The data of the recording.

    Returns:
        Recording: The recording object.
    """
    db_obj = Recording(**recording_data)
    session.add(db_obj)
    session.commit()
    session.refresh(db_obj)
    return db_obj


def update_video_start_time(
    session: SaSession, recording: Recording, video_start_time: float
) -> None:
    """Update the video start time of a specific recording.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object to update.
        video_start_time (float): The new video start time to set.
    """
    # Find the recording by its timestamp
    recording = session.query(Recording).filter(Recording.id == recording.id).first()

    if not recording:
        logger.error(f"No recording found with id {recording.id}.")
        return

    # Update the video start time
    recording.video_start_time = video_start_time

    # the function is called from a different process which uses a different
    # session from the one used to create the recording object, so we need to
    # add the recording object to the session
    session.add(recording)
    # Commit the changes to the database
    session.commit()

    logger.info(
        f"Updated video start time for recording {recording.timestamp} to"
        f" {video_start_time}."
    )


def insert_audio_info(
    session: SaSession,
    recording: Recording,
    timestamp: float,
    sample_rate: int,
    word_list: list,
) -> None:
    """Create an AudioInfo entry in the database.

    Args:
        session (sa.orm.Session): The database session.
        recording (Recording): The recording object.
        timestamp (float): The timestamp of the audio.
        sample_rate (int): The sample rate of the audio.
        word_list (list): A list of words with timestamps.
    """
    audio_info = AudioInfo(
        recording_timestamp=recording.timestamp,
        recording_id=recording.id,
        timestamp=timestamp,
        sample_rate=sample_rate,
        words_with_timestamps=json.dumps(word_list),
    )
    session.add(audio_info)
    session.commit()


