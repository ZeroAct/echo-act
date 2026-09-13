"""The settings screen's contract.

The screen is where four requirements become visible or do not exist at all:
F-78's distinction between the configured limit and the one in force, F-67's
refusal to move to a different speaker on its own, F-71's once-only
credential, and F-76's four separate deletions with their confirmations.
Each of those is asserted here on the widget that ships, because none of them
can be checked in the model layer -- they are promises about what a person
sees and is asked.

Nothing here shows a window: ``grab()`` forces the same layout pass without
needing a desktop, which is also what keeps the geometry assertions honest
on a machine with no font database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QLayout,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSlider,
    QTreeWidget,
    QWidget,
)

import echoact.paths
from echoact.audio.devices import OutputDevice
from echoact.config.settings import (
    DEFAULT_MODEL_ID,
    DisplayLanguage,
    Settings,
    output_device_key,
)
from echoact.db.store import Store
from echoact.domain import Budget, Capability, Gender, Language, SpeakingStyle, VoiceSettings
from echoact.policy import (
    CPU_PERCENT_MAX,
    CPU_PERCENT_MIN,
    CREDENTIAL_DAYS_MAX,
    CREDENTIAL_DAYS_MIN,
    GB,
    GIB,
    RETENTION_MAX_BYTES,
    RETENTION_MIN_BYTES,
    VOICE_PRESET_MAX,
)
from echoact.security.credentials import CredentialStore
from echoact.ui import i18n, theme
from echoact.ui.settings_view import (
    CHANGE_HISTORY,
    Machine,
    ResetScope,
    SettingsView,
    StorageSizes,
    directory_bytes,
    storage_snapshot,
)

SPEAKERS = OutputDevice(
    index=3,
    name="Studio Monitors",
    host_api="WASAPI",
    max_channels=2,
    default_samplerate=48000.0,
    is_default=False,
)
HEADSET = OutputDevice(
    index=7,
    name="USB Headset",
    host_api="WASAPI",
    max_channels=2,
    default_samplerate=44100.0,
    is_default=True,
)


@pytest.fixture(scope="session")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path) -> Iterator[Path]:
    """Never the real user data directory: this screen measures and deletes."""
    was = os.environ.get("ECHOACT_DATA_DIR")
    os.environ["ECHOACT_DATA_DIR"] = str(tmp_path)
    echoact.paths.data_dir.cache_clear()
    try:
        yield tmp_path
    finally:
        if was is None:
            os.environ.pop("ECHOACT_DATA_DIR", None)
        else:
            os.environ["ECHOACT_DATA_DIR"] = was
        echoact.paths.data_dir.cache_clear()


@pytest.fixture(autouse=True)
def english() -> Iterator[None]:
    """Every assertion below reads English text, and the language is global."""
    was = i18n.current()
    i18n.set_language(i18n.Lang.EN)
    yield
    i18n.set_language(was)


class Answers:
    """A stand-in for the two dialogs, recording what the user was asked."""

    def __init__(self, *, confirm: bool = True, text: str | None = "typed") -> None:
        self.confirm_answer = confirm
        self.text_answer = text
        self.confirmations: list[tuple[str, str, str]] = []
        self.prompts: list[tuple[str, str, str]] = []

    def confirm(self, title: str, body: str, accept: str) -> bool:
        self.confirmations.append((title, body, accept))
        return self.confirm_answer

    def ask(self, title: str, prompt: str, value: str) -> str | None:
        self.prompts.append((title, prompt, value))
        return self.text_answer

    @property
    def last_body(self) -> str:
        return self.confirmations[-1][1]


def make_view(
    app: QApplication,
    settings: Settings | None = None,
    *,
    machine: Machine | None = None,
    devices: list[OutputDevice] | None = None,
    answers: Answers | None = None,
) -> tuple[SettingsView, Answers]:
    replies = answers or Answers()
    view = SettingsView(
        theme.LIGHT,
        settings or Settings(),
        machine=machine or Machine(total_ram_bytes=16 * GIB, logical_cpus=8),
        device_lister=lambda: list(devices if devices is not None else [HEADSET, SPEAKERS]),
    )
    view.confirm = replies.confirm
    view.ask_text = replies.ask
    view.resize(900, 700)
    view.ensurePolished()
    view.grab()
    return view, replies


@pytest.fixture()
def view(app: QApplication) -> SettingsView:
    made, _ = make_view(app)
    return made


def capture(signal) -> list:
    seen: list = []
    signal.connect(lambda *args: seen.append(args[0] if len(args) == 1 else args))
    return seen


def visible(widget: QWidget) -> bool:
    """Whether the widget would show if the window were shown.

    Nothing here calls ``show()`` -- the offscreen platform has no font
    database on Windows -- so ``isVisible()`` is false for every widget on
    the page and would make each of these assertions pass for the wrong
    reason.
    """
    return widget.isVisibleTo(widget.window())


def texts(widget: QWidget) -> str:
    """Every label on a panel, joined -- for asserting a statement is present."""
    from PySide6.QtWidgets import QLabel

    return " ".join(lb.text() for lb in widget.findChildren(QLabel))


# ==================================================================
# Resources (F-20, F-21, F-78)
# ==================================================================


def test_processor_share_offers_exactly_f20s_range(view: SettingsView) -> None:
    assert (view.cpu.minimum(), view.cpu.maximum()) == (CPU_PERCENT_MIN, CPU_PERCENT_MAX)
    assert view.cpu.value() == 20


def test_memory_defaults_to_the_amount_f21_recommends_for_this_computer(
    app: QApplication,
) -> None:
    view, _ = make_view(app, machine=Machine(16 * GIB, 8))
    assert view.memory_auto.isChecked()
    assert not view.memory.isEnabled()
    # 25% of 16 GiB is 4 GiB, inside F-21's 2-6 GiB band.
    assert view.memory.value() == pytest.approx(4.0)
    assert "4.0 GiB" in view.memory_auto.text()


def test_memory_recommendation_is_held_inside_f21s_two_to_six_gib_band(
    app: QApplication,
) -> None:
    small, _ = make_view(app, machine=Machine(4 * GIB, 4))
    big, _ = make_view(app, machine=Machine(128 * GIB, 32))
    assert small.memory.value() == pytest.approx(2.0)  # 25% would be 1 GiB
    assert big.memory.value() == pytest.approx(6.0)  # 25% would be 32 GiB


def test_the_configurable_ceiling_is_the_smaller_of_32_gib_and_half_the_ram(
    app: QApplication,
) -> None:
    laptop, _ = make_view(app, machine=Machine(8 * GIB, 8))
    workstation, _ = make_view(app, machine=Machine(128 * GIB, 32))
    assert laptop.memory.maximum() == pytest.approx(4.0)
    assert workstation.memory.maximum() == pytest.approx(32.0)


def test_a_computer_below_the_two_gib_floor_is_told_so_rather_than_offered_nothing(
    app: QApplication,
) -> None:
    tiny, _ = make_view(app, machine=Machine(3 * GIB, 2))
    assert "less memory than EchoAct needs" in texts(tiny.content)
    assert tiny.memory.maximum() == pytest.approx(1.5)


def test_changing_the_processor_share_emits_the_new_value(view: SettingsView) -> None:
    seen = capture(view.cpu_changed)
    view.cpu.setValue(45)
    assert seen == [45]


def test_choosing_a_custom_memory_limit_emits_bytes_and_the_default_emits_none(
    view: SettingsView,
) -> None:
    seen = capture(view.memory_changed)
    view.memory_auto.setChecked(False)
    view.memory.setValue(5.0)
    view.memory_auto.setChecked(True)
    assert seen[0] == 4 * GIB  # unchecking hands back the value on screen
    assert seen[1] == 5 * GIB
    assert seen[-1] is None


def test_showing_remembered_settings_emits_nothing(app: QApplication) -> None:
    view, _ = make_view(app)
    seen = capture(view.cpu_changed) + capture(view.memory_changed)
    view.apply(Settings(cpu_percent=55, memory_bytes=3 * GIB))
    assert seen == []
    assert view.cpu.value() == 55
    assert view.memory.value() == pytest.approx(3.0)


def test_the_applied_budget_is_shown_apart_from_the_configured_one(app: QApplication) -> None:
    view, _ = make_view(app, Settings(cpu_percent=60, memory_bytes=6 * GIB))
    view.set_resource_state(
        Budget(cpu_percent=20, memory_bytes=2 * GIB, intra_op_threads=2), job_running=True
    )
    configured = view.resources_configured.text()
    applied = view.resources_applied.text()
    assert "60%" in configured and "6.0 GiB" in configured
    assert "20%" in applied and "2.0 GiB" in applied
    assert configured != applied


def test_with_no_job_running_the_screen_says_the_values_apply_to_the_next_job(
    view: SettingsView,
) -> None:
    view.set_resource_state(None, job_running=False)
    assert "next job" in view.resources_applied.text()
    assert not visible(view.apply_row)


def test_a_change_during_a_job_offers_the_next_job_or_a_confirmed_cancellation(
    app: QApplication,
) -> None:
    answers = Answers(confirm=True)
    view, _ = make_view(app, answers=answers)
    view.set_resource_state(
        Budget(cpu_percent=20, memory_bytes=2 * GIB, intra_op_threads=2), job_running=True
    )
    seen = capture(view.apply_now_requested)

    view.cpu.setValue(50)
    assert visible(view.apply_row)
    assert "next job" in texts(view.apply_row)

    view.apply_cancel.click()
    assert len(seen) == 1
    assert "cancels the job that is running" in answers.last_body
    assert not visible(view.apply_row)


def test_a_change_during_a_job_moves_the_configured_line_not_only_the_offer(
    app: QApplication,
) -> None:
    """F-78's two numbers are only useful if the configured one is current.

    Nothing hands the saved settings back to this screen -- an edit leaves as
    a signal and the window applies it -- so the configured half is the
    controls', and reading it from the settings the screen opened with tells
    the owner their change did not take.
    """
    view, _ = make_view(app, Settings(cpu_percent=20, memory_bytes=4 * GIB))
    view.set_resource_state(
        Budget(cpu_percent=20, memory_bytes=2 * GIB, intra_op_threads=2), job_running=True
    )

    view.cpu.setValue(65)
    view.memory.setValue(6.0)

    configured = view.resources_configured.text()
    applied = view.resources_applied.text()
    assert "65%" in configured and "6.0 GiB" in configured
    assert "20%" in applied and "2.0 GiB" in applied
    # F-78 flags the applied line while the two disagree, which they now do.
    assert view.resources_applied.property("role") == "warn"


def test_declining_the_cancellation_leaves_the_job_alone(app: QApplication) -> None:
    answers = Answers(confirm=False)
    view, _ = make_view(app, answers=answers)
    view.set_resource_state(
        Budget(cpu_percent=20, memory_bytes=2 * GIB, intra_op_threads=2), job_running=True
    )
    seen = capture(view.apply_now_requested)
    view.cpu.setValue(50)
    view.apply_cancel.click()
    assert seen == []
    assert visible(view.apply_row)  # the offer stays; the change still lands next job


def test_choosing_apply_to_the_next_job_asks_nothing_and_cancels_nothing(
    app: QApplication,
) -> None:
    answers = Answers()
    view, _ = make_view(app, answers=answers)
    view.set_resource_state(
        Budget(cpu_percent=20, memory_bytes=2 * GIB, intra_op_threads=2), job_running=True
    )
    seen = capture(view.apply_now_requested)
    view.cpu.setValue(35)
    view.apply_next.click()
    assert seen == []
    assert answers.confirmations == []
    assert not visible(view.apply_row)


# ==================================================================
# Audio (F-67)
# ==================================================================


def test_an_output_device_is_remembered_by_key_and_never_by_index(view: SettingsView) -> None:
    seen = capture(view.output_device_changed)
    view.device.setCurrentIndex(view.device.findData(SPEAKERS.key))
    assert seen == [SPEAKERS.key]
    assert seen[0] == output_device_key(SPEAKERS.host_api, SPEAKERS.name)
    assert str(SPEAKERS.index) not in seen[0]


def test_the_system_default_is_offered_as_a_choice_of_its_own(view: SettingsView) -> None:
    assert view.device.itemData(0) is None
    seen = capture(view.output_device_changed)
    view.device.setCurrentIndex(view.device.findData(SPEAKERS.key))
    view.device.setCurrentIndex(0)
    assert seen[-1] is None


def test_a_remembered_device_that_is_gone_stays_selected_and_is_labelled(
    app: QApplication,
) -> None:
    remembered = output_device_key("WASAPI", "Desk Speakers")
    view, _ = make_view(app, Settings(output_device=remembered), devices=[HEADSET])
    seen = capture(view.output_device_changed)
    assert view.device.currentData() == remembered
    assert "not connected" in view.device.currentText()
    assert seen == []  # F-67: nothing switched speakers on the user's behalf


def test_an_unlistable_audio_system_is_reported_and_the_rest_stays_usable(
    app: QApplication,
) -> None:
    from echoact.errors import Code, EchoActError

    def broken() -> list[OutputDevice]:
        raise EchoActError(Code.OUTPUT_DEVICE_UNAVAILABLE, "No audio system here.")

    view = SettingsView(
        theme.LIGHT, Settings(), machine=Machine(16 * GIB, 8), device_lister=broken
    )
    view.grab()
    assert visible(view.device_note)
    assert "No audio system here." in view.device_note.text()
    assert view.cpu.isEnabled()


def test_volume_and_mute_emit_and_the_screen_says_the_wav_is_unaffected(
    view: SettingsView,
) -> None:
    volumes = capture(view.volume_changed)
    mutes = capture(view.muted_changed)
    view.volume.setValue(40)
    view.muted.setChecked(True)
    assert volumes == [pytest.approx(0.4)]
    assert mutes == [True]
    assert "does not change the saved WAV file" in texts(view.content)


# ==================================================================
# Reading and display (F-86, F-83, F-30, A-22)
# ==================================================================


def test_the_display_language_shown_is_the_remembered_one(app: QApplication) -> None:
    view, _ = make_view(app, Settings(display_language=DisplayLanguage.KO))
    assert view.language.currentData() == "ko"


def test_choosing_a_display_language_emits_only_that_value(view: SettingsView) -> None:
    before = view.settings
    seen = capture(view.display_language_changed)
    view.language.setCurrentIndex(view.language.findData("ko"))
    assert seen == ["ko"]
    # A-22: the screen changes no document, job, or setting of its own.
    assert view.settings is before


def test_the_display_language_leaves_in_the_type_the_setting_holds(
    app: QApplication,
) -> None:
    """F-86: a bare ``"ko"`` is not what ``Settings.display_language`` holds.

    ``Settings.with_`` is ``dataclasses.replace`` and coerces nothing, so a
    plain string lands in the field and every ``.value`` read on it raises --
    ``to_dict`` on the way to disk, and this screen on the way back -- which
    leaves the choice neither saved nor shown.
    """
    view, _ = make_view(app, Settings(display_language=DisplayLanguage.EN))
    seen = capture(view.display_language_changed)
    view.language.setCurrentIndex(view.language.findData("ko"))

    assert seen == [DisplayLanguage.KO]
    assert isinstance(seen[0], DisplayLanguage)

    saved = view.settings.with_(display_language=seen[0])
    assert saved.to_dict()["display_language"] == "ko"  # it can be persisted
    view.apply(saved)  # and shown back without raising
    assert view.language.currentData() == "ko"


def test_switching_the_language_retranslates_the_same_widgets_in_place(
    view: SettingsView,
) -> None:
    heading = view.cpu.accessibleName()
    same_widget = view.cpu
    i18n.set_language(i18n.Lang.KO)
    try:
        assert view.cpu.accessibleName() != heading
        assert view.cpu.accessibleName() == "프로세서 사용률"
        assert "음량" in texts(view.content)
        assert view.cpu is same_widget
        assert view.cpu.value() == 20  # the value survived the switch
    finally:
        i18n.set_language(i18n.Lang.EN)
    assert view.cpu.accessibleName() == heading


def test_autoplay_and_following_emit_their_new_state(view: SettingsView) -> None:
    autoplay = capture(view.autoplay_changed)
    follow = capture(view.follow_changed)
    view.autoplay.setChecked(False)
    view.follow.setChecked(False)
    assert autoplay == [False]
    assert follow == [False]


# ==================================================================
# Integrations (F-46, F-71, F-79, N-31, 4.1)
# ==================================================================


def test_the_bind_address_and_port_are_disclosed_beside_the_switch(view: SettingsView) -> None:
    assert "127.0.0.1:8765" in view.rest_note.text()
    assert "this computer only" in view.rest_note.text()


def test_the_mcp_consequence_is_stated_where_rest_is_turned_off(view: SettingsView) -> None:
    standing = texts(view.content)
    assert "turning it off also makes MCP unavailable" in standing


def test_turning_rest_off_while_mcp_is_on_confirms_the_consequence(app: QApplication) -> None:
    answers = Answers(confirm=True)
    view, _ = make_view(app, Settings(mcp_enabled=True), answers=answers)
    seen = capture(view.rest_enabled_changed)
    view.rest.setChecked(False)
    assert seen == [False]
    assert "MCP is a client of this service" in answers.last_body
    assert not view.mcp.isEnabled()


def test_declining_that_confirmation_leaves_the_service_on(app: QApplication) -> None:
    answers = Answers(confirm=False)
    view, _ = make_view(app, Settings(mcp_enabled=True), answers=answers)
    seen = capture(view.rest_enabled_changed)
    view.rest.setChecked(False)
    assert seen == []
    assert view.rest.isChecked()


def test_mcp_cannot_be_enabled_while_the_rest_service_is_off(app: QApplication) -> None:
    view, _ = make_view(app, Settings(rest_enabled=False))
    assert not view.mcp.isEnabled()
    assert not view.port.isEnabled()


def test_a_port_conflict_offers_another_port_and_binds_none_by_itself(
    view: SettingsView,
) -> None:
    seen = capture(view.rest_port_changed)
    view.show_port_conflict(8765)
    assert visible(view.conflict)
    assert "8765" in view.conflict_text.text()
    assert "does not pick another port on its own" in view.conflict_text.text()
    assert seen == []  # nothing was chosen for the owner

    view.conflict_use.click()
    assert seen == [8766]
    assert not visible(view.conflict)


def test_the_owner_can_restart_the_service_on_a_port_they_typed(view: SettingsView) -> None:
    seen = capture(view.rest_port_changed)
    view.port.setValue(9100)
    assert seen == []  # typing a port restarts nothing
    view.port_restart.click()
    assert seen == [9100]


def test_a_port_that_is_only_typed_is_not_disclosed_as_the_one_in_service(
    view: SettingsView,
) -> None:
    """F-46 / N-31: the disclosure names where the listener is.

    Typing a port binds nothing, so a client configured from the base URL on
    this screen must still reach the service.
    """
    view.port.setValue(9100)
    view.retranslate()  # what a display-language switch does, in one call

    assert "127.0.0.1:8765" in view.rest_note.text()
    assert "http://127.0.0.1:8765/api/v1" in view.rest_how.text()


def test_restarting_moves_the_disclosure_with_the_service(view: SettingsView) -> None:
    seen = capture(view.rest_port_changed)
    view.port.setValue(9100)
    view.port_restart.click()

    assert seen == [9100]
    assert "127.0.0.1:9100" in view.rest_note.text()
    assert "http://127.0.0.1:9100/api/v1" in view.rest_how.text()


def test_a_refused_port_puts_the_disclosure_back_on_the_one_still_serving(
    view: SettingsView,
) -> None:
    view.port.setValue(9100)
    view.port_restart.click()
    view.show_port_conflict(9100)
    view.retranslate()  # nothing later may re-render it back to the typed port

    assert "127.0.0.1:8765" in view.rest_note.text()
    assert "http://127.0.0.1:8765/api/v1" in view.rest_how.text()


def test_credential_lifetime_offers_one_to_365_days(view: SettingsView) -> None:
    assert (view.credential_days.minimum(), view.credential_days.maximum()) == (
        CREDENTIAL_DAYS_MIN,
        CREDENTIAL_DAYS_MAX,
    )
    assert view.credential_days.value() == 90


def _store(tmp: Path) -> CredentialStore:
    return CredentialStore.load(tmp / "credentials.json")


def test_the_client_list_shows_name_capabilities_last_access_and_expiry(
    app: QApplication, tmp_path: Path
) -> None:
    view, _ = make_view(app)
    store = _store(tmp_path)
    issued = store.issue("Editor", {Capability.GENERATE, Capability.READ_RESULTS}, days=90)
    issued.credential.last_access_at = issued.credential.created_at
    view.set_credentials(store.list())

    item = view.clients.topLevelItem(0)
    assert item.text(0) == "Editor"
    assert "Generate" in item.text(1) and "Read results" in item.text(1)
    assert item.text(2) != "never"
    assert "in 89 days" in item.text(3) or "in 90 days" in item.text(3)


def test_a_revoked_client_is_shown_as_revoked_and_cannot_be_reissued(
    app: QApplication, tmp_path: Path
) -> None:
    view, _ = make_view(app)
    store = _store(tmp_path)
    issued = store.issue("Script", {Capability.GENERATE})
    store.revoke(issued.ref)
    view.set_credentials(store.list())
    view.clients.setCurrentItem(view.clients.topLevelItem(0))
    assert view.clients.topLevelItem(0).text(3) == "Revoked"
    assert not view.revoke.isEnabled()
    assert not view.reissue.isEnabled()


def test_a_credential_expiring_within_seven_days_warns(app: QApplication, tmp_path: Path) -> None:
    view, _ = make_view(app)
    store = _store(tmp_path)
    store.issue("Nearly gone", {Capability.GENERATE}, days=3)
    view.set_credentials(store.list())
    assert visible(view.expiry_warning)
    assert "Nearly gone" in view.expiry_warning.text()

    store.revoke(store.list()[0].ref)
    view.set_credentials(store.list())
    assert not visible(view.expiry_warning)


def test_a_far_off_expiry_does_not_warn(app: QApplication, tmp_path: Path) -> None:
    view, _ = make_view(app)
    store = _store(tmp_path)
    store.issue("Fine", {Capability.GENERATE}, days=90)
    view.set_credentials(store.list())
    assert not visible(view.expiry_warning)


def test_issuing_asks_for_a_name_and_emits_it_with_the_chosen_lifetime(
    app: QApplication,
) -> None:
    answers = Answers(text="  My editor  ")
    view, _ = make_view(app, answers=answers)
    seen = capture(view.credential_issue_requested)
    view.credential_days.setValue(30)
    view.issue.click()
    name, capabilities, days = seen[0]
    assert name == "My editor"
    assert Capability.GENERATE in capabilities
    assert Capability.OWNER not in capabilities
    assert days == 30


def test_cancelling_the_name_prompt_issues_nothing(app: QApplication) -> None:
    view, _ = make_view(app, answers=Answers(text=None))
    seen = capture(view.credential_issue_requested)
    view.issue.click()
    assert seen == []


def test_nothing_is_selected_when_the_screen_opens(view: SettingsView) -> None:
    assert not view.reissue.isEnabled()
    assert not view.revoke.isEnabled()
    assert "next credential" in view.permission_heading.text()
    assert view.permission_boxes[Capability.GENERATE].isChecked()
    assert view.permission_boxes[Capability.READ_RESULTS].isChecked()
    assert not view.permission_boxes[Capability.READ_HISTORY].isChecked()


def test_the_connection_method_is_reviewable(view: SettingsView) -> None:
    assert "http://127.0.0.1:8765/api/v1" in view.rest_how.text()
    assert "over stdio" in texts(view.content)


def test_the_three_permissions_are_granted_separately_to_a_new_credential(
    app: QApplication,
) -> None:
    answers = Answers(text="Reader")
    view, _ = make_view(app, answers=answers)
    seen = capture(view.credential_issue_requested)

    view.permission_boxes[Capability.GENERATE].setChecked(False)
    view.permission_boxes[Capability.READ_HISTORY].setChecked(True)
    view.issue.click()

    _, capabilities, _ = seen[0]
    assert capabilities == frozenset({Capability.READ_RESULTS, Capability.READ_HISTORY})


def test_selecting_a_client_shows_its_permissions_and_editing_them_emits_a_change(
    app: QApplication, tmp_path: Path
) -> None:
    view, _ = make_view(app)
    store = _store(tmp_path)
    issued = store.issue("Editor", {Capability.GENERATE})
    view.set_credentials(store.list())
    view.clients.setCurrentItem(view.clients.topLevelItem(0))
    seen = capture(view.credential_capabilities_changed)

    assert view.permission_boxes[Capability.GENERATE].isChecked()
    assert not view.permission_boxes[Capability.READ_RESULTS].isChecked()
    assert seen == []  # showing a client's permissions changes none of them
    assert issued.credential.name in view.permission_heading.text()

    view.permission_boxes[Capability.READ_RESULTS].setChecked(True)
    ref, capabilities = seen[0]
    assert ref == issued.ref
    assert capabilities == frozenset({Capability.GENERATE, Capability.READ_RESULTS})


def test_the_owner_credential_is_not_narrowed_from_this_screen(
    app: QApplication, tmp_path: Path
) -> None:
    view, _ = make_view(app)
    store = _store(tmp_path)
    store.ensure_owner_credential(name="EchoAct (this computer)")
    view.set_credentials(store.list())
    view.clients.setCurrentItem(view.clients.topLevelItem(0))
    assert all(box.isChecked() for box in view.permission_boxes.values())
    assert not any(box.isEnabled() for box in view.permission_boxes.values())


def test_a_new_credential_is_shown_once_copyable_and_says_only_a_verifier_is_kept(
    view: SettingsView,
) -> None:
    token = "eak_editor_" + "a" * 44
    view.show_new_credential(token)
    assert visible(view.secret)
    assert view.secret_value.text() == token
    assert view.secret_value.isReadOnly()
    assert "only a verifier" in texts(view.secret)
    assert "shown once" in texts(view.secret)

    view.secret_done.click()
    assert not visible(view.secret)
    assert view.secret_value.text() == ""


def test_revoking_confirms_the_immediate_loss_of_access_first(
    app: QApplication, tmp_path: Path
) -> None:
    answers = Answers(confirm=False)
    view, _ = make_view(app, answers=answers)
    store = _store(tmp_path)
    issued = store.issue("Script", {Capability.GENERATE})
    view.set_credentials(store.list())
    view.clients.setCurrentItem(view.clients.topLevelItem(0))
    seen = capture(view.credential_revoke_requested)

    view.revoke.click()
    assert seen == []
    assert "loses access immediately" in answers.last_body

    answers.confirm_answer = True
    view.revoke.click()
    assert seen == [issued.ref]


def test_reissuing_confirms_that_the_old_credential_stops_working(
    app: QApplication, tmp_path: Path
) -> None:
    answers = Answers(confirm=True)
    view, _ = make_view(app, answers=answers)
    store = _store(tmp_path)
    issued = store.issue("Script", {Capability.GENERATE})
    view.set_credentials(store.list())
    view.clients.setCurrentItem(view.clients.topLevelItem(0))
    seen = capture(view.credential_reissue_requested)
    view.reissue.click()
    assert seen == [issued.ref]
    assert "stops working at once" in answers.last_body


# ==================================================================
# Storage (F-73, F-76)
# ==================================================================


SIZES = StorageSizes(
    model_cache_bytes=385_000_000,
    documents_bytes=2_500_000,
    audio_bytes=750_000_000,
    temp_bytes=12_000_000,
    log_bytes=3_000_000,
    retained_used_bytes=752_500_000,
    retention_limit_bytes=5 * GB,
)


def test_each_kind_of_stored_data_is_sized_separately(view: SettingsView) -> None:
    view.set_storage(SIZES)
    shown = {key: lb.text() for key, lb in view.size_labels.items()}
    assert shown["model_cache"] == "385 MB"
    assert shown["documents"] == "2.5 MB"
    assert shown["audio"] == "750 MB"
    assert shown["temp"] == "12.0 MB"
    assert shown["logs"] == "3.0 MB"
    assert len(set(shown.values())) == 5
    assert "5.0 GB" in view.retention_used.text()


def test_the_retention_limit_offers_one_to_one_hundred_gb(view: SettingsView) -> None:
    assert view.retention.minimum() * GB == RETENTION_MIN_BYTES
    assert view.retention.maximum() * GB == RETENTION_MAX_BYTES
    seen = capture(view.retention_changed)
    view.retention.setValue(20)
    assert seen == [20 * GB]


def test_cleanup_is_offered_and_says_what_it_leaves_alone(view: SettingsView) -> None:
    seen = capture(view.cleanup_requested)
    view.cleanup.click()
    assert len(seen) == 1
    assert "left alone" in texts(view.content)


def test_the_cleanup_report_counts_in_english_that_reads(view: SettingsView) -> None:
    view.report_cleanup(4, 12_000_000)
    assert "Removed 4 items, freeing 12.0 MB." == view.cleanup_result.text()
    view.report_cleanup(1, 2000)
    assert "Removed 1 item, freeing 2.0 kB." == view.cleanup_result.text()
    view.report_cleanup(0, 0)
    assert "Nothing had expired." == view.cleanup_result.text()


def test_the_four_deletions_are_separate_buttons(view: SettingsView) -> None:
    assert set(view.reset_buttons) == set(ResetScope)
    assert len({b for b in view.reset_buttons.values()}) == 4


@pytest.mark.parametrize("scope", list(ResetScope))
def test_each_deletion_confirms_its_own_scope_before_anything_happens(
    app: QApplication, scope: ResetScope
) -> None:
    answers = Answers(confirm=False)
    view, _ = make_view(app, answers=answers)
    view.set_storage(SIZES)
    seen = capture(view.reset_requested)

    view.reset_buttons[scope].click()
    assert seen == []
    assert len(answers.confirmations) == 1

    answers.confirm_answer = True
    view.reset_buttons[scope].click()
    assert seen == [scope.value]


def test_deleting_retained_data_states_what_cannot_be_recovered_and_what_is_left(
    app: QApplication,
) -> None:
    answers = Answers(confirm=True)
    view, _ = make_view(app, answers=answers)
    view.reset_buttons[ResetScope.RETAINED_DATA].click()
    body = answers.last_body
    assert "cannot be recovered" in body
    assert "exported yourself" in body and "backups are not touched" in body


def test_deleting_the_model_cache_states_its_size_and_that_it_must_be_downloaded_again(
    app: QApplication,
) -> None:
    answers = Answers(confirm=True)
    view, _ = make_view(app, answers=answers)
    view.set_storage(SIZES)
    view.reset_buttons[ResetScope.MODEL_CACHE].click()
    body = answers.last_body
    assert "385 MB" in body
    assert "downloaded again" in body


def test_resetting_voice_and_display_settings_promises_the_data_is_untouched(
    app: QApplication,
) -> None:
    answers = Answers(confirm=True)
    view, _ = make_view(app, answers=answers)
    view.reset_buttons[ResetScope.VOICE_AND_DISPLAY].click()
    assert "Documents, history, and audio are untouched" in answers.last_body


def test_items_that_could_not_be_deleted_are_named(view: SettingsView) -> None:
    assert not visible(view.failures)
    view.report_failures(["audio/2026/job-7.wav", "models/supertonic3"])
    assert visible(view.failures)
    assert "job-7.wav" in view.failures.text()


def test_the_storage_snapshot_measures_each_directory_apart(data_dir: Path) -> None:
    echoact.paths.ensure_tree()
    (echoact.paths.temp_dir() / "chunk.wav").write_bytes(b"\0" * 5000)
    (echoact.paths.log_dir() / "echoact.log").write_bytes(b"\0" * 700)
    nested = echoact.paths.model_cache_dir() / "supertonic3"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "model.onnx").write_bytes(b"\0" * 90_000)

    store = Store(data_dir / "echoact.sqlite3", audio_root=data_dir / "audio")
    try:
        sizes = storage_snapshot(store)
    finally:
        store.close()

    assert sizes.temp_bytes == 5000
    assert sizes.log_bytes == 700
    assert sizes.model_cache_bytes == 90_000
    assert sizes.audio_bytes == 0
    assert sizes.retention_limit_bytes == 5 * GB


def test_measuring_a_directory_ignores_what_it_cannot_read(tmp_path: Path) -> None:
    assert directory_bytes(tmp_path / "not-there") == 0
    (tmp_path / "a").write_bytes(b"1234")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"12345")
    assert directory_bytes(tmp_path) == 9


# ==================================================================
# Voice presets (F-66, 4.1)
# ==================================================================


def _voice(**kw: object) -> VoiceSettings:
    return VoiceSettings(
        **{
            "model_id": DEFAULT_MODEL_ID,
            "language": Language.AUTO,
            "gender": Gender.FEMALE,
            "voice_id": "F1",
            "style": SpeakingStyle.NATURAL,
            "tempo": 1.0,
            **kw,
        }
    )


def _preset_settings(n: int) -> Settings:
    voice = VoiceSettings(
        model_id=DEFAULT_MODEL_ID,
        language=Language.AUTO,
        gender=Gender.FEMALE,
        voice_id="F1",
        style=SpeakingStyle.NATURAL,
        tempo=1.0,
    )
    settings = Settings()
    for i in range(n):
        settings = settings.save_preset(f"Preset {i}", voice)
    return settings


def test_presets_are_listed_with_their_count_against_the_limit(app: QApplication) -> None:
    view, _ = make_view(app, _preset_settings(3))
    assert view.presets.count() == 3
    assert f"3 of {VOICE_PRESET_MAX}" in view.preset_count.text()
    assert not visible(view.preset_limit)


def test_saving_is_refused_once_one_hundred_presets_exist(app: QApplication) -> None:
    view, _ = make_view(app, _preset_settings(VOICE_PRESET_MAX))
    seen = capture(view.preset_save_requested)
    assert visible(view.preset_limit)
    assert not view.preset_save.isEnabled()
    view.preset_name.setText("One more")
    view._on_preset_save()
    assert seen == []


def test_saving_over_an_existing_name_is_confirmed_first(app: QApplication) -> None:
    answers = Answers(confirm=False)
    view, _ = make_view(app, _preset_settings(2), answers=answers)
    seen = capture(view.preset_save_requested)

    view.preset_name.setText("Preset 1")
    view.preset_save.click()
    assert seen == []
    assert "already exists" in answers.last_body

    answers.confirm_answer = True
    view.preset_save.click()
    assert seen == ["Preset 1"]
    assert view.preset_name.text() == ""


def test_a_new_preset_name_is_saved_without_a_question(app: QApplication) -> None:
    answers = Answers()
    view, _ = make_view(app, _preset_settings(1), answers=answers)
    seen = capture(view.preset_save_requested)
    view.preset_name.setText("Bedtime")
    view.preset_save.click()
    assert seen == ["Bedtime"]
    assert answers.confirmations == []


def test_applying_renaming_and_deleting_act_on_the_selected_preset(
    app: QApplication,
) -> None:
    answers = Answers(confirm=True, text="Renamed")
    view, _ = make_view(app, _preset_settings(2), answers=answers)
    applied = capture(view.preset_apply_requested)
    renamed = capture(view.preset_rename_requested)
    deleted = capture(view.preset_delete_requested)

    view.presets.setCurrentRow(1)
    view.preset_apply.click()
    view.preset_rename.click()
    view.preset_delete.click()

    assert applied == ["Preset 1"]
    assert renamed == [("Preset 1", "Renamed")]
    assert deleted == ["Preset 1"]


def _named_presets() -> Settings:
    return Settings().save_preset("Morning", _voice(tempo=1.2)).save_preset(
        "Bedtime", _voice(tempo=0.8)
    )


def test_renaming_onto_an_existing_name_confirms_the_overwrite_first(
    app: QApplication,
) -> None:
    """4.1: a duplicate name is confirmed, whichever box it is typed into.

    ``Settings.rename_preset`` drops the preset that was already called this,
    so an unconfirmed rename deletes the owner's other preset -- while typing
    the same name into the Save box would have asked.
    """
    answers = Answers(confirm=False, text="Bedtime")
    view, _ = make_view(app, _named_presets(), answers=answers)
    seen = capture(view.preset_rename_requested)

    view.presets.setCurrentRow(0)  # Morning
    view.preset_rename.click()
    assert seen == []
    assert "already exists" in answers.last_body

    answers.confirm_answer = True
    view.preset_rename.click()
    assert seen == [("Morning", "Bedtime")]


def test_renaming_to_a_free_name_asks_nothing(app: QApplication) -> None:
    answers = Answers(text="Evening")
    view, _ = make_view(app, _named_presets(), answers=answers)
    seen = capture(view.preset_rename_requested)

    view.presets.setCurrentRow(0)
    view.preset_rename.click()
    assert seen == [("Morning", "Evening")]
    assert answers.confirmations == []


def test_a_preset_naming_a_model_or_voice_that_is_gone_is_flagged_not_substituted(
    app: QApplication,
) -> None:
    settings = (
        Settings()
        .save_preset("Fine", _voice())
        .save_preset("Old model", _voice(model_id="qwen3-tts"))
        .save_preset("Old voice", _voice(voice_id="F9"))
    )
    view, _ = make_view(app, settings)
    rows = [view.presets.item(i).text() for i in range(view.presets.count())]
    assert rows[0] == "Fine"
    assert "this model is not available" in rows[1]
    assert "this voice is not available" in rows[2]

    applied = capture(view.preset_apply_requested)
    view.presets.setCurrentRow(1)
    assert not view.preset_apply.isEnabled()
    assert view.preset_delete.isEnabled()  # deleting it is the likely fix
    view.presets.setCurrentRow(0)
    assert view.preset_apply.isEnabled()
    view.preset_apply.click()
    assert applied == ["Fine"]


def test_preset_actions_are_disabled_with_nothing_selected(app: QApplication) -> None:
    view, _ = make_view(app, _preset_settings(0))
    assert not view.preset_apply.isEnabled()
    assert not view.preset_delete.isEnabled()
    assert visible(view.presets_empty)


# ==================================================================
# About (F-75)
# ==================================================================


def test_the_installed_version_and_change_history_are_shown(view: SettingsView) -> None:
    from echoact import __version__

    shown = texts(view.content)
    assert __version__ in shown
    for version, released, lines in CHANGE_HISTORY:
        assert version in shown and released in shown
        for line in lines:
            assert line in shown


def test_the_released_version_is_only_queried_when_the_user_asks(view: SettingsView) -> None:
    seen = capture(view.version_check_requested)
    assert seen == []  # nothing at construction
    assert not visible(view.release_note)
    assert "Nothing is downloaded, installed, or restarted" in texts(view.content)

    view.check_version.click()
    assert len(seen) == 1
    view.set_released_version("0.2.0")
    assert "0.2.0 has been released" in view.release_note.text()
    view.set_released_version(None, error="No network.")
    assert "No network." in view.release_note.text()


# ==================================================================
# Accessibility and layout (N-30)
# ==================================================================

INTERACTIVE = (QPushButton, QCheckBox, QComboBox, QAbstractSpinBox, QSlider, QLineEdit,
               QListWidget, QTreeWidget)


def _interactive(view: SettingsView) -> list[QWidget]:
    found: list[QWidget] = []
    for kind in INTERACTIVE:
        found.extend(w for w in view.content.findChildren(kind))
    # A spin box owns an internal line edit that is not a control of its own.
    return [w for w in found if not isinstance(w.parent(), QAbstractSpinBox)]


def test_every_control_has_an_accessible_name(view: SettingsView) -> None:
    view.set_storage(SIZES)
    nameless = [
        f"{type(w).__name__}:{w.property('text') or w.objectName()}"
        for w in _interactive(view)
        if not w.accessibleName()
    ]
    assert nameless == []


def test_every_control_can_be_reached_from_the_keyboard(view: SettingsView) -> None:
    from PySide6.QtCore import Qt

    unreachable = [
        type(w).__name__
        for w in _interactive(view)
        if w.focusPolicy() == Qt.FocusPolicy.NoFocus
    ]
    assert unreachable == []


def _layout_rects(layout: QLayout) -> list[QRect]:
    rects = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget()
        if widget is not None and not widget.isVisibleTo(widget.window()):
            continue
        if item.isEmpty():
            continue
        rects.append(item.geometry())
    return rects


def _overlaps(view: SettingsView) -> list[tuple[QRect, QRect]]:
    bad = []
    for layout in view.content.findChildren(QLayout):
        rects = _layout_rects(layout)
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                if a.intersects(b) and not a.isEmpty() and not b.isEmpty():
                    bad.append((a, b))
    return bad


@pytest.mark.parametrize(
    ("width", "height", "scaling"), [(1280, 720, "100%"), (640, 360, "200%")]
)
def test_nothing_overlaps_or_is_clipped_at_1280x720(
    app: QApplication, width: int, height: int, scaling: str
) -> None:
    """N-30 at both ends of the supported scaling range.

    OS scaling is emulated by halving the logical size rather than by setting
    a device pixel ratio: at 200% the widgets keep their point sizes and the
    window has half as many logical pixels to place them in, which is exactly
    the condition that clips a layout.
    """
    view, _ = make_view(app)
    view.set_storage(SIZES)
    view.resize(width, height)
    view.ensurePolished()
    view.grab()

    assert view.horizontalScrollBar().maximum() == 0, f"content overflows sideways at {scaling}"
    assert view.content.minimumSizeHint().width() <= view.viewport().width()
    assert _overlaps(view) == []
    for panel in view.content.findChildren(QWidget, "Panel"):
        layout = panel.layout()
        if layout is None:
            continue
        assert panel.height() >= layout.minimumSize().height(), "a panel is squeezed"


def test_nothing_is_clipped_in_korean_either(app: QApplication) -> None:
    """N-30 holds in both display languages, and Korean sets its own widths."""
    view, _ = make_view(app)
    view.set_storage(SIZES)
    i18n.set_language(i18n.Lang.KO)
    try:
        view.resize(640, 360)
        view.ensurePolished()
        view.grab()
        assert view.horizontalScrollBar().maximum() == 0
        assert view.content.minimumSizeHint().width() <= view.viewport().width()
        assert _overlaps(view) == []
    finally:
        i18n.set_language(i18n.Lang.EN)


def test_the_screen_scrolls_rather_than_compressing(app: QApplication) -> None:
    view, _ = make_view(app)
    view.set_storage(SIZES)
    view.resize(1280, 720)
    view.grab()
    assert view.verticalScrollBar().maximum() > 0
    assert view.content.height() >= view.content.minimumSizeHint().height()
    assert view.widgetResizable()


def test_every_remembered_setting_is_shown_where_it_belongs(app: QApplication) -> None:
    """F-24: a relaunch shows what was saved, and shows it without emitting."""
    remembered = Settings(
        cpu_percent=35,
        memory_bytes=5 * GIB,
        volume=0.4,
        muted=True,
        autoplay=False,
        follow=False,
        output_device=HEADSET.key,
        display_language=DisplayLanguage.KO,
        rest_enabled=False,
        rest_port=9000,
        mcp_enabled=False,
        credential_days=30,
        retention_bytes=12 * GB,
    )
    view, _ = make_view(app)
    changes = [
        capture(view.cpu_changed),
        capture(view.memory_changed),
        capture(view.volume_changed),
        capture(view.muted_changed),
        capture(view.autoplay_changed),
        capture(view.follow_changed),
        capture(view.output_device_changed),
        capture(view.display_language_changed),
        capture(view.rest_enabled_changed),
        capture(view.rest_port_changed),
        capture(view.mcp_enabled_changed),
        capture(view.credential_days_changed),
        capture(view.retention_changed),
    ]
    view.apply(remembered)

    assert view.cpu.value() == 35
    assert not view.memory_auto.isChecked()
    assert view.memory.value() == pytest.approx(5.0)
    assert view.volume.value() == 40
    assert view.muted.isChecked()
    assert not view.autoplay.isChecked()
    assert not view.follow.isChecked()
    assert view.device.currentData() == HEADSET.key
    assert view.language.currentData() == "ko"
    assert not view.rest.isChecked()
    assert view.port.value() == 9000
    assert view.credential_days.value() == 30
    assert view.retention.value() == 12
    assert all(seen == [] for seen in changes)


def test_every_string_the_screen_shows_has_a_korean_translation(view: SettingsView) -> None:
    """F-86 offers Korean, so a label added without one would ship English."""
    i18n.set_language(i18n.Lang.KO)
    try:
        missing = sorted(
            {source for _, _, source, _ in view._translatables if i18n.tr(source) == source}
        )
    finally:
        i18n.set_language(i18n.Lang.EN)
    assert missing == []


# ======================================================================
# N-30 -- scrolling the page rather than the values
# ======================================================================


def test_a_wheel_over_a_spin_box_scrolls_the_page_instead_of_changing_the_value(
    app: QApplication,
) -> None:
    """Reading down a long settings page must not edit it on the way.

    The screen is one scroll area, so Qt hands the wheel to whatever value
    control is under the pointer. N-30 asks for the panel to stay reachable
    by scrolling, and a spin box that eats the gesture takes that away twice:
    the page stands still and the processor share has changed.
    """
    view, _ = make_view(app)
    view.cpu.setValue(20)
    bar = view.verticalScrollBar()
    bar.setValue(0)

    QApplication.sendEvent(view.cpu, _wheel(view.cpu))

    assert view.cpu.value() == 20
    assert bar.value() > 0


def test_a_wheel_over_the_output_device_does_not_change_the_speaker(
    app: QApplication,
) -> None:
    """The same guard, on the control where the accident matters most.

    F-67 will not move to a different speaker without the user's say-so, and
    a scroll that lands on this combo box would be exactly that -- decided by
    where the pointer happened to be.
    """
    view, _ = make_view(app)
    before = view.device.currentIndex()
    changes = capture(view.output_device_changed)

    QApplication.sendEvent(view.device, _wheel(view.device))

    assert view.device.currentIndex() == before
    assert changes == []


def _wheel(widget: QWidget) -> QWheelEvent:
    """One notch downwards, over the middle of the widget."""
    centre = QPointF(widget.rect().center())
    return QWheelEvent(
        centre,
        widget.mapToGlobal(centre),
        QPoint(0, -40),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
