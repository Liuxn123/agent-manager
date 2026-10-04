from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

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
