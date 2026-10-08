"""复用组件：车站自动补全输入框。"""
from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import QCompleter, QLineEdit, QWidget


class StationEdit(QLineEdit):
    """带模糊补全的车站输入框。selected_code() 返回三字码或空串。"""

    def __init__(self, stations, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stations = stations
        self._model = QStringListModel(self)
        completer = QCompleter(self._model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(completer)
        self.textEdited.connect(self._refresh)

    def _refresh(self, text: str) -> None:
        hits = self._stations.search(text)
        self._model.setStringList([s.name for s in hits])

    def selected_code(self) -> str:
        st = self._stations.by_name(self.text().strip())
        return st.code if st else ""

    def set_station(self, name: str) -> None:
        self.setText(name)
