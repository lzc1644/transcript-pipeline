"""内网 PDF 书库：持久资料、页面修订、材料和识别任务。"""
from .store import LibraryError, LibraryStore
from .materials import MaterialsMixin
from .jobs import JobsMixin
from .imports import ImportsMixin


class LibraryService(MaterialsMixin, JobsMixin, ImportsMixin, LibraryStore):
    pass


__all__ = ['LibraryService', 'LibraryError']
