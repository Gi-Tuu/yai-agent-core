"""Subprocess probe for #169: load Panel.qml offscreen and assert CROSS-CORE logic.

Run by pytest in a separate process so Qt teardown cannot crash the main test process.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QML_DIR = ROOT / "shell" / "desktop" / "qml"
PANEL = QML_DIR / "Panel.qml"


class Workbench(QObject):
    crossCoreChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._edges: list[dict] = []

    @Property("QVariantList", notify=crossCoreChanged)
    def crossCoreEdges(self) -> list[dict]:
        return self._edges

    def set_edges(self, edges: list[dict]) -> None:
        self._edges = edges
        self.crossCoreChanged.emit()

    @Property("QVariantList", constant=True)
    def specialistModel(self) -> list[dict]:
        return []

    @Property(str, constant=True)
    def activeSpecialistId(self) -> str:
        return ""

    @Property(str, constant=True)
    def activeName(self) -> str:
        return ""

    @Property(bool, constant=True)
    def activeBusy(self) -> bool:
        return False

    @Property(bool, constant=True)
    def activeCanRetry(self) -> bool:
        return False

    @Property(bool, constant=True)
    def activeCoreEnabled(self) -> bool:
        return True

    @Property(str, constant=True)
    def activeRunMode(self) -> str:
        return "demo"

    @Property(str, constant=True)
    def activePendingKind(self) -> str:
        return ""

    @Property(str, constant=True)
    def activePendingPayload(self) -> str:
        return "{}"

    @Property(str, constant=True)
    def answer(self) -> str:
        return ""

    @Property(str, constant=True)
    def codeVault(self) -> str:
        return "{}"

    @Property(str, constant=True)
    def learningVault(self) -> str:
        return "{}"

    @Property(int, constant=True)
    def nebulaParticles(self) -> int:
        return 0

    @Slot(str, result=bool)
    def startTask(self, _text: str) -> bool:
        return True

    @Slot()
    def cancel(self) -> None:
        pass

    @Slot()
    def retry(self) -> None:
        pass

    @Slot(str)
    def decide(self, _decision: str) -> None:
        pass

    @Slot(str)
    def setActiveSpecialist(self, _sid: str) -> None:
        pass


class Host(QObject):
    @Property(str)
    def capText(self) -> str:
        return ""

    @Property(float)
    def orbPhase(self) -> float:
        return 0.0

    @Property(bool)
    def typing(self) -> bool:
        return False

    @Property(bool)
    def pushUser(self) -> bool:
        return False

    @Slot()
    def startSystemMove(self) -> None:
        pass


def _theme_color(name: str) -> str:
    text = (QML_DIR / "YaiTheme" / "Theme.qml").read_text(encoding="utf-8")
    match = re.search(rf"readonly property color {name}:\s*\"([^\"]+)\"", text)
    assert match, name
    return match.group(1).lower()


def main() -> int:
    app = QGuiApplication([])
    engine = QQmlEngine()
    engine.addImportPath(str(QML_DIR))
    workbench = Workbench()
    host = Host()
    workbench.setParent(engine)
    host.setParent(engine)
    engine.rootContext().setContextProperty("workbench", workbench)
    engine.rootContext().setContextProperty("host", host)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(PANEL)))
    root = component.create(engine.rootContext())
    assert root is not None, [error.toString() for error in component.errors()]
    strip = root.findChild(QObject, "crossCoreStrip")
    assert strip is not None

    assert strip.property("visible") is False

    workbench.set_edges([{"prefix": "sales", "calls": 2, "tokens": 120, "failures": 0}])
    app.processEvents()
    assert strip.property("visible") is True
    summary = str(strip.property("crossCoreSummary"))
    assert "sales·2" in summary and "120t" in summary, summary
    assert strip.property("crossCoreColor").name().lower() == _theme_color("text")

    workbench.set_edges([{"prefix": "notes", "calls": 1, "tokens": 0, "failures": 1}])
    app.processEvents()
    summary = str(strip.property("crossCoreSummary"))
    assert "notes·1" in summary and "✕1" in summary, summary
    assert strip.property("crossCoreColor").name().lower() == _theme_color("bad")

    sys.stdout.write("QML_OK\n")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
