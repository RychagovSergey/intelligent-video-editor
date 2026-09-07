"""Операции над таймлайном (ТЗ п. 3.3)."""
from __future__ import annotations

import pytest

from backend.app.timeline import ops
from backend.app.timeline.schema import Timeline, empty_timeline


@pytest.fixture
def tl() -> Timeline:
    return empty_timeline()


def add(tl: Timeline, name: str, duration: float = 10.0, **kw):
    return ops.add_clip(tl, source_id=1, kind="video", name=name, source_duration=duration, **kw)


def test_new_project_has_video_and_audio_tracks(tl: Timeline) -> None:
    assert [t.kind for t in tl.tracks] == ["video", "audio", "text"]
    assert tl.duration == 0.0


def test_clips_are_appended_one_after_another(tl: Timeline) -> None:
    add(tl, "a.mp4", 10)
    add(tl, "b.mp4", 5)
    assert [(c.start, c.end) for c in tl.tracks[0].sorted_clips()] == [(0.0, 10.0), (10.0, 15.0)]
    assert tl.duration == 15.0


def test_image_gets_default_display_duration(tl: Timeline) -> None:
    clip = ops.add_clip(tl, source_id=2, kind="image", name="p.png")
    assert clip.duration == 3.0


def test_audio_goes_to_audio_track(tl: Timeline) -> None:
    clip = ops.add_clip(tl, source_id=3, kind="audio", name="track.mp3", source_duration=120)
    assert clip in tl.first_track("audio").clips
    assert not tl.first_track("video").clips


def test_out_point_is_clamped_to_source_duration(tl: Timeline) -> None:
    clip = ops.add_clip(tl, source_id=1, kind="video", name="a.mp4", out_point=99, source_duration=10)
    assert clip.out_point == 10.0


def test_speed_changes_timeline_duration_not_source_range(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    ops.set_speed(tl, clip.id, speed=2.0)
    assert clip.source_duration == 10.0
    assert clip.duration == 5.0


def test_split_produces_two_adjacent_clips(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    head, tail = ops.split_clip(tl, clip.id, at=4.0)

    assert head.start == 0.0 and head.end == 4.0
    assert tail.start == 4.0 and tail.end == 10.0
    assert head.out_point == tail.in_point == 4.0
    assert len(tl.tracks[0].clips) == 2


def test_split_respects_speed(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    ops.set_speed(tl, clip.id, speed=2.0)     # 10 с исходника → 5 с на таймлайне
    head, tail = ops.split_clip(tl, clip.id, at=2.0)
    assert head.out_point == 4.0              # 2 с таймлайна = 4 с исходника
    assert tail.in_point == 4.0


def test_split_near_edge_is_rejected(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    with pytest.raises(ops.TimelineError):
        ops.split_clip(tl, clip.id, at=0.01)


def test_move_onto_occupied_place_is_rejected_and_rolls_back(tl: Timeline) -> None:
    first = add(tl, "a.mp4", 10)
    second = add(tl, "b.mp4", 10)

    with pytest.raises(ops.TimelineError):
        ops.move_clip(tl, second.id, start=5.0)

    assert second.start == 10.0     # позиция не изменилась


def test_move_to_free_place_works(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    ops.move_clip(tl, clip.id, start=20.0)
    assert clip.start == 20.0


def test_clip_cannot_move_to_track_of_other_kind(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    with pytest.raises(ops.TimelineError):
        ops.move_clip(tl, clip.id, start=0.0, track_id="audio_1")


def test_trim_keeps_start_by_default(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    ops.trim_clip(tl, clip.id, in_point=2.0, source_duration=10)
    assert clip.start == 0.0
    assert clip.duration == 8.0


def test_trim_below_minimum_is_rejected(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    with pytest.raises(ops.TimelineError):
        ops.trim_clip(tl, clip.id, in_point=5.0, out_point=5.0)


def test_delete_and_close_gaps(tl: Timeline) -> None:
    add(tl, "a.mp4", 10)
    middle = add(tl, "b.mp4", 5)
    add(tl, "c.mp4", 4)

    ops.delete_clip(tl, middle.id)
    ops.close_gaps(tl, "video_1")

    assert [(c.name, c.start) for c in tl.tracks[0].sorted_clips()] == [("a.mp4", 0.0), ("c.mp4", 10.0)]


def test_timeline_survives_json_roundtrip(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    ops.set_speed(tl, clip.id, speed=0.5)

    restored = Timeline(**tl.model_dump())

    assert restored.duration == tl.duration
    assert restored.tracks[0].clips[0].speed == 0.5
    assert restored.version == 1


def test_unknown_clip_raises(tl: Timeline) -> None:
    with pytest.raises(ops.TimelineError):
        ops.delete_clip(tl, "clip_nope")


def test_mute_flag_survives_json_roundtrip(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    ops.set_muted(tl, clip.id, muted=True)

    restored = Timeline(**tl.model_dump())

    assert restored.tracks[0].clips[0].muted is True


def test_fade_is_set_on_clip(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    ops.set_fade(tl, clip.id, fade_in=1.0, fade_out=2.0)
    assert clip.fade_in == 1.0
    assert clip.fade_out == 2.0


def test_fade_is_clamped_to_clip_duration(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 3)
    ops.set_fade(tl, clip.id, fade_in=10.0)
    assert clip.fade_in == clip.duration == 3.0


def test_fade_rejects_negative_values(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    with pytest.raises(ops.TimelineError):
        ops.set_fade(tl, clip.id, fade_in=-1.0)


# --- переходы (set_transition/clear_transition) ---


def test_transition_shifts_this_and_later_clips_left(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 10)
    c = add(tl, "c.mp4", 10)

    ops.set_transition(tl, b.id, kind="crossfade", duration=1.0)

    assert a.start == 0.0 and a.end == 10.0
    assert b.transition_in is not None
    assert b.transition_in.kind == "crossfade"
    assert b.start == 9.0                 # 10 - 1
    assert c.start == 19.0                # был 20, минус 1
    assert tl.duration == 29.0             # 30 - 1


def test_transition_requires_adjacent_clips(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 10, start=15.0)   # с разрывом
    with pytest.raises(ops.TimelineError):
        ops.set_transition(tl, b.id, kind="crossfade", duration=1.0)


def test_transition_requires_previous_clip(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    with pytest.raises(ops.TimelineError):
        ops.set_transition(tl, a.id, kind="crossfade", duration=1.0)


def test_transition_rejects_duration_longer_than_clip(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 3)
    with pytest.raises(ops.TimelineError):
        ops.set_transition(tl, b.id, kind="crossfade", duration=5.0)


def test_transition_rejects_unknown_kind(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 10)
    with pytest.raises(ops.TimelineError):
        ops.set_transition(tl, b.id, kind="whip_pan", duration=0.5)


def test_clear_transition_restores_positions(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 10)
    c = add(tl, "c.mp4", 10)
    ops.set_transition(tl, b.id, kind="crossfade", duration=1.0)

    ops.clear_transition(tl, b.id)

    assert b.transition_in is None
    assert b.start == 10.0
    assert c.start == 20.0
    assert tl.duration == 30.0


def test_setting_transition_twice_replaces_it(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 10)
    ops.set_transition(tl, b.id, kind="crossfade", duration=1.0)

    ops.set_transition(tl, b.id, kind="dip_to_black", duration=2.0)

    assert b.transition_in.kind == "dip_to_black"
    assert b.transition_in.duration == 2.0
    assert b.start == 8.0                  # 10 - 2, не накопленный сдвиг двух вызовов


def test_transition_overlap_does_not_block_further_edits(tl: Timeline) -> None:
    """_overlaps должен пропускать ровно заявленное переходом перекрытие."""
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 10)
    ops.set_transition(tl, b.id, kind="crossfade", duration=1.0)

    ops.set_muted(tl, b.id, muted=True)    # не трогает позицию/длительность — не должно падать
    assert b.muted is True


def test_moving_transitioned_clip_clears_transition(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 10)
    c = add(tl, "c.mp4", 10)
    ops.set_transition(tl, b.id, kind="crossfade", duration=1.0)

    ops.move_clip(tl, b.id, start=50.0)

    assert b.transition_in is None
    assert c.start == 20.0                 # вернулся на место, как будто перехода не было


def test_deleting_clip_clears_next_clips_dangling_transition(tl: Timeline) -> None:
    a = add(tl, "a.mp4", 10)
    b = add(tl, "b.mp4", 10)
    c = add(tl, "c.mp4", 10)
    ops.set_transition(tl, b.id, kind="crossfade", duration=1.0)

    ops.delete_clip(tl, a.id)

    assert b.transition_in is None
    assert b.start == 10.0
    assert c.start == 20.0


# --- текстовые слои (add_text_clip) ---


def test_add_text_clip_lands_on_text_track(tl: Timeline) -> None:
    clip = ops.add_text_clip(tl, text="Привет", duration=2.0)
    text_track = tl.first_track("text")
    assert text_track is not None
    assert clip in text_track.clips
    assert clip.kind == "text"
    assert clip.duration == 2.0
    assert clip.position == "bottom"
    assert clip.animation == "fade"


def test_add_text_clip_creates_missing_text_track(tl: Timeline) -> None:
    """Проекты, сохранённые до текстовых слоёв, не имеют дорожки text_1."""
    tl.tracks = [t for t in tl.tracks if t.kind != "text"]
    clip = ops.add_text_clip(tl, text="Заголовок", duration=1.5)
    assert tl.first_track("text") is not None
    assert clip in tl.first_track("text").clips


def test_add_text_clip_rejects_empty_text(tl: Timeline) -> None:
    with pytest.raises(ops.TimelineError):
        ops.add_text_clip(tl, text="   ", duration=2.0)


def test_add_text_clip_rejects_bad_position(tl: Timeline) -> None:
    with pytest.raises(ops.TimelineError):
        ops.add_text_clip(tl, text="Привет", duration=2.0, position="left")


def test_add_text_clip_conflict_falls_back_to_append(tl: Timeline) -> None:
    ops.add_text_clip(tl, text="Первый", duration=3.0)
    second = ops.add_text_clip(tl, text="Второй", duration=2.0, start=0.0)
    assert second.start == 3.0


def test_set_text_properties_updates_fields(tl: Timeline) -> None:
    clip = ops.add_text_clip(tl, text="Привет", duration=2.0)
    ops.set_text_properties(tl, clip.id, text="Пока", font_size=72, position="top", animation="none")
    assert clip.text == "Пока"
    assert clip.name == "Пока"
    assert clip.font_size == 72
    assert clip.position == "top"
    assert clip.animation == "none"


def test_set_text_properties_partial_update(tl: Timeline) -> None:
    clip = ops.add_text_clip(tl, text="Привет", duration=2.0, font_size=48, position="bottom")
    ops.set_text_properties(tl, clip.id, font_size=90)
    assert clip.text == "Привет"
    assert clip.font_size == 90
    assert clip.position == "bottom"


def test_set_text_properties_rejects_non_text_clip(tl: Timeline) -> None:
    clip = add(tl, "a.mp4", 10)
    with pytest.raises(ops.TimelineError):
        ops.set_text_properties(tl, clip.id, text="Привет")
