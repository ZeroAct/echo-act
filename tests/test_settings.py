"""Behaviour of the persisted settings file (F-24, F-86, F-78, Section 4.1)."""

from __future__ import annotations

import inspect
import json
import os
import threading
from pathlib import Path

import pytest

from echoact import paths
from echoact.config.settings import (
    DEFAULT_MODEL_ID,
    DEFAULT_VOICE_ID,
    SETTINGS_SCHEMA_VERSION,
    DisplayLanguage,
    Settings,
    SettingsModelPreferences,
    SettingsStore,
    VoicePreset,
    load_settings,
    os_display_language,
    output_device_key,
    save_settings,
)
from echoact.domain import Budget, Gender, Language, SpeakingStyle, VoiceSettings
from echoact.errors import Code, EchoActError
from echoact.policy import (
    AUTOPLAY_DEFAULT,
    CLOSE_TO_TRAY_DEFAULT,
    CPU_PERCENT_DEFAULT,
    CREDENTIAL_DAYS_DEFAULT,
    FOLLOW_DEFAULT,
    MEMORY_CEILING_BYTES,
    MEMORY_FLOOR_BYTES,
    REST_PORT_DEFAULT,
    RETENTION_DEFAULT_BYTES,
    RETENTION_MAX_BYTES,
    TEMPO_DEFAULT,
    TEMPO_MAX,
    VOICE_PRESET_MAX,
)


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real user data directory; ``data_dir`` is lru_cached."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOACT_DISPLAY_LANGUAGE", "en")
    paths.data_dir.cache_clear()
    yield tmp_path
    paths.data_dir.cache_clear()


def _voice(**kw: object) -> VoiceSettings:
    base = {
        "model_id": DEFAULT_MODEL_ID,
        "language": Language.KO,
        "gender": Gender.MALE,
        "voice_id": "M3",
        "style": SpeakingStyle.CALM,
        "tempo": 1.25,
    }
    base.update(kw)
    return VoiceSettings(**base)  # type: ignore[arg-type]


# ----------------------------------------------------------------- F-24 ---


def test_defaults_match_the_documented_first_launch_policy() -> None:
    s = Settings()
    assert s.voice.model_id == DEFAULT_MODEL_ID
    assert s.voice.voice_id == DEFAULT_VOICE_ID
    assert s.voice.language is Language.AUTO
    assert s.voice.style is SpeakingStyle.NATURAL
    assert s.voice.tempo == TEMPO_DEFAULT
    assert s.cpu_percent == CPU_PERCENT_DEFAULT
    assert s.memory_bytes is None
    assert s.autoplay is AUTOPLAY_DEFAULT is True
    assert s.follow is FOLLOW_DEFAULT is True
    assert (s.volume, s.muted, s.output_device) == (1.0, False, None)
    assert s.os_notifications is False
    assert s.close_to_tray is CLOSE_TO_TRAY_DEFAULT is True
    assert (s.rest_enabled, s.rest_port, s.mcp_enabled) == (True, REST_PORT_DEFAULT, True)
    assert s.external_play is True  # F-89
    assert s.credential_days == CREDENTIAL_DAYS_DEFAULT
    assert s.autosave_documents is False
    assert s.retain_history is False
    assert s.retention_bytes == RETENTION_DEFAULT_BYTES
    assert s.scheduled_backup is False
    assert s.scheduled_backup_location is None
    assert s.authorised_downloads == ()
    assert dict(s.accepted_licences) == {}
    assert s.presets == ()


def test_every_remembered_setting_survives_a_save_and_reload() -> None:
    original = Settings(
        voice=_voice(),
        cpu_percent=55,
        memory_bytes=3 * (1 << 30),
        autoplay=False,
        follow=False,
        volume=0.4,
        muted=True,
        output_device=output_device_key("MME", "Speakers (Realtek)"),
        display_language=DisplayLanguage.KO,
        os_notifications=True,
        close_to_tray=False,
        rest_enabled=False,
        rest_port=9100,
        mcp_enabled=False,
        external_play=False,
        credential_days=30,
        autosave_documents=True,
        retain_history=True,
        retention_bytes=RETENTION_MAX_BYTES,
        scheduled_backup=True,
        scheduled_backup_location="D:/backups",
        authorised_downloads=(DEFAULT_MODEL_ID,),
        accepted_licences={DEFAULT_MODEL_ID: "openrail-m-1"},
        presets=(VoicePreset("Podcast", _voice(tempo=0.9)),),
    )
    save_settings(original)

    restored, problems = load_settings()
    assert problems == ()
    assert restored == original


def test_unsaved_input_and_playback_position_are_never_written() -> None:
    """F-24 restores settings but explicitly not the previous session."""
    fields = set(Settings.__dataclass_fields__)
    forbidden = {"text", "input", "draft", "playback", "position", "last_job", "cursor"}
    assert not {f for f in fields if any(word in f for word in forbidden)}
    assert not {k for k in Settings().to_dict() if any(word in k for word in forbidden)}


def test_a_missing_file_is_a_first_launch_and_not_a_problem() -> None:
    settings, problems = load_settings()
    assert settings == Settings()
    assert problems == ()
    assert not paths.settings_path().exists()


# ------------------------------------------------------- atomic save, N-14 ---


def test_save_replaces_the_file_atomically_and_leaves_no_temporary_behind() -> None:
    save_settings(Settings())
    save_settings(Settings(cpu_percent=42))
    target = paths.settings_path()
    assert json.loads(target.read_text(encoding="utf-8"))["cpu_percent"] == 42
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_a_failed_save_leaves_the_previous_file_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """N-14: previously sound data survives a save that fails midway."""
    save_settings(Settings(cpu_percent=30))
    sound = paths.settings_path().read_bytes()

    real_replace = os.replace

    def explode(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(EchoActError) as caught:
        save_settings(Settings(cpu_percent=60))
    monkeypatch.setattr(os, "replace", real_replace)

    assert caught.value.code is Code.STORAGE_FULL
    assert paths.settings_path().read_bytes() == sound
    assert load_settings()[0].cpu_percent == 30
    assert [p.name for p in paths.settings_path().parent.iterdir()] == ["settings.json"]


def test_a_damaged_file_falls_back_to_defaults_with_a_reported_problem() -> None:
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text('{"cpu_percent": 4', encoding="utf-8")

    settings, problems = load_settings()

    assert settings == Settings()
    assert [p.code for p in problems] == [Code.FILE_CORRUPT]
    # The evidence is left alone rather than quarantined behind the user's back.
    assert paths.settings_path().read_text(encoding="utf-8") == '{"cpu_percent": 4'


def test_a_file_that_is_not_an_object_falls_back_to_defaults() -> None:
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text("[1, 2, 3]", encoding="utf-8")
    settings, problems = load_settings()
    assert settings == Settings()
    assert [p.code for p in problems] == [Code.FILE_CORRUPT]


def test_a_problem_message_never_carries_the_users_home_path() -> None:
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text("not json", encoding="utf-8")
    _, problems = load_settings()
    text = " ".join([problems[0].message, *problems[0].remedies])
    assert str(Path.home()) not in text
    assert "<data>/settings.json" in text


# --------------------------------------------------- unknown-key round trip ---


def test_keys_from_a_newer_version_survive_a_round_trip() -> None:
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    payload = Settings().to_dict()
    payload["tomorrows_setting"] = {"nested": [1, 2]}
    paths.settings_path().write_text(json.dumps(payload), encoding="utf-8")

    settings, problems = load_settings()
    assert problems == ()
    assert settings.extra == {"tomorrows_setting": {"nested": [1, 2]}}

    save_settings(settings.with_(cpu_percent=33))
    written = json.loads(paths.settings_path().read_text(encoding="utf-8"))
    assert written["tomorrows_setting"] == {"nested": [1, 2]}
    assert written["cpu_percent"] == 33


def test_an_unknown_key_cannot_shadow_a_known_one() -> None:
    settings = Settings(cpu_percent=25, extra={"cpu_percent": 999})
    assert settings.to_dict()["cpu_percent"] == 25


# ------------------------------------------------------ repairing bad values ---


@pytest.mark.parametrize(
    ("key", "written", "expected", "code"),
    [
        ("cpu_percent", 5, 10, Code.FILE_CORRUPT),
        ("cpu_percent", 95, 70, Code.FILE_CORRUPT),
        ("cpu_percent", "lots", CPU_PERCENT_DEFAULT, Code.FILE_CORRUPT),
        ("volume", 4.5, 1.0, Code.FILE_CORRUPT),
        ("rest_port", 80, 1024, Code.FILE_CORRUPT),
        ("credential_days", 4000, 365, Code.FILE_CORRUPT),
        ("retention_bytes", 1, 1_000_000_000, Code.FILE_CORRUPT),
        ("memory_bytes", 64 * (1 << 30), MEMORY_CEILING_BYTES, Code.FILE_CORRUPT),
        ("memory_bytes", 1024, MEMORY_FLOOR_BYTES, Code.FILE_CORRUPT),
        ("display_language", "fr", DisplayLanguage.EN, Code.LANGUAGE_UNKNOWN),
        ("autoplay", "yes", True, Code.FILE_CORRUPT),
    ],
)
def test_an_out_of_range_value_is_clamped_and_reported(
    key: str, written: object, expected: object, code: Code
) -> None:
    settings, problems = Settings.from_dict({key: written})
    assert getattr(settings, key) == expected
    assert [p.code for p in problems] == [code]


def test_an_unreadable_memory_setting_returns_to_sizing_from_the_machine() -> None:
    """F-21's default is derived from total RAM, so ``None`` is the right
    repair; the 2 GiB floor is what F-23 refuses below, not a default."""
    settings, problems = Settings.from_dict({"memory_bytes": "lots"})
    assert settings.memory_bytes is None
    assert [p.code for p in problems] == [Code.FILE_CORRUPT]


def test_a_null_reads_the_same_as_an_absent_key() -> None:
    settings, problems = Settings.from_dict({"cpu_percent": None, "volume": None})
    assert (settings.cpu_percent, settings.volume) == (CPU_PERCENT_DEFAULT, 1.0)
    assert problems == ()


def test_a_tempo_outside_the_supported_range_is_clamped_and_reported() -> None:
    settings, problems = Settings.from_dict({"voice": {"tempo": 3.0}})
    assert settings.voice.tempo == TEMPO_MAX
    assert Code.TEMPO_OUT_OF_RANGE in [p.code for p in problems]
    settings.voice.validate()


def test_one_unreadable_voice_field_does_not_lose_the_others() -> None:
    settings, problems = Settings.from_dict(
        {
            "voice": {
                "model_id": DEFAULT_MODEL_ID,
                "language": "martian",
                "gender": "male",
                "voice_id": "M2",
                "style": "calm",
                "tempo": 1.4,
            }
        }
    )
    assert settings.voice.language is Language.AUTO
    assert (settings.voice.gender, settings.voice.voice_id) == (Gender.MALE, "M2")
    assert settings.voice.style is SpeakingStyle.CALM
    assert settings.voice.tempo == 1.4
    assert [p.code for p in problems] == [Code.LANGUAGE_UNKNOWN]


def test_an_unreadable_settings_value_never_widens_a_safety_limit() -> None:
    settings, _ = Settings.from_dict({"cpu_percent": 100, "retention_bytes": 10 ** 15})
    assert settings.cpu_percent == 70
    assert settings.retention_bytes == RETENTION_MAX_BYTES


# ----------------------------------------------------------------- F-86 ---


def test_display_language_defaults_to_the_os_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECHOACT_DISPLAY_LANGUAGE", "ko_KR.UTF-8")
    assert os_display_language() is DisplayLanguage.KO
    assert Settings().display_language is DisplayLanguage.KO


def test_display_language_falls_back_to_english_for_anything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECHOACT_DISPLAY_LANGUAGE", "de-DE")
    assert os_display_language() is DisplayLanguage.EN


def test_display_language_is_independent_of_the_narration_language() -> None:
    """F-86: changing the UI language leaves the job's settings untouched."""
    settings = Settings(voice=_voice(language=Language.KO))
    switched = settings.with_(display_language=DisplayLanguage.EN)
    assert switched.voice == settings.voice


# ----------------------------------------------------------------- F-78 ---


def test_settings_hold_the_configured_value_and_the_job_holds_the_applied_one() -> None:
    settings = Settings(cpu_percent=20, memory_bytes=6 * (1 << 30))
    applied = Budget(cpu_percent=20, memory_bytes=2 * (1 << 30), intra_op_threads=2)

    view = settings.policy_view(applied)

    assert view.configured_memory_bytes == 6 * (1 << 30)
    assert view.applied is applied
    assert view.differs is True
    assert settings.memory_bytes == 6 * (1 << 30), "settings must not learn the applied value"


def test_no_running_job_means_no_applied_value_to_show() -> None:
    view = Settings().policy_view(None)
    assert view.applied is None
    assert view.differs is False


# ------------------------------------------------------ F-09 / 5.3, N-11 ---


def test_no_model_may_be_downloaded_for_an_integration_by_default() -> None:
    assert Settings().may_download(DEFAULT_MODEL_ID) is False


def test_the_owner_can_authorise_and_withdraw_a_download_per_model() -> None:
    allowed = Settings().authorise_download(DEFAULT_MODEL_ID)
    assert allowed.may_download(DEFAULT_MODEL_ID) is True
    assert allowed.may_download("something-else") is False
    assert allowed.authorise_download(DEFAULT_MODEL_ID, False).may_download(DEFAULT_MODEL_ID) is False


def test_authorising_the_same_model_twice_does_not_duplicate_it() -> None:
    twice = Settings().authorise_download(DEFAULT_MODEL_ID).authorise_download(DEFAULT_MODEL_ID)
    assert twice.authorised_downloads == (DEFAULT_MODEL_ID,)


def test_licence_acceptance_is_tied_to_the_licence_that_was_accepted() -> None:
    """N-11: consent to one set of terms is not consent to the next."""
    accepted = Settings().accept_licence(DEFAULT_MODEL_ID, "openrail-m-1")
    assert accepted.licence_accepted(DEFAULT_MODEL_ID, "openrail-m-1") is True
    assert accepted.licence_accepted(DEFAULT_MODEL_ID, "openrail-m-2") is False
    assert accepted.licence_accepted("another-model", "openrail-m-1") is False


# ----------------------------------------------------------------- F-66 ---


def test_a_preset_saves_only_the_voice_settings() -> None:
    settings = Settings(cpu_percent=60, retention_bytes=RETENTION_MAX_BYTES)
    with_preset = settings.save_preset("Podcast", _voice())
    applied = with_preset.apply_preset("Podcast")

    assert applied.voice == _voice()
    assert applied.cpu_percent == 60
    assert applied.retention_bytes == RETENTION_MAX_BYTES


def test_saving_a_preset_under_an_existing_name_overwrites_it_in_place() -> None:
    settings = Settings().save_preset("A", _voice()).save_preset("B", _voice(tempo=0.8))
    updated = settings.save_preset("A", _voice(tempo=1.5))

    assert [p.name for p in updated.presets] == ["A", "B"]
    assert updated.preset("A").voice.tempo == 1.5


def test_the_hundred_and_first_new_preset_is_refused() -> None:
    settings = Settings()
    for i in range(VOICE_PRESET_MAX):
        settings = settings.save_preset(f"p{i}", _voice())
    assert len(settings.presets) == VOICE_PRESET_MAX

    # Overwriting an existing name still works at the limit.
    assert len(settings.save_preset("p0", _voice(tempo=1.1)).presets) == VOICE_PRESET_MAX

    with pytest.raises(EchoActError) as caught:
        settings.save_preset("one too many", _voice())
    assert caught.value.detail == {"limit": VOICE_PRESET_MAX}


def test_a_preset_needs_a_name() -> None:
    with pytest.raises(EchoActError) as caught:
        Settings().save_preset("   ", _voice())
    assert caught.value.code is Code.INPUT_EMPTY


def test_renaming_a_preset_keeps_its_position() -> None:
    settings = (
        Settings()
        .save_preset("A", _voice())
        .save_preset("B", _voice(tempo=0.8))
        .save_preset("C", _voice(tempo=1.4))
    )
    renamed = settings.rename_preset("B", "Bee")
    assert [p.name for p in renamed.presets] == ["A", "Bee", "C"]
    assert renamed.preset("Bee").voice.tempo == 0.8


def test_deleting_or_applying_a_missing_preset_is_a_clear_refusal() -> None:
    for call in (
        lambda: Settings().delete_preset("gone"),
        lambda: Settings().apply_preset("gone"),
        lambda: Settings().rename_preset("gone", "new"),
    ):
        with pytest.raises(EchoActError) as caught:
            call()
        assert caught.value.code is Code.NOT_FOUND


def test_presets_survive_a_round_trip_and_a_broken_one_is_dropped() -> None:
    payload = Settings().save_preset("Keep", _voice()).to_dict()
    payload["presets"].append({"name": "Broken"})
    payload["presets"].append({"name": "Keep", "voice": _voice().to_dict()})

    settings, problems = Settings.from_dict(payload)

    assert [p.name for p in settings.presets] == ["Keep"]
    assert [p.code for p in problems] == [Code.FILE_CORRUPT]


# ------------------------------------------------------------ the store ---


def test_the_store_reports_what_the_last_load_had_to_repair() -> None:
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text(json.dumps({"cpu_percent": 500}), encoding="utf-8")

    store = SettingsStore()

    assert store.current.cpu_percent == 70
    assert [p.code for p in store.problems] == [Code.FILE_CORRUPT]


def test_the_store_keeps_the_on_disk_value_when_a_save_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SettingsStore()
    store.save(Settings(cpu_percent=30))

    def explode(src: object, dst: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(EchoActError) as caught:
        store.update(cpu_percent=65)

    assert caught.value.code is Code.FILE_PERMISSION
    assert store.current.cpu_percent == 30


def test_the_store_writes_where_paths_says(data_dir: Path) -> None:
    store = SettingsStore()
    assert store.path == data_dir / "settings.json"
    store.save(Settings())
    assert store.path.exists()


# ---------------------------------------- numbers JSON has and Python has not ---


def _write(text: str) -> None:
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("document", "field", "expected"),
    [
        ('{"memory_bytes": NaN}', "memory_bytes", None),
        ('{"memory_bytes": Infinity}', "memory_bytes", MEMORY_CEILING_BYTES),
        ('{"memory_bytes": -Infinity}', "memory_bytes", MEMORY_FLOOR_BYTES),
        ('{"cpu_percent": Infinity}', "cpu_percent", 70),
        ('{"cpu_percent": NaN}', "cpu_percent", CPU_PERCENT_DEFAULT),
        ('{"rest_port": NaN}', "rest_port", REST_PORT_DEFAULT),
        ('{"rest_port": Infinity}', "rest_port", 65535),
        ('{"credential_days": -Infinity}', "credential_days", 1),
        ('{"retention_bytes": 1e400}', "retention_bytes", RETENTION_MAX_BYTES),
        (
            '{"retention_bytes": ' + "1" + "0" * 400 + "}",
            "retention_bytes",
            RETENTION_MAX_BYTES,
        ),
        ('{"volume": NaN}', "volume", 1.0),
        ('{"volume": 1e400}', "volume", 1.0),
    ],
)
def test_a_number_json_allows_but_python_cannot_convert_never_stops_the_launch(
    document: str, field: str, expected: object
) -> None:
    """F-24: load repairs every field on its own and never raises.

    ``json.loads`` reads ``NaN`` and ``Infinity``, and a JSON integer literal
    has no size limit, so a hand-edited file can hold values that ``int()``
    and ``float()`` refuse outright rather than merely dislike.
    """
    _write(document)

    settings, problems = load_settings()

    assert getattr(settings, field) == expected
    assert [p.code for p in problems] == [Code.FILE_CORRUPT]


def test_a_preset_tempo_that_is_not_a_number_does_not_stop_the_launch() -> None:
    payload = Settings().to_dict()
    payload["presets"] = [{"name": "P", "voice": _voice().to_dict()}]
    _write(json.dumps(payload).replace('"tempo": 1.25', '"tempo": NaN'))

    settings, problems = load_settings()

    assert settings.presets[0].voice.tempo == TEMPO_DEFAULT
    assert Code.TEMPO_OUT_OF_RANGE in [p.code for p in problems]
    settings.presets[0].voice.validate()


def test_a_value_that_could_never_be_read_back_is_refused_before_the_file_is_written() -> None:
    """The app must not be able to write a settings file it cannot load.

    ``json.dumps`` spells a float infinity ``Infinity`` unless told not to, so
    one unchecked ``update`` used to leave a file that failed every launch
    afterwards.  The last sound file stays exactly as it was instead.
    """
    store = SettingsStore()
    store.save(Settings(cpu_percent=30))
    sound = paths.settings_path().read_bytes()

    with pytest.raises(EchoActError) as caught:
        store.update(memory_bytes=float("inf"))

    assert caught.value.code is Code.INTERNAL
    assert paths.settings_path().read_bytes() == sound
    assert store.current.cpu_percent == 30
    assert load_settings()[0].memory_bytes is None


# ----------------------------------------------- two saves at once, N-14 ---


def _pause_first_fsync(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Hold the first ``save_settings`` inside ``fsync``, temp file on disk."""
    reached = threading.Event()
    release = threading.Event()
    real_fsync = os.fsync

    def hold(fd: int) -> None:
        real_fsync(fd)
        if not reached.is_set():
            reached.set()
            assert release.wait(10), "the paused save was never released"

    monkeypatch.setattr(os, "fsync", hold)
    return reached, release


def test_two_saves_at_once_do_not_write_to_the_same_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N-14: the save that lands has to be the save that reported success.

    A temp name fixed per process is shared by every concurrent save in that
    process: the second writer truncates the first's payload and the two
    renames race, so one caller's content lands while the *other* caller is
    told it saved.
    """
    reached, release = _pause_first_fsync(monkeypatch)
    failures: list[BaseException] = []

    def slow_save() -> None:
        try:
            save_settings(Settings(cpu_percent=70).save_preset("Podcast", _voice()))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            failures.append(exc)

    first = threading.Thread(target=slow_save)
    first.start()
    assert reached.wait(10)

    target = paths.settings_path()
    temporaries = [p for p in target.parent.iterdir() if p != target]
    assert len(temporaries) == 1, "the paused save should own exactly one temporary file"
    held = temporaries[0]
    held_payload = held.read_text(encoding="utf-8")

    save_settings(Settings(cpu_percent=11))  # a whole second save, start to finish

    assert held.read_text(encoding="utf-8") == held_payload, "the second save reused the temp file"
    assert json.loads(target.read_text(encoding="utf-8"))["cpu_percent"] == 11

    release.set()
    first.join(10)
    assert not failures, f"the paused save failed: {failures}"
    landed = json.loads(target.read_text(encoding="utf-8"))
    assert (landed["cpu_percent"], len(landed["presets"])) == (70, 1)
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_the_store_serialises_updates_so_the_first_one_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUI, the REST service and MCP all change settings from their own
    threads; an unguarded read-modify-write drops whichever change reaches the
    file first while telling both callers they succeeded."""
    store = SettingsStore()
    store.load()
    reached, release = _pause_first_fsync(monkeypatch)
    done = threading.Event()
    failures: list[BaseException] = []

    def call(**kw: object) -> None:
        try:
            store.update(**kw)
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            failures.append(exc)

    def second_update() -> None:
        call(rest_port=9100)
        done.set()

    first = threading.Thread(target=call, kwargs={"cpu_percent": 70})
    first.start()
    assert reached.wait(10)

    second = threading.Thread(target=second_update)
    second.start()
    assert not done.wait(0.3), "the second update did not wait for the first to land"

    release.set()
    first.join(10)
    second.join(10)
    assert not failures, f"an update failed: {failures}"

    written = json.loads(paths.settings_path().read_text(encoding="utf-8"))
    assert (written["cpu_percent"], written["rest_port"]) == (70, 9100)
    assert (store.current.cpu_percent, store.current.rest_port) == (70, 9100)


# ------------------------------------------ presets are clamped too, F-07 ---


def test_a_preset_restored_from_disk_is_clamped_like_the_voice_it_replaces() -> None:
    """F-66's presets reach ``Settings.voice`` by their own route out of the
    file, so F-07's range has to be enforced on both."""
    payload = Settings().to_dict()
    payload["presets"] = [{"name": "evil", "voice": {**_voice().to_dict(), "tempo": 99.0}}]
    _write(json.dumps(payload))

    settings, problems = load_settings()

    assert settings.presets[0].voice.tempo == TEMPO_MAX
    assert Code.TEMPO_OUT_OF_RANGE in [p.code for p in problems]

    applied = settings.apply_preset("evil")
    assert applied.voice.tempo == TEMPO_MAX
    applied.voice.validate()


def test_a_preset_field_this_version_does_not_know_falls_back_like_the_voice() -> None:
    payload = Settings().to_dict()
    payload["presets"] = [{"name": "P", "voice": {**_voice().to_dict(), "style": "martian"}}]
    _write(json.dumps(payload))

    settings, problems = load_settings()

    assert settings.presets[0].voice.style is SpeakingStyle.NATURAL
    assert [p.code for p in problems] == [Code.STYLE_UNKNOWN]


# ---------------------------------------------- the version marker, N-15 ---


def test_a_file_from_a_newer_version_is_not_interpreted_under_this_versions_rules() -> None:
    """N-15.  A key this build recognises may mean something else in the
    format that wrote it, so none of them is read."""
    _write(
        json.dumps(
            {
                "version": SETTINGS_SCHEMA_VERSION + 6,
                "cpu_percent": 55,
                "retention_bytes": 7,
                "future_thing": 1,
            }
        )
    )

    settings, problems = load_settings()

    assert settings.schema_version == SETTINGS_SCHEMA_VERSION + 6
    assert settings.from_a_newer_version is True
    assert settings.cpu_percent == CPU_PERCENT_DEFAULT
    assert settings.extra == {"future_thing": 1}
    assert [p.code for p in problems] == [Code.FILE_UNSUPPORTED]


def test_a_file_from_a_newer_version_is_never_written_over() -> None:
    """N-15: an older app does not destructively modify a newer format."""
    document = json.dumps({"version": SETTINGS_SCHEMA_VERSION + 6, "cpu_percent": 55})
    _write(document)
    store = SettingsStore()

    with pytest.raises(EchoActError) as caught:
        store.update(cpu_percent=33)

    assert caught.value.code is Code.FILE_UNSUPPORTED
    assert paths.settings_path().read_text(encoding="utf-8") == document


def test_this_versions_marker_is_written_and_read_back() -> None:
    save_settings(Settings(cpu_percent=33))
    written = json.loads(paths.settings_path().read_text(encoding="utf-8"))
    assert written["version"] == SETTINGS_SCHEMA_VERSION

    settings, problems = load_settings()

    assert (settings.schema_version, settings.cpu_percent) == (SETTINGS_SCHEMA_VERSION, 33)
    assert problems == ()


def test_an_unreadable_version_marker_does_not_lock_the_user_out_of_their_settings() -> None:
    _write(json.dumps({"version": "seven", "cpu_percent": 33}))

    settings, problems = load_settings()

    assert settings.cpu_percent == 33
    assert settings.from_a_newer_version is False
    assert [p.code for p in problems] == [Code.FILE_CORRUPT]


# ---------------------------- one home for the owner's two decisions, N-11 ---


def test_the_model_registry_can_be_handed_the_settings_file_as_its_preferences() -> None:
    """F-09 / N-11: ``ModelPreferences`` is the shape the registry asks for,
    and N-11 and 5.3 both put these records in settings."""
    from echoact.models.registry import ModelPreferences

    prefs = SettingsModelPreferences(SettingsStore())

    for name in (
        "license_accepted",
        "record_license_acceptance",
        "download_authorised",
        "set_download_authorised",
    ):
        wanted = inspect.signature(getattr(ModelPreferences, name))
        wanted = wanted.replace(parameters=list(wanted.parameters.values())[1:])
        assert inspect.signature(getattr(prefs, name)) == wanted, name


def test_a_licence_accepted_through_the_registry_is_the_one_settings_reports() -> None:
    """N-11's gate must be answered by one record, not two that can disagree."""
    from echoact.models.registry import ModelRegistry

    store = SettingsStore()
    registry = ModelRegistry(preferences=SettingsModelPreferences(store))
    assert registry.license_acceptance_required(DEFAULT_MODEL_ID) is True

    registry.accept_license(DEFAULT_MODEL_ID)

    assert registry.license_acceptance_required(DEFAULT_MODEL_ID) is False
    assert list(store.current.accepted_licences) == [DEFAULT_MODEL_ID]
    assert not (store.path.parent / "model_state.json").exists()


def test_a_download_authorisation_survives_a_restart_of_the_store() -> None:
    """5.3: the pre-authorisation an integration is checked against is the one
    the owner set, read back from the settings file."""
    prefs = SettingsModelPreferences(SettingsStore())
    assert prefs.download_authorised(DEFAULT_MODEL_ID) is False

    prefs.set_download_authorised(DEFAULT_MODEL_ID, True)
    assert SettingsModelPreferences(SettingsStore()).download_authorised(DEFAULT_MODEL_ID) is True

    prefs.set_download_authorised(DEFAULT_MODEL_ID, False)
    assert SettingsModelPreferences(SettingsStore()).download_authorised(DEFAULT_MODEL_ID) is False


# ------------------------------------------ the remembered speaker, F-67 ---


def test_the_remembered_output_device_is_the_key_the_audio_module_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-67: a speaker that is present must not read back as one that is gone.

    ``devices.resolve`` matches on ``OutputDevice.key`` and nothing else, so
    settings has to hold that spelling; a bare device name matches nothing,
    and ``resolve`` defines nothing as "the remembered device is gone".
    """
    from echoact.audio import devices

    speaker = devices.OutputDevice(
        index=3,
        name="Speakers (Realtek High Definition Audio)",
        host_api="MME",
        max_channels=2,
        default_samplerate=44100.0,
        is_default=True,
    )
    monkeypatch.setattr(devices, "list_output_devices", lambda: [speaker])
    assert output_device_key(speaker.host_api, speaker.name) == speaker.key

    save_settings(Settings(output_device=output_device_key(speaker.host_api, speaker.name)))
    restored, problems = load_settings()

    assert problems == ()
    assert devices.resolve(restored.output_device) == speaker
    assert devices.resolve(speaker.name) is None, "a bare name is not what this field holds"
