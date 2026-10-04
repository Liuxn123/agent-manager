from __future__ import annotations

import math

from PySide6.QtCore import QByteArray, Qt, QRect, QSize, QPoint, QTimer
from PySide6.QtGui import QIcon, QPainter, QPixmap, QTextDocument, QTextOption, QAbstractTextDocumentLayout, QPalette
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QLayout, QTableWidget, QStyledItemDelegate, QStyleOptionViewItem, QStyle


class WrappingDelegate(QStyledItemDelegate):
    """Use the same text layout for painting and measuring unbroken paths."""
    def document(self, option, index):
        self.initStyleOption(option, index)
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        settings = QTextOption()
        settings.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        document.setDefaultTextOption(settings)
        document.setPlainText(option.text)
        document.setTextWidth(max(20, self.parent().columnWidth(index.column()) - 16))
        return document

    def sizeHint(self, option, index):
        document = self.document(QStyleOptionViewItem(option), index)
        return QSize(self.parent().columnWidth(index.column()), max(42, math.ceil(document.size().height()) + 12))

    def paint(self, painter, option, index):
        prepared = QStyleOptionViewItem(option)
        document = self.document(prepared, index)
        prepared.text = ""
        prepared.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, prepared, painter, prepared.widget)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette = prepared.palette
        if prepared.state & QStyle.StateFlag.State_Selected:
            context.palette.setColor(QPalette.ColorRole.Text, prepared.palette.color(QPalette.ColorRole.HighlightedText))
        rectangle = prepared.rect.adjusted(8, 6, -8, -6)
        painter.save()
        painter.setClipRect(rectangle)
        painter.translate(rectangle.topLeft())
        document.documentLayout().draw(painter, context)
        painter.restore()


class FlowLayout(QLayout):
    """Keep action labels intact and wrap actions when a window gets narrow."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(8)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.arrange(QRect(0, 0, width, 0), True)

    def setGeometry(self, rectangle):
        super().setGeometry(rectangle)
        self.arrange(rectangle, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self.items:
            size = size.expandedTo(item.minimumSize())
        return size

    def arrange(self, rectangle, measuring):
        x, y, height = rectangle.x(), rectangle.y(), 0
        for item in self.items:
            size = item.sizeHint().expandedTo(item.minimumSize())
            if x > rectangle.x() and x + size.width() > rectangle.right() + 1:
                x = rectangle.x()
                y += height + self.spacing()
                height = 0
            if not measuring:
                item.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + self.spacing()
            height = max(height, size.height())
        return y + height - rectangle.y()


class ReadableTable(QTableWidget):
    """Wrap cell text and recompute row height after columns or the view resize."""
    def __init__(self, rows, columns):
        super().__init__(rows, columns)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setItemDelegate(WrappingDelegate(self))
        self.row_timer = QTimer(self)
        self.row_timer.setSingleShot(True)
        self.row_timer.timeout.connect(self.resizeRowsToContents)
        self.horizontalHeader().sectionResized.connect(lambda *_: self.row_timer.start(30))
        self.model().rowsInserted.connect(lambda *_: self.row_timer.start(30))
        self.model().dataChanged.connect(lambda *_: self.row_timer.start(30))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.row_timer.start(30)

PATHS = [
    '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4m-5-12 3 3-3 3m6 0h4"/>',
    '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6h.1M7 17h.1"/>',
    '<path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    '<rect x="4" y="7" width="16" height="14" rx="4"/><path d="M12 3v4M8 12h.1M16 12h.1M8 17h8M1 12h3m16 0h3"/>',
    '<path d="M12 3v12m-4-4 4 4 4-4M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    '<path d="M4 4v16h16M8 14l4-5 4 3 4-7"/>',
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2"/>',
]


def nav_icon(index: int) -> QIcon:
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><g fill="none" stroke="#818baf" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + PATHS[index] + '</g></svg>'
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    return QIcon(pixmap)


def card(title: str, value: str, note: str) -> tuple[QFrame, QLabel, QLabel]:
    panel = QFrame()
    panel.setObjectName("Card")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(22, 18, 22, 18)
    label = QLabel(title)
    label.setObjectName("Subtitle")
    layout.addWidget(label)
    number = QLabel(value)
    number.setObjectName("CardValue")
    layout.addWidget(number)
    detail = QLabel(note)
    detail.setObjectName("Subtitle")
    detail.setWordWrap(True)
    layout.addWidget(detail)
    return panel, number, detail
