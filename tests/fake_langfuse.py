"""A fake of the part of the Langfuse SDK the sink uses (docs/DECISIONS-langfuse.md, L2): it records every call and sends nothing anywhere.
The names and keyword arguments are the ones of langfuse 4.17.0 (`Langfuse.start_observation`, `LangfuseObservationWrapper.start_observation/update/end`,
`Langfuse.create_trace_id`, `propagate_attributes`); tests/test_observability_real_sdk.py runs the same flow against the real SDK so the fake cannot drift."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager


class FakeObservation:
    def __init__(self, client: "FakeClient", parent: "FakeObservation | None", **kwargs) -> None:
        self.client, self.parent, self.kwargs = client, parent, kwargs
        self.name, self.as_type = kwargs["name"], kwargs.get("as_type", "span")
        self.propagated = [dict(p) for p in client.propagated]  # the propagate_attributes contexts this was created inside
        self.updates: list[dict] = []
        self.ended = False
        self.end_time: int | None = None
        self.end_order: int | None = None
        client.observations.append(self)

    def start_observation(self, **kwargs) -> "FakeObservation":
        self.client.maybe_fail("start_observation")
        return FakeObservation(self.client, self, **kwargs)

    def update(self, **kwargs) -> "FakeObservation":
        self.client.maybe_fail("update")
        self.updates.append(kwargs)
        return self

    def end(self, *, end_time: int | None = None) -> "FakeObservation":
        self.client.maybe_fail("end")
        self.ended, self.end_time = True, end_time
        self.end_order = self.client.next_end()
        return self

    @property
    def merged(self) -> dict:
        """Everything set at creation and by update(), later values winning."""
        out = dict(self.kwargs)
        for update in self.updates:
            out.update({k: v for k, v in update.items() if v is not None})
        return out

    def every_string(self) -> list[str]:
        """Every string the SDK was handed for this observation (to look for what must never be there)."""
        found: list[str] = []

        def walk(value):
            if isinstance(value, str):
                found.append(value)
            elif isinstance(value, dict):
                for k, v in value.items():
                    found.append(str(k))
                    walk(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    walk(v)

        walk(self.kwargs)
        walk(self.updates)
        for p in self.propagated:
            walk(p)
        return found


class FakeClient:
    """`fail`: names of the methods that raise (start_observation, update, end, create_trace_id) to prove a Langfuse failure never reaches a request."""

    def __init__(self, fail: set[str] | frozenset[str] = frozenset()) -> None:
        self.observations: list[FakeObservation] = []
        self.propagated: list[dict] = []
        self.fail, self.calls, self._ends = set(fail), [], 0

    def maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"fake langfuse: {name} failed")

    def next_end(self) -> int:
        self._ends += 1
        return self._ends

    @staticmethod
    def create_trace_id(*, seed: str | None = None) -> str:
        return hashlib.sha256(seed.encode("utf-8")).digest()[:16].hex()

    def start_observation(self, **kwargs) -> FakeObservation:
        self.maybe_fail("start_observation")
        return FakeObservation(self, None, **kwargs)

    def propagate_attributes(self, **kwargs):
        """The stand-in for `langfuse.propagate_attributes`: observations created inside the `with` carry these attributes."""

        @contextmanager
        def scope():
            self.propagated.append(kwargs)
            try:
                yield
            finally:
                self.propagated.pop()

        return scope()

    @property
    def root(self) -> FakeObservation:
        (root,) = [o for o in self.observations if o.parent is None]
        return root

    @property
    def children(self) -> list[FakeObservation]:
        return [o for o in self.observations if o.parent is not None]
