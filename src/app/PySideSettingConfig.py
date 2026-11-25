"""AutoSI 的 PySide6 正式 GUI（方案 A：macOS 系统设置式单列分组）。"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QProcess, QProcessEnvironment, QRect, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence,
    QPainter, QPen, QRadialGradient, QShortcut,
)
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QButtonGroup, QRadioButton,
    QGraphicsDropShadowEffect, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from app.AutoSIGuiModel import (
    FUNCTIONS,
    GUI_FUNCTIONS,
    GuiModel,
    runtime_base_dir,
    stackup_hdi_choices,
    stackup_layer_choices,
    stackup_material_choices,
    stackup_package_choices,
    subprocess_environment,
    write_parallel_workers,
    write_settings,
)
from siwave_extractor.stackup_xml import (
    LAYER_TABLE_COLUMNS,
    layer_cell_state,
    layer_role,
    format_layer_thickness,
    layer_step_color,
    parse_layer_thickness,
)
from app.LogTool import save_run_log
from app.WorkspacePage import WorkspaceInputWizard, WorkspacePage, popup_workspace_menu
from app.workspace import OUTPUT_NETS, existing_output_dir, has_workspace_layout, input_dir, sidecar_path, source_root, workspace_name


BLUE = "#007AFF"
BLUE_HOVER = "#0066D6"
BLUE_SOFT = "#EAF4FF"
INK = "#000000"
MUTED = "#000000"
BG = "#F5F5F7"
SURFACE = "#FFFFFF"
BORDER = "#D9D9DE"
GREEN = "#248A3D"
AMBER = "#B35C00"
RED = "#C9342F"


STYLE = f"""
* {{ font-family: "Inter Variable", "MiSans VF", "Microsoft YaHei UI"; color: {INK}; font-weight: 600; }}
QToolTip {{
  color: #0F172A; background-color: #FFFFFF; border: 1px solid #E5E7EB;
  padding: 4px 8px;
}}
QMainWindow, QWidget#root, QWidget#welcomePage, QWidget#workspacePage, QWidget#workspace, QStackedWidget#rootStack {{ background: {BG}; }}
QWidget#welcomePage, QWidget#workspacePage {{ background: #FFFFFF; }}
QFrame#titleBar {{ background: rgba(255,255,255,245); border-bottom: 1px solid #EEF0F3; }}
QFrame#tipBar {{ background: rgba(255,255,255,248); border-top: 1px solid #E5E5EA; }}
QPushButton#menuButton {{
  min-height: 32px; max-height: 32px; min-width: 34px; max-width: 34px;
  padding: 0; border: none; border-radius: 8px; background: transparent;
}}
QPushButton#menuButton:hover {{ background: #F3F4F6; }}
QPushButton#menuButton:focus {{ border: 1px solid #D0D5DD; }}
QPushButton#welcomeGuide {{
  min-height: 44px; max-height: 44px; padding: 0 18px; border-radius: 10px;
  border: 1px solid #E5E7EB; background: #FFFFFF; color: #1F2937;
  font-size: 14px; font-weight: 600;
}}
QPushButton#welcomeGuide:hover {{ background: #F7F8FA; }}
QPushButton#welcomeGuide:focus {{ border: 1px solid #2F6CF6; }}
QPushButton#welcomeEnter {{
  min-height: 44px; max-height: 44px; padding: 0 20px; border-radius: 10px; border: none;
  color: #FFFFFF; font-size: 14px; font-weight: 600;
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1A73FF, stop:1 #0052E0);
}}
QPushButton#welcomeEnter:hover {{
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1668F0, stop:1 #0047C7);
}}
QPushButton#welcomeEnter:focus {{ border: none; }}
QFrame#actionBar {{ background: rgba(255,255,255,248); border-top: 1px solid #E5E5EA; }}
QLabel#licenseStatus[state="valid"] {{ color: {GREEN}; }}
QLabel#licenseStatus[state="warning"] {{ color: {AMBER}; }}
QLabel#licenseStatus[state="error"] {{ color: {RED}; }}
QLabel#licenseStatus[state="dev"] {{ color: {BLUE}; }}
QFrame#sidebar {{ background: rgba(255,255,255,248); border-right: 1px solid #E5E5EA; }}
QFrame#card, QFrame#resultCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 16px; }}
QFrame#stackCard {{ background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px; }}
QLabel#stackTitle {{ color: #0F172A; font-size: 16px; font-weight: 700; background: transparent; }}
QLabel#stackSection {{ color: #0F172A; font-size: 13px; font-weight: 700; background: transparent; }}
QLabel#stackCount {{ color: #334155; font-size: 12px; font-weight: 700; background: transparent; }}
QLabel#stackFoot {{ color: #475569; font-size: 12px; font-weight: 600; background: transparent; }}
QFrame#stackPill {{ background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 8px; }}
QLabel#stackPillLabel {{ color: #334155; font-size: 12px; font-weight: 700; background: transparent; }}
QLabel#stackNote {{ color: #475569; font-size: 12px; font-weight: 600; background: transparent; }}
QFrame#stackRule {{ background: #E5E7EB; border: none; min-height: 1px; max-height: 1px; }}
QTableWidget#stackLayerTable {{ background: #FFFFFF; color: #0F172A; border: none; border-radius: 0; gridline-color: transparent; font-size: 12px; font-weight: 600; }}
QTableWidget#stackLayerTable::item {{ color: #0F172A; padding: 0px; border: none; }}
QLineEdit#stackLayerEdit {{
  color: #0F172A; border: none; border-radius: 0; border-bottom: 2px solid #7DD3FC;
  min-height: 0px; padding: 0 6px; font-size: 12px; font-weight: 600;
}}
QLineEdit#stackLayerEdit:focus {{ border: none; border-radius: 0; border-bottom: 2px solid #7DD3FC; }}
QTableWidget#stackLayerTable QHeaderView::section {{
  background: #F8FAFC; color: #1E293B; font-size: 11px; font-weight: 700;
  border: none; border-bottom: 1px solid #E2E8F0; padding: 2px 6px;
}}
QComboBox#stackPillBox {{ color: #0F172A; border: none; background: transparent; min-height: 26px; max-height: 26px; padding: 0 2px; }}
QComboBox#stackPillBox:focus {{ border: none; }}
QPushButton#stackSave {{
  color: #FFFFFF; background: #2563EB; border: none; border-radius: 8px;
  min-height: 32px; max-height: 32px; padding: 0 14px; font-weight: 600;
}}
QPushButton#stackSave:hover {{ background: #1D4ED8; }}
QPushButton#stackSave:focus {{ border: none; }}
QPushButton#stackReset {{
  color: #334155; background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 8px;
  min-height: 32px; max-height: 32px; padding: 0 12px; font-weight: 600;
}}
QPushButton#stackReset:hover {{ background: #F8FAFC; }}
QPushButton#stackReset:focus {{ border: 1px solid #BFDBFE; }}
QFrame#stackMatItem {{ background: transparent; border: 1px solid transparent; border-radius: 8px; }}
QFrame#stackMatItem[selected="true"] {{ background: #EFF6FF; border: 1px solid #2563EB; }}
QFrame#resultCard[state="success"] {{ border: 1px solid #A8D8B2; background: #FBFFFC; }}
QFrame#resultCard[state="error"] {{ border: 1px solid #E5B1AD; background: #FFFCFB; }}
QFrame#resultCard[state="cancelled"] {{ border: 1px solid #E8C48D; background: #FFFCF5; }}
QFrame#banner {{ background: #FFF2F0; border: 1px solid #F1B5B1; border-radius: 12px; }}
QLabel#eyebrow {{ color: {MUTED}; font-size: 12px; font-weight: 700; }}
QLabel#pageTitle {{ font-size: 30px; font-weight: 700; }}
QLabel#cardTitle {{ font-size: 17px; font-weight: 700; }}
QLabel#muted, QLabel#detail {{ color: {MUTED}; font-size: 13px; }}
QLabel#success {{ color: {GREEN}; font-weight: 600; }}
QLabel#warning {{ color: {AMBER}; font-weight: 600; }}
QLabel#runMetric {{ color: #52525B; font-size: 12px; font-weight: 600; }}
QLabel#shortcutKey {{ color: #1F2937; background: #F2F2F7; border: 1px solid #D7D7DC; border-radius: 5px; padding: 2px 5px; font-family: Consolas; font-size: 10px; font-weight: 700; }}
QScrollArea#shortcutScroll {{ border: none; background: transparent; }}
QWidget#shortcutPanel {{ background: transparent; }}
QPushButton {{ min-height: 36px; padding: 0 14px; border-radius: 10px; border: 1px solid {BORDER}; background: {SURFACE}; }}
QPushButton:hover {{ background: #F2F2F7; }}
QPushButton:focus {{ border: 2px solid {BLUE}; }}
QPushButton#primary {{ color: white; background: {BLUE}; border: none; min-height: 44px; font-weight: 700; }}
QPushButton#primary:hover {{ background: {BLUE_HOVER}; }}
QPushButton#primary:disabled {{ background: #B7CDE5; color: #F4F7FA; }}
QPushButton#stop {{ color: {RED}; border: 1px solid #E1A4A0; background: #FFF8F7; font-weight: 700; }}
QPushButton#stop:hover {{ background: #FFEDEC; }}
QPushButton#stop:disabled {{ color: #A98B89; background: #F5F1F1; border-color: #DDD1D0; }}
QPushButton#nav {{ text-align: left; border: none; background: transparent; min-height: 46px; padding: 0 12px; font-weight: 600; }}
QPushButton#nav:hover {{ background: #F2F2F7; }}
QPushButton#nav[active="true"] {{ color: {BLUE}; background: {BLUE_SOFT}; }}
QComboBox, QLineEdit {{ min-height: 38px; padding: 0 10px; border-radius: 9px; border: 1px solid {BORDER}; background: #FAFAFC; selection-background-color: #D6EAFF; selection-color: {INK}; }}
QTableWidget {{ background: #FAFAFC; border: 1px solid {BORDER}; border-radius: 10px; gridline-color: #E5E5EA; }}
QTableWidget::item {{ padding: 4px; }}
QHeaderView::section {{ background: #F2F2F7; border: none; border-right: 1px solid #E5E5EA; border-bottom: 1px solid #E5E5EA; padding: 6px; font-weight: 700; }}
QComboBox:focus, QLineEdit:focus {{ border: 2px solid {BLUE}; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{ selection-background-color: #D6EAFF; selection-color: {INK}; outline: 0; }}
QCheckBox {{ spacing: 9px; min-height: 30px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; }}
QRadioButton {{ spacing: 8px; min-height: 28px; }}
QRadioButton::indicator {{ width: 16px; height: 16px; }}
QTextEdit {{ background: #111318; color: #D9E0E8; border: 1px solid #2B2F36; border-radius: 14px; padding: 10px; font-family: Consolas; font-size: 12px; }}
QProgressBar {{ min-height: 5px; max-height: 5px; border: none; border-radius: 2px; background: #E5E5EA; }}
QProgressBar::chunk {{ background: {BLUE}; border-radius: 2px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ width: 10px; background: transparent; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C7C7CC; min-height: 30px; border-radius: 5px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QPushButton#setupBack {{
  min-height: 32px; max-height: 32px; padding: 0 12px; border-radius: 10px;
  border: 1px solid #E5E7EB; background: #FFFFFF; color: #4B5563;
  font-size: 13px; font-weight: 600;
}}
QPushButton#setupBack:hover {{ background: #F8FAFC; }}
QPushButton#setupBack:focus {{ border: 1px solid #CBD5E1; }}
QFrame#setupField {{ background: #FFFFFF; border: 2px solid #3B82F6; border-radius: 18px; }}
QLineEdit#workspaceNameEdit {{
  border: none; background: transparent; min-height: 36px; padding: 0 8px;
  font-size: 16px; font-weight: 600; color: #0F172A;
}}
QLineEdit#workspaceNameEdit:focus {{ border: none; }}
QPushButton#setupDrop {{
  min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px;
  padding: 0; border-radius: 10px; border: 1px solid #E5E7EB; background: #F8FAFC;
  color: #64748B; font-size: 12px; font-weight: 600;
}}
QPushButton#setupDrop:hover {{ background: #F1F5F9; }}
QPushButton#setupDrop:focus {{ border: 1px solid #CBD5E1; }}
QPushButton#setupEnter {{
  min-height: 44px; max-height: 44px; padding: 0 18px; border-radius: 12px; border: none;
  color: #FFFFFF; font-size: 14px; font-weight: 600;
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1A73FF, stop:1 #0052E0);
}}
QPushButton#setupEnter:hover {{
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1668F0, stop:1 #0047C7);
}}
QPushButton#setupEnter:focus {{ border: none; }}
QDialog#workspaceImportDialog {{ background: #FFFFFF; }}
QLabel#workspaceImportPrompt {{ font-size: 18px; font-weight: 700; }}
QPushButton#workspaceSkip {{ min-height: 22px; max-height: 22px; padding: 0 8px; border: none; background: transparent; color: #8E8E93; font-size: 11px; font-weight: 600; }}
QPushButton#workspaceSkip:hover {{ color: #4A4A4F; background: transparent; }}
QPushButton#workspaceSkip:focus {{ border: none; }}
QMenu#workspaceChooserMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; padding: 6px; }}
QMenu#workspaceChooserMenu::item {{ padding: 8px 16px; border-radius: 8px; }}
QMenu#workspaceChooserMenu::item:selected {{ background: {BLUE_SOFT}; }}
QFrame#workspaceChooserPopup {{
  background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 14px;
}}
QListWidget#workspaceChooserList {{
  background: transparent; border: none; outline: none;
  font-size: 14px; font-weight: 600; color: #0F172A;
}}
QListWidget#workspaceChooserList::item {{
  padding: 0 16px; border-radius: 8px; color: #0F172A;
}}
QListWidget#workspaceChooserList::item:hover {{ background: #F1F5F9; color: #0F172A; }}
QListWidget#workspaceChooserList::item:selected {{ background: #E8F1FF; color: #0F172A; }}
QListWidget#workspaceChooserList QScrollBar:vertical {{
  width: 8px; background: transparent; margin: 4px 2px 4px 0;
}}
QListWidget#workspaceChooserList QScrollBar::handle:vertical {{
  background: #94A3B8; min-height: 28px; border-radius: 4px;
}}
QListWidget#workspaceChooserList QScrollBar::add-line:vertical,
QListWidget#workspaceChooserList QScrollBar::sub-line:vertical {{ height: 0; }}
QListWidget#workspaceChooserList QScrollBar::add-page:vertical,
QListWidget#workspaceChooserList QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout():
            _clear(item.layout())


def _shadow(widget: QWidget) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(22)
    effect.setOffset(0, 5)
    effect.setColor(QColor(20, 25, 35, 24))
    widget.setGraphicsEffect(effect)


def _label(text: str, name: str = "muted", wrap: bool = False) -> QLabel:
    result = QLabel(text)
    result.setObjectName(name)
    result.setWordWrap(wrap)
    return result


class TitleBar(QFrame):
    sidebarRequested = Signal()
    pinRequested = Signal(bool)
    homeRequested = Signal()
    workspaceRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("titleBar")
        self.setFixedHeight(56)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 18, 0)
        layout.setSpacing(10)
        self.sidebar_button = SidebarToggleButton()
        self.sidebar_button.setObjectName("menuButton")
        self.sidebar_button.setAccessibleName("折叠或展开侧栏")
        self.sidebar_button.setToolTip("折叠侧栏  Ctrl+B")
        self.sidebar_button.setFixedSize(34, 32)
        self.sidebar_button.setFlat(True)
        self.sidebar_button.clicked.connect(self.sidebarRequested)
        layout.addWidget(self.sidebar_button)
        self.brand = QLabel("Fibocom · AutoSI")
        self.brand.setStyleSheet("color:#3C3C43; font-size:13px; font-weight:600; background:transparent;")
        layout.addWidget(self.brand)
        self.home_button = QPushButton("返回")
        self.home_button.setObjectName("homeButton")
        self.home_button.setAccessibleName("返回上一层")
        self.home_button.setToolTip("返回上一层  Ctrl+H")
        self.home_button.setFixedHeight(32)
        self.home_button.clicked.connect(self.homeRequested.emit)
        self.home_button.hide()
        layout.addWidget(self.home_button)
        self.workspace_button = QPushButton("工作区  ▾")
        self.workspace_button.setObjectName("homeButton")
        self.workspace_button.setAccessibleName("切换工作区")
        self.workspace_button.setToolTip("下拉切换工作区")
        self.workspace_button.setFixedHeight(32)
        self.workspace_button.clicked.connect(self.workspaceRequested.emit)
        self.workspace_button.hide()
        layout.addWidget(self.workspace_button)
        layout.addStretch()
        badge = QLabel("INTERNAL")
        badge.setObjectName("internalBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedHeight(22)
        badge.setStyleSheet(
            "color:#FFFFFF; background:#2F6CF6; border-radius:11px;"
            "padding:0 10px; font-size:10px; font-weight:700;"
        )
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(6)
        pin_label = QLabel("置顶")
        pin_label.setStyleSheet("color:#8E8E93; font-size:12px; font-weight:600; background:transparent;")
        layout.addWidget(pin_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.pin_switch = MacSwitch(False, "窗口置顶")
        self.pin_switch.setObjectName("pinSwitch")
        self.pin_switch.setToolTip("保持窗口置顶")
        self.pin_switch.toggled.connect(self.pinRequested.emit)
        layout.addWidget(self.pin_switch, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_home_visible(self, visible: bool) -> None:
        self.home_button.setVisible(visible)

    def set_setup_chrome(self, setup: bool) -> None:
        if setup:
            self.brand.setTextFormat(Qt.TextFormat.RichText)
            self.brand.setText(
                '<span style="color:#0066FF; font-weight:700;">Fibocom</span>'
                '<span style="color:#1F2937; font-weight:600;"> · AutoSI</span>'
            )
            self.brand.setStyleSheet("font-size:13px; background:transparent;")
            self.home_button.setText("← 返回")
            self.home_button.setObjectName("setupBack")
        else:
            self.brand.setTextFormat(Qt.TextFormat.PlainText)
            self.brand.setText("Fibocom · AutoSI")
            self.brand.setStyleSheet("color:#3C3C43; font-size:13px; font-weight:600; background:transparent;")
            self.home_button.setText("返回")
            self.home_button.setObjectName("homeButton")
        self.home_button.style().unpolish(self.home_button)
        self.home_button.style().polish(self.home_button)

    def set_workspace_visible(self, visible: bool, name: str = "") -> None:
        self.workspace_button.setVisible(visible)
        label = name or "工作区"
        self.workspace_button.setText(f"{label}  ▾")


class MarqueeLabel(QWidget):
    """无缝横向滚动；仅在文案真正变化时重设起点，避免中途被“刷新”。"""

    def __init__(self, text: str = ""):
        super().__init__()
        self.setObjectName("tipTicker")
        self._text = ""
        self._offset = 0.0
        self._gap = 72
        self.setFixedHeight(22)
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._advance)
        self.timer.start()
        self.setText(text)

    def setText(self, text: str) -> None:
        text = text or ""
        if text == self._text:
            return
        self._text = text
        self.setAccessibleName(text)
        # 从右侧进入，只在文案变更时重置（库校验完成那一次）
        self._offset = float(max(self.width(), 1))
        self.update()

    def resizeEvent(self, event) -> None:
        # 宽度变化不强制跳回，只在尚未开始时对齐
        if not self._text:
            self._offset = float(event.size().width())
        super().resizeEvent(event)

    def _cycle_width(self) -> float:
        if not self._text:
            return 1.0
        return float(self.fontMetrics().horizontalAdvance(self._text) + self._gap)

    def _advance(self) -> None:
        if not self._text or self.width() <= 0:
            return
        self._offset -= 1.0
        cycle = self._cycle_width()
        # 无缝循环：滚过一整段后接回，不突然跳到屏幕右侧造成“刷新感”
        while self._offset <= -cycle:
            self._offset += cycle
        self.update()

    def paintEvent(self, event) -> None:
        if not self._text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setClipRect(event.rect())
        painter.setPen(QColor(INK))
        metrics = self.fontMetrics()
        baseline = (self.height() + metrics.ascent() - metrics.descent()) // 2
        cycle = self._cycle_width()
        x = self._offset
        # 多画几段，窗口很宽时也能铺满
        while x < self.width():
            painter.drawText(int(x), baseline, self._text)
            x += cycle
            if cycle <= 0:
                break


# 底部滚动条展示的快捷键提示（不再占侧栏空间）
SHORTCUT_TIP = (
    "快捷键：Ctrl+Enter 运行  ·  Ctrl+R 刷新  ·  Ctrl+B 侧栏  ·  "
    "Ctrl+H 返回  ·  Alt+1…5 切换功能  ·  Esc 终止运行"
)

# 与 MAIN.exe / 发布包同目录的操作指导文档
OPS_GUIDE_FILENAME = "自动化仿真操作指导.docx"
OPS_GUIDE_TIP = (
    "操作指导：欢迎页可点「查看操作指导」，"
    f"或打开程序目录下的《{OPS_GUIDE_FILENAME.removesuffix('.docx')}》"
)


def ops_guide_path(base_dir: Path) -> Path:
    root = Path(base_dir).resolve()
    for candidate in (
        root / OPS_GUIDE_FILENAME,
        root / "_internal" / OPS_GUIDE_FILENAME,
        source_root() / OPS_GUIDE_FILENAME,
    ):
        if candidate.is_file():
            return candidate
    return root / OPS_GUIDE_FILENAME


def open_ops_guide(base_dir: Path, parent: QWidget | None = None) -> bool:
    """打开程序目录下的《自动化仿真操作指导.docx》。找不到时提示用户。"""
    path = ops_guide_path(base_dir)
    if path.is_file():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        return True
    message = (
        f"未找到操作指导文件：\n{path}\n\n"
        "请确认该文档与 MAIN.exe 在同一目录（发布包会随包分发）。"
    )
    if parent is not None:
        QMessageBox.warning(parent, "操作指导", message)
    return False


class TipBar(QFrame):
    libraryMessageReady = Signal(str)

    def __init__(self, base_dir: Path, snp_library: Path | None):
        super().__init__()
        self.base_dir = base_dir
        self.snp_library = snp_library
        self.setObjectName("tipBar")
        self.setFixedHeight(36)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.scroll_host = QWidget()
        self.scroll_host.setObjectName("tipNormal")
        scroll = QHBoxLayout(self.scroll_host)
        scroll.setContentsMargins(18, 6, 18, 6)
        scroll.setSpacing(10)
        title = QLabel("提示")
        title.setStyleSheet(f"color:{BLUE}; font-size:11px; font-weight:700;")
        scroll.addWidget(title)
        license_state, license_text = self._license_status(base_dir)
        self.license_label = QLabel(license_text)
        self.license_label.setObjectName("licenseStatus")
        self.license_label.setProperty("state", license_state)
        self.license_label.setAccessibleName("授权状态")
        scroll.addWidget(self.license_label)
        scroll.addWidget(QLabel("|"))
        self.ticker = MarqueeLabel()
        scroll.addWidget(self.ticker, 1)
        self.setup_host = QWidget()
        self.setup_host.setObjectName("tipWelcome")
        setup = QHBoxLayout(self.setup_host)
        setup.setContentsMargins(22, 0, 22, 0)
        setup.setSpacing(16)
        self.setup_license = QLabel(self._setup_license_text(license_state, license_text))
        license_color = {"valid": "#16A34A", "warning": "#B45309", "error": "#DC2626", "dev": "#2563EB"}.get(license_state, "#16A34A")
        self.setup_license.setStyleSheet(
            f"color:{license_color}; font-size:12px; font-weight:600; background:transparent;"
        )
        self.setup_guide = QLabel("操作指导：欢迎页可点「查看操作指导」")
        self.setup_guide.setStyleSheet("color:#9CA3AF; font-size:12px; font-weight:500; background:transparent;")
        setup.addWidget(self.setup_license)
        setup.addStretch(1)
        setup.addWidget(self.setup_guide)
        layout.addWidget(self.scroll_host, 1)
        layout.addWidget(self.setup_host, 1)
        self.setup_host.hide()
        self._messages = [
            "S参数库：正在校验官方完整性...",
            OPS_GUIDE_TIP,
            SHORTCUT_TIP,
        ]
        self.libraryMessageReady.connect(self._set_library_message)
        self.ticker.setText(self._compose_text())
        threading.Thread(target=self._verify_library, daemon=True).start()

    def _compose_text(self) -> str:
        parts = [m.strip() for m in self._messages if m and m.strip()]
        return "          ·          ".join(parts)

    def set_setup_mode(self, enabled: bool) -> None:
        self.scroll_host.setVisible(not enabled)
        self.setup_host.setVisible(enabled)
        self.setFixedHeight(42 if enabled else 36)

    @staticmethod
    def _setup_license_text(state: str, text: str) -> str:
        if state == "valid":
            return f"提示 {text} | 用于仿真。"
        return f"提示 {text}"

    def _set_library_message(self, text: str) -> None:
        self._messages[0] = text
        # 合成长文案一次更新；MarqueeLabel 仅在内容变化时重设，之后连续无缝滚
        self.ticker.setText(self._compose_text())

    @staticmethod
    def _license_status(base_dir: Path, today=None) -> tuple[str, str]:
        if os.environ.get("AUTOSI_DEV") == "1":
            return "dev", "授权：开发模式（未执行正式授权校验）"
        try:
            from datetime import date
            from app.LicenseGuard import verify_runtime_license

            ok, message = verify_runtime_license(base_dir)
            if not ok:
                return "error", f"授权失效：{message.replace(chr(10), ' ')}"
            match = re.search(r"\d{4}-\d{2}-\d{2}", message)
            if not match:
                return "valid", f"授权有效：{message}"
            expiry = date.fromisoformat(match.group())
            days_left = (expiry - (today or date.today())).days
            if 0 <= days_left <= 14:
                return "warning", f"授权临期：剩余 {days_left} 天（至 {expiry.isoformat()}）"
            return "valid", f"授权有效：至 {expiry.isoformat()}"
        except Exception as exc:
            return "error", f"授权状态读取失败：{exc}"

    def _verify_library(self) -> None:
        if os.environ.get("AUTOSI_DEV") == "1":
            message = "S参数库：开发模式已跳过完整性校验。"
        elif not self.snp_library:
            message = "S参数库：未找到。解决：请从部门平台下载官方 S 参数库，并放到 D:\\S参数库、C:\\Project\\S参数库，或程序目录的 S参数库 文件夹。"
        else:
            try:
                from app.SnpLibrarySeal import CACHE_NAME, verify_library
                ok, problems = verify_library(self.snp_library, self.base_dir / CACHE_NAME)
                if ok:
                    message = "S参数库：官方完整性校验通过，可放心用于仿真。"
                else:
                    detail = "；".join(problems[:2])
                    if len(problems) > 2:
                        detail += f"；等共 {len(problems)} 项"
                    message = f"S参数库：非官方或已被篡改（{detail}）。解决：不要继续运行仿真，请删除当前库后从部门平台重新下载官方库；若仍失败，联系管理员重新封库/恢复官方库。"
            except Exception as exc:
                message = f"S参数库：校验失败（{exc}）。解决：请检查库文件是否完整，必要时联系管理员恢复官方库。"
        self.libraryMessageReady.emit(message)

    def set_library(self, snp_library: Path | None) -> None:
        self.snp_library = Path(snp_library) if snp_library else None
        self._messages[0] = "S参数库：正在校验官方完整性..."
        self.ticker.setText(self._compose_text())
        threading.Thread(target=self._verify_library, daemon=True).start()


class Card(QFrame):
    def __init__(self, title: str = "", detail: str = ""):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 18, 20, 18)
        self.body.setSpacing(11)
        if title:
            self.body.addWidget(_label(title, "cardTitle"))
        if detail:
            self.body.addWidget(_label(detail, "detail", True))


class SidebarToggleButton(QPushButton):
    """不依赖字体字形的三横线侧栏按钮。"""

    def __init__(self):
        super().__init__("")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(75, 75, 80), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for y in (12, 16, 20):
            painter.drawLine(11, y, 23, y)


class MacSwitch(QAbstractButton):
    """轻量 macOS 风格双态开关，保留键盘与无障碍 Toggle 行为。"""

    def __init__(self, checked: bool, accessible_name: str):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setAccessibleName(accessible_name)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(42, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(42, 24)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(1, 2, 40, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(BLUE if self.isChecked() else "#C7C7CC"))
        painter.drawRoundedRect(track, 10, 10)
        knob_x = 20 if self.isChecked() else 3
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(knob_x, 4, 16, 16))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(BLUE), 1.5))
            painter.drawRoundedRect(QRectF(0.5, 0.5, 41, 23), 11.5, 11.5)


_LAYER_ROW_PX = 28


def _draw_layer_text(painter: QPainter, rect, text: str, *, tabular: bool) -> None:
    """厚度用等宽数字，小数点在每一层上落在同一条竖线。"""
    if not tabular or not text:
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        return
    metrics = painter.fontMetrics()
    slot = max(metrics.horizontalAdvance(ch) for ch in "0123456789")
    x = rect.left()
    for ch in text:
        if ch.isdigit():
            painter.drawText(
                QRect(x, rect.top(), slot, rect.height()),
                Qt.AlignmentFlag.AlignCenter,
                ch,
            )
            x += slot
        else:
            width = max(1, metrics.horizontalAdvance(ch))
            painter.drawText(
                QRect(x, rect.top(), width + 1, rect.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                ch,
            )
            x += width


class _StackLayerDelegate(QStyledItemDelegate):
    """层叠表自己填色：分档实色，可填格只加一条底边，锁定格画一把小锁。"""

    def paint(self, painter, option, index):
        painter.save()
        painter.setFont(option.font)
        bg = index.data(Qt.ItemDataRole.BackgroundRole)
        color = bg.color() if isinstance(bg, QBrush) else QColor("#FFFFFF")
        painter.fillRect(option.rect, color)
        state = index.data(Qt.ItemDataRole.UserRole) or ""
        if state == "edit":
            y = option.rect.bottom() - 1
            painter.setPen(QPen(QColor("#7DD3FC"), 2))
            painter.drawLine(option.rect.left(), y, option.rect.right(), y)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        fg = index.data(Qt.ItemDataRole.ForegroundRole)
        painter.setPen(fg.color() if isinstance(fg, QBrush) else QColor("#0F172A"))
        right = 22 if state == "lock" else 8
        _draw_layer_text(
            painter,
            option.rect.adjusted(8, 0, -right, 0),
            text,
            tabular=(state == "edit"),
        )
        if state == "lock":
            _paint_lock(painter, option.rect)
        painter.restore()

    def createEditor(self, parent, option, index):
        if index.data(Qt.ItemDataRole.UserRole) != "edit":
            return None
        editor = QLineEdit(parent)
        editor.setObjectName("stackLayerEdit")
        editor.setFrame(False)
        editor.setFont(option.font)
        bg = index.data(Qt.ItemDataRole.BackgroundRole)
        fill = bg.color().name() if isinstance(bg, QBrush) else "#FFFFFF"
        editor.setStyleSheet(
            "QLineEdit#stackLayerEdit {"
            f" background: {fill}; color: #0F172A; border: none; border-radius: 0;"
            " border-bottom: 2px solid #7DD3FC; min-height: 0px; padding: 0 8px; font-weight: 600; }"
            "QLineEdit#stackLayerEdit:focus {"
            f" background: {fill}; border: none; border-bottom: 2px solid #7DD3FC; }}"
        )
        return editor

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def setEditorData(self, editor, index):
        editor.setText(str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
        editor.selectAll()


def _paint_lock(painter: QPainter, rect) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor("#64748B"), 1.2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    x = rect.right() - 16
    y = rect.center().y() - 4
    painter.drawRoundedRect(QRectF(x, y + 3, 8, 6), 1.2, 1.2)
    painter.drawArc(QRectF(x + 1.2, y - 1, 5.6, 5.6), 0, 180 * 16)
    painter.restore()


class ClickSelectCombo(QComboBox):
    """必须点击打开再选；滚轮交给外层滚动，不改当前值。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    def wheelEvent(self, event):
        event.ignore()


class MacComboBox(QComboBox):
    """在 Fusion/高 DPI 下始终绘制清晰的轻量下拉箭头。"""

    def wheelEvent(self, event):
        event.ignore()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isEnabled():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#78787D"), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x = self.width() - 18
        y = self.height() // 2 - 1
        painter.drawLine(x - 4, y - 2, x, y + 2)
        painter.drawLine(x, y + 2, x + 4, y - 2)


_WELCOME_SANS = '"MiSans VF", "Microsoft YaHei UI"'


def _welcome_label(text: str, *, color: str, size: int, weight: int, tracking_em: float = 0.0) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color:{color}; font-size:{size}px; font-weight:{weight}; font-style:normal; "
        f"font-family:{_WELCOME_SANS}; background:transparent;"
    )
    font = QFont("MiSans VF")
    font.setPixelSize(size)
    font.setWeight(QFont.Weight(weight))
    font.setItalic(False)
    # 灰度抗锯齿、不套 ClearType，字形更接近效果图里那版平滑展示字。
    font.setStyleStrategy(
        QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.NoSubpixelAntialias
    )
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if tracking_em:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, tracking_em * size)
    label.setFont(font)
    return label


class WelcomePage(QWidget):
    enterRequested = Signal()

    def __init__(self, base_dir: Path):
        super().__init__()
        self.base_dir = Path(base_dir).resolve()
        self.setObjectName("welcomePage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 12, 48, 12)
        outer.setSpacing(0)
        outer.addStretch(1)

        block = QWidget()
        block.setObjectName("welcomeBlock")
        column = QVBoxLayout(block)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(18)
        brand_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fibocom = _welcome_label("Fibocom", color="#0066FF", size=42, weight=630, tracking_em=0.01)
        fibocom.setObjectName("welcomeFibocom")
        fibocom.setAccessibleName("Fibocom")
        brand_row.addWidget(fibocom, 0, Qt.AlignmentFlag.AlignVCenter)
        divider = QFrame()
        divider.setFixedSize(1, 40)
        divider.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        divider.setStyleSheet("background:#CBD5E1; border:none;")
        brand_row.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)
        product = _welcome_label("AutoSI", color="#0F172A", size=42, weight=700, tracking_em=-0.03)
        product.setObjectName("welcomeProduct")
        product.setAccessibleName("AutoSI")
        brand_row.addWidget(product, 0, Qt.AlignmentFlag.AlignVCenter)
        brand_wrap = QHBoxLayout()
        brand_wrap.addStretch(1)
        brand_wrap.addLayout(brand_row)
        brand_wrap.addStretch(1)
        column.addLayout(brand_wrap)
        column.addSpacing(16)

        title = _welcome_label("仿真自动化工具", color="#0F172A", size=20, weight=600)
        title.setObjectName("welcomeHeadline")
        title.setAccessibleName("仿真自动化工具")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        column.addSpacing(40)

        actions = QHBoxLayout()
        actions.setSpacing(14)
        actions.addStretch(1)
        guide = QPushButton("查看操作指导")
        guide.setObjectName("welcomeGuide")
        guide.setAccessibleName("查看操作指导")
        guide.setToolTip(f"打开程序目录下的 {OPS_GUIDE_FILENAME}")
        guide.setCursor(Qt.CursorShape.PointingHandCursor)
        guide.setFixedHeight(44)
        guide.clicked.connect(self._open_ops_guide)
        actions.addWidget(guide, 0, Qt.AlignmentFlag.AlignVCenter)
        enter = QPushButton("进入 AutoSI →")
        enter.setObjectName("welcomeEnter")
        enter.setAccessibleName("进入 AutoSI")
        enter.setCursor(Qt.CursorShape.PointingHandCursor)
        enter.setFixedHeight(44)
        enter.clicked.connect(self.enterRequested)
        enter.setDefault(True)
        shadow = QGraphicsDropShadowEffect(enter)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 82, 224, 64))
        enter.setGraphicsEffect(shadow)
        actions.addWidget(enter, 0, Qt.AlignmentFlag.AlignVCenter)
        actions.addStretch(1)
        column.addLayout(actions)

        outer.addWidget(block, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        rect = self.rect()
        painter.save()
        painter.translate(rect.center().x(), rect.center().y() - rect.height() * 0.04)
        painter.scale(rect.width() * 0.52, rect.height() * 0.46)
        glow = QRadialGradient(QPointF(0, 0), 1)
        glow.setColorAt(0.0, QColor(206, 224, 255))
        glow.setColorAt(0.34, QColor(228, 238, 255))
        glow.setColorAt(0.68, QColor(244, 248, 255))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(QPointF(0, 0), 1, 1)
        painter.restore()
        painter.end()

    def _open_ops_guide(self) -> None:
        open_ops_guide(self.base_dir, parent=self)


class Sidebar(QFrame):
    functionRequested = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("sidebar")
        self.expanded = True
        self.setFixedWidth(238)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 22, 14, 18)
        layout.setSpacing(6)
        self.heading = _label("AutoSI 流程", "cardTitle")
        layout.addWidget(self.heading)
        self.caption = _label("选择要执行的步骤", "muted")
        layout.addWidget(self.caption)
        layout.addSpacing(12)
        self.buttons: dict[str, QPushButton] = {}
        for key in GUI_FUNCTIONS:
            title, _desc = FUNCTIONS[key]
            button = QPushButton(f"{int(key):02d}   {title}")
            button.setObjectName("nav")
            button.setAccessibleName(f"功能{key} {title}")
            button.clicked.connect(lambda _checked=False, value=key: self.functionRequested.emit(value))
            layout.addWidget(button)
            self.buttons[key] = button
        layout.addStretch(1)
        note = _label("© Fibocom MBB\n仅限内部使用", "muted")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note = note
        layout.addWidget(note)

    def select(self, function: str) -> None:
        for key, button in self.buttons.items():
            button.setProperty("active", key == function)
            button.style().unpolish(button)
            button.style().polish(button)

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self.setFixedWidth(238 if self.expanded else 74)
        self.heading.setVisible(self.expanded)
        self.caption.setVisible(self.expanded)
        self.note.setVisible(self.expanded)
        for key, button in self.buttons.items():
            button.setText(f"{int(key):02d}" if not self.expanded else f"{int(key):02d}   {FUNCTIONS[key][0]}")
            button.setToolTip(FUNCTIONS[key][0] if not self.expanded else "")


class ConfigPage(QWidget):
    runRequested = Signal()
    odbRequested = Signal()
    refreshRequested = Signal()
    snpLibraryChanged = Signal(str)

    def __init__(self, model: GuiModel):
        super().__init__()
        self.model = model
        self.function = "1"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content.setObjectName("configContent")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(34, 28, 42, 30)
        self.content_layout.setSpacing(14)
        self.content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll)
        self.action_bar = QFrame()
        self.action_bar.setObjectName("actionBar")
        action_layout = QVBoxLayout(self.action_bar)
        action_layout.setContentsMargins(34, 10, 42, 14)
        action_layout.setSpacing(8)
        self.warning = QFrame()
        self.warning.setObjectName("banner")
        warning_layout = QHBoxLayout(self.warning)
        warning_layout.setContentsMargins(14, 9, 14, 9)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(f"color:{RED}; font-weight:600;")
        warning_layout.addWidget(self.warning_label)
        action_layout.addWidget(self.warning)
        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self.refresh_button = QPushButton("↻  刷新输入")
        self.refresh_button.setAccessibleName("刷新输入文件")
        self.refresh_button.setToolTip("重新扫描原理图、ODB、SIwave 工程、网络表和 S 参数子项目  Ctrl+R")
        self.refresh_button.clicked.connect(self.refreshRequested)
        action_row.addWidget(self.refresh_button)
        self.run_button = QPushButton()
        self.run_button.setObjectName("primary")
        self.run_button.setToolTip("运行当前功能  Ctrl+Enter")
        self.run_button.clicked.connect(self.runRequested)
        action_row.addWidget(self.run_button, 1)
        action_layout.addLayout(action_row)
        self.refresh_status = _label("", "runMetric", True)
        self.refresh_status.setAccessibleName("输入刷新结果")
        self.refresh_status.hide()
        action_layout.addWidget(self.refresh_status)
        layout.addWidget(self.action_bar)

    def _combo(self, values: list[str], current: str, accessible: str, callback) -> QComboBox:
        combo = MacComboBox()
        combo.setAccessibleName(accessible)
        combo.addItems(values or ["— 暂无可用项 —"])
        combo.setEnabled(len(values) > 1)
        if current in values:
            combo.setCurrentText(current)
        combo.currentTextChanged.connect(callback)
        return combo

    def _combo_with_import(
        self,
        values: list[str],
        current: str,
        accessible: str,
        callback,
        kind: str,
    ) -> tuple[QHBoxLayout, QComboBox, QPushButton]:
        """下拉 +「添加…」：选文件并复制到工程对应目录。"""
        spec = GuiModel.INPUT_IMPORT_KINDS[kind]
        combo = self._combo(values, current, accessible, callback)
        label = str(spec["button"])
        button = QPushButton(label)
        button.setAccessibleName(f"添加{accessible}")
        button.setMinimumWidth(max(72, button.fontMetrics().horizontalAdvance(label) + 24))
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        button.clicked.connect(lambda _checked=False, k=kind: self._import_input_file(k))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(combo, 1)
        row.addWidget(button, 0)
        return row, combo, button

    @staticmethod
    def _reload_combo(combo: QComboBox, values: list[str], current: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values or ["— 暂无可用项 —"])
        combo.setEnabled(len(values) > 1)
        if current in values:
            combo.setCurrentText(current)
        combo.blockSignals(False)

    def _import_input_file(self, kind: str) -> None:
        spec = GuiModel.INPUT_IMPORT_KINDS.get(kind)
        if not spec:
            QMessageBox.warning(self, "导入失败", f"未知输入类型：{kind}")
            return
        initial = (
            existing_output_dir(self.model.base_dir, OUTPUT_NETS)
            if kind == "netlist"
            else input_dir(self.model.base_dir)
        )
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
        name, error = self.model.import_input_file(kind, selected)
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
            name, error = self.model.import_input_file(kind, selected, overwrite=True)
        if error:
            from app.file_kinds import import_error_text, import_error_title
            QMessageBox.warning(self, import_error_title(error), import_error_text(error))
            return
        self._apply_import_result(kind)
        self.refresh_status.setText(f"已添加 {name}")
        self.refresh_status.show()
        self.update_validation()

    def _apply_import_result(self, kind: str) -> None:
        if kind == "schematic" and hasattr(self, "schematic_combo"):
            self._reload_combo(
                self.schematic_combo,
                self.model.schematics,
                self.model.selection.schematic,
            )
        elif kind == "siw" and hasattr(self, "siw_combo"):
            if getattr(self, "_siw_combo_named", False):
                self._refill_data_combo(
                    self.siw_combo,
                    self.model.siw_files,
                    self.model.selection.siw,
                )
            else:
                self._reload_combo(
                    self.siw_combo,
                    self.model.siw_files,
                    self.model.selection.siw,
                )
        elif kind == "netlist" and hasattr(self, "netlist_combo"):
            self._reload_combo(
                self.netlist_combo,
                self.model.netlists,
                self.model.selection.netlist,
            )
        elif kind == "odb" and hasattr(self, "odb_combo"):
            self._reload_combo(
                self.odb_combo,
                self.model.odb_choices(),
                self.model.selection.odb,
            )
            if hasattr(self, "odb_button"):
                self.odb_button.setEnabled(bool(self.model.selection.odb))
            if hasattr(self, "odb_status"):
                self.odb_status.setText(f"已添加 {self.model.selection.odb}")
                self.odb_status.setObjectName("success")
                self.odb_status.style().unpolish(self.odb_status)
                self.odb_status.style().polish(self.odb_status)

    def _header(self) -> None:
        title, detail = FUNCTIONS[self.function]
        self.content_layout.addWidget(_label(f"功能 {self.function}", "eyebrow"))
        self.content_layout.addWidget(_label(title, "pageTitle"))
        self.content_layout.addWidget(_label(detail, "muted"))

    def rebuild(self, function: str) -> None:
        self.function = function
        _clear(self.content_layout)
        if function != "2":
            self._header()
            self.content_layout.addSpacing(5)
        if function in ("1", "5", "6"):
            self._build_schematic()
            if function == "5":
                self._build_refnum_ads()
        elif function == "2":
            self._build_initialization()
        elif function == "3":
            self._build_extraction()
        elif function == "4":
            self._build_ads()
        self.run_button.setText({"1":"生成网络频段信息", "2":"初始化 SIWAVE", "3":"抽取 S 参数", "4":"生成 ADS 工程", "5":"生成 refnum", "6":"导出器件 Pin 路径"}[function])
        self.run_button.setAccessibleName(f"运行功能{function}")
        self.content_layout.addStretch()
        self.update_validation()
        self._configure_tab_order()
        self.scroll.verticalScrollBar().setValue(0)

    def _configure_tab_order(self) -> None:
        form_controls = [
            widget
            for widget in self.content.findChildren(QWidget)
            if isinstance(widget, (QComboBox, QLineEdit, QAbstractButton))
            and widget.focusPolicy() != Qt.FocusPolicy.NoFocus
            and not widget.isHidden()
        ]
        self._tab_order_widgets = form_controls + [
            self.refresh_button,
            self.run_button,
        ]
        for current, following in zip(
            self._tab_order_widgets, self._tab_order_widgets[1:]
        ):
            QWidget.setTabOrder(current, following)

    def _build_schematic(self) -> None:
        card = Card("原理图")
        row, self.schematic_combo, self.schematic_import_button = self._combo_with_import(
            self.model.schematics,
            self.model.selection.schematic,
            "原理图文件",
            self._set_schematic,
            "schematic",
        )
        card.body.addLayout(row)
        self.content_layout.addWidget(card)
        self._build_snp_card()

    def _build_refnum_ads(self) -> None:
        card = Card("器件值")
        source = self.model.selection.refnum_value_source
        ads_on = source != "schematic"
        ads = QRadioButton("附加 ADS 匹配值")
        ads.setAccessibleName("附加 ADS 匹配值")
        sch = QRadioButton("附加原理图原值")
        sch.setAccessibleName("附加原理图原值")
        ads.setChecked(ads_on)
        sch.setChecked(not ads_on)
        group = QButtonGroup(card)
        group.setExclusive(True)
        group.addButton(ads)
        group.addButton(sch)
        self.refnum_value_group = group
        self.refnum_ads_radio = ads
        self.refnum_sch_radio = sch
        card.body.addWidget(ads)
        current = self.model.selection.refnum_ads_workspace.strip()
        if not current:
            found = self.model.discover_ads_workspaces()
            if found:
                current = str(found[0])
                self.model.selection.refnum_ads_workspace = current
        edit = QLineEdit(current)
        edit.setAccessibleName("ADS 工程路径")
        edit.setPlaceholderText("功能4生成的 ADS 工程文件夹")
        edit.textChanged.connect(self._set_refnum_ads)
        self.refnum_ads_edit = edit
        browse = QPushButton("浏览…")
        browse.setAccessibleName("浏览 ADS 工程文件夹")
        browse.setMinimumWidth(72)
        browse.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        browse.clicked.connect(self._browse_refnum_ads)
        self.refnum_ads_browse = browse
        path = QWidget()
        row = QHBoxLayout(path)
        row.setContentsMargins(26, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, 1)
        row.addWidget(browse, 0)
        card.body.addWidget(path)
        card.body.addWidget(sch)
        ads.toggled.connect(self._set_refnum_value_source)
        self._apply_refnum_ads_import(ads_on)
        self.content_layout.addWidget(card)

    def _set_refnum_value_source(self, ads_on: bool) -> None:
        source = "ads" if ads_on else "schematic"
        self.model.selection.refnum_value_source = source
        self.model.selection.refnum_ads_import = source == "ads"
        self._apply_refnum_ads_import(source == "ads")

    def _apply_refnum_ads_import(self, on: bool) -> None:
        for widget in (
            getattr(self, "refnum_ads_edit", None),
            getattr(self, "refnum_ads_browse", None),
        ):
            if widget is not None:
                widget.setEnabled(on)

    def _set_refnum_ads(self, value: str) -> None:
        self.model.selection.refnum_ads_workspace = (value or "").strip()

    def _browse_refnum_ads(self) -> None:
        initial = self.model.selection.refnum_ads_workspace or str(
            existing_output_dir(self.model.base_dir, "ADS")
        )
        selected = QFileDialog.getExistingDirectory(self, "选择 ADS 工程", initial)
        if not selected:
            return
        self.model.selection.refnum_ads_workspace = selected
        if hasattr(self, "refnum_ads_edit"):
            self.refnum_ads_edit.setText(selected)

    def _build_snp_card(self) -> None:
        card = Card("S 参数库")
        values = [str(path) for path in self.model.snp_candidates]
        current = str(self.model.snp_library) if self.model.snp_library else ""
        combo = MacComboBox()
        combo.setAccessibleName("S 参数库路径")
        combo.addItems(values or ["— 未找到，请浏览 —"])
        combo.setEnabled(True)
        if current in values:
            combo.setCurrentText(current)
        combo.currentTextChanged.connect(self._set_snp_library)
        self.snp_combo = combo
        browse = QPushButton("浏览…")
        browse.setAccessibleName("浏览 S 参数库文件夹")
        browse.setMinimumWidth(72)
        browse.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        browse.clicked.connect(self._browse_snp_library)
        self.snp_browse_button = browse
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(combo, 1)
        row.addWidget(browse, 0)
        card.body.addLayout(row)
        if self.model.snp_library:
            card.body.addWidget(_label(f"当前：{self.model.snp_library}", "success", True))
        else:
            card.body.addWidget(_label("未选择官方 S 参数库", "warning", True))
        self.content_layout.addWidget(card)

    def _set_snp_library(self, value: str) -> None:
        text = (value or "").strip()
        if not text or text.startswith("—"):
            return
        error = self.model.set_snp_library(text)
        if error:
            QMessageBox.warning(self, "S 参数库", error)
            return
        self.snpLibraryChanged.emit(str(self.model.snp_library))

    def _browse_snp_library(self) -> None:
        initial = (
            str(self.model.snp_library)
            if self.model.snp_library
            else str(self.model.runtime_dir)
        )
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择 S 参数库文件夹",
            initial,
        )
        if not selected:
            return
        error = self.model.set_snp_library(selected)
        if error:
            QMessageBox.warning(self, "S 参数库", error)
            return
        self.snpLibraryChanged.emit(str(self.model.snp_library))
        self.rebuild(self.function)

    def _set_schematic(self, value: str) -> None:
        if value in self.model.schematics:
            self.model.selection.schematic = value
        self.update_validation()

    @staticmethod
    def _refill_data_combo(combo: QComboBox, values: list[str], current: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        items = list(values)
        if current and current not in items and not str(current).startswith("—"):
            items.insert(0, current)
        if not items:
            combo.addItem("— 暂无可用项 —", "")
            combo.setEnabled(False)
        else:
            for value in items:
                combo.addItem(Path(value).name, value)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.setEnabled(len(values) > 1)
        combo.blockSignals(False)

    def _on_named_siw(self, _index: int) -> None:
        value = self.siw_combo.currentData()
        self._set_siw("" if value is None else str(value))

    def _build_initialization(self) -> None:
        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        title = _label("初始化 SIWAVE", "pageTitle")
        title.setAccessibleName("初始化 SIWAVE")
        eyebrow = _label("功能 2 · 层叠与材料", "eyebrow")
        head.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        head.addWidget(eyebrow, 0, Qt.AlignmentFlag.AlignVCenter)
        self.content_layout.addLayout(head)

        odb_card = Card("ODB++")
        odb_row, self.odb_combo, self.odb_import_button = self._combo_with_import(
            self.model.odb_choices(),
            self.model.selection.odb,
            "ODB++ 文件",
            self._set_odb,
            "odb",
        )
        self.odb_combo.setEnabled(bool(self.model.odb_choices()))
        self.odb_button = QPushButton("生成 SIwave 工程")
        self.odb_button.setEnabled(bool(self.model.selection.odb))
        self.odb_button.setAccessibleName("生成 SIwave 工程")
        self.odb_button.clicked.connect(self.odbRequested)
        odb_row.addWidget(self.odb_button, 0)
        odb_card.body.addLayout(odb_row)
        self.odb_status = _label("", "muted")
        self.odb_status.setWordWrap(True)
        odb_card.body.addWidget(self.odb_status)
        self.content_layout.addWidget(odb_card)

        card = Card()
        top = QHBoxLayout()
        top.setSpacing(28)
        self._siw_combo_named = True
        self.siw_combo = MacComboBox()
        self.siw_combo.setAccessibleName("SIwave 工程")
        self._refill_data_combo(self.siw_combo, self.model.siw_files, self.model.selection.siw)
        self.siw_combo.currentIndexChanged.connect(self._on_named_siw)
        add_label = str(GuiModel.INPUT_IMPORT_KINDS["siw"]["button"])
        self.siw_import_button = QPushButton(add_label)
        self.siw_import_button.setAccessibleName("添加SIwave 工程")
        self.siw_import_button.setMinimumWidth(
            max(72, self.siw_import_button.fontMetrics().horizontalAdvance(add_label) + 24)
        )
        self.siw_import_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.siw_import_button.clicked.connect(lambda: self._import_input_file("siw"))
        siw_row = QHBoxLayout()
        siw_row.setSpacing(8)
        siw_row.addWidget(self.siw_combo, 1)
        siw_row.addWidget(self.siw_import_button, 0)
        siw_host = QWidget()
        siw_col = QVBoxLayout(siw_host)
        siw_col.setContentsMargins(0, 0, 0, 0)
        siw_col.setSpacing(6)
        siw_col.addWidget(_label("SIwave 工程", "cardTitle"))
        siw_col.addLayout(siw_row)
        top.addWidget(siw_host, 1)

        self.deactivate_box = MacSwitch(self.model.selection.deactivate_rlc, "去使能 R / L / C")
        self.deactivate_box.toggled.connect(self._set_deactivate)
        self.stackup_box = MacSwitch(self.model.selection.import_stackup, "导入叠层信息")
        self.stackup_box.toggled.connect(self._set_stackup_enabled)
        switch_row = QHBoxLayout()
        switch_row.setSpacing(22)
        switch_row.addWidget(self._switch_pair("去使能 R / L / C", self.deactivate_box))
        switch_row.addWidget(self._switch_pair("导入叠层信息", self.stackup_box))
        switch_host = QWidget()
        switch_col = QVBoxLayout(switch_host)
        switch_col.setContentsMargins(0, 0, 0, 0)
        switch_col.addStretch(1)
        switch_col.addLayout(switch_row)
        top.addWidget(switch_host, 0, Qt.AlignmentFlag.AlignBottom)
        card.body.addLayout(top)
        self.content_layout.addWidget(card)

        self.stackup_tables = QFrame()
        self.stackup_tables.setObjectName("stackCard")
        self.stackup_tables.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.stackup_tables.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        stack_body = QVBoxLayout(self.stackup_tables)
        stack_body.setContentsMargins(16, 14, 16, 14)
        stack_body.setSpacing(10)
        self.package_combo = self._combo(
            stackup_package_choices(self.model.board_tree),
            self.model.selection.package,
            "封装类型",
            self._set_package,
        )
        self.material_combo = MacComboBox()
        self.layers_combo = MacComboBox()
        self.hdi_combo = MacComboBox()
        self.material_combo.setAccessibleName("板材")
        self.layers_combo.setAccessibleName("层数")
        self.hdi_combo.setAccessibleName("HDI 阶数")
        self.layers_combo.setEditable(True)
        self.hdi_combo.setEditable(True)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(_label("层叠 Stackup", "stackTitle"))
        head.addStretch(1)
        self.stackup_reset = QPushButton("恢复默认")
        self.stackup_reset.setObjectName("stackReset")
        self.stackup_reset.clicked.connect(self._reset_stackup_edits)
        self.stackup_save = QPushButton("保存层叠")
        self.stackup_save.setObjectName("stackSave")
        self.stackup_save.clicked.connect(self._save_stackup)
        head.addWidget(self.stackup_reset)
        head.addWidget(self.stackup_save)
        stack_body.addLayout(head)
        pills = QHBoxLayout()
        pills.setSpacing(8)
        for caption, combo in (
            ("封装", self.package_combo),
            ("板材", self.material_combo),
            ("层数", self.layers_combo),
            ("HDI", self.hdi_combo),
        ):
            pills.addWidget(self._stack_pill(caption, combo))
        self.material_combo.currentTextChanged.connect(self._set_material)
        self.layers_combo.activated.connect(
            lambda _i: self._set_layers(self.layers_combo.currentText())
        )
        self.layers_combo.lineEdit().editingFinished.connect(
            lambda: self._set_layers(self.layers_combo.currentText())
        )
        self.hdi_combo.activated.connect(
            lambda _i: self._set_hdi(self.hdi_combo.currentText())
        )
        self.hdi_combo.lineEdit().editingFinished.connect(
            lambda: self._set_hdi(self.hdi_combo.currentText())
        )
        pills.addStretch(1)
        stack_body.addLayout(pills)
        rule = QFrame()
        rule.setObjectName("stackRule")
        rule.setFixedHeight(1)
        stack_body.addWidget(rule)

        left = QWidget()
        left.setFixedWidth(200)
        left_col = QVBoxLayout(left)
        left_col.setContentsMargins(0, 0, 12, 0)
        left_col.setSpacing(8)
        mat_head = QHBoxLayout()
        mat_head.addWidget(_label("材料", "stackSection"))
        mat_head.addStretch(1)
        self.mat_count = _label("0", "stackCount")
        self._mat_selected = 0
        self._mat_expanded = None
        self._mat_items = []
        self._mat_prop_error = None
        mat_head.addWidget(self.mat_count)
        left_col.addLayout(mat_head)
        self.mat_scroll = QScrollArea()
        self.mat_scroll.setWidgetResizable(True)
        self.mat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.mat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        mat_host = QWidget()
        mat_host.setAutoFillBackground(False)
        self.mat_list_layout = QVBoxLayout(mat_host)
        self.mat_list_layout.setContentsMargins(0, 0, 0, 0)
        self.mat_list_layout.setSpacing(4)
        self.mat_scroll.setWidget(mat_host)
        self.mat_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        viewport = self.mat_scroll.viewport()
        viewport.setAutoFillBackground(True)
        viewport.setStyleSheet("background: #FFFFFF;")
        left_col.addWidget(self.mat_scroll, 1)

        right = QWidget()
        right_col = QVBoxLayout(right)
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(8)
        layer_head = QHBoxLayout()
        layer_head.setSpacing(8)
        layer_head.addWidget(_label("主体 · 层叠表", "stackSection"))
        layer_head.addWidget(_label("材料仅供引用，可增删", "stackNote"))
        layer_head.addStretch(1)
        right_col.addLayout(layer_head)
        self.layer_table = self._stackup_table(160, expand=True)
        self.layer_table.setItemDelegate(_StackLayerDelegate(self.layer_table))
        self.layer_table.viewport().installEventFilter(self)
        self.layer_table.cellChanged.connect(self._on_layer_cell)
        right_col.addWidget(self.layer_table, 1)
        split = QHBoxLayout()
        split.setSpacing(16)
        split.addWidget(left, 0)
        split.addWidget(right, 1)
        stack_body.addLayout(split, 1)
        foot_rule = QFrame()
        foot_rule.setObjectName("stackRule")
        foot_rule.setFixedHeight(1)
        stack_body.addWidget(foot_rule)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 2, 0, 0)
        foot.setSpacing(16)
        foot_left = QWidget()
        foot_left.setFixedWidth(200)
        foot_left_lay = QHBoxLayout(foot_left)
        foot_left_lay.setContentsMargins(0, 0, 12, 0)
        foot_left_lay.setSpacing(0)
        self.stackup_add_mat = QPushButton("＋ 添加材料")
        self.stackup_add_mat.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stackup_add_mat.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.stackup_add_mat.setStyleSheet(
            "QPushButton { min-height: 34px; max-height: 34px; padding: 0 14px;"
            " border: 1px dashed #2563EB; border-radius: 8px; background: transparent;"
            " color: #1D4ED8; font-weight: 700; }"
            "QPushButton:hover { background: #EFF6FF; }"
        )
        self.stackup_add_mat.clicked.connect(self._add_stackup_material)
        foot_left_lay.addWidget(self.stackup_add_mat, 0, Qt.AlignmentFlag.AlignLeft)
        foot_left_lay.addStretch(1)
        foot.addWidget(foot_left, 0)
        foot.addWidget(_label("删除材料前检查是否仍被层叠引用", "stackFoot"), 1)
        stack_body.addLayout(foot)
        self.content_layout.addWidget(self.stackup_tables, 1)
        self._refresh_stackup_combos()
        self.stackup_tables.setVisible(self.model.selection.import_stackup)

    @staticmethod
    def _switch_pair(text: str, switch: MacSwitch) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(QLabel(text))
        row.addWidget(switch)
        return host

    @staticmethod
    def _stack_pill(title: str, combo: QComboBox) -> QWidget:
        frame = QFrame()
        frame.setObjectName("stackPill")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(frame)
        row.setContentsMargins(10, 2, 8, 2)
        row.setSpacing(4)
        row.addWidget(_label(title, "stackPillLabel"))
        combo.setObjectName("stackPillBox")
        combo.setFixedWidth(96)
        row.addWidget(combo)
        return frame

    def _set_odb(self, value: str) -> None:
        if value and not value.startswith("—"):
            error = self.model.set_odb_source(value)
            if error and hasattr(self, "odb_status"):
                self.odb_status.setText(error)
                self.odb_status.setObjectName("warning")
        if hasattr(self, "odb_button"):
            self.odb_button.setEnabled(bool(self.model.selection.odb))

    def _set_siw(self, value: str) -> None:
        if value in self.model.siw_files:
            self.model.selection.siw = value
        self.update_validation()

    def _set_deactivate(self, value: bool) -> None:
        self.model.selection.deactivate_rlc = value
        self.update_validation()

    def _set_stackup_enabled(self, value: bool) -> None:
        self.model.selection.import_stackup = value
        if hasattr(self, "stackup_tables"):
            self.stackup_tables.setVisible(value)
        self.update_validation()

    def _set_package(self, value: str) -> None:
        if not value or value.startswith("—") or value == self.model.selection.package:
            return
        self.model.selection.package = value
        self.model.selection.material = ""
        self.model.selection.layers = ""
        self.model.selection.hdi = ""
        self.model.normalize_stackup()
        self._refresh_stackup_combos(update_package=False)

    def _set_material(self, value: str) -> None:
        if value and value != self.model.selection.material:
            self.model.selection.material = value
            self.model.selection.layers = ""
            self.model.selection.hdi = ""
            self.model.normalize_stackup()
            self._refresh_stackup_combos(update_package=False)

    def _set_layers(self, value: str) -> None:
        if not value or value == self.model.selection.layers:
            return
        self.model.selection.layers = value
        self.model.normalize_stackup()
        self._refresh_stackup_combos(update_package=False, update_material=False)

    def _set_hdi(self, value: str) -> None:
        if not value or value == self.model.selection.hdi:
            return
        self.model.selection.hdi = value
        self._update_stackup_preview()
        self.update_validation()

    @staticmethod
    def _stackup_table(min_h: int, expand: bool = False) -> QTableWidget:
        table = QTableWidget()
        table.setMinimumHeight(min_h)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        if expand:
            table.setObjectName("stackLayerTable")
            table.setShowGrid(False)
            table.setWordWrap(False)
            table.setEditTriggers(
                QAbstractItemView.EditTrigger.DoubleClicked
                | QAbstractItemView.EditTrigger.SelectedClicked
                | QAbstractItemView.EditTrigger.EditKeyPressed
                | QAbstractItemView.EditTrigger.AnyKeyPressed
            )
            table.horizontalHeader().setDefaultAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setStretchLastSection(True)
            table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            table.verticalHeader().setDefaultSectionSize(_LAYER_ROW_PX)
            table.horizontalHeader().setFixedHeight(24)
            table.setMaximumHeight(16777215)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            table.setAlternatingRowColors(False)
        else:
            table.setMaximumHeight(min_h + 80)
        return table

    def _reset_stackup_edits(self) -> None:
        self.model.reload_stackup_doc()
        self._fill_stackup_tables()
        self._update_stackup_edit_status()

    def _capture_stackup_combos(self) -> None:
        s = self.model.selection
        s.package = self.package_combo.currentText().strip()
        s.material = self.material_combo.currentText().strip()
        s.layers = self.layers_combo.currentText().strip()
        s.hdi = self.hdi_combo.currentText().strip()

    def _save_stackup(self) -> None:
        self._capture_stackup_combos()
        try:
            path = self.model.save_custom_stackup()
        except ValueError as exc:
            QMessageBox.warning(self, "保存层叠", str(exc))
            return
        self._refresh_stackup_combos(update_package=False, update_material=False)
        self._update_stackup_edit_status()
        QMessageBox.information(self, "保存层叠", f"已保存，下次可直接再导入：\n{path}")

    def _add_stackup_material(self) -> None:
        if self.model.stackup_doc is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("添加材料")
        dialog.setMinimumWidth(320)
        form = QVBoxLayout(dialog)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(8)
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("必填")
        dk_edit = QLineEdit()
        dk_edit.setPlaceholderText("可空")
        df_edit = QLineEdit()
        df_edit.setPlaceholderText("可空")
        form.addWidget(_label("名称", "muted"))
        form.addWidget(name_edit)
        form.addWidget(_label("Dk", "muted"))
        form.addWidget(dk_edit)
        form.addWidget(_label("Df", "muted"))
        form.addWidget(df_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if ok is not None:
            ok.setText("添加")
        if cancel is not None:
            cancel.setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addWidget(buttons)
        while True:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            error = self._commit_new_material(name_edit.text(), dk_edit.text(), df_edit.text())
            if not error:
                return
            QMessageBox.warning(self, "添加材料", error)

    def _commit_new_material(self, name: str, dk: str, df: str) -> str:
        """加入左侧材料名单。返回错误说明；空字符串表示已加入。"""
        doc = self.model.stackup_doc
        if doc is None:
            return "当前没有叠层。"
        name = name.strip()
        if not name:
            return "请填写材料名称。"
        if name.lower() == "copper":
            return "copper 是导体，不能加进材料名单。"
        if re.fullmatch(r"__new_\d+", name):
            return "请换一个材料名称。"
        existing = {str(row.get("Name") or "").strip() for row in doc.material_rows()}
        if name in existing:
            return f"材料「{name}」已经在名单里。"
        doc.add_material(dk.strip(), df.strip())
        self._mat_selected = len(doc.material_rows()) - 1
        doc.set_material_value(self._mat_selected, "Name", name)
        self._fill_stackup_tables()
        self._update_stackup_edit_status()
        bar = self.mat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        return ""

    def _fill_stackup_tables(self) -> None:
        doc = self.model.stackup_doc
        ready = doc is not None
        if hasattr(self, "stackup_add_mat"):
            self.stackup_add_mat.setEnabled(ready)
        if hasattr(self, "stackup_save"):
            self.stackup_save.setEnabled(ready)
            self.stackup_reset.setEnabled(ready)
        self._fill_material_list()
        if doc is None:
            table = self.layer_table
            table.blockSignals(True)
            table.clear()
            table.setRowCount(0)
            table.setColumnCount(0)
            table.blockSignals(False)
            return
        self._layer_cols = doc.layer_columns()
        self._refresh_layer_table()

    def _material_layer_refs(self, name: str) -> int:
        doc = self.model.stackup_doc
        key = (name or "").strip()
        if doc is None or not key:
            return 0
        count = 0
        for row in doc.layer_rows():
            used = {
                str(row.get("Material") or "").strip(),
                str(row.get("FillMaterial") or "").strip(),
            }
            if key in used:
                count += 1
        return count

    def _remove_stackup_material(self, row: int) -> None:
        doc = self.model.stackup_doc
        if doc is None:
            return
        rows = doc.material_rows()
        if row < 0 or row >= len(rows):
            return
        name = str(rows[row].get("Name") or "").strip()
        refs = self._material_layer_refs(name)
        if refs:
            shown = name or "未命名"
            QMessageBox.warning(
                self,
                "删除材料",
                f"材料「{shown}」仍被 {refs} 层引用。请先在层叠表里改掉 Material 或 FillMaterial，再删除。",
            )
            return
        doc.remove_material(row)
        selected = getattr(self, "_mat_selected", 0) or 0
        if row < selected:
            self._mat_selected = selected - 1
        elif row == selected and selected >= len(rows) - 1:
            self._mat_selected = max(0, selected - 1)
        expanded = getattr(self, "_mat_expanded", None)
        if expanded is not None:
            if row == expanded:
                self._mat_expanded = None
            elif row < expanded:
                self._mat_expanded = expanded - 1
        self._fill_stackup_tables()
        self._update_stackup_edit_status()

    def _fill_material_list(self) -> None:
        layout = getattr(self, "mat_list_layout", None)
        if layout is None:
            return
        _clear(layout)
        self._mat_items = []
        doc = self.model.stackup_doc
        rows = doc.material_rows() if doc is not None else []
        if hasattr(self, "mat_count"):
            self.mat_count.setText(str(len(rows)))
        selected = getattr(self, "_mat_selected", None)
        if rows and (selected is None or selected < 0 or selected >= len(rows)):
            selected = 0
            self._mat_selected = 0
        expanded = getattr(self, "_mat_expanded", None)
        if expanded is not None and (expanded < 0 or expanded >= len(rows)):
            expanded = None
            self._mat_expanded = None
        self._mat_prop_error = None
        for index, row in enumerate(rows):
            raw = str(row.get("Name") or "").strip()
            shown = raw or "未命名"
            host = QFrame()
            host.setObjectName("stackMatItem")
            host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            host.setFrameShape(QFrame.Shape.NoFrame)
            host.setCursor(Qt.CursorShape.PointingHandCursor)
            host.setProperty("selected", index == selected)
            self._paint_mat_item(host, index == selected)
            tip = self._material_tip(row)
            col = QVBoxLayout(host)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            name_row = QWidget()
            name_row.setObjectName("stackMatName")
            name_row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            name_bg = "#EFF6FF" if index == selected else "#FFFFFF"
            name_row.setStyleSheet(f"QWidget#stackMatName {{ background: {name_bg}; }}")
            name_row.setCursor(Qt.CursorShape.PointingHandCursor)
            row_lay = QHBoxLayout(name_row)
            row_lay.setContentsMargins(8, 4, 4, 4)
            row_lay.setSpacing(4)
            label = QLabel(shown)
            label.setStyleSheet(
                "QLabel { color: #0F172A; font-size: 13px; font-weight: 700;"
                f" background: {name_bg}; border: none; }}"
            )
            if tip and index != expanded:
                label.setToolTip(tip)
            label.mousePressEvent = lambda _event, rr=index: self._toggle_material(rr)
            name_row.mousePressEvent = lambda _event, rr=index: self._toggle_material(rr)
            row_lay.addWidget(label, 1)
            button = QPushButton("−")
            button.setToolTip("删除材料")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { border: 1px solid #CBD5E1; border-radius: 6px;"
                " background: #FFFFFF; color: #1E293B;"
                " min-height: 22px; max-height: 22px; min-width: 22px; max-width: 22px;"
                " padding: 0; }"
                "QPushButton:hover { color: #B91C1C; background: #FEF2F2; border-color: #FECACA; }"
            )
            button.clicked.connect(lambda _checked=False, rr=index: self._remove_stackup_material(rr))
            row_lay.addWidget(button, 0)
            col.addWidget(name_row)
            if index == expanded:
                col.addWidget(self._material_prop_panel(index, row))
            layout.addWidget(host)
            self._mat_items.append(host)
        layout.addStretch(1)

    def _material_prop_panel(self, index: int, row: dict) -> QWidget:
        panel = QWidget()
        panel.setObjectName("stackMatProps")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        panel.setStyleSheet("QWidget#stackMatProps { background: #EFF6FF; }")
        form = QVBoxLayout(panel)
        form.setContentsMargins(8, 0, 6, 8)
        form.setSpacing(4)
        field_style = (
            "QLineEdit { color: #0F172A; background: #FFFFFF; border: 1px solid #CBD5E1;"
            " border-radius: 6px; min-height: 28px; max-height: 28px; padding: 0 6px;"
            " font-weight: 600; }"
            "QLineEdit:focus { border: 1px solid #2563EB; }"
        )
        name_edit = QLineEdit(str(row.get("Name") or ""))
        dk_edit = QLineEdit(str(row.get("Permittivity") or ""))
        df_edit = QLineEdit(str(row.get("DielectricLossTangent") or ""))
        name_edit.setObjectName("stackMatNameEdit")
        name_edit.setStyleSheet(field_style)
        dk_edit.setStyleSheet(field_style)
        df_edit.setStyleSheet(field_style)
        name_edit.setPlaceholderText("材料名称")
        dk_edit.setPlaceholderText("例如 3.8")
        df_edit.setPlaceholderText("例如 0.02")
        form.addWidget(self._prop_caption("Name（名称）"))
        form.addWidget(name_edit)
        form.addWidget(self._prop_caption("Dielectric constant（介电常数）"))
        form.addWidget(dk_edit)
        form.addWidget(self._prop_caption("Loss tangent（损耗角正切）"))
        form.addWidget(df_edit)
        error = QLabel("")
        error.setWordWrap(True)
        error.setMaximumHeight(0)
        error.setStyleSheet(
            "QLabel { color: #B91C1C; font-size: 11px; font-weight: 700; background: #EFF6FF; }"
        )
        form.addWidget(error)
        self._mat_prop_error = error
        actions = QHBoxLayout()
        actions.setSpacing(6)
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        for btn, primary in ((cancel, False), (save, True)):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setFixedHeight(28)
            name = "stackMatPropSave" if primary else "stackMatPropCancel"
            btn.setObjectName(name)
            if primary:
                btn.setStyleSheet(
                    "QPushButton#stackMatPropSave { color: #FFFFFF; background: #2563EB;"
                    " border: 1px solid #2563EB; border-radius: 6px; padding: 0 8px;"
                    " min-height: 28px; max-height: 28px; font-weight: 700; outline: none; }"
                    "QPushButton#stackMatPropSave:hover { color: #FFFFFF; background: #1D4ED8;"
                    " border: 1px solid #1D4ED8; min-height: 28px; max-height: 28px; }"
                    "QPushButton#stackMatPropSave:focus { color: #FFFFFF; background: #2563EB;"
                    " border: 1px solid #1D4ED8; min-height: 28px; max-height: 28px; outline: none; }"
                    "QPushButton#stackMatPropSave:pressed { color: #FFFFFF; background: #1E40AF;"
                    " border: 1px solid #1E40AF; min-height: 28px; max-height: 28px; }"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton#stackMatPropCancel { color: #1E293B; background: #FFFFFF;"
                    " border: 1px solid #CBD5E1; border-radius: 6px; padding: 0 8px;"
                    " min-height: 28px; max-height: 28px; font-weight: 600; outline: none; }"
                    "QPushButton#stackMatPropCancel:hover { color: #1E293B; background: #F8FAFC;"
                    " border: 1px solid #94A3B8; min-height: 28px; max-height: 28px; }"
                    "QPushButton#stackMatPropCancel:focus { color: #1E293B; background: #FFFFFF;"
                    " border: 1px solid #94A3B8; min-height: 28px; max-height: 28px; outline: none; }"
                    "QPushButton#stackMatPropCancel:pressed { color: #1E293B; background: #F1F5F9;"
                    " border: 1px solid #64748B; min-height: 28px; max-height: 28px; }"
                )
        cancel.clicked.connect(self._cancel_material_props)
        save.clicked.connect(
            lambda _checked=False, rr=index, name=name_edit, dk=dk_edit, df=df_edit: self._save_material_props(
                rr, name.text(), dk.text(), df.text()
            )
        )
        actions.addWidget(cancel, 1)
        actions.addWidget(save, 1)
        form.addLayout(actions)
        return panel

    @staticmethod
    def _prop_caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            "QLabel { color: #334155; font-size: 11px; font-weight: 700; background: #EFF6FF; }"
        )
        return label

    def _toggle_material(self, index: int) -> None:
        self._mat_selected = index
        if getattr(self, "_mat_expanded", None) == index:
            self._mat_expanded = None
        else:
            self._mat_expanded = index
        self._fill_material_list()

    def _cancel_material_props(self) -> None:
        self._mat_expanded = None
        self._fill_material_list()

    @staticmethod
    def _prop_number(text: str) -> str | None:
        raw = str(text or "").strip()
        if not raw:
            return ""
        try:
            value = float(raw)
        except ValueError:
            return None
        if value < 0:
            return None
        return raw

    def _save_material_props(self, index: int, name: str, permittivity: str, loss: str) -> None:
        doc = self.model.stackup_doc
        if doc is None:
            return
        dk = self._prop_number(permittivity)
        df = self._prop_number(loss)
        error = getattr(self, "_mat_prop_error", None)
        if dk is None or df is None:
            if error is not None:
                error.setText("请填写不小于 0 的数字")
                error.setMaximumHeight(36)
            return
        message = doc.rename_editable_material(index, name)
        if message:
            if error is not None:
                error.setText(message)
                error.setMaximumHeight(36)
            return
        doc.set_material_properties(index, dk, df)
        self._refresh_layer_table()
        self._mat_expanded = None
        self._fill_material_list()
        self._update_stackup_edit_status()

    @staticmethod
    def _paint_mat_item(host: QFrame, selected: bool) -> None:
        host.setProperty("selected", bool(selected))
        if selected:
            host.setStyleSheet(
                "QFrame#stackMatItem { background: #EFF6FF; border: 1px solid #2563EB; border-radius: 8px; }"
                "QWidget#stackMatProps { background: #EFF6FF; }"
            )
        else:
            host.setStyleSheet(
                "QFrame#stackMatItem { background: transparent; border: 1px solid transparent; border-radius: 8px; }"
            )

    @staticmethod
    def _material_tip(row: dict) -> str:
        dk = str(row.get("Permittivity") or "").strip()
        df = str(row.get("DielectricLossTangent") or "").strip()
        parts = []
        if dk:
            parts.append(f"Dk {dk}")
        if df:
            parts.append(f"Df {df}")
        return "  ".join(parts)

    def _refresh_layer_table(self) -> None:
        doc = self.model.stackup_doc
        if doc is None or not hasattr(self, "layer_table"):
            return
        self._layer_cols = list(LAYER_TABLE_COLUMNS)
        self._load_layer_table(doc, doc.layer_rows())
        self._install_material_combos()
        self._expand_layer_table()

    def _install_material_combos(self) -> None:
        doc = self.model.stackup_doc
        table = getattr(self, "layer_table", None)
        cols = getattr(self, "_layer_cols", [])
        if doc is None or table is None:
            return
        choices = doc.fill_material_choices()
        rows = doc.layer_rows()
        table.blockSignals(True)
        for field in ("Material", "FillMaterial"):
            if field not in cols:
                continue
            col = cols.index(field)
            for r, row in enumerate(rows):
                if layer_cell_state(layer_role(row), field) != "combo":
                    continue
                current = str(row.get(field, "") or "")
                combo = ClickSelectCombo()
                combo.setObjectName("stackLayerCombo")
                fill = layer_step_color(layer_role(row), field)
                combo.setStyleSheet(
                    "QComboBox#stackLayerCombo {"
                    f" background: {fill}; color: #0F172A; border: none; border-radius: 0;"
                    " border-bottom: 2px solid #7DD3FC;"
                    " min-height: 0px; padding: 0 6px; font-size: 12px; font-weight: 600; }"
                    "QComboBox#stackLayerCombo:hover, QComboBox#stackLayerCombo:focus {"
                    f" background: {fill}; color: #0F172A; border: none; border-radius: 0;"
                    " border-bottom: 2px solid #7DD3FC; outline: none; }"
                    "QComboBox#stackLayerCombo::drop-down { border: none; width: 16px; background: transparent; }"
                )
                combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                combo.setCursor(Qt.CursorShape.PointingHandCursor)
                items = list(choices)
                if current and current not in items:
                    items.insert(0, current)
                combo.blockSignals(True)
                combo.addItems(items)
                if current:
                    combo.setCurrentText(current)
                combo.blockSignals(False)
                combo.currentTextChanged.connect(
                    lambda text, rr=r, ff=field: self._on_layer_material(rr, ff, text)
                )
                table.setCellWidget(r, col, combo)
                item = table.item(r, col)
                if item is not None:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.blockSignals(False)

    def _on_layer_material(self, row: int, field: str, text: str) -> None:
        doc = self.model.stackup_doc
        if doc is None:
            return
        text = str(text or "").strip()
        if not text:
            return
        rows = doc.layer_rows()
        if row < 0 or row >= len(rows):
            return
        if str(rows[row].get(field) or "") == text:
            return
        doc.set_layer_value(row, field, text)
        QTimer.singleShot(0, self._refresh_after_layer_material)

    def _refresh_after_layer_material(self) -> None:
        self._refresh_layer_table()
        self._update_stackup_edit_status()

    def _expand_layer_table(self) -> None:
        table = getattr(self, "layer_table", None)
        if table is None:
            return
        rows = table.rowCount()
        if rows <= 0:
            table.setMinimumHeight(80)
            return
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for i in range(rows):
            table.setRowHeight(i, _LAYER_ROW_PX)
        height = table.horizontalHeader().height() + 2 * table.frameWidth() + 4
        for i in range(rows):
            height += table.rowHeight(i)
        table.setMinimumHeight(height)
        table.setMaximumHeight(16777215)

    def _load_layer_table(self, doc, rows: list[dict]) -> None:
        table = self.layer_table
        columns = list(LAYER_TABLE_COLUMNS)
        ink = {
            "text": "#0F172A",
            "edit": "#0F172A",
            "combo": "#0F172A",
            "lock": "#64748B",
            "auto": "#94A3B8",
            "blank": "#94A3B8",
        }
        table.blockSignals(True)
        # 下拉框挂在视口上。只清行不会把上一轮的控件删掉，刷新一次就叠一层。
        for child in table.viewport().findChildren(QComboBox):
            if child.objectName() == "stackLayerCombo":
                child.hide()
                child.setParent(None)
                child.deleteLater()
        table.setRowCount(0)
        table.clear()
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(
            ["Thickness (mm)" if name == "Thickness" else name for name in columns]
        )
        table.setRowCount(len(rows))
        table.verticalHeader().setDefaultSectionSize(_LAYER_ROW_PX)
        for r, row in enumerate(rows):
            role = layer_role(row)
            dk, df = doc.layer_dk_df_text(row)
            for c, col in enumerate(columns):
                state = layer_cell_state(role, col)
                if col == "Thickness":
                    text = format_layer_thickness(row.get("Thickness", ""))
                elif col == "Dielectric constant":
                    text = dk
                elif col == "loss tangent":
                    text = df
                elif state == "blank":
                    text = ""
                else:
                    text = str(row.get(col, "") or "")
                item = QTableWidgetItem(text)
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if state == "edit":
                    flags |= Qt.ItemFlag.ItemIsEditable
                item.setFlags(flags)
                item.setBackground(QColor(layer_step_color(role, col)))
                item.setForeground(QColor(ink.get(state, "#0F172A")))
                item.setData(Qt.ItemDataRole.UserRole, state)
                table.setItem(r, c, item)
        table.blockSignals(False)

    def _on_layer_cell(self, row: int, col: int) -> None:
        doc = self.model.stackup_doc
        cols = getattr(self, "_layer_cols", [])
        if doc is None or col >= len(cols):
            return
        field = cols[col]
        if field != "Thickness":
            return
        item = self.layer_table.item(row, col)
        parsed = parse_layer_thickness(item.text() if item else "")
        if parsed is None:
            self._refresh_layer_table()
            return
        doc.set_layer_value(row, field, parsed)
        self._refresh_layer_table()
        self._update_stackup_edit_status()

    def eventFilter(self, watched, event):
        # 表格刷新或页面拆掉时，Qt 仍会把事件送进这里。
        # 此时再调用基类 eventFilter，会碰到已经删除的 C++ 对象并报错。
        try:
            if event.type() != QEvent.Type.MouseMove:
                return False
            if watched is None or not isValid(self) or not isValid(watched):
                return False
            table = getattr(self, "layer_table", None)
            if table is None or not isValid(table):
                return False
            viewport = table.viewport()
            if watched is not viewport or not isValid(viewport):
                return False
            index = table.indexAt(event.position().toPoint())
            state = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else ""
            cursor = {
                "lock": Qt.CursorShape.ForbiddenCursor,
                "edit": Qt.CursorShape.IBeamCursor,
                "combo": Qt.CursorShape.PointingHandCursor,
            }.get(state, Qt.CursorShape.ArrowCursor)
            viewport.setCursor(cursor)
        except RuntimeError:
            return False
        return False

    def _update_stackup_edit_status(self) -> None:
        return

    @staticmethod
    def _set_combo(combo: QComboBox, values: list[str], selected: str, editable: bool = False) -> None:
        combo.blockSignals(True)
        combo.setEditable(editable)
        combo.clear()
        combo.addItems(values)
        combo.setEnabled(True)
        if selected:
            if selected not in values:
                combo.addItem(selected)
            combo.setCurrentText(selected)
        combo.blockSignals(False)

    def _refresh_stackup_combos(self, update_package: bool = True, update_material: bool = True) -> None:
        self.model.normalize_stackup()
        s = self.model.selection
        if update_package:
            self._set_combo(self.package_combo, stackup_package_choices(self.model.board_tree), s.package)
        materials = stackup_material_choices(self.model.board_tree, s.package)
        if update_material:
            self._set_combo(self.material_combo, materials, s.material)
        layers = stackup_layer_choices(self.model.board_tree, s.package, s.material)
        self._set_combo(self.layers_combo, layers, s.layers, editable=True)
        hdis = stackup_hdi_choices(self.model.board_tree, s.package, s.material, s.layers)
        self._set_combo(self.hdi_combo, hdis, s.hdi, editable=True)
        self._update_stackup_preview()

    def _update_stackup_preview(self) -> None:
        if not hasattr(self, "layer_table"):
            return
        self.model.reload_stackup_doc()
        if hasattr(self, "layer_table"):
            self._fill_stackup_tables()
        self._update_stackup_edit_status()

    def _build_extraction(self) -> None:
        self._siw_combo_named = False
        card = Card("输入")
        card.body.addWidget(_label("网络表（功能一产物）", "detail"))
        net_row, self.netlist_combo, self.netlist_import_button = self._combo_with_import(
            self.model.netlists,
            self.model.selection.netlist,
            "网络表（功能一产物）",
            self._set_netlist,
            "netlist",
        )
        card.body.addLayout(net_row)
        card.body.addWidget(_label("SIwave 工程", "detail"))
        siw_row, self.siw_combo, self.siw_import_button = self._combo_with_import(
            self.model.siw_files,
            self.model.selection.siw,
            "SIwave 工程",
            self._set_siw,
            "siw",
        )
        card.body.addLayout(siw_row)
        card.body.addWidget(_label("输出目录", "detail"))
        output = QLineEdit(self.model.selection.output_dir)
        output.setAccessibleName("S 参数输出目录")
        output.textChanged.connect(self._set_output)
        card.body.addWidget(output)
        card.body.addWidget(_label("并行进程数", "detail"))
        workers = MacComboBox()
        workers.setAccessibleName("并行抽取进程数")
        workers.addItems(["1", "2", "3", "4"])
        workers.setCurrentText(str(self.model.selection.workers))
        workers.currentTextChanged.connect(lambda value: setattr(self.model.selection, "workers", int(value)))
        card.body.addWidget(workers)
        self.content_layout.addWidget(card)

    def _set_netlist(self, value: str) -> None:
        if value in self.model.netlists:
            self.model.selection.netlist = value
        self.update_validation()

    def _set_output(self, value: str) -> None:
        self.model.selection.output_dir = value

    def _build_ads(self) -> None:
        # ADS 生成依赖原理图拓扑 + PCB S 参数：两项均在本页配置，不做跨功能 ini 比对
        sch_card = Card("原理图")
        sch_row, self.schematic_combo, self.schematic_import_button = self._combo_with_import(
            self.model.schematics,
            self.model.selection.schematic,
            "原理图文件",
            self._set_schematic,
            "schematic",
        )
        sch_card.body.addLayout(sch_row)
        self.content_layout.addWidget(sch_card)
        self._build_snp_card()
        card = Card("PCB S 参数（功能三产物）")
        combo = self._combo(self.model.subprojects, self.model.selection.subproject, "PCB S 参数（功能三产物）", self._set_subproject)
        card.body.addWidget(combo)
        self.ads_target = _label("", "detail", True)
        card.body.addWidget(self.ads_target)
        self.content_layout.addWidget(card)
        self.match_50r_box = MacSwitch(self.model.selection.match_50r, "天线口自动匹配仿真")
        self.match_50r_box.toggled.connect(self._set_match_50r)
        match_row = QHBoxLayout()
        match_row.addWidget(QLabel("天线口自动匹配仿真"))
        match_row.addStretch()
        match_row.addWidget(self.match_50r_box)
        self.content_layout.addLayout(match_row)
        self.match_50r_hint = _label(
            "将按现有拓扑、从标准电感电容表里选值调 L/C，使天线口尽量靠近 50Ω，带内 VSWR < 1.3 为验收。",
            "detail",
            True,
        )
        self.match_50r_hint.setVisible(self.model.selection.match_50r)
        self.content_layout.addWidget(self.match_50r_hint)
        self._update_ads_target()

    def _set_match_50r(self, value: bool) -> None:
        self.model.selection.match_50r = value
        if hasattr(self, "match_50r_hint"):
            self.match_50r_hint.setVisible(value)

    def _set_subproject(self, value: str) -> None:
        if value in self.model.subprojects:
            self.model.selection.subproject = value
        self._update_ads_target()
        self.update_validation()

    def _update_ads_target(self) -> None:
        if hasattr(self, "ads_target"):
            label, path = self.model.output_target("4")
            self.ads_target.setText(f"{label}：{path}")

    def update_validation(self) -> None:
        if not hasattr(self, "run_button"):
            return
        ok, message = self.model.validate(self.function)
        self.run_button.setEnabled(ok)
        self.warning.setVisible(bool(message))
        self.warning_label.setText(f"⚠  {message}")

    def mark_odb_state(self, state: str, text: str) -> None:
        if not hasattr(self, "odb_status"):
            return
        self.odb_status.setText(text)
        self.odb_status.setObjectName("success" if state == "success" else "warning" if state == "error" else "muted")
        self.odb_status.style().unpolish(self.odb_status)
        self.odb_status.style().polish(self.odb_status)
        idle = state != "running"
        self.odb_button.setEnabled(idle and bool(self.model.selection.odb))
        if hasattr(self, "odb_import_button"):
            self.odb_import_button.setEnabled(idle)
        if hasattr(self, "odb_combo"):
            self.odb_combo.setEnabled(idle and bool(self.model.odb_choices()))

    def show_refresh_result(self, text: str) -> None:
        self.refresh_button.setText("✓  已刷新")
        self.refresh_status.setText(text)
        self.refresh_status.show()
        QTimer.singleShot(1500, lambda: self.refresh_button.setText("↻  刷新输入"))


class RunPage(QWidget):
    backRequested = Signal()
    stopRequested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 28, 42, 30)
        layout.setSpacing(12)
        self.eyebrow = _label("正在准备", "eyebrow")
        layout.addWidget(self.eyebrow)
        self.title = _label("运行 AutoSI", "pageTitle")
        layout.addWidget(self.title)
        self.status = _label("正在启动后台任务…", "muted", True)
        layout.addWidget(self.status)
        self.current_progress = _label("当前进展：等待后台任务输出…", "detail", True)
        layout.addWidget(self.current_progress)
        metrics = QHBoxLayout()
        metrics.setSpacing(18)
        self.elapsed_label = _label("已用时间 00:00", "runMetric")
        self.heartbeat_label = _label("最后更新：尚无输出", "runMetric")
        metrics.addWidget(self.elapsed_label)
        metrics.addWidget(self.heartbeat_label)
        metrics.addStretch()
        layout.addLayout(metrics)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setAccessibleName("运行日志")
        layout.addWidget(self.log, 1)
        self.stop_button = QPushButton("■  终止运行")
        self.stop_button.setObjectName("stop")
        self.stop_button.setToolTip("终止当前运行  Esc")
        self.stop_button.clicked.connect(self.stopRequested)
        layout.addWidget(self.stop_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.result = QFrame()
        self.result.setObjectName("resultCard")
        result_layout = QVBoxLayout(self.result)
        result_layout.setContentsMargins(18, 15, 18, 15)
        self.result_title = _label("运行结果", "cardTitle")
        result_layout.addWidget(self.result_title)
        self.result_detail = _label("", "detail", True)
        result_layout.addWidget(self.result_detail)
        self.open_button = QPushButton("打开结果目录")
        self.open_button.clicked.connect(self._open_result)
        self._result_path: Path | None = None
        result_layout.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.result)
        self.back_button = QPushButton("←  返回配置")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.backRequested)
        layout.addWidget(self.back_button)
        self.result.hide()
        self._started_monotonic = 0.0
        self._last_update_monotonic: float | None = None
        self._running = False
        self._metrics_timer = QTimer(self)
        self._metrics_timer.setInterval(1000)
        self._metrics_timer.timeout.connect(self._update_metrics)

    def begin(self, function: str) -> None:
        self.eyebrow.setText(f"功能 {function}")
        self.title.setText(FUNCTIONS[function][0])
        self.status.setText("正在启动后台任务…")
        self.status.setStyleSheet(f"color:{MUTED};")
        self.progress.setRange(0, 0)
        self.progress.setStyleSheet("")
        self.log.clear()
        self.result.hide()
        self.result.setProperty("state", "")
        self.back_button.setEnabled(False)
        self.stop_button.setText("■  终止运行")
        self.stop_button.setEnabled(True)
        self.stop_button.show()
        self.current_progress.setText("当前进展：等待后台任务输出…")
        self._started_monotonic = time.monotonic()
        self._last_update_monotonic = None
        self._running = True
        self._update_metrics()
        self._metrics_timer.start()

    def mark_stopping(self) -> None:
        self.status.setText("正在终止后台任务…")
        self.status.setStyleSheet(f"color:{AMBER}; font-weight:700;")
        self.stop_button.setText("正在终止…")
        self.stop_button.setEnabled(False)
        self.current_progress.setText("当前进展：正在停止后台任务及其子进程")

    def cancelled(self) -> None:
        self._finish_metrics("状态：已终止")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setStyleSheet(
            f"QProgressBar::chunk {{ background:{AMBER}; border-radius:2px; }}"
        )
        self.status.setText("已由用户终止")
        self.status.setStyleSheet(f"color:{AMBER}; font-weight:700;")
        self.result_title.setText("■  运行已终止")
        self.result_title.setStyleSheet(f"color:{AMBER}; font-weight:700;")
        self.result_detail.setText("后台任务已按用户要求终止，可返回配置后重新运行。")
        self.result.setProperty("state", "cancelled")
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        self.open_button.hide()
        self.result.show()
        self.stop_button.hide()
        self.back_button.setEnabled(True)

    def append(self, text: str) -> None:
        text = re.sub(r"\x1b\[[0-9;]*m", "", text).replace("\r", "\n")
        cursor = self.log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()
        latest = self._latest_progress_line(text)
        if latest:
            if self._running and self.status.text() == "正在启动后台任务…":
                self.status.setText("后台任务运行中")
            self.current_progress.setText(f"当前进展：{latest}")
            self._last_update_monotonic = time.monotonic()
            self._update_metrics()

    @staticmethod
    def _latest_progress_line(text: str) -> str:
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in re.sub(r"\x1b\[[0-9;]*m", "", text).replace("\r", "\n").splitlines()
        ]
        meaningful = [
            line for line in lines
            if line and not re.fullmatch(r"[-=.#*>\s]+", line)
        ]
        if not meaningful:
            return ""
        line = meaningful[-1]
        return line if len(line) <= 120 else line[:119] + "…"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    def _update_metrics(self) -> None:
        if not self._started_monotonic:
            return
        now = time.monotonic()
        self.elapsed_label.setText(
            f"已用时间 {self._format_duration(now - self._started_monotonic)}"
        )
        if self._running:
            if self._last_update_monotonic is None:
                self.heartbeat_label.setText("最后更新：尚无输出")
            else:
                age = max(0, int(now - self._last_update_monotonic))
                self.heartbeat_label.setText(
                    "最后更新：刚刚" if age < 2 else f"最后更新：{age} 秒前"
                )

    def _finish_metrics(self, final_state: str) -> None:
        self._running = False
        self._metrics_timer.stop()
        self._update_metrics()
        self.heartbeat_label.setText(final_state)

    def finish(self, ok: bool, label: str, path: Path, started_at: float) -> None:
        self._finish_metrics("状态：已完成" if ok else "状态：执行失败")
        self.current_progress.setText(
            "当前进展：结果已生成" if ok else "当前进展：任务未完成，请查看日志"
        )
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.status.setText("全部完成" if ok else "执行失败，请查看日志")
        self.status.setStyleSheet(f"color:{GREEN if ok else RED}; font-weight:700;")
        self.result_title.setText("✓  运行完成" if ok else "×  运行未完成")
        self.result_title.setStyleSheet(f"color:{GREEN if ok else RED}; font-weight:700;")
        self.result.setProperty("state", "success" if ok else "error")
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        recent = []
        if path.is_dir():
            try:
                recent = sorted((p for p in path.iterdir() if p.stat().st_mtime >= started_at - 2), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            except OSError:
                recent = []
        detail = f"{label}：{path}"
        if ok:
            detail += "\n" + ("本次更新：" + "；".join(p.name + ("/" if p.is_dir() else "") for p in recent) if recent else "未发现本次新增或更新的文件")
        else:
            detail = "请先查看上方日志中的失败原因，修正输入、文件占用或环境后重试。"
        self.result_detail.setText(detail)
        self.open_button.setVisible(ok)
        self.open_button.setEnabled(ok and path.is_dir())
        if not ok:
            self.progress.setStyleSheet(f"QProgressBar::chunk {{ background:{RED}; border-radius:2px; }}")
        self._result_path = path
        self.result.show()
        self.stop_button.hide()
        self.back_button.setEnabled(True)

    def attach_log(self, log_path: Path) -> None:
        if not log_path:
            return
        detail = self.result_detail.text().strip()
        line = f"日志：{log_path}"
        self.result_detail.setText(f"{detail}\n{line}" if detail else line)
        self._result_path = log_path.parent
        self.open_button.setText("打开日志目录")
        self.open_button.setVisible(True)
        self.open_button.setEnabled(log_path.parent.is_dir())

    def _open_result(self) -> None:
        if self._result_path and self._result_path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result_path)))


class AutoSIWindow(QMainWindow):
    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        start_on_welcome: bool = True,
        settings: QSettings | None = None,
    ):
        super().__init__()
        self.base_dir = Path(base_dir or runtime_base_dir()).resolve()
        self.ui_settings = (
            settings if settings is not None else QSettings("Fibocom", "AutoSI")
        )
        self.model = GuiModel(self.base_dir)
        self.function = "1"
        self.process: QProcess | None = None
        self.process_kind = ""
        self._stop_requested = False
        self._function_log_path: Path | None = None
        self.run_started_at = 0.0
        self.generated_siw = ""
        self._always_on_top = False
        self.setWindowTitle("Fibocom AutoSI")
        self.resize(1100, 680)
        self.setMinimumSize(960, 640)
        icon = next(
            (
                candidate
                for candidate in (
                    self.base_dir / "fibocom.ico",
                    self.base_dir / "_internal" / "fibocom.ico",
                    source_root() / "fibocom.ico",
                )
                if candidate.is_file()
            ),
            self.base_dir / "fibocom.ico",
        )
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.title_bar = TitleBar()
        self.title_bar.sidebarRequested.connect(self.toggle_sidebar)
        self.title_bar.pinRequested.connect(self.set_always_on_top)
        self.title_bar.homeRequested.connect(self.go_back)
        self.title_bar.workspaceRequested.connect(self._show_workspace_switcher)
        root_layout.addWidget(self.title_bar)
        self.root_stack = QStackedWidget()
        self.root_stack.setObjectName("rootStack")
        root_layout.addWidget(self.root_stack, 1)
        self.welcome = WelcomePage(self.base_dir)
        self.welcome.enterRequested.connect(self.enter_workspace)
        self.root_stack.addWidget(self.welcome)
        self.workspace_page = WorkspacePage(self.base_dir)
        self.workspace_page.workspaceChosen.connect(self.open_workspace)
        self.workspace_page.workspaceCreated.connect(self.open_created_workspace)
        self.root_stack.addWidget(self.workspace_page)
        self.workspace = QWidget()
        self.workspace.setObjectName("workspace")
        work_layout = QHBoxLayout(self.workspace)
        work_layout.setContentsMargins(0, 0, 0, 0)
        work_layout.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.functionRequested.connect(self.select_function)
        work_layout.addWidget(self.sidebar)
        self.pages = QStackedWidget()
        self.config_page = ConfigPage(self.model)
        self.config_page.runRequested.connect(self.start_function)
        self.config_page.odbRequested.connect(self.start_odb_import)
        self.config_page.refreshRequested.connect(
            lambda: self.refresh_files(manual=True)
        )
        self.config_page.snpLibraryChanged.connect(self._on_snp_library_changed)
        self.run_page = RunPage()
        self.run_page.backRequested.connect(self.back_to_config)
        self.run_page.stopRequested.connect(self.request_stop)
        self.pages.addWidget(self.config_page)
        self.pages.addWidget(self.run_page)
        work_layout.addWidget(self.pages, 1)
        self.root_stack.addWidget(self.workspace)
        self.tip_bar = TipBar(self.base_dir, self.model.snp_library)
        root_layout.addWidget(self.tip_bar)
        last_function = str(self.ui_settings.value("ui/last_function", "1"))
        self.select_function(last_function if last_function in GUI_FUNCTIONS else "1")
        sidebar_expanded = self.ui_settings.value(
            "ui/sidebar_expanded", True, type=bool
        )
        if self.sidebar.expanded != sidebar_expanded:
            self.sidebar.toggle()
        saved_geometry = self.ui_settings.value("ui/window_geometry")
        if saved_geometry:
            self.restoreGeometry(saved_geometry)
        self.root_stack.setCurrentWidget(self.welcome if start_on_welcome else self.workspace)
        self._sync_home_button()
        self.file_timer = QTimer(self)
        self.file_timer.setInterval(2000)
        self.file_timer.timeout.connect(self.refresh_files)
        self.file_timer.start()
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self.toggle_sidebar)
        QShortcut(QKeySequence("Ctrl+H"), self, activated=self.go_back)
        QShortcut(QKeySequence("Alt+1"), self, activated=lambda: self.select_function("1"))
        QShortcut(QKeySequence("Alt+2"), self, activated=lambda: self.select_function("2"))
        QShortcut(QKeySequence("Alt+3"), self, activated=lambda: self.select_function("3"))
        QShortcut(QKeySequence("Alt+4"), self, activated=lambda: self.select_function("4"))
        QShortcut(QKeySequence("Alt+5"), self, activated=lambda: self.select_function("5"))
        QShortcut(QKeySequence("Alt+6"), self, activated=lambda: self.select_function("6"))
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.trigger_primary_action)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.trigger_primary_action)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.trigger_refresh)
        QShortcut(QKeySequence("Esc"), self, activated=self.request_stop)

    def _sync_home_button(self) -> None:
        current = self.root_stack.currentWidget()
        on_welcome = current is self.welcome
        on_setup = current is self.workspace_page
        self.title_bar.set_home_visible(not on_welcome)
        self.title_bar.set_setup_chrome(on_setup)
        self.tip_bar.set_setup_mode(on_setup)
        on_flow = current is self.workspace
        name = workspace_name(self.model.base_dir) if on_flow and has_workspace_layout(self.model.base_dir) else ""
        self.title_bar.set_workspace_visible(on_flow, name)

    def _is_process_running(self) -> bool:
        return bool(
            self.process
            and self.process.state() != QProcess.ProcessState.NotRunning
        )

    def enter_workspace(self) -> None:
        self.workspace_page.runtime_dir = self.base_dir
        self.workspace_page.reload()
        self.root_stack.setCurrentWidget(self.workspace_page)
        self._sync_home_button()
        self.workspace_page.name_edit.setFocus()

    def open_workspace(self, path: str) -> None:
        self.model.bind_workspace(Path(path))
        self.config_page.rebuild(self.function)
        self.root_stack.setCurrentWidget(self.workspace)
        self._sync_home_button()
        self.config_page.run_button.setFocus()

    def open_created_workspace(self, path: str) -> None:
        self.model.bind_workspace(Path(path))
        if not self._prompt_new_workspace_inputs():
            return
        self.open_workspace(path)

    def _prompt_new_workspace_inputs(self) -> bool:
        wizard = WorkspaceInputWizard(self.model, self)
        return bool(wizard.exec())

    def go_back(self) -> None:
        """返回上一层：工作区 → 新建工作区页 → 欢迎页。"""
        current = self.root_stack.currentWidget()
        if current is self.workspace:
            self.return_to_workspace_page()
        elif current is self.workspace_page:
            self.return_to_welcome()

    def _show_workspace_switcher(self) -> None:
        if self.root_stack.currentWidget() is not self.workspace:
            return
        path = popup_workspace_menu(
            self.title_bar.workspace_button,
            self.base_dir,
            current=self.model.base_dir,
        )
        if path:
            self.switch_workspace(path)

    def switch_workspace(self, path: str) -> None:
        dest = Path(path).resolve()
        if dest == Path(self.model.base_dir).resolve():
            return
        if self._is_process_running():
            answer = QMessageBox.question(
                self,
                "切换工作区",
                "当前有任务正在运行。切换工作区将终止任务，确定吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._terminate_running_process_for_home()
        if self.pages.currentWidget() is not self.config_page:
            self.pages.setCurrentWidget(self.config_page)
        self.open_workspace(str(dest))

    def return_to_workspace_page(self) -> None:
        if self.root_stack.currentWidget() is not self.workspace:
            return
        if self._is_process_running():
            answer = QMessageBox.question(
                self,
                "返回",
                "当前有任务正在运行。返回将终止任务，确定吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._terminate_running_process_for_home()
        if self.pages.currentWidget() is not self.config_page:
            self.pages.setCurrentWidget(self.config_page)
        self.enter_workspace()

    def return_to_welcome(self) -> None:
        """回到欢迎页；运行中需确认并终止任务；返回前切回配置页。"""
        if self.root_stack.currentWidget() is self.welcome:
            return
        if self._is_process_running():
            answer = QMessageBox.question(
                self,
                "返回",
                "当前有任务正在运行。返回将终止任务，确定吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._terminate_running_process_for_home()
        if self.pages.currentWidget() is not self.config_page:
            self.model.refresh()
            self.select_function(self.function)
        else:
            self.pages.setCurrentWidget(self.config_page)
        self.root_stack.setCurrentWidget(self.welcome)
        self._sync_home_button()

    def _terminate_running_process_for_home(self) -> None:
        """终止后台任务并同步收口，避免 finished 槽与返回路径双重处理。"""
        process = self.process
        if not process or process.state() == QProcess.ProcessState.NotRunning:
            return
        kind = self.process_kind
        self._stop_requested = True
        if kind == "function":
            self.run_page.mark_stopping()
            self.run_page.append("\n[用户终止] 返回上一层，正在停止后台任务…\n")
            self._drain_process_output()
        try:
            process.readyReadStandardOutput.disconnect(self._read_process_output)
        except (RuntimeError, TypeError):
            pass
        try:
            process.finished.disconnect(self._process_finished)
        except (RuntimeError, TypeError):
            pass
        try:
            process.errorOccurred.disconnect(self._process_error)
        except (RuntimeError, TypeError):
            pass
        pid = int(process.processId())
        if sys.platform == "win32" and pid > 0:
            QProcess.startDetached("taskkill", ["/PID", str(pid), "/T", "/F"])
        else:
            process.kill()
        process.waitForFinished(2000)
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished(1000)
        if kind == "function":
            self.run_page.cancelled()
            log_path = self._persist_function_log("cancel")
            if log_path:
                self.run_page.attach_log(log_path)
        elif kind == "odb":
            self.config_page.mark_odb_state("error", "已取消：返回时终止了 ODB 导入")
        self.process = None
        self.process_kind = ""
        self._stop_requested = False

    def toggle_sidebar(self) -> None:
        if self.root_stack.currentWidget() is self.workspace:
            self.sidebar.toggle()

    def trigger_primary_action(self) -> None:
        if (
            self.root_stack.currentWidget() is self.workspace
            and self.pages.currentWidget() is self.config_page
            and self.config_page.run_button.isEnabled()
        ):
            self.config_page.run_button.click()

    def trigger_refresh(self) -> None:
        if (
            self.root_stack.currentWidget() is self.workspace
            and self.pages.currentWidget() is self.config_page
            and not self.process
        ):
            self.config_page.refresh_button.click()

    def set_always_on_top(self, enabled: bool) -> None:
        if sys.platform == "win32":
            if QApplication.platformName() == "offscreen":
                self._always_on_top = enabled
                return
            import ctypes
            from ctypes import wintypes

            set_window_pos = ctypes.windll.user32.SetWindowPos
            set_window_pos.argtypes = (
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.UINT,
            )
            set_window_pos.restype = wintypes.BOOL
            insert_after = wintypes.HWND(-1 if enabled else -2)
            flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
            if not set_window_pos(wintypes.HWND(int(self.winId())), insert_after, 0, 0, 0, 0, flags):
                self.title_bar.pin_switch.blockSignals(True)
                self.title_bar.pin_switch.setChecked(self._always_on_top)
                self.title_bar.pin_switch.blockSignals(False)
                QMessageBox.warning(self, "置顶切换失败", "系统未能修改窗口置顶状态，请重试。")
                return
        else:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
            self.show()
        self._always_on_top = enabled

    def _on_snp_library_changed(self, path: str) -> None:
        self.tip_bar.set_library(Path(path) if path else None)

    def select_function(self, function: str) -> None:
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            return
        if function not in GUI_FUNCTIONS:
            function = "1"
        self.function = function
        self.sidebar.select(function)
        self.config_page.rebuild(function)
        self.pages.setCurrentWidget(self.config_page)

    def refresh_files(self, manual: bool = False) -> None:
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            return
        before = (tuple(self.model.schematics), tuple(self.model.odb_files), tuple(self.model.siw_files), tuple(self.model.netlists), tuple(self.model.subprojects))
        self.model.refresh()
        after = (tuple(self.model.schematics), tuple(self.model.odb_files), tuple(self.model.siw_files), tuple(self.model.netlists), tuple(self.model.subprojects))
        on_config_page = (
            self.root_stack.currentWidget() is self.workspace
            and self.pages.currentWidget() is self.config_page
        )
        if (before != after or manual) and on_config_page:
            self.config_page.rebuild(self.function)
        if manual and on_config_page:
            added = sum(len(set(new) - set(old)) for old, new in zip(before, after))
            removed = sum(len(set(old) - set(new)) for old, new in zip(before, after))
            if added or removed:
                parts = []
                if added:
                    parts.append(f"新增 {added} 项")
                if removed:
                    parts.append(f"移除 {removed} 项")
                message = "已刷新输入：" + "，".join(parts)
            else:
                message = "已刷新输入：列表无变化"
            self.config_page.show_refresh_result(message)

    def _run_log_project(self) -> str:
        schematic = self.model.selection.schematic
        if schematic:
            stem = Path(schematic).stem
            idx = stem.upper().find("_SCH")
            return stem[:idx] if idx != -1 else stem
        return workspace_name(self.model.base_dir)

    def _persist_function_log(self, status: str) -> Path | None:
        if self.process_kind != "function" or self._function_log_path is not None:
            return self._function_log_path
        try:
            self._function_log_path = save_run_log(
                self.model.base_dir,
                self.function,
                self.run_page.log.toPlainText(),
                project=self._run_log_project(),
                status=status,
                extra_root=self.model.runtime_dir,
            )
        except OSError:
            return None
        return self._function_log_path

    def _drain_process_output(self) -> None:
        if self.process:
            self._read_process_output()

    def _start_process(self, command: list[str], kind: str) -> None:
        self._stop_requested = False
        self._function_log_path = None
        self.process_kind = kind
        process = QProcess(self)
        self.process = process
        process.setWorkingDirectory(str(self.model.runtime_dir))
        environment = QProcessEnvironment.systemEnvironment()
        for key, value in subprocess_environment(self.model.runtime_dir).items():
            environment.insert(key, value)
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_process_output)
        process.errorOccurred.connect(self._process_error)
        process.finished.connect(self._process_finished)
        process.setProgram(command[0])
        process.setArguments(command[1:])
        process.start()

    def start_function(self) -> None:
        ok, message = self.model.validate(self.function)
        if not ok:
            title = "没有 50R 网络" if "NET_CLASS 50R" in message else "无法运行"
            QMessageBox.warning(self, title, message)
            return
        values = self.model.collect_settings(self.function)
        if values:
            write_settings(self.model.runtime_dir, values)
        if self.function == "3":
            write_parallel_workers(self.model.runtime_dir, self.model.selection.workers)
        command, error = self.model.command_for(self.function)
        if error or not command:
            QMessageBox.warning(self, "无法运行", error or "未生成执行命令")
            return
        self.run_started_at = time.time()
        self.run_page.begin(self.function)
        self.pages.setCurrentWidget(self.run_page)
        self._start_process(command, "function")

    def start_odb_import(self) -> None:
        command, error, output = self.model.odb_import_command()
        if error or not command:
            self.config_page.mark_odb_state("error", error or "无法生成导入命令")
            return
        self.generated_siw = output
        self.config_page.mark_odb_state("running", "正在导入 ODB，请等待 SIwave 完成…")
        self._start_process(command, "odb")

    def _read_process_output(self) -> None:
        if not self.process:
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if self.process_kind == "function":
            self.run_page.append(text)
        elif self.process_kind == "odb":
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                self.config_page.mark_odb_state("running", f"SIwave：{lines[-1][:160]}")

    def _process_error(self, error) -> None:
        if self._stop_requested:
            return
        if self.process_kind == "function":
            self.run_page.append(f"\n启动失败：{error}\n")
            if error == QProcess.ProcessError.FailedToStart:
                log_path = self._persist_function_log("fail")
                if log_path:
                    self.run_page.attach_log(log_path)
        else:
            self.config_page.mark_odb_state("error", f"启动失败：{error}")

    def _process_finished(self, exit_code: int, _status) -> None:
        self._drain_process_output()
        stopped_by_user = self._stop_requested
        ok = exit_code == 0
        if self.process_kind == "odb":
            if ok:
                self.model.refresh()
                if self.generated_siw in self.model.siw_files:
                    self.model.selection.siw = self.generated_siw
                self.config_page.rebuild("2")
                self.config_page.mark_odb_state("success", f"✓ 已生成并选中 {self.generated_siw}")
            else:
                self.config_page.mark_odb_state("error", f"ODB 导入失败（退出码 {exit_code}）")
        else:
            label, path = self.model.output_target(self.function)
            if stopped_by_user:
                self.run_page.append("\n[用户终止] 后台任务已停止。\n")
                self.run_page.cancelled()
                log_path = self._persist_function_log("cancel")
            else:
                self.run_page.finish(ok, label, path, self.run_started_at)
                if ok and self.function == "4":
                    ael = sidecar_path(self.model.runtime_dir, "fix_snp_auto.ael")
                    command = f'load("{str(ael).replace(os.sep, "/")}");'
                    QApplication.clipboard().setText(command)
                    self.run_page.append("\nADS 最后一步命令已复制到剪贴板：\n" + command + "\n")
                log_path = self._persist_function_log("ok" if ok else "fail")
            if log_path and (not ok or stopped_by_user):
                self.run_page.attach_log(log_path)
            if not ok and not stopped_by_user:
                detail = f"功能 {self.function} 未完成（退出码 {exit_code}）。"
                if log_path:
                    detail += f"\n日志已保存：{log_path}"
                QMessageBox.critical(self, "AutoSI 执行失败", detail)
        self.process = None
        self.process_kind = ""
        self._stop_requested = False

    def request_stop(self) -> None:
        if (
            not self.process
            or self.process_kind != "function"
            or self.process.state() == QProcess.ProcessState.NotRunning
            or self._stop_requested
        ):
            return
        self._stop_function_now()

    def _stop_function_now(self) -> None:
        process = self.process
        if (
            not process
            or process.state() == QProcess.ProcessState.NotRunning
            or self._stop_requested
        ):
            return
        self._stop_requested = True
        self.run_page.mark_stopping()
        self.run_page.append("\n[用户终止] 正在停止后台任务，请稍候…\n")
        pid = int(process.processId())
        if sys.platform == "win32" and pid > 0:
            QProcess.startDetached(
                "taskkill", ["/PID", str(pid), "/T", "/F"]
            )
        else:
            process.terminate()
        QTimer.singleShot(1500, lambda: self._force_kill_process(process))

    def _force_kill_process(self, process: QProcess) -> None:
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()

    def back_to_config(self) -> None:
        completed = self.run_page.status.text() == "全部完成"
        next_function = {"1":"2", "2":"3", "3":"4", "4":"5"}.get(self.function) if completed else None
        self.model.refresh()
        self.select_function(next_function or self.function)

    def closeEvent(self, event) -> None:
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            answer = QMessageBox.question(self, "任务仍在运行", "后台任务仍在运行。确定退出并终止任务吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.process.kill()
            self.process.waitForFinished(2000)
        self.ui_settings.setValue("ui/window_geometry", self.saveGeometry())
        self.ui_settings.setValue("ui/sidebar_expanded", self.sidebar.expanded)
        self.ui_settings.setValue("ui/last_function", self.function)
        self.ui_settings.sync()
        event.accept()


def _load_fonts() -> None:
    asset_dir = Path(__file__).resolve().parent.parent / "gui_assets"
    for name in ("InterVariable.ttf", "MiSansVF.ttf"):
        font = asset_dir / name
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    # Qt offscreen 与部分精简目标机不会自动枚举系统字体；显式注册 Windows 自带
    # 微软雅黑，既保证中文渲染，也不把受许可限制的系统字体打进发布包。
    for name in ("msyh.ttc", "msyhbd.ttc", "msyhl.ttc"):
        system_font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if system_font.is_file():
            QFontDatabase.addApplicationFont(str(system_font))


def run_app(base_dir: str | os.PathLike | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AutoSI")
    app.setOrganizationName("Fibocom")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    _load_fonts()
    app.setFont(QFont("Inter Variable", 11, QFont.Weight.DemiBold))
    window = AutoSIWindow(Path(base_dir) if base_dir else None)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run_app())
