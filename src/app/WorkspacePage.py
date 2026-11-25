"""欢迎页之后、五步流程之前的工作区门槛。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QInputMethodEvent,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyleOptionFrame,
    QVBoxLayout,
    QWidget,
)

from app.workspace import create_workspace, input_dir, list_workspaces, workspace_name


class WorkspaceInputWizard(QDialog):
    """新建工作区后：先原理图 TXT，再 ODB++；每步可 skip。"""

    STEPS = (
        ("schematic", "请导入基于原理图导出的TXT文件"),
        ("odb", "请导入由PCB导出的ODB++文件"),
    )

    def __init__(self, model, parent: QWidget | None = None):
        super().__init__(parent)
        self.model = model
        self._index = 0
        self._finished = False
        self.setObjectName("workspaceImportDialog")
        self.setWindowTitle("导入输入")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedSize(520, 220)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 22, 16)
        root.setSpacing(18)
        self.prompt = QLabel()
        self.prompt.setObjectName("workspaceImportPrompt")
        self.prompt.setWordWrap(True)
        self.prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.prompt, 1)
        self.choose_button = QPushButton("选择文件…")
        self.choose_button.setObjectName("primary")
        self.choose_button.setAccessibleName("选择要导入的文件")
        self.choose_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.choose_button.setFixedHeight(40)
        self.choose_button.clicked.connect(self._choose_file)
        root.addWidget(self.choose_button, 0, Qt.AlignmentFlag.AlignHCenter)
        skip_row = QHBoxLayout()
        skip_row.addStretch(1)
        self.skip_button = QPushButton("skip")
        self.skip_button.setObjectName("workspaceSkip")
        self.skip_button.setAccessibleName("跳过这一步")
        self.skip_button.setToolTip("跳过这一步")
        self.skip_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_button.setFixedHeight(22)
        self.skip_button.clicked.connect(self._skip)
        skip_row.addWidget(self.skip_button, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(skip_row)
        self._render()

    def _kind(self) -> str:
        return self.STEPS[self._index][0]

    def _render(self) -> None:
        kind, text = self.STEPS[self._index]
        self.prompt.setText(text)
        self.choose_button.setAccessibleName(
            "选择原理图 TXT" if kind == "schematic" else "选择 ODB++ 文件"
        )

    def reject(self) -> None:
        return

    def closeEvent(self, event) -> None:
        if self._finished:
            event.accept()
            return
        event.ignore()

    def _skip(self) -> None:
        self._advance()

    def _advance(self) -> None:
        self._index += 1
        if self._index >= len(self.STEPS):
            self._finished = True
            self.accept()
            return
        self._render()

    def _choose_file(self) -> None:
        spec = self.model.INPUT_IMPORT_KINDS[self._kind()]
        initial = input_dir(self.model.base_dir)
        if not initial.is_dir():
            initial = self.model.base_dir
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            spec["title"],
            str(initial),
            spec["filter"],
        )
        if not selected:
            return
        name, error = self.model.import_input_file(self._kind(), selected)
        if error and error.startswith("EXISTS:"):
            dest = error.split(":", 1)[1]
            answer = QMessageBox.question(
                self,
                "文件已存在",
                f"目标已存在：\n{dest}\n\n是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            name, error = self.model.import_input_file(self._kind(), selected, overwrite=True)
        if error:
            from app.file_kinds import import_error_text, import_error_title
            QMessageBox.warning(self, import_error_title(error), import_error_text(error))
            return
        self._advance()


def popup_workspace_menu(
    anchor: QWidget,
    runtime_dir: Path,
    current: Path | None = None,
) -> str | None:
    """在锚点下方弹出工作区名单，选中则返回路径。"""
    paths = list_workspaces(runtime_dir)
    if not paths:
        return None
    menu = QMenu(anchor)
    menu.setObjectName("workspaceChooserMenu")
    current_key = Path(current).resolve() if current else None
    for path in paths:
        action = menu.addAction(workspace_name(path))
        action.setData(str(path))
        if current_key is not None and path.resolve() == current_key:
            action.setCheckable(True)
            action.setChecked(True)
    chosen = menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
    if chosen is None:
        return None
    path = chosen.data()
    return str(path) if path else None


class _NameEdit(QLineEdit):
    """名称框。样式表会把占位符刷淡，空框时自己画深色提示。"""

    _HINT = QColor("#1F2937")
    _INK = QColor("#0F172A")
    _PAPER = QColor("#FFFFFF")

    def __init__(self):
        super().__init__()
        self._preedit = False

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        self._preedit = bool(event.preeditString())
        super().inputMethodEvent(event)

    def paintEvent(self, event) -> None:
        hint = self.placeholderText()
        cover = bool(hint) and not self.text() and not self._preedit
        super().paintEvent(event)
        if not cover:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(self.rect(), self._PAPER)
        painter.setPen(self._HINT)
        painter.setFont(_misans(16, 600, hinting=True))
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        contents = self.style().subElementRect(
            QStyle.SubElement.SE_LineEditContents, option, self
        )
        area = self.rect()
        area.setLeft(contents.left())
        area.setRight(min(self.width() - 2, contents.right()))
        painter.drawText(
            area,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            hint,
        )
        if self.hasFocus():
            caret = self.cursorRect()
            caret.setX(max(1, contents.left() - 2))
            caret.setWidth(1)
            painter.fillRect(caret, self._INK)
        painter.end()


class _DropButton(QPushButton):
    def __init__(self):
        super().__init__("")
        self.setFixedSize(36, 36)

    def paintEvent(self, _event) -> None:
        super().paintEvent(_event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#64748B"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        cx = self.width() / 2
        cy = self.height() / 2 + 1
        painter.drawLine(cx - 5, cy - 2, cx, cy + 3)
        painter.drawLine(cx, cy + 3, cx + 5, cy - 2)
        painter.end()


def _misans(size: int, weight: int, *, hinting: bool = False) -> QFont:
    font = QFont("MiSans VF")
    font.setPixelSize(size)
    font.setWeight(QFont.Weight(weight))
    if hinting:
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        font.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
    else:
        font.setStyleStrategy(
            QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.NoSubpixelAntialias
        )
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


_CHOOSER_ROW = 44
_CHOOSER_VISIBLE = 5


class _WorkspacePopup(QFrame):
    """和输入条等宽的工作区名单。超出固定行数时在框内下滑。"""

    chosen = Signal(str)

    def __init__(self, paths: list[Path], width: int, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("workspaceChooserPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(width)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(0)
        self.list = QListWidget()
        self.list.setObjectName("workspaceChooserList")
        self.list.setFont(_misans(14, 600, hinting=True))
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if not paths:
            item = QListWidgetItem("没有已有工作区")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(QColor("#1F2937"))
            item.setSizeHint(QSize(width, _CHOOSER_ROW))
            self.list.addItem(item)
            shown = 1
        else:
            for path in paths:
                name = workspace_name(path)
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setToolTip(name)
                item.setSizeHint(QSize(width, _CHOOSER_ROW))
                self.list.addItem(item)
            shown = min(len(paths), _CHOOSER_VISIBLE)
            self.list.setCurrentRow(0)
        self.list.setFixedHeight(shown * _CHOOSER_ROW)
        self.setFixedHeight(shown * _CHOOSER_ROW + 14)
        self.list.itemClicked.connect(self._activate)
        self.list.itemActivated.connect(self._activate)
        layout.addWidget(self.list)

    def _activate(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        self.chosen.emit(str(path))
        self.close()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.list.setFocus()


class WorkspacePage(QWidget):
    workspaceChosen = Signal(str)
    workspaceCreated = Signal(str)

    def __init__(self, runtime_dir: Path):
        super().__init__()
        self.runtime_dir = Path(runtime_dir).resolve()
        self.setObjectName("workspacePage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 12, 48, 12)
        root.setSpacing(0)
        root.addStretch(1)

        block = QWidget()
        column = QVBoxLayout(block)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        eyebrow = QHBoxLayout()
        eyebrow.setSpacing(6)
        eyebrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fibocom = QLabel("Fibocom")
        fibocom.setFont(_misans(14, 700))
        fibocom.setStyleSheet("color:#0066FF; background:transparent;")
        auto = QLabel("AutoSI")
        auto.setFont(_misans(14, 500))
        auto.setStyleSheet("color:#334155; background:transparent;")
        eyebrow.addWidget(fibocom)
        eyebrow.addWidget(auto)
        eyebrow_wrap = QHBoxLayout()
        eyebrow_wrap.addStretch(1)
        eyebrow_wrap.addLayout(eyebrow)
        eyebrow_wrap.addStretch(1)
        column.addLayout(eyebrow_wrap)
        column.addSpacing(10)

        title = QLabel("建立工作区")
        title.setFont(_misans(28, 700))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color:#0F172A; background:transparent;")
        column.addWidget(title)
        column.addSpacing(8)

        subtitle = QLabel("输入名称新建，或从列表选择已有项目")
        subtitle.setFont(_misans(14, 500))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color:#94A3B8; background:transparent;")
        column.addWidget(subtitle)
        column.addSpacing(28)

        field = QFrame()
        field.setObjectName("setupField")
        field.setFixedSize(640, 64)
        self.field = field
        glow = QGraphicsDropShadowEffect(field)
        glow.setBlurRadius(22)
        glow.setOffset(0, 6)
        glow.setColor(QColor(59, 130, 246, 56))
        field.setGraphicsEffect(glow)
        row = QHBoxLayout(field)
        row.setContentsMargins(14, 8, 8, 8)
        row.setSpacing(8)
        self.name_edit = _NameEdit()
        self.name_edit.setObjectName("workspaceNameEdit")
        self.name_edit.setFont(_misans(16, 600, hinting=True))
        self.name_edit.setPlaceholderText("输入项目名称，或选择已有工作区")
        self.name_edit.setAccessibleName("新工作区名称")
        self.name_edit.returnPressed.connect(self._create)
        row.addWidget(self.name_edit, 1)
        self.drop_button = _DropButton()
        self.drop_button.setObjectName("setupDrop")
        self.drop_button.setAccessibleName("选择已有工作区")
        self.drop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.drop_button.setFixedSize(36, 36)
        self.drop_button.clicked.connect(self._show_chooser)
        row.addWidget(self.drop_button)
        self.create_button = QPushButton("进入 →")
        self.create_button.setObjectName("setupEnter")
        self.create_button.setAccessibleName("进入工作区")
        self.create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_button.setFixedHeight(44)
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(self._create)
        row.addWidget(self.create_button)
        column.addWidget(field, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(10)

        hints = QHBoxLayout()
        hints.setContentsMargins(4, 0, 4, 0)
        left = QLabel("下拉可拉取已有工作区")
        left.setFont(_misans(12, 500))
        left.setStyleSheet("color:#94A3B8; background:transparent;")
        right = QLabel("直接输入则创建新工作区")
        right.setFont(_misans(12, 500))
        right.setStyleSheet("color:#94A3B8; background:transparent;")
        hints.addWidget(left)
        hints.addStretch(1)
        hints.addWidget(right)
        hint_wrap = QWidget()
        hint_wrap.setFixedWidth(640)
        hint_wrap.setLayout(hints)
        column.addWidget(hint_wrap, 0, Qt.AlignmentFlag.AlignHCenter)

        root.addWidget(block, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch(1)
        self.reload()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        rect = self.rect()
        painter.save()
        painter.translate(rect.center().x(), rect.center().y() - rect.height() * 0.02)
        painter.scale(rect.width() * 0.55, rect.height() * 0.46)
        glow = QRadialGradient(QPointF(0, 0), 1)
        glow.setColorAt(0.0, QColor(214, 230, 255))
        glow.setColorAt(0.4, QColor(232, 241, 255))
        glow.setColorAt(0.75, QColor(248, 250, 255))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(QPointF(0, 0), 1, 1)
        painter.restore()
        painter.end()

    def reload(self) -> None:
        self._paths = list_workspaces(self.runtime_dir)

    def _show_chooser(self) -> None:
        self.reload()
        popup = _WorkspacePopup(self._paths, self.field.width(), self)
        popup.chosen.connect(self.workspaceChosen)
        self._chooser = popup
        popup.adjustSize()
        pos = self.field.mapToGlobal(QPoint(0, self.field.height() + 6))
        screen = self.field.screen()
        if screen is not None:
            bounds = screen.availableGeometry()
            if pos.y() + popup.height() > bounds.bottom():
                pos.setY(self.field.mapToGlobal(QPoint(0, 0)).y() - popup.height() - 6)
            pos.setX(max(bounds.left() + 8, min(pos.x(), bounds.right() - popup.width() - 8)))
        popup.move(pos)
        popup.show()

    def _create(self) -> None:
        try:
            path = create_workspace(self.runtime_dir, self.name_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "无法创建工作区", str(exc))
            return
        self.name_edit.clear()
        self.reload()
        self.workspaceCreated.emit(str(path))
