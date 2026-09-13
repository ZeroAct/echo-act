"""The settings screen: everything the owner can change, in one scrolling page.

The screen is a stack of panels rather than a tab strip.  N-30 requires the
main features to stay reachable at 1280x720 with 100-200% OS scaling, and a
tab strip solves that by hiding five sixths of the screen; a single column
that scrolls keeps everything reachable at any scale and needs no second
navigation model for the keyboard to walk.

It is a *view*.  It holds no ``Application``, writes no file, and starts no
job: every edit leaves as a signal carrying the new value, and the window
turns that into ``Application.update_settings`` or into the one call that
belongs to it (issuing a credential, restarting the service, deleting a
scope).  That split is what lets F-78 be honest -- the screen can show a
value the running job is *not* using, because it never applied it itself --
and it is what keeps the destructive paths in one place where the
confirmation and the doing sit next to each other.

Two seams exist for testing and for honesty about modality:
:attr:`SettingsView.confirm` and :attr:`SettingsView.ask_text`.  Both default
to Qt dialogs; a caller may replace them.  Nothing else in this module opens
a dialog, so every confirmation F-76 and F-66 require passes through one
function.
"""

from __future__ import annotations

import os
import time
import weakref
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import psutil
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..audio import devices as audio_devices
from ..audio.devices import OutputDevice
from ..config.budget import default_memory_bytes, memory_ceiling_bytes
from ..config.settings import (
    DisplayLanguage,
    ResourcePolicyView,
    Settings,
    VoicePreset,
)
from ..domain import Budget, Capability
from ..errors import EchoActError
from ..models.catalog import MANIFEST
from ..paths import log_dir, model_cache_dir, temp_dir
from ..policy import (
    API_PREFIX,
    CPU_PERCENT_DEFAULT,
    CPU_PERCENT_MAX,
    CPU_PERCENT_MIN,
    CREDENTIAL_DAYS_MAX,
    CREDENTIAL_DAYS_MIN,
    GB,
    GIB,
    MEMORY_FLOOR_BYTES,
    REST_HOST,
    RETENTION_DEFAULT_BYTES,
    RETENTION_MAX_BYTES,
    RETENTION_MIN_BYTES,
    VOICE_PRESET_MAX,
)
from ..security.credentials import Credential, CredentialStatus
from . import brand, icons
from .controls import guard_wheel
from .i18n import add_korean, bytes_size, memory_size, on_change, tr
from .theme import METRICS, Palette

if TYPE_CHECKING:  # pragma: no cover - imports for types only
    from ..db.store import Store
    from ..models.registry import ModelRegistry

#: The lowest port a standard user account can bind, not a product policy.
#: N-17 pins the address to loopback, so only the port is the owner's to pick.
MIN_USER_PORT: Final = 1024
MAX_PORT: Final = 65535

#: The author credit shown at the top of the screen.  It is the same in
#: every language, so it is deliberately absent from the translation table.
AUTHOR: Final = "Huijae Lee (ZeroAct)"
AUTHOR_GITHUB_URL: Final = "https://github.com/ZeroAct"
AUTHOR_GITHUB_LABEL: Final = "github.com/ZeroAct"

#: F-75's change history.  There is no changelog file in the repository yet,
#: so the history lives beside the screen that shows it rather than being
#: invented at display time; a release that adds one should read it instead.
CHANGE_HISTORY: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    (
        "0.1.0",
        "2026-09-11",
        (
            "Korean and English speech generation that runs entirely on this computer.",
            "Local REST and MCP integrations, reachable from this computer only.",
            "Documents, history, and audio kept under a retention limit you set.",
        ),
    ),
)


add_korean(
    {
        # -- resources -----------------------------------------------------
        "Resources": "자원",
        "How much of this computer EchoAct may use while it generates speech.":
            "음성을 생성하는 동안 에코액트가 사용할 수 있는 이 컴퓨터의 자원입니다.",
        "Processor share": "프로세서 사용률",
        "10-70% of this computer's total processor capacity. The default is 20%.":
            "이 컴퓨터 전체 프로세서 용량의 10~70%입니다. 기본값은 20%입니다.",
        "Memory limit": "메모리 한도",
        "Use the amount recommended for this computer ({size})":
            "이 컴퓨터에 권장되는 값 사용 ({size})",
        "At most {ceiling} on this computer: 32 GiB, or half of installed memory, "
        "whichever is smaller.":
            "이 컴퓨터에서는 최대 {ceiling}입니다. 32GiB와 설치된 메모리의 절반 중 작은 값입니다.",
        "This computer has less memory than EchoAct needs to load a model ({floor}).":
            "이 컴퓨터의 메모리가 모델을 불러오는 데 필요한 값({floor})보다 적습니다.",
        "Configured: {cpu}% processor, {memory} memory":
            "설정값: 프로세서 {cpu}%, 메모리 {memory}",
        "Applied to the job running now: {cpu}% processor, {memory} memory":
            "지금 실행 중인 작업에 적용된 값: 프로세서 {cpu}%, 메모리 {memory}",
        "No job is running. These values apply to the next job.":
            "실행 중인 작업이 없습니다. 이 값은 다음 작업에 적용됩니다.",
        "The applied memory can be lower than the configured value when the computer "
        "has little free memory.":
            "컴퓨터의 여유 메모리가 적으면 적용되는 메모리가 설정값보다 낮을 수 있습니다.",
        "This change applies to the next job.": "이 변경은 다음 작업부터 적용됩니다.",
        "Apply to the next job": "다음 작업에 적용",
        "Cancel the job and apply now": "작업을 취소하고 지금 적용",
        "Cancel the running job?": "실행 중인 작업을 취소할까요?",
        "Applying the new limits now cancels the job that is running. Audio already "
        "generated stays available; the rest is not generated.":
            "새 한도를 지금 적용하면 실행 중인 작업이 취소됩니다. 이미 생성된 오디오는 그대로 "
            "남고 나머지는 생성되지 않습니다.",
        # -- audio ---------------------------------------------------------
        "Audio": "오디오",
        "Output device": "출력 장치",
        "System default output": "시스템 기본 출력",
        "{name} (not connected)": "{name} (연결되지 않음)",
        "The audio system could not be listed: {reason}":
            "오디오 장치 목록을 읽지 못했습니다: {reason}",
        "Look for devices again": "장치 다시 찾기",
        "Volume": "음량",
        "Mute": "음소거",
        "Volume changes what you hear now. It does not change the saved WAV file.":
            "음량은 지금 들리는 소리만 바꿉니다. 저장된 WAV 파일은 바뀌지 않습니다.",
        # -- reading and display -------------------------------------------
        "Reading and display": "읽기와 표시",
        "Display language": "표시 언어",
        "Changing the display language does not change your documents, saved jobs, "
        "or what the local service answers.":
            "표시 언어를 바꿔도 문서, 저장된 작업, 로컬 서비스의 응답은 바뀌지 않습니다.",
        # -- integrations ---------------------------------------------------
        "Integrations": "연동",
        "Local REST service": "로컬 REST 서비스",
        "Listening on {host}:{port} — this computer only. Every request needs a "
        "credential.":
            "{host}:{port}에서 대기 중이며 이 컴퓨터에서만 접근할 수 있습니다. 모든 요청에는 "
            "자격 증명이 필요합니다.",
        "MCP works through this service, so turning it off also makes MCP unavailable.":
            "MCP는 이 서비스를 통해 동작하므로, 이 서비스를 끄면 MCP도 사용할 수 없습니다.",
        "Turn the local service off?": "로컬 서비스를 끌까요?",
        "MCP is a client of this service, so turning it off makes MCP unavailable "
        "too. Generation, playback, and the library keep working.":
            "MCP는 이 서비스의 클라이언트이므로 서비스를 끄면 MCP도 사용할 수 없습니다. 생성, "
            "재생, 라이브러리는 그대로 사용할 수 있습니다.",
        "Turn off": "끄기",
        "Port": "포트",
        "Restart the service on this port": "이 포트로 서비스 다시 시작",
        "Port {port} is in use by another program. EchoAct does not close it and "
        "does not pick another port on its own.":
            "포트 {port}는 다른 프로그램이 사용 중입니다. 에코액트는 그 프로그램을 종료하지 "
            "않으며 다른 포트를 임의로 선택하지도 않습니다.",
        "Use port {port} instead": "대신 포트 {port} 사용",
        "Use a different port": "다른 포트 사용",
        "MCP server": "MCP 서버",
        "Off by default. It exposes nothing the REST service does not already expose.":
            "기본값은 꺼짐입니다. REST 서비스가 제공하지 않는 기능은 제공하지 않습니다.",
        "New credentials expire after": "새 자격 증명 유효 기간",
        "days": "일",
        "Clients connect to {url} with their credential. Nothing else on the network "
        "can reach it.":
            "클라이언트는 자격 증명을 사용해 {url}에 연결합니다. 네트워크의 다른 어떤 것도 "
            "접근할 수 없습니다.",
        "Started by your MCP client over stdio, using its own REST credential.":
            "MCP 클라이언트가 stdio로 실행하며, 자체 REST 자격 증명을 사용합니다.",
        "Permissions for the next credential": "다음에 발급할 자격 증명의 권한",
        "Permissions for {name}": "{name}의 권한",
        "Clients": "클라이언트",
        "Name": "이름",
        "Permissions": "권한",
        "Last access": "마지막 접근",
        "Expires": "만료",
        "never": "없음",
        "Owner (every permission)": "소유자 (모든 권한)",
        "Generate": "생성",
        "Read results": "결과 읽기",
        "Read history": "기록 읽기",
        "Revoked": "해지됨",
        "Expired": "만료됨",
        "{date} (in {days} days)": "{date} ({days}일 남음)",
        "No clients yet.": "아직 클라이언트가 없습니다.",
        "Issue a credential": "자격 증명 발급",
        "Reissue": "재발급",
        "Revoke": "해지",
        "A credential for {name} expires in {days} days. Reissue it before then.":
            "{name}의 자격 증명이 {days}일 후 만료됩니다. 그 전에 재발급하세요.",
        "Name this client": "클라이언트 이름",
        "What is connecting? For example, an editor or a script.":
            "무엇이 연결되나요? 예를 들어 편집기나 스크립트입니다.",
        "Copy this credential now. It is shown once.": "이 자격 증명을 지금 복사하세요. 한 번만 표시됩니다.",
        "EchoAct keeps only a verifier, from which the credential cannot be "
        "recovered. If it is lost, reissue it.":
            "에코액트는 검증값만 보관하며, 여기에서 자격 증명을 복원할 수 없습니다. 잃어버리면 "
            "재발급하세요.",
        "Copied": "복사됨",
        "Revoke this credential?": "이 자격 증명을 해지할까요?",
        "{name} loses access immediately, including to results it already "
        "generated, and its running job is canceled.":
            "{name}은(는) 이미 생성한 결과를 포함해 즉시 접근할 수 없게 되며, 실행 중인 작업은 "
            "취소됩니다.",
        "Reissue this credential?": "이 자격 증명을 재발급할까요?",
        "The current credential for {name} stops working at once. The new one is "
        "shown once, here.":
            "{name}의 현재 자격 증명은 즉시 사용할 수 없게 됩니다. 새 자격 증명은 여기에 한 번만 "
            "표시됩니다.",
        # -- storage ---------------------------------------------------------
        "Storage": "저장 공간",
        "Model cache": "모델 캐시",
        "Documents and history": "문서와 기록",
        "Temporary data": "임시 데이터",
        "Logs": "로그",
        "Retention limit": "보관 한도",
        "1-100 GB for documents, history, and audio together. Lowering it below "
        "what is stored keeps that data and refuses new retention.":
            "문서, 기록, 오디오를 합쳐 1~100GB입니다. 이미 저장된 양보다 낮추면 기존 데이터는 "
            "유지되고 새로운 보관만 거부됩니다.",
        "{used} of {limit} used": "{limit} 중 {used} 사용",
        "Clean up expired data": "만료된 데이터 정리",
        "Data in use by playback, generation, or a backup is left alone.":
            "재생, 생성, 백업이 사용 중인 데이터는 건드리지 않습니다.",
        "Removed {n} items, freeing {size}.": "{n}개 항목을 지워 {size}를 확보했습니다.",
        "Removed 1 item, freeing {size}.": "항목 1개를 지워 {size}를 확보했습니다.",
        "Nothing had expired.": "만료된 항목이 없습니다.",
        "Reset voice and display settings": "음성과 표시 설정 초기화",
        "Voice, tempo, style, display language, and playback options return to "
        "their defaults. Documents, history, and audio are untouched.":
            "음성, 속도, 스타일, 표시 언어, 재생 옵션이 기본값으로 돌아갑니다. 문서, 기록, "
            "오디오는 그대로 유지됩니다.",
        "Revoke integration permissions": "연동 권한 해지",
        "Every client credential stops working at once and their running jobs are "
        "canceled. A new owner credential is issued on the next launch.":
            "모든 클라이언트 자격 증명이 즉시 무효화되고 실행 중인 작업은 취소됩니다. 새 소유자 "
            "자격 증명은 다음 실행 시 발급됩니다.",
        "Delete retained data": "보관된 데이터 삭제",
        "Saved documents, job history, and generated audio are deleted from this "
        "app and cannot be recovered. Files you exported yourself and your own "
        "backups are not touched.":
            "저장된 문서, 작업 기록, 생성된 오디오가 이 앱에서 삭제되며 복구할 수 없습니다. "
            "사용자가 직접 내보낸 파일과 백업은 삭제되지 않습니다.",
        "Delete the model cache": "모델 캐시 삭제",
        "The downloaded model files are deleted, about {size}. Generation needs "
        "them downloaded again before it can run, which needs a network.":
            "내려받은 모델 파일 약 {size}가 삭제됩니다. 다시 생성하려면 네트워크를 통해 다시 "
            "내려받아야 합니다.",
        "Delete": "삭제",
        "Reset": "초기화",
        "Revoke all": "모두 해지",
        "This cannot be undone.": "이 작업은 되돌릴 수 없습니다.",
        "These items could not be deleted: {items}": "다음 항목은 삭제하지 못했습니다: {items}",
        # -- presets -----------------------------------------------------------
        "Voice presets": "음성 사전 설정",
        "{n} of {max} saved": "{max}개 중 {n}개 저장됨",
        "No presets yet.": "아직 사전 설정이 없습니다.",
        "{name} — this model is not available": "{name} — 이 모델을 사용할 수 없습니다",
        "{name} — this voice is not available": "{name} — 이 음성을 사용할 수 없습니다",
        "The limit of {max} presets has been reached.": "사전 설정 {max}개 한도에 도달했습니다.",
        "Apply": "적용",
        "Rename": "이름 변경",
        "Save the current voice settings as": "현재 음성 설정을 다음 이름으로 저장",
        "A name for this preset": "사전 설정 이름",
        "Replace the preset {name}?": "사전 설정 {name}을(를) 바꿀까요?",
        "A preset with this name already exists. Saving replaces its settings.":
            "같은 이름의 사전 설정이 이미 있습니다. 저장하면 설정이 바뀝니다.",
        "A preset with this name already exists. Renaming replaces its settings.":
            "같은 이름의 사전 설정이 이미 있습니다. 이름을 바꾸면 그 설정이 바뀝니다.",
        "Replace": "바꾸기",
        "Delete the preset {name}?": "사전 설정 {name}을(를) 삭제할까요?",
        "New name": "새 이름",
        # -- about ---------------------------------------------------------------
        "About": "정보",
        "Installed version {version}": "설치된 버전 {version}",
        "Check for a released version": "출시된 버전 확인",
        "Nothing is downloaded, installed, or restarted without your consent. "
        "Checking sends only a version query.":
            "사용자의 동의 없이는 아무것도 내려받거나 설치하거나 다시 시작하지 않습니다. 확인 "
            "시에는 버전 조회만 보냅니다.",
        "Version {version} has been released.": "버전 {version}이(가) 출시되었습니다.",
        "This is the newest released version.": "현재 최신 출시 버전입니다.",
        "The released version could not be checked: {reason}":
            "출시된 버전을 확인하지 못했습니다: {reason}",
        "Change history": "변경 내역",
        "Korean and English speech generation that runs entirely on this computer.":
            "이 컴퓨터에서만 동작하는 한국어와 영어 음성 생성.",
        "Local REST and MCP integrations, reachable from this computer only.":
            "이 컴퓨터에서만 접근할 수 있는 로컬 REST 및 MCP 연동.",
        "Documents, history, and audio kept under a retention limit you set.":
            "사용자가 정한 보관 한도 안에서 유지되는 문서, 기록, 오디오.",
        # -- shared ----------------------------------------------------------------
        "Settings": "설정",
        "Everything EchoAct remembers between launches.":
            "에코액트가 실행 사이에 기억하는 모든 설정입니다.",
        "Refresh": "새로 고침",
        "Done": "완료",
    }
)


class ResetScope(StrEnum):
    """F-76's four scopes, offered separately and never as one button."""

    VOICE_AND_DISPLAY = "voice_and_display"
    INTEGRATIONS = "integrations"
    RETAINED_DATA = "retained_data"
    MODEL_CACHE = "model_cache"


@dataclass(frozen=True, slots=True)
class Machine:
    """The two machine facts F-20 and F-21 are expressed against.

    Passed in rather than read at every repaint so that a test can describe
    an 8 GiB laptop and a 128 GiB workstation without owning either.
    """

    total_ram_bytes: int
    logical_cpus: int

    @classmethod
    def detect(cls) -> Machine:
        return cls(
            total_ram_bytes=int(psutil.virtual_memory().total),
            logical_cpus=int(psutil.cpu_count(logical=True) or 1),
        )


@dataclass(frozen=True, slots=True)
class StorageSizes:
    """F-73's five figures, each measured apart from the others.

    They are not summed into a single "app data" number anywhere: F-73 asks
    for them separately because the answers to "why is this large" and "what
    may I delete" differ per line -- the model cache is re-downloadable, logs
    expire on their own, and retained data is the only one the retention
    limit governs.
    """

    model_cache_bytes: int
    documents_bytes: int
    audio_bytes: int
    temp_bytes: int
    log_bytes: int
    retained_used_bytes: int
    retention_limit_bytes: int


def directory_bytes(root: Path) -> int:
    """Bytes under a directory, ignoring what cannot be read.

    A settings screen that raises because a log file vanished mid-walk would
    be worse than one that under-reports by that file's size, so every
    ``OSError`` here is skipped rather than propagated.
    """
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def storage_snapshot(
    store: Store | None = None, registry: ModelRegistry | None = None
) -> StorageSizes:
    """Measure F-73's five figures now.

    Called by the window, not by the view: it walks directories, and the
    view must not decide when the main thread does that.  The database half
    comes from ``Store.storage_usage`` rather than from a second walk of the
    audio tree, because that is the number the retention limit is enforced
    against and two disagreeing figures would be worse than one.
    """
    if store is None:
        documents = audio = used = 0
        limit = RETENTION_DEFAULT_BYTES
    else:
        usage = store.storage_usage()
        documents = usage.document_bytes + usage.job_text_bytes
        audio = usage.audio_bytes
        used = usage.total_bytes
        limit = usage.limit_bytes
    cache = registry.total_disk_usage() if registry is not None else directory_bytes(
        model_cache_dir()
    )
    return StorageSizes(
        model_cache_bytes=cache,
        documents_bytes=documents,
        audio_bytes=audio,
        temp_bytes=directory_bytes(temp_dir()),
        log_bytes=directory_bytes(log_dir()),
        retained_used_bytes=used,
        retention_limit_bytes=limit,
    )


_CAPABILITY_TEXT: Final[dict[Capability, str]] = {
    Capability.OWNER: "Owner (every permission)",
    Capability.GENERATE: "Generate",
    Capability.READ_RESULTS: "Read results",
    Capability.READ_HISTORY: "Read history",
}


def _default_confirm(parent: QWidget, title: str, body: str, accept: str) -> bool:
    """The one modal path in this module.

    The accepting button is never the default: F-76's deletions and F-78's
    cancellation are acts, and a dialog whose Enter key performs them turns
    an act into a reflex.
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(body)
    go = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    keep = box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.exec()
    return box.clickedButton() is go


def _default_ask_text(parent: QWidget, title: str, prompt: str, value: str) -> str | None:
    text, ok = QInputDialog.getText(parent, title, prompt, QLineEdit.EchoMode.Normal, value)
    return text if ok else None


def _date(at: float | None) -> str:
    if at is None:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(at))


def _date_time(at: float | None) -> str:
    if at is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at))


class SettingsView(QScrollArea):
    """F-20, F-21, F-24, F-46, F-66, F-67, F-71, F-73, F-75, F-76, F-78, F-79,
    F-83, F-86, N-30 and Section 4.1's operational defaults, on one page.

    Every signal carries the new value in the type the settings field holds,
    so the window's slot is ``update_settings(field=value)`` and no widget
    state has to be read back out of here.
    """

    # -- resources (F-20, F-21, F-78) -------------------------------------
    cpu_changed = Signal(int)
    memory_changed = Signal(object)  # int bytes, or None for F-21's default
    apply_now_requested = Signal()

    # -- audio (F-67) -------------------------------------------------------
    output_device_changed = Signal(object)  # OutputDevice.key, or None
    devices_refresh_requested = Signal()
    volume_changed = Signal(float)
    muted_changed = Signal(bool)

    # -- reading and display (F-83, F-30, F-86) -----------------------------
    autoplay_changed = Signal(bool)
    follow_changed = Signal(bool)
    #: A ``DisplayLanguage``, not its bare ``str`` value.  ``Settings``
    #: holds the enum and ``Settings.with_`` is ``dataclasses.replace``,
    #: which coerces nothing: a ``"ko"`` stored through it turns the field
    #: into a plain string, and every ``.value`` read on it afterwards --
    #: ``Settings.to_dict`` when saving, and ``_render_languages`` here --
    #: raises.  F-86 would then neither persist nor retranslate.
    display_language_changed = Signal(object)

    # -- integrations (F-46, F-71, F-79) ------------------------------------
    rest_enabled_changed = Signal(bool)
    rest_port_changed = Signal(int)
    mcp_enabled_changed = Signal(bool)
    credential_days_changed = Signal(int)
    credential_issue_requested = Signal(str, object, int)  # name, capabilities, days
    credential_capabilities_changed = Signal(str, object)  # ref, capabilities
    credential_reissue_requested = Signal(str)  # ref
    credential_revoke_requested = Signal(str)  # ref

    # -- storage (F-73, F-76) ------------------------------------------------
    #: Bytes, as ``object``: Section 4.1's 100 GB maximum does not fit the
    #: 32-bit C++ ``int`` a ``Signal(int)`` marshals through, and Qt truncates
    #: rather than refusing -- the owner would set 100 GB and be given 1.4 GB.
    retention_changed = Signal(object)
    cleanup_requested = Signal()
    storage_refresh_requested = Signal()
    reset_requested = Signal(str)  # a ResetScope value

    # -- presets (F-66) --------------------------------------------------------
    preset_apply_requested = Signal(str)
    preset_save_requested = Signal(str)
    preset_rename_requested = Signal(str, str)
    preset_delete_requested = Signal(str)

    # -- about (F-75) ------------------------------------------------------------
    version_check_requested = Signal()

    def __init__(
        self,
        palette: Palette,
        settings: Settings,
        *,
        machine: Machine | None = None,
        device_lister: Callable[[], Sequence[OutputDevice]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._settings = settings
        self._machine = machine or Machine.detect()
        self._device_lister = device_lister or audio_devices.list_output_devices

        self.confirm: Callable[[str, str, str], bool] = (
            lambda title, body, accept: _default_confirm(self, title, body, accept)
        )
        self.ask_text: Callable[[str, str, str], str | None] = (
            lambda title, prompt, value: _default_ask_text(self, title, prompt, value)
        )

        self._loading = False
        self._translatables: list[tuple[QWidget, str, str, dict[str, Any]]] = []
        self._applied: Budget | None = None
        self._job_running = False
        self._pending_resource_change = False
        self._devices: list[OutputDevice] = []
        self._device_error = ""
        self._credentials: list[Credential] = []
        # F-61 authorises the three separately, so the set a new credential
        # gets is a choice made before issuing rather than a constant.  The
        # two an integration needs to be useful are on by default; reading
        # the owner's retained history is not.
        self._pending_capabilities = frozenset(
            {Capability.GENERATE, Capability.READ_RESULTS}
        )
        self._sizes: StorageSizes | None = None
        self._presets: tuple[VoicePreset, ...] = settings.presets
        self._conflict_port: int | None = None
        # The port the service is on, as far as this screen has been told.
        # It is not ``self.port.value()``: that is the port the owner has
        # *typed*, and F-79 binds nothing until they press restart.
        self._service_port = settings.rest_port
        self._service_port_before: int | None = None
        self._unavailable: set[str] = set()

        self.setObjectName("Root")
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        # Horizontal scrolling is left on rather than forced off: if a future
        # panel does outgrow 1280x720 at 200% scaling, N-30 prefers a
        # scrollbar to a clipped control, and the test that would then fail
        # says so in one line.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.content = QWidget()
        self.content.setObjectName("Root")
        column = QVBoxLayout(self.content)
        m = METRICS
        column.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad_wide)
        column.setSpacing(m.gap_wide)

        title = QLabel()
        title.setProperty("role", "title")
        self._text(title, "Settings")
        column.addWidget(title)
        column.addWidget(
            self._label("Everything EchoAct remembers between launches.", "secondary")
        )
        column.addWidget(self._credit_line())
        column.addWidget(self._build_resources())
        column.addWidget(self._build_audio())
        column.addWidget(self._build_display())
        column.addWidget(self._build_integrations())
        column.addWidget(self._build_storage())
        column.addWidget(self._build_presets())
        column.addWidget(self._build_about())
        column.addStretch(1)
        self.setWidget(self.content)

        # Every control with a value the wheel would change, now that they
        # all exist.  This screen is one long scroll, so without it reading
        # down the page edits the settings being read (N-30).
        guard_wheel(
            self.cpu,
            self.memory,
            self.device,
            self.volume,
            self.language,
            self.port,
            self.credential_days,
            self.retention,
        )

        self.apply(settings)
        self.refresh_devices()
        _follow_language(self)

    # ==================================================================
    # Small builders
    # ==================================================================

    def _panel(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setObjectName("Panel")
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        box = QVBoxLayout(panel)
        m = METRICS
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)
        heading = QLabel()
        heading.setProperty("role", "section")
        self._text(heading, title)
        box.addWidget(heading)
        return panel, box

    def _text(self, widget: QWidget, source: str, *, setter: str = "setText", **fmt: Any) -> None:
        """Set a translated string and remember how to set it again.

        F-86 switches the language in place, so every literal the screen
        shows is recorded here with the arguments it was formatted with.
        Re-rendering from the recorded source is what keeps the switch from
        needing the screen to be rebuilt -- rebuilding would lose the
        scroll position and the keyboard focus the user was at.
        """
        self._translatables.append((widget, setter, source, fmt))
        getattr(widget, setter)(tr(source).format(**fmt) if fmt else tr(source))

    def _label(self, source: str, role: str | None = None, **fmt: Any) -> QLabel:
        lb = QLabel()
        if role:
            lb.setProperty("role", role)
        lb.setWordWrap(True)
        self._text(lb, source, **fmt)
        return lb

    def _credit_line(self) -> QLabel:
        """The author credit at the top of the screen.

        Deliberately not recorded through :meth:`_text`: the name and the
        handle are the same in every language, so recording them would only
        give the translator a string they cannot improve.  The link opens in
        the browser rather than anywhere in-app -- EchoAct has no page to
        show it in, and opening externally is what makes the one network
        request it causes the user's own.
        """
        lb = QLabel()
        lb.setProperty("role", "muted")
        lb.setWordWrap(True)
        lb.setTextFormat(Qt.TextFormat.RichText)
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        lb.setOpenExternalLinks(True)
        lb.setText(
            f'{AUTHOR} · <a href="{AUTHOR_GITHUB_URL}" '
            f'style="color:{self._palette.accent};">{AUTHOR_GITHUB_LABEL}</a>'
        )
        lb.setAccessibleName(f"{AUTHOR} — {AUTHOR_GITHUB_URL}")
        return lb

    def _brand_lockup(self) -> QWidget:
        """The mark, the name, and the name in Korean: a product, not a form.

        Set once and not recorded through :meth:`_text` -- the wordmark is
        "EchoAct" in both languages and the hangul line is its reading, so
        there is nothing to translate and everything to be confused by if
        a future language translated them.
        """
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(METRICS.gap_wide)
        tile = QLabel()
        tile.setPixmap(
            brand.tile_pixmap(44, self._palette.accent, self._palette.text_on_accent, dpr=2.0)
        )
        tile.setAccessibleName("EchoAct")
        line.addWidget(tile)
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel("EchoAct")
        name.setProperty("role", "title")
        names.addWidget(name)
        hangul = QLabel("에코액트")
        hangul.setProperty("role", "muted")
        names.addWidget(hangul)
        line.addLayout(names)
        line.addStretch(1)
        return row

    def _value_label(self, role: str | None = "secondary") -> QLabel:
        lb = QLabel()
        if role:
            lb.setProperty("role", role)
        lb.setWordWrap(True)
        return lb

    def _field(
        self, box: QVBoxLayout, name: str, widget: QWidget, note: str | None = None
    ) -> None:
        """A caption, a control, and an optional explanation, in that order.

        The caption is a separate label rather than a placeholder or a
        tooltip because N-30 needs the control to carry an accessible name
        that a screen reader announces without hovering.
        """
        caption = self._label(name, "secondary")
        box.addWidget(caption)
        self._text(widget, name, setter="setAccessibleName")
        box.addWidget(widget)
        if note:
            box.addWidget(self._label(note, "muted"))

    def _bullet(self, source: str) -> QWidget:
        """A list item whose mark is not part of the translated string.

        Prefixing the text with the bullet would make "·  Local REST and MCP
        integrations" the catalogue key, which no translation would ever
        match; keeping the mark in its own label also gives the wrapped
        lines a hanging indent.
        """
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(METRICS.gap)
        mark = QLabel("·")
        mark.setProperty("role", "muted")
        mark.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        line.addWidget(mark)
        line.addWidget(self._label(source, "muted"), 1)
        return row

    def _button(self, source: str, variant: str | None = None) -> QPushButton:
        b = QPushButton()
        if variant:
            b.setProperty("variant", variant)
        self._text(b, source)
        self._text(b, source, setter="setAccessibleName")
        b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return b

    def _check(self, source: str, **fmt: Any) -> QCheckBox:
        c = QCheckBox()
        self._text(c, source, **fmt)
        self._text(c, source, setter="setAccessibleName", **fmt)
        return c

    # ==================================================================
    # Resources (F-20, F-21, F-78)
    # ==================================================================

    def _build_resources(self) -> QFrame:
        panel, box = self._panel("Resources")
        box.addWidget(
            self._label(
                "How much of this computer EchoAct may use while it generates speech.",
                "secondary",
            )
        )

        self.cpu = QSpinBox()
        self.cpu.setRange(CPU_PERCENT_MIN, CPU_PERCENT_MAX)
        self.cpu.setSingleStep(5)
        self.cpu.setSuffix(" %")
        self.cpu.setValue(CPU_PERCENT_DEFAULT)
        self._field(
            box,
            "Processor share",
            self.cpu,
            "10-70% of this computer's total processor capacity. The default is 20%.",
        )

        ceiling = memory_ceiling_bytes(self._machine.total_ram_bytes)
        recommended = default_memory_bytes(self._machine.total_ram_bytes)
        box.addWidget(self._label("Memory limit", "secondary"))
        self.memory_auto = self._check(
            "Use the amount recommended for this computer ({size})",
            size=memory_size(recommended),
        )
        self.memory = QDoubleSpinBox()
        self.memory.setDecimals(1)
        self.memory.setSingleStep(0.5)
        self.memory.setSuffix(" GiB")
        # F-23's floor is the smallest configuration that can load a model at
        # all, so it is the bottom of the range rather than a value the user
        # may pick and then be refused for picking.  A machine whose F-21
        # ceiling falls under that floor gets the ceiling as both ends and a
        # plain statement, which is more honest than an empty range.
        low = min(MEMORY_FLOOR_BYTES, ceiling)
        self.memory.setRange(low / GIB, max(low, ceiling) / GIB)
        box.addWidget(self.memory_auto)
        self._text(self.memory, "Memory limit", setter="setAccessibleName")
        box.addWidget(self.memory)
        box.addWidget(
            self._label(
                "At most {ceiling} on this computer: 32 GiB, or half of installed memory, "
                "whichever is smaller.",
                "muted",
                ceiling=memory_size(ceiling),
            )
        )
        if ceiling < MEMORY_FLOOR_BYTES:
            box.addWidget(
                self._label(
                    "This computer has less memory than EchoAct needs to load a model "
                    "({floor}).",
                    "warn",
                    floor=memory_size(MEMORY_FLOOR_BYTES),
                )
            )

        box.addWidget(_separator())
        self.resources_configured = self._value_label()
        self.resources_applied = self._value_label()
        box.addWidget(self.resources_configured)
        box.addWidget(self.resources_applied)
        box.addWidget(
            self._label(
                "The applied memory can be lower than the configured value when the "
                "computer has little free memory.",
                "muted",
            )
        )

        self.apply_row = QWidget()
        row = QVBoxLayout(self.apply_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(METRICS.gap_tight)
        row.addWidget(self._label("This change applies to the next job.", "warn"))
        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.gap)
        self.apply_next = self._button("Apply to the next job", "quiet")
        self.apply_cancel = self._button("Cancel the job and apply now", "danger")
        buttons.addWidget(self.apply_next)
        buttons.addWidget(self.apply_cancel)
        buttons.addStretch(1)
        row.addLayout(buttons)
        self.apply_row.setVisible(False)
        box.addWidget(self.apply_row)

        self.cpu.valueChanged.connect(self._on_cpu)
        self.memory.valueChanged.connect(self._on_memory)
        self.memory_auto.toggled.connect(self._on_memory_auto)
        self.apply_next.clicked.connect(self._dismiss_apply_row)
        self.apply_cancel.clicked.connect(self._on_apply_now)
        return panel

    def _on_cpu(self, value: int) -> None:
        if self._loading:
            return
        self._note_resource_change()
        self.cpu_changed.emit(int(value))

    def _on_memory(self, value: float) -> None:
        if self._loading or self.memory_auto.isChecked():
            return
        self._note_resource_change()
        self.memory_changed.emit(int(round(value * GIB)))

    def _on_memory_auto(self, checked: bool) -> None:
        self.memory.setEnabled(not checked)
        if self._loading:
            return
        self._note_resource_change()
        if checked:
            self.memory_changed.emit(None)
        else:
            self.memory_changed.emit(int(round(self.memory.value() * GIB)))

    def _note_resource_change(self) -> None:
        """F-78: a change during a job is a choice between two outcomes.

        The change itself has already been saved by the time this runs --
        the signal reaches ``update_settings``, which F-78 says applies to
        the *next* job -- so this offers the other outcome rather than
        holding the change hostage to a decision the user may not want to
        make now.

        The two lines above the offer are re-rendered first: the window does
        not hand the saved settings back, so the "Configured:" line would
        otherwise keep the value the screen was opened with while the control
        beside it shows the new one.
        """
        self._render_resources()
        if not self._job_running:
            return
        self._pending_resource_change = True
        self.apply_row.setVisible(True)

    def _dismiss_apply_row(self) -> None:
        self._pending_resource_change = False
        self.apply_row.setVisible(False)

    def _on_apply_now(self) -> None:
        if not self.confirm(
            tr("Cancel the running job?"),
            tr(
                "Applying the new limits now cancels the job that is running. Audio "
                "already generated stays available; the rest is not generated."
            ),
            tr("Cancel the job and apply now"),
        ):
            return
        self._dismiss_apply_row()
        self.apply_now_requested.emit()

    def set_resource_state(self, applied: Budget | None, *, job_running: bool) -> None:
        """F-78's two numbers: what is set, and what the job is running under.

        ``applied`` is the ``Budget`` the running job recorded, not a fresh
        resolution of the current settings -- resolving again would show the
        user a number no job has ever used and call it "applied".
        """
        self._applied = applied
        self._job_running = job_running
        if not job_running:
            self._pending_resource_change = False
        self.apply_row.setVisible(job_running and self._pending_resource_change)
        self._render_resources()

    def _configured_memory_bytes(self) -> int | None:
        """The memory limit the screen is offering, in bytes, or ``None`` for
        F-21's per-machine default.

        Read from the control rather than from ``self._settings`` for the same
        reason as :meth:`_configured_view`, but with one deference to the
        store: the box shows one decimal, so a saved 3.33 GiB reads back as
        3.3.  While the box is still showing the saved figure, the saved
        figure *is* the configured one -- taking the rounded one instead would
        report a difference from the running job's budget that nobody made.
        """
        if self.memory_auto.isChecked():
            return None
        saved = self._settings.memory_bytes
        if saved is not None and round(saved / GIB, self.memory.decimals()) == self.memory.value():
            return saved
        return int(round(self.memory.value() * GIB))

    def _configured_view(self) -> ResourcePolicyView:
        """F-78's pair, with the configured half taken from the controls.

        The screen owns the configured value between the edit and the save:
        an edit leaves as a signal, the window turns it into
        ``update_settings``, and nothing hands the result back here.  Reading
        ``self._settings`` for this line would therefore show the owner the
        value the screen was opened with, and ``differs`` -- which decides
        whether the applied line is flagged -- would be computed against that
        same stale number and stay ``False`` exactly when it should not.
        """
        return ResourcePolicyView(
            configured_cpu_percent=int(self.cpu.value()),
            configured_memory_bytes=self._configured_memory_bytes(),
            applied=self._applied,
        )

    def _render_resources(self) -> None:
        view = self._configured_view()
        configured_memory = (
            memory_size(view.configured_memory_bytes)
            if view.configured_memory_bytes is not None
            else memory_size(default_memory_bytes(self._machine.total_ram_bytes))
        )
        self.resources_configured.setText(
            tr("Configured: {cpu}% processor, {memory} memory").format(
                cpu=view.configured_cpu_percent, memory=configured_memory
            )
        )
        if self._job_running and self._applied is not None:
            self.resources_applied.setText(
                tr("Applied to the job running now: {cpu}% processor, {memory} memory").format(
                    cpu=self._applied.cpu_percent,
                    memory=memory_size(self._applied.memory_bytes),
                )
            )
            self.resources_applied.setProperty("role", "warn" if view.differs else "secondary")
        else:
            self.resources_applied.setText(
                tr("No job is running. These values apply to the next job.")
            )
            self.resources_applied.setProperty("role", "secondary")
        _repolish(self.resources_applied)

    # ==================================================================
    # Audio (F-67)
    # ==================================================================

    def _build_audio(self) -> QFrame:
        panel, box = self._panel("Audio")

        self.device = QComboBox()
        self._field(box, "Output device", self.device)
        self.device_note = self._value_label("warn")
        self.device_note.setVisible(False)
        box.addWidget(self.device_note)
        self.device_refresh = self._button("Look for devices again", "quiet")
        refresh_row = QHBoxLayout()
        refresh_row.addWidget(self.device_refresh)
        refresh_row.addStretch(1)
        box.addLayout(refresh_row)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setSingleStep(5)
        self.volume.setPageStep(10)
        self.volume.setValue(100)
        volume_row = QHBoxLayout()
        volume_row.setSpacing(METRICS.gap)
        volume_row.addWidget(self._label("Volume", "secondary"))
        volume_row.addStretch(1)
        self.volume_value = self._value_label()
        volume_row.addWidget(self.volume_value)
        box.addLayout(volume_row)
        self._text(self.volume, "Volume", setter="setAccessibleName")
        box.addWidget(self.volume)

        self.muted = self._check("Mute")
        box.addWidget(self.muted)
        box.addWidget(
            self._label(
                "Volume changes what you hear now. It does not change the saved WAV file.",
                "muted",
            )
        )

        self.device.currentIndexChanged.connect(self._on_device)
        self.device_refresh.clicked.connect(self._on_device_refresh)
        self.volume.valueChanged.connect(self._on_volume)
        self.muted.toggled.connect(self._on_muted)
        return panel

    def _on_device_refresh(self) -> None:
        self.refresh_devices()
        self.devices_refresh_requested.emit()

    def refresh_devices(self) -> None:
        """Re-read the device list (F-67, F-68).

        Enumeration failure is a state of the screen, not an exception:
        F-79's spirit -- a subsystem being unavailable never takes the rest
        of the app with it -- applies just as well to the audio system, and
        the user still has every other setting to change.
        """
        try:
            self._devices = list(self._device_lister())
            self._device_error = ""
        except EchoActError as exc:
            self._devices = []
            self._device_error = exc.message
        self._render_devices()

    def _render_devices(self) -> None:
        was = self._loading
        self._loading = True
        try:
            self.device.clear()
            self.device.addItem(tr("System default output"), None)
            keys = set()
            for d in self._devices:
                self.device.addItem(f"{d.name} — {d.host_api}", d.key)
                keys.add(d.key)
            chosen = self._settings.output_device
            if chosen is not None and chosen not in keys:
                # F-67 forbids moving to another speaker without confirmation,
                # so a remembered device that is not present stays selected and
                # is labelled.  Dropping it from the list would silently reselect
                # the system default, which is that same unconfirmed switch.
                self.device.addItem(
                    tr("{name} (not connected)").format(name=chosen.split("::", 1)[-1]), chosen
                )
            index = self.device.findData(chosen)
            self.device.setCurrentIndex(max(0, index))
            self.device_note.setVisible(bool(self._device_error))
            if self._device_error:
                self.device_note.setText(
                    tr("The audio system could not be listed: {reason}").format(
                        reason=self._device_error
                    )
                )
        finally:
            self._loading = was

    def _on_device(self, _index: int) -> None:
        if self._loading:
            return
        self.output_device_changed.emit(self.device.currentData())

    def _on_volume(self, value: int) -> None:
        self.volume_value.setText(f"{value}%")
        if self._loading:
            return
        self.volume_changed.emit(value / 100)

    def _on_muted(self, checked: bool) -> None:
        if self._loading:
            return
        self.muted_changed.emit(bool(checked))

    # ==================================================================
    # Reading and display (F-83, F-30, F-86)
    # ==================================================================

    def _build_display(self) -> QFrame:
        panel, box = self._panel("Reading and display")

        self.language = QComboBox()
        self._field(box, "Display language", self.language)
        box.addWidget(
            self._label(
                "Changing the display language does not change your documents, saved "
                "jobs, or what the local service answers.",
                "muted",
            )
        )

        box.addWidget(_separator())
        self.autoplay = self._check("Play as soon as ready")
        self.follow = self._check("Follow the reading position")
        box.addWidget(self.autoplay)
        box.addWidget(self.follow)

        self._render_languages()
        self.language.currentIndexChanged.connect(self._on_language)
        self.autoplay.toggled.connect(self._on_autoplay)
        self.follow.toggled.connect(self._on_follow)
        return panel

    def _render_languages(self) -> None:
        was = self._loading
        self._loading = True
        try:
            self.language.clear()
            self.language.addItem(tr("Korean"), DisplayLanguage.KO.value)
            self.language.addItem(tr("English"), DisplayLanguage.EN.value)
            index = self.language.findData(self._settings.display_language.value)
            self.language.setCurrentIndex(max(0, index))
        finally:
            self._loading = was

    def _on_language(self, _index: int) -> None:
        if self._loading:
            return
        # The combo carries the plain value because Qt marshals a ``str``
        # subclass into the item data as a ``str``; the enum is rebuilt here so
        # that what leaves the screen is what ``Settings.display_language``
        # holds, per this module's contract.
        self.display_language_changed.emit(DisplayLanguage(str(self.language.currentData())))

    def _on_autoplay(self, checked: bool) -> None:
        if not self._loading:
            self.autoplay_changed.emit(bool(checked))

    def _on_follow(self, checked: bool) -> None:
        if not self._loading:
            self.follow_changed.emit(bool(checked))

    # ==================================================================
    # Integrations (F-46, F-71, F-79, N-31, 4.1)
    # ==================================================================

    def _build_integrations(self) -> QFrame:
        panel, box = self._panel("Integrations")

        self.rest = self._check("Local REST service")
        box.addWidget(self.rest)
        self.rest_note = self._value_label("muted")
        box.addWidget(self.rest_note)
        self.rest_how = self._value_label("muted")
        box.addWidget(self.rest_how)
        # F-46 wants the consequence stated at the point of the change, so it
        # is standing text beside the switch and not only in the confirmation
        # the user sees after deciding.
        box.addWidget(
            self._label(
                "MCP works through this service, so turning it off also makes MCP "
                "unavailable.",
                "muted",
            )
        )

        self.port = QSpinBox()
        self.port.setRange(MIN_USER_PORT, MAX_PORT)
        self.port.setGroupSeparatorShown(False)
        self._field(box, "Port", self.port)
        self.port_restart = self._button("Restart the service on this port")
        port_row = QHBoxLayout()
        port_row.addWidget(self.port_restart)
        port_row.addStretch(1)
        box.addLayout(port_row)

        self.conflict = QWidget()
        conflict_box = QVBoxLayout(self.conflict)
        conflict_box.setContentsMargins(0, 0, 0, 0)
        conflict_box.setSpacing(METRICS.gap_tight)
        self.conflict_text = self._value_label("warn")
        conflict_box.addWidget(self.conflict_text)
        self.conflict_use = self._button("Use a different port")
        conflict_row = QHBoxLayout()
        conflict_row.addWidget(self.conflict_use)
        conflict_row.addStretch(1)
        conflict_box.addLayout(conflict_row)
        self.conflict.setVisible(False)
        box.addWidget(self.conflict)

        box.addWidget(_separator())
        self.mcp = self._check("MCP server")
        box.addWidget(self.mcp)
        box.addWidget(
            self._label(
                "Off by default. It exposes nothing the REST service does not already "
                "expose.",
                "muted",
            )
        )
        box.addWidget(
            self._label(
                "Started by your MCP client over stdio, using its own REST credential.",
                "muted",
            )
        )

        box.addWidget(_separator())
        self.credential_days = QSpinBox()
        self.credential_days.setRange(CREDENTIAL_DAYS_MIN, CREDENTIAL_DAYS_MAX)
        self.credential_days.setSuffix(" " + tr("days"))
        self._field(box, "New credentials expire after", self.credential_days)

        box.addWidget(self._label("Clients", "section"))
        self.clients = QTreeWidget()
        self.clients.setRootIsDecorated(False)
        self.clients.setColumnCount(4)
        self.clients.setUniformRowHeights(True)
        self.clients.setMinimumHeight(120)
        self.clients.setMinimumWidth(280)
        self._text(self.clients, "Clients", setter="setAccessibleName")
        box.addWidget(self.clients)
        self.clients_empty = self._label("No clients yet.", "muted")
        box.addWidget(self.clients_empty)
        self.expiry_warning = self._value_label("warn")
        self.expiry_warning.setVisible(False)
        box.addWidget(self.expiry_warning)

        self.permission_heading = self._value_label("secondary")
        box.addWidget(self.permission_heading)
        self.permission_boxes: dict[Capability, QCheckBox] = {}
        permission_row = QHBoxLayout()
        permission_row.setSpacing(METRICS.gap)
        for capability in (
            Capability.GENERATE,
            Capability.READ_RESULTS,
            Capability.READ_HISTORY,
        ):
            check = self._check(_CAPABILITY_TEXT[capability])
            check.toggled.connect(lambda checked, c=capability: self._on_capability(c, checked))
            self.permission_boxes[capability] = check
            permission_row.addWidget(check)
        permission_row.addStretch(1)
        box.addLayout(permission_row)

        client_buttons = QHBoxLayout()
        client_buttons.setSpacing(METRICS.gap)
        self.issue = self._button("Issue a credential")
        self.reissue = self._button("Reissue")
        self.revoke = self._button("Revoke", "danger")
        for b in (self.issue, self.reissue, self.revoke):
            client_buttons.addWidget(b)
        client_buttons.addStretch(1)
        box.addLayout(client_buttons)

        self.secret = QFrame()
        self.secret.setObjectName("Panel")
        secret_box = QVBoxLayout(self.secret)
        secret_box.setContentsMargins(METRICS.pad, METRICS.pad, METRICS.pad, METRICS.pad)
        secret_box.setSpacing(METRICS.gap_tight)
        secret_box.addWidget(self._label("Copy this credential now. It is shown once.", "warn"))
        self.secret_value = QLineEdit()
        self.secret_value.setReadOnly(True)
        self._text(self.secret_value, "Copy this credential now. It is shown once.",
                   setter="setAccessibleName")
        secret_box.addWidget(self.secret_value)
        secret_row = QHBoxLayout()
        secret_row.setSpacing(METRICS.gap)
        self.secret_copy = self._button("Copy")
        self.secret_copy.setIcon(icons.icon("copy", self._palette.text_secondary))
        self.secret_done = self._button("Done", "quiet")
        secret_row.addWidget(self.secret_copy)
        secret_row.addWidget(self.secret_done)
        secret_row.addStretch(1)
        secret_box.addLayout(secret_row)
        secret_box.addWidget(
            self._label(
                "EchoAct keeps only a verifier, from which the credential cannot be "
                "recovered. If it is lost, reissue it.",
                "muted",
            )
        )
        self.secret.setVisible(False)
        box.addWidget(self.secret)

        self._render_client_columns()
        # Without this the three permission boxes would start blank while the
        # next credential really would carry two grants, and the reissue and
        # revoke buttons would look available with nothing selected (N-09).
        self._render_client_buttons()
        self.rest.toggled.connect(self._on_rest)
        self.port_restart.clicked.connect(self._on_port_restart)
        self.conflict_use.clicked.connect(self._on_use_offered_port)
        self.mcp.toggled.connect(self._on_mcp)
        self.credential_days.valueChanged.connect(self._on_credential_days)
        self.issue.clicked.connect(self._on_issue)
        self.reissue.clicked.connect(self._on_reissue)
        self.revoke.clicked.connect(self._on_revoke)
        self.secret_copy.clicked.connect(self._on_copy_secret)
        self.secret_done.clicked.connect(self.dismiss_new_credential)
        self.clients.currentItemChanged.connect(lambda *_: self._render_client_buttons())
        return panel

    def _render_client_columns(self) -> None:
        self.clients.setHeaderLabels(
            [tr("Name"), tr("Permissions"), tr("Last access"), tr("Expires")]
        )

    def _on_rest(self, checked: bool) -> None:
        if self._loading:
            return
        if not checked and self.mcp.isChecked():
            # The consequence is confirmed only when it actually bites: with
            # MCP already off, turning REST off costs the user nothing MCP
            # related, and a dialog there would train them to dismiss this one.
            if not self.confirm(
                tr("Turn the local service off?"),
                tr(
                    "MCP is a client of this service, so turning it off makes MCP "
                    "unavailable too. Generation, playback, and the library keep working."
                ),
                tr("Turn off"),
            ):
                self._set_checked(self.rest, True)
                return
        self.rest_enabled_changed.emit(bool(checked))
        self._render_service()

    def _on_port_restart(self) -> None:
        self.conflict.setVisible(False)
        self._request_port(int(self.port.value()))

    def _request_port(self, port: int) -> None:
        """Ask for the service to move, and say so (F-79, F-46, N-31).

        The disclosure moves with the request rather than with the typing,
        and :meth:`show_port_conflict` moves it back if the bind was refused
        -- those two are the only things this screen is ever told about where
        the service is.
        """
        self._service_port_before = self._service_port
        self._service_port = port
        self._render_service()
        self.rest_port_changed.emit(port)

    def _on_use_offered_port(self) -> None:
        """F-79: the alternative port is offered, and taking it is an act.

        The port is not bound until the owner presses this, which is the
        difference between offering an alternative and choosing one -- the
        latter is what F-79 forbids.
        """
        if self._conflict_port is None:
            return
        chosen = self._conflict_port
        self._set_value(self.port, chosen)
        self.conflict.setVisible(False)
        self._request_port(int(chosen))

    def show_port_conflict(self, port: int, suggestion: int | None = None) -> None:
        """Report that the port is occupied and offer another one (F-79).

        A refused bind is also the only way this screen learns that the port
        it just asked for is not the one in service, so the disclosure above
        goes back to the port that is.
        """
        if self._service_port == port and self._service_port_before is not None:
            self._service_port = self._service_port_before
            self._render_service()
        self._service_port_before = None
        self._conflict_port = suggestion or _next_port(port)
        self.conflict_text.setText(
            tr(
                "Port {port} is in use by another program. EchoAct does not close it "
                "and does not pick another port on its own."
            ).format(port=port)
        )
        text = tr("Use port {port} instead").format(port=self._conflict_port)
        self.conflict_use.setText(text)
        self.conflict_use.setAccessibleName(text)
        self.conflict.setVisible(True)

    def clear_port_conflict(self) -> None:
        self._conflict_port = None
        self.conflict.setVisible(False)

    def _on_mcp(self, checked: bool) -> None:
        if not self._loading:
            self.mcp_enabled_changed.emit(bool(checked))

    def _on_credential_days(self, value: int) -> None:
        if not self._loading:
            self.credential_days_changed.emit(int(value))

    def _on_issue(self) -> None:
        name = self.ask_text(
            tr("Name this client"),
            tr("What is connecting? For example, an editor or a script."),
            "",
        )
        if not name or not name.strip():
            return
        self.credential_issue_requested.emit(
            name.strip(), self._pending_capabilities, int(self.credential_days.value())
        )

    def _selected_credential(self) -> Credential | None:
        item = self.clients.currentItem()
        if item is None:
            return None
        ref = item.data(0, Qt.ItemDataRole.UserRole)
        for cred in self._credentials:
            if cred.ref == ref:
                return cred
        return None

    def _on_reissue(self) -> None:
        cred = self._selected_credential()
        if cred is None:
            return
        if not self.confirm(
            tr("Reissue this credential?"),
            tr(
                "The current credential for {name} stops working at once. The new one "
                "is shown once, here."
            ).format(name=cred.name),
            tr("Reissue"),
        ):
            return
        self.credential_reissue_requested.emit(cred.ref)

    def _on_revoke(self) -> None:
        cred = self._selected_credential()
        if cred is None:
            return
        if not self.confirm(
            tr("Revoke this credential?"),
            tr(
                "{name} loses access immediately, including to results it already "
                "generated, and its running job is canceled."
            ).format(name=cred.name),
            tr("Revoke"),
        ):
            return
        self.credential_revoke_requested.emit(cred.ref)

    def show_new_credential(self, token: str) -> None:
        """F-71: the credential exists on screen once, and is copyable.

        The token is put in a read-only line edit rather than a label so the
        keyboard can select it; nothing here writes it anywhere, and
        :meth:`dismiss_new_credential` clears the widget so it does not
        survive in the screen's state either.
        """
        self.secret_value.setText(token)
        self.secret.setVisible(True)
        self.secret_value.setFocus()
        self.secret_value.selectAll()

    def dismiss_new_credential(self) -> None:
        self.secret_value.clear()
        self.secret.setVisible(False)

    def _on_copy_secret(self) -> None:
        self.secret_value.selectAll()
        self.secret_value.copy()

    def set_credentials(
        self, credentials: Iterable[Credential], *, now: float | None = None
    ) -> None:
        """F-71's list: name, capabilities, last access, expiry."""
        self._credentials = list(credentials)
        self._render_credentials(now)

    def _render_credentials(self, now: float | None = None) -> None:
        selected = self.clients.currentItem()
        keep = selected.data(0, Qt.ItemDataRole.UserRole) if selected is not None else None
        self.clients.clear()
        for cred in self._credentials:
            item = QTreeWidgetItem(
                [
                    cred.name,
                    ", ".join(
                        tr(_CAPABILITY_TEXT[c])
                        for c in sorted(cred.capabilities, key=lambda c: c.value)
                        if c in _CAPABILITY_TEXT
                    ),
                    _date_time(cred.last_access_at) or tr("never"),
                    _expiry_text(cred, now),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, cred.ref)
            self.clients.addTopLevelItem(item)
            if cred.ref == keep:
                self.clients.setCurrentItem(item)
        for column in range(4):
            self.clients.resizeColumnToContents(column)
        self.clients.setVisible(bool(self._credentials))
        self.clients_empty.setVisible(not self._credentials)

        warned = [c for c in self._credentials if c.expiry_warning(now)]
        self.expiry_warning.setVisible(bool(warned))
        if warned:
            first = warned[0]
            self.expiry_warning.setText(
                tr("A credential for {name} expires in {days} days. Reissue it before then.")
                .format(name=first.name, days=max(0, int(first.days_until_expiry(now))))
            )
        self._render_client_buttons()

    def _render_client_buttons(self) -> None:
        cred = self._selected_credential()
        self.reissue.setEnabled(cred is not None and cred.revoked_at is None)
        self.revoke.setEnabled(cred is not None and cred.revoked_at is None)
        self._render_permissions()

    def _render_permissions(self) -> None:
        """F-61's three grants, for the selected client or for the next one.

        One group of checkboxes serves both, because they are the same three
        permissions either way; the heading above says which set a tick
        changes, so the owner is never guessing whose permissions they are
        editing.
        """
        cred = self._selected_credential()
        if cred is None:
            self.permission_heading.setText(tr("Permissions for the next credential"))
            chosen: frozenset[Capability] = self._pending_capabilities
            editable = True
        else:
            self.permission_heading.setText(
                tr("Permissions for {name}").format(name=cred.name)
            )
            chosen = cred.effective_capabilities
            # An owner credential is the app's own, and narrowing it here
            # would lock the owner out of their own service; a revoked one
            # has no permissions left to change.
            editable = not cred.is_owner and cred.revoked_at is None
        was, self._loading = self._loading, True
        try:
            for capability, check in self.permission_boxes.items():
                check.setChecked(capability in chosen)
                check.setEnabled(editable)
        finally:
            self._loading = was

    def _on_capability(self, _capability: Capability, _checked: bool) -> None:
        if self._loading:
            return
        chosen = frozenset(c for c, box in self.permission_boxes.items() if box.isChecked())
        cred = self._selected_credential()
        if cred is None:
            self._pending_capabilities = chosen
            return
        # F-71: the change takes effect immediately, which is the store's to
        # do -- and 5.3 may require that client's jobs to be cancelled with
        # it, which only the window can arrange.
        self.credential_capabilities_changed.emit(cred.ref, chosen)

    def _render_service(self) -> None:
        self.rest_note.setText(
            tr(
                "Listening on {host}:{port} — this computer only. Every request needs a "
                "credential."
            ).format(host=REST_HOST, port=self._service_port)
        )
        # F-46 lists the connection method among what must be reviewable, and
        # for a REST client the base URL is the whole of it.
        self.rest_how.setText(
            tr(
                "Clients connect to {url} with their credential. Nothing else on the "
                "network can reach it."
            ).format(url=f"http://{REST_HOST}:{self._service_port}{API_PREFIX}")
        )
        on = self.rest.isChecked()
        self.port.setEnabled(on)
        self.port_restart.setEnabled(on)
        # F-58: MCP is a REST client, so it cannot be enabled while REST is off.
        self.mcp.setEnabled(on)
        self.issue.setEnabled(on)

    # ==================================================================
    # Storage (F-73, F-76)
    # ==================================================================

    def _build_storage(self) -> QFrame:
        panel, box = self._panel("Storage")

        grid = QGridLayout()
        grid.setHorizontalSpacing(METRICS.pad)
        grid.setVerticalSpacing(METRICS.gap_tight)
        self.size_labels: dict[str, QLabel] = {}
        rows = (
            ("model_cache", "Model cache"),
            ("documents", "Documents and history"),
            ("audio", "Audio"),
            ("temp", "Temporary data"),
            ("logs", "Logs"),
        )
        for row, (key, name) in enumerate(rows):
            grid.addWidget(self._label(name, "secondary"), row, 0)
            value = self._value_label()
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(value, row, 1)
            self.size_labels[key] = value
        grid.setColumnStretch(0, 1)
        box.addLayout(grid)

        self.storage_refresh = self._button("Refresh", "quiet")
        refresh_row = QHBoxLayout()
        refresh_row.addWidget(self.storage_refresh)
        refresh_row.addStretch(1)
        box.addLayout(refresh_row)

        box.addWidget(_separator())
        self.retention = QSpinBox()
        self.retention.setRange(RETENTION_MIN_BYTES // GB, RETENTION_MAX_BYTES // GB)
        self.retention.setSuffix(" GB")
        self._field(
            box,
            "Retention limit",
            self.retention,
            "1-100 GB for documents, history, and audio together. Lowering it below "
            "what is stored keeps that data and refuses new retention.",
        )
        self.retention_used = self._value_label("muted")
        box.addWidget(self.retention_used)

        self.cleanup = self._button("Clean up expired data")
        cleanup_row = QHBoxLayout()
        cleanup_row.addWidget(self.cleanup)
        cleanup_row.addStretch(1)
        box.addLayout(cleanup_row)
        box.addWidget(
            self._label("Data in use by playback, generation, or a backup is left alone.", "muted")
        )
        self.cleanup_result = self._value_label("ok")
        self.cleanup_result.setVisible(False)
        box.addWidget(self.cleanup_result)

        box.addWidget(_separator())
        self.reset_buttons: dict[ResetScope, QPushButton] = {}
        for scope, button_text, description in (
            (
                ResetScope.VOICE_AND_DISPLAY,
                "Reset",
                "Voice, tempo, style, display language, and playback options return to "
                "their defaults. Documents, history, and audio are untouched.",
            ),
            (
                ResetScope.INTEGRATIONS,
                "Revoke all",
                "Every client credential stops working at once and their running jobs "
                "are canceled. A new owner credential is issued on the next launch.",
            ),
            (
                ResetScope.RETAINED_DATA,
                "Delete",
                "Saved documents, job history, and generated audio are deleted from this "
                "app and cannot be recovered. Files you exported yourself and your own "
                "backups are not touched.",
            ),
            (
                ResetScope.MODEL_CACHE,
                "Delete",
                "The downloaded model files are deleted, about {size}. Generation needs "
                "them downloaded again before it can run, which needs a network.",
            ),
        ):
            box.addWidget(self._label(_SCOPE_TITLE[scope], "secondary"))
            row = QHBoxLayout()
            row.setSpacing(METRICS.gap)
            button = self._button(button_text, "danger")
            button.setMinimumWidth(120)
            self._text(button, _SCOPE_TITLE[scope], setter="setAccessibleName")
            row.addWidget(button)
            row.addStretch(1)
            box.addLayout(row)
            if scope is ResetScope.MODEL_CACHE:
                self.model_cache_note = self._label(description, "muted", size=bytes_size(0))
                box.addWidget(self.model_cache_note)
            else:
                box.addWidget(self._label(description, "muted"))
            button.clicked.connect(lambda _=False, s=scope: self._on_reset(s))
            self.reset_buttons[scope] = button

        self.failures = self._value_label("danger")
        self.failures.setVisible(False)
        box.addWidget(self.failures)

        self.retention.valueChanged.connect(self._on_retention)
        self.cleanup.clicked.connect(self.cleanup_requested)
        self.storage_refresh.clicked.connect(self.storage_refresh_requested)
        return panel

    def _on_retention(self, value: int) -> None:
        if not self._loading:
            self.retention_changed.emit(int(value) * GB)

    def _on_reset(self, scope: ResetScope) -> None:
        """F-76: each scope confirms its own impact before anything goes.

        The confirmation names what is deleted, what cannot be recovered, and
        what is deliberately left alone -- the user's own exported files and
        backups -- because F-76 requires all three and a generic "are you
        sure" answers none of them.
        """
        if not self.confirm(
            tr(_SCOPE_TITLE[scope]) + "?",
            self._scope_body(scope),
            tr(_SCOPE_ACCEPT[scope]),
        ):
            return
        self.failures.setVisible(False)
        self.reset_requested.emit(scope.value)

    def _scope_body(self, scope: ResetScope) -> str:
        if scope is ResetScope.MODEL_CACHE:
            size = bytes_size(self._sizes.model_cache_bytes if self._sizes else 0)
            body = tr(
                "The downloaded model files are deleted, about {size}. Generation needs "
                "them downloaded again before it can run, which needs a network."
            ).format(size=size)
        elif scope is ResetScope.RETAINED_DATA:
            body = tr(
                "Saved documents, job history, and generated audio are deleted from this "
                "app and cannot be recovered. Files you exported yourself and your own "
                "backups are not touched."
            )
        elif scope is ResetScope.INTEGRATIONS:
            body = tr(
                "Every client credential stops working at once and their running jobs "
                "are canceled. A new owner credential is issued on the next launch."
            )
        else:
            body = tr(
                "Voice, tempo, style, display language, and playback options return to "
                "their defaults. Documents, history, and audio are untouched."
            )
        if scope is ResetScope.VOICE_AND_DISPLAY:
            return body
        return body + " " + tr("This cannot be undone.")

    def set_storage(self, sizes: StorageSizes) -> None:
        """F-73's five figures, each on its own line."""
        self._sizes = sizes
        self._render_storage()

    def _render_storage(self) -> None:
        sizes = self._sizes
        if sizes is None:
            return
        self.size_labels["model_cache"].setText(bytes_size(sizes.model_cache_bytes))
        self.size_labels["documents"].setText(bytes_size(sizes.documents_bytes))
        self.size_labels["audio"].setText(bytes_size(sizes.audio_bytes))
        self.size_labels["temp"].setText(bytes_size(sizes.temp_bytes))
        self.size_labels["logs"].setText(bytes_size(sizes.log_bytes))
        self.retention_used.setText(
            tr("{used} of {limit} used").format(
                used=bytes_size(sizes.retained_used_bytes),
                limit=bytes_size(sizes.retention_limit_bytes),
            )
        )
        self.model_cache_note.setText(
            tr(
                "The downloaded model files are deleted, about {size}. Generation needs "
                "them downloaded again before it can run, which needs a network."
            ).format(size=bytes_size(sizes.model_cache_bytes))
        )

    def report_cleanup(self, removed: int, freed_bytes: int) -> None:
        """F-73's cleanup, reported in the number the user can act on.

        Three phrasings rather than one with a count, because "Removed 1
        items" and "Removed 0 items" are both wrong in English and the
        second is not even about a removal.
        """
        self.cleanup_result.setVisible(True)
        if removed <= 0:
            self.cleanup_result.setText(tr("Nothing had expired."))
        elif removed == 1:
            self.cleanup_result.setText(
                tr("Removed 1 item, freeing {size}.").format(size=bytes_size(freed_bytes))
            )
        else:
            self.cleanup_result.setText(
                tr("Removed {n} items, freeing {size}.").format(
                    n=removed, size=bytes_size(freed_bytes)
                )
            )

    def report_failures(self, items: Sequence[str]) -> None:
        """F-76: what could not be deleted is named, not swallowed."""
        self.failures.setVisible(bool(items))
        if items:
            self.failures.setText(
                tr("These items could not be deleted: {items}").format(items=", ".join(items))
            )

    # ==================================================================
    # Voice presets (F-66)
    # ==================================================================

    def _build_presets(self) -> QFrame:
        panel, box = self._panel("Voice presets")

        self.preset_count = self._value_label("muted")
        box.addWidget(self.preset_count)
        self.presets = QListWidget()
        self.presets.setMinimumHeight(110)
        self._text(self.presets, "Voice presets", setter="setAccessibleName")
        box.addWidget(self.presets)
        self.presets_empty = self._label("No presets yet.", "muted")
        box.addWidget(self.presets_empty)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.gap)
        self.preset_apply = self._button("Apply")
        self.preset_rename = self._button("Rename")
        self.preset_delete = self._button("Delete", "danger")
        for b in (self.preset_apply, self.preset_rename, self.preset_delete):
            buttons.addWidget(b)
        buttons.addStretch(1)
        box.addLayout(buttons)

        box.addWidget(_separator())
        box.addWidget(self._label("Save the current voice settings as", "secondary"))
        save_row = QHBoxLayout()
        save_row.setSpacing(METRICS.gap)
        self.preset_name = QLineEdit()
        self._text(self.preset_name, "A name for this preset", setter="setPlaceholderText")
        self._text(self.preset_name, "A name for this preset", setter="setAccessibleName")
        self.preset_save = self._button("Save")
        save_row.addWidget(self.preset_name, 1)
        save_row.addWidget(self.preset_save)
        box.addLayout(save_row)
        self.preset_limit = self._label(
            "The limit of {max} presets has been reached.", "warn", max=VOICE_PRESET_MAX
        )
        self.preset_limit.setVisible(False)
        box.addWidget(self.preset_limit)

        self.presets.currentRowChanged.connect(lambda *_: self._render_preset_buttons())
        self.preset_apply.clicked.connect(self._on_preset_apply)
        self.preset_rename.clicked.connect(self._on_preset_rename)
        self.preset_delete.clicked.connect(self._on_preset_delete)
        self.preset_save.clicked.connect(self._on_preset_save)
        self.preset_name.returnPressed.connect(self._on_preset_save)
        return panel

    def _render_presets(self) -> None:
        was = self._loading
        self._loading = True
        try:
            current = self._selected_preset()
            self.presets.clear()
            self._unavailable = set()
            for preset in self._presets:
                problem = _preset_problem(preset)
                item = QListWidgetItem(
                    preset.name if problem is None else tr(problem).format(name=preset.name)
                )
                item.setData(Qt.ItemDataRole.UserRole, preset.name)
                self.presets.addItem(item)
                if problem is not None:
                    self._unavailable.add(preset.name)
            if current is not None:
                for row in range(self.presets.count()):
                    item = self.presets.item(row)
                    if item.data(Qt.ItemDataRole.UserRole) == current:
                        self.presets.setCurrentItem(item)
                        break
        finally:
            self._loading = was
        self.presets.setVisible(bool(self._presets))
        self.presets_empty.setVisible(not self._presets)
        self.preset_count.setText(
            tr("{n} of {max} saved").format(n=len(self._presets), max=VOICE_PRESET_MAX)
        )
        full = len(self._presets) >= VOICE_PRESET_MAX
        self.preset_limit.setVisible(full)
        self.preset_save.setEnabled(not full)
        self._render_preset_buttons()

    def _render_preset_buttons(self) -> None:
        name = self._selected_preset()
        for b in (self.preset_rename, self.preset_delete):
            b.setEnabled(name is not None)
        # F-66: an unavailable model or voice is flagged and never
        # substituted, so applying such a preset is refused rather than
        # silently corrected to a voice the user did not choose.  Renaming
        # and deleting stay available -- deleting it is the likely fix.
        self.preset_apply.setEnabled(name is not None and name not in self._unavailable)

    def _selected_preset(self) -> str | None:
        item = self.presets.currentItem()
        if item is None:
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _on_preset_apply(self) -> None:
        name = self._selected_preset()
        if name:
            self.preset_apply_requested.emit(name)

    def _on_preset_rename(self) -> None:
        """F-66 / 4.1: a rename onto an occupied name is an overwrite.

        ``Settings.rename_preset`` drops the preset that was already called
        this, deliberately and without asking -- the same overwrite
        ``save_preset`` performs -- so the confirmation 4.1 requires for a
        duplicate name has to be given here, on the way in, exactly as it is
        for the Save box.
        """
        name = self._selected_preset()
        if not name:
            return
        new = self.ask_text(tr("Rename"), tr("New name"), name)
        if not new or not new.strip() or new.strip() == name:
            return
        clean = new.strip()
        if any(p.name == clean for p in self._presets) and not self.confirm(
            tr("Replace the preset {name}?").format(name=clean),
            tr("A preset with this name already exists. Renaming replaces its settings."),
            tr("Replace"),
        ):
            return
        self.preset_rename_requested.emit(name, clean)

    def _on_preset_delete(self) -> None:
        name = self._selected_preset()
        if not name:
            return
        if not self.confirm(
            tr("Delete the preset {name}?").format(name=name),
            tr("This cannot be undone."),
            tr("Delete"),
        ):
            return
        self.preset_delete_requested.emit(name)

    def _on_preset_save(self) -> None:
        """4.1: a duplicate name is confirmed, then overwritten.

        The confirmation is here rather than in ``Settings.save_preset``,
        which overwrites without asking on purpose -- by the time the model
        is asked, the answer has been given.
        """
        name = self.preset_name.text().strip()
        if not name:
            return
        if len(self._presets) >= VOICE_PRESET_MAX and all(p.name != name for p in self._presets):
            return
        if any(p.name == name for p in self._presets):
            if not self.confirm(
                tr("Replace the preset {name}?").format(name=name),
                tr("A preset with this name already exists. Saving replaces its settings."),
                tr("Replace"),
            ):
                return
        self.preset_name.clear()
        self.preset_save_requested.emit(name)

    # ==================================================================
    # About (F-75, F-80)
    # ==================================================================

    def _build_about(self) -> QFrame:
        panel, box = self._panel("About")
        box.addWidget(self._brand_lockup())
        box.addWidget(self._label("Installed version {version}", "secondary", version=__version__))

        self.check_version = self._button("Check for a released version")
        check_row = QHBoxLayout()
        check_row.addWidget(self.check_version)
        check_row.addStretch(1)
        box.addLayout(check_row)
        box.addWidget(
            self._label(
                "Nothing is downloaded, installed, or restarted without your consent. "
                "Checking sends only a version query.",
                "muted",
            )
        )
        self.release_note = self._value_label()
        self.release_note.setVisible(False)
        box.addWidget(self.release_note)

        box.addWidget(_separator())
        box.addWidget(self._label("Change history", "section"))
        for version, released, lines in CHANGE_HISTORY:
            box.addWidget(self._value_with_text(f"{version} — {released}", "secondary"))
            for line in lines:
                box.addWidget(self._bullet(line))

        self.check_version.clicked.connect(self.version_check_requested)
        return panel

    def _value_with_text(self, text: str, role: str | None = None) -> QLabel:
        lb = self._value_label(role)
        lb.setText(text)
        return lb

    def set_released_version(self, version: str | None, *, error: str | None = None) -> None:
        """F-75: report what a check found, and download nothing.

        Called only after :attr:`version_check_requested`; there is no timer
        and no start-up call anywhere in this module, because F-75 makes the
        query the user's to ask for.
        """
        self.release_note.setVisible(True)
        if error:
            self.release_note.setText(
                tr("The released version could not be checked: {reason}").format(reason=error)
            )
        elif version and version != __version__:
            self.release_note.setText(
                tr("Version {version} has been released.").format(version=version)
            )
        else:
            self.release_note.setText(tr("This is the newest released version."))

    # ==================================================================
    # Applying settings, and retranslating
    # ==================================================================

    def apply(self, settings: Settings) -> None:
        """Show remembered settings without emitting a change (F-24).

        The view keeps a reference rather than a copy: ``Settings`` is frozen,
        so the window replacing it is the only way this changes, and the
        screen can never disagree with what was saved.
        """
        self._settings = settings
        self._presets = settings.presets
        self._loading = True
        try:
            self.cpu.setValue(
                max(CPU_PERCENT_MIN, min(CPU_PERCENT_MAX, settings.cpu_percent))
            )
            auto = settings.memory_bytes is None
            self.memory_auto.setChecked(auto)
            self.memory.setEnabled(not auto)
            shown = (
                settings.memory_bytes
                if settings.memory_bytes is not None
                else default_memory_bytes(self._machine.total_ram_bytes)
            )
            self.memory.setValue(shown / GIB)
            self.volume.setValue(int(round(settings.volume * 100)))
            self.volume_value.setText(f"{int(round(settings.volume * 100))}%")
            self.muted.setChecked(settings.muted)
            self.autoplay.setChecked(settings.autoplay)
            self.follow.setChecked(settings.follow)
            self.rest.setChecked(settings.rest_enabled)
            self.port.setValue(
                max(MIN_USER_PORT, min(MAX_PORT, settings.rest_port))
            )
            self.mcp.setChecked(settings.mcp_enabled)
            self.credential_days.setValue(
                max(CREDENTIAL_DAYS_MIN, min(CREDENTIAL_DAYS_MAX, settings.credential_days))
            )
            self.retention.setValue(
                max(
                    RETENTION_MIN_BYTES // GB,
                    min(RETENTION_MAX_BYTES // GB, settings.retention_bytes // GB),
                )
            )
        finally:
            self._loading = False
        # A remembered port is one the service was started on; a port typed
        # into the box afterwards is not, so the disclosure follows this and
        # not the control.
        self._service_port = int(self.port.value())
        self._service_port_before = None
        self._render_languages()
        self._render_devices()
        self._render_service()
        self._render_resources()
        self._render_presets()

    @property
    def settings(self) -> Settings:
        return self._settings

    def retranslate(self) -> None:
        """F-86, in place.

        Every literal is re-set from its recorded source, and every rendered
        value is recomputed; the widgets themselves are the same objects, so
        the focused control and the scroll position survive the switch --
        which is the difference between changing a language and losing your
        place.
        """
        for widget, setter, source, fmt in self._translatables:
            getattr(widget, setter)(tr(source).format(**fmt) if fmt else tr(source))
        self.credential_days.setSuffix(" " + tr("days"))
        self._render_client_columns()
        self._render_languages()
        self._render_devices()
        self._render_service()
        self._render_resources()
        self._render_credentials()
        self._render_storage()
        self._render_presets()
        if self._conflict_port is not None and self.conflict.isVisible():
            text = tr("Use port {port} instead").format(port=self._conflict_port)
            self.conflict_use.setText(text)
            self.conflict_use.setAccessibleName(text)

    # -- guarded programmatic changes ------------------------------------

    def _set_checked(self, box: QCheckBox, checked: bool) -> None:
        was, self._loading = self._loading, True
        try:
            box.setChecked(checked)
        finally:
            self._loading = was

    def _set_value(self, spin: QSpinBox, value: int) -> None:
        was, self._loading = self._loading, True
        try:
            spin.setValue(value)
        finally:
            self._loading = was


def _separator() -> QFrame:
    f = QFrame()
    f.setProperty("role", "separator")
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFixedHeight(1)
    return f


def _repolish(widget: QWidget) -> None:
    """Re-evaluate a property selector after the property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _next_port(port: int) -> int:
    """The port to offer after a conflict (F-79).

    One above the occupied one, wrapping to the bottom of the user range at
    the top end.  It is only a suggestion: nothing binds it until the owner
    presses the button, so the choice stays theirs.
    """
    if port >= MAX_PORT:
        return MIN_USER_PORT
    return max(MIN_USER_PORT, port + 1)


def _preset_problem(preset: VoicePreset) -> str | None:
    """F-66's warning: the model or the voice this preset names is gone.

    Checked against the shipped manifest rather than against what is
    downloaded: a model that is merely not prepared yet can still be
    prepared, and flagging that as unavailable would warn about every
    preset on a fresh install.
    """
    voice = preset.voice
    if not MANIFEST.has(voice.model_id):
        return "{name} — this model is not available"
    try:
        MANIFEST.get(voice.model_id).validate_voice(voice.voice_id, voice.gender)
    except EchoActError:
        return "{name} — this voice is not available"
    return None


def _expiry_text(cred: Credential, now: float | None = None) -> str:
    status = cred.status(now)
    if status is CredentialStatus.REVOKED:
        return tr("Revoked")
    if status is CredentialStatus.EXPIRED:
        return tr("Expired")
    return tr("{date} (in {days} days)").format(
        date=_date(cred.expires_at), days=max(0, int(cred.days_until_expiry(now)))
    )


_SCOPE_TITLE: Final[dict[ResetScope, str]] = {
    ResetScope.VOICE_AND_DISPLAY: "Reset voice and display settings",
    ResetScope.INTEGRATIONS: "Revoke integration permissions",
    ResetScope.RETAINED_DATA: "Delete retained data",
    ResetScope.MODEL_CACHE: "Delete the model cache",
}

_SCOPE_ACCEPT: Final[dict[ResetScope, str]] = {
    ResetScope.VOICE_AND_DISPLAY: "Reset",
    ResetScope.INTEGRATIONS: "Revoke all",
    ResetScope.RETAINED_DATA: "Delete",
    ResetScope.MODEL_CACHE: "Delete",
}


def _follow_language(view: SettingsView) -> None:
    """Retranslate this view whenever the display language changes.

    ``i18n.on_change`` keeps its listeners for the life of the process, so
    the hook holds a weak reference and does nothing once the view is gone;
    a bound method here would keep a closed settings screen alive and, worse,
    would eventually call into a deleted C++ object.
    """
    ref = weakref.ref(view)

    def hook(_lang: object) -> None:
        target = ref()
        if target is None or not _alive(target):
            return
        target.retranslate()

    on_change(hook)


def _alive(widget: QWidget) -> bool:
    try:
        from shiboken6 import isValid
    except ImportError:  # pragma: no cover - shiboken ships with PySide6
        return True
    return bool(isValid(widget))


__all__ = [
    "CHANGE_HISTORY",
    "Machine",
    "ResetScope",
    "SettingsView",
    "StorageSizes",
    "directory_bytes",
    "storage_snapshot",
]
