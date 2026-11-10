"""テスト用の最小限なインメモリ Firestore (app.store が使う API のみ実装)."""
from __future__ import annotations

import copy
import uuid

from google.api_core.exceptions import AlreadyExists, NotFound


class FakeSnapshot:
    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocument:
    def __init__(self, client, path):
        self._c = client
        self._path = path
        self.id = path[-1]

    def collection(self, name):
        return FakeCollection(self._c, self._path + (name,))

    def get(self):
        return FakeSnapshot(self, self._c.docs.get(self._path))

    def set(self, data):
        self._c.docs[self._path] = copy.deepcopy(data)

    def create(self, data):
        if self._path in self._c.docs:
            raise AlreadyExists("exists")
        self.set(data)

    def update(self, fields):
        if self._path not in self._c.docs:
            raise NotFound("missing")
        self._c.docs[self._path].update(copy.deepcopy(fields))

    def delete(self):
        self._c.docs.pop(self._path, None)


class FakeCollection:
    def __init__(self, client, path, order=None):
        self._c = client
        self._path = path
        self._order = order

    def document(self, doc_id=None):
        return FakeDocument(self._c, self._path + (doc_id or uuid.uuid4().hex,))

    def order_by(self, field):
        return FakeCollection(self._c, self._path, field)

    def stream(self):
        n = len(self._path)
        snaps = [FakeSnapshot(FakeDocument(self._c, p), d) for p, d in list(self._c.docs.items())
                 if len(p) == n + 1 and p[:n] == self._path]
        if self._order:
            snaps.sort(key=lambda s: s._data[self._order])
        return iter(snaps)


class FakeBatch:
    def __init__(self):
        self._ops = []

    def set(self, ref, data):
        self._ops.append(lambda: ref.set(data))

    def update(self, ref, data):
        self._ops.append(lambda: ref.update(data))

    def delete(self, ref):
        self._ops.append(ref.delete)

    def commit(self):
        for op in self._ops:
            op()


class FakeFirestore:
    def __init__(self):
        self.docs: dict[tuple, dict] = {}

    def collection(self, name):
        return FakeCollection(self, (name,))

    def batch(self):
        return FakeBatch()
