from __future__ import annotations

import random
from datetime import date


def select_artist(artists: list[dict], runtime: dict) -> dict:
    if not artists:
        raise RuntimeError("Artist list is empty")
    recently = list(runtime.get("recentlyUsed") or [])
    pool = [a for a in artists if a["user_id"] not in recently]
    if not pool:
        recently = []
        pool = artists
    selected = random.choice(pool)
    recently.append(selected["user_id"])
    max_history = max(1, len(artists) - 3)
    if len(recently) > max_history:
        recently = recently[-max_history:]
    runtime["recentlyUsed"] = recently
    return selected


class State:
    """In-memory state seeded from the batch. Nothing is written to disk:
    changes go to the bot through `sink` (acks in the private group)."""

    STATS_GROUP = 150

    def __init__(self, posted: dict, runtime: dict, sink=None, dry: bool = False):
        self._posted = dict(posted or {})
        self._runtime = dict(runtime or {})
        self._runtime.setdefault("recentlyUsed", [])
        self._runtime.setdefault("recentCaptions", {})
        self._runtime.setdefault("failures", {})
        self._synced = dict(self._posted)
        self.sink = sink
        self.dry = dry
        self.stats: dict[str, list] = {}   # tag -> [translation, count, nsfw, status, used, last_seen]

    def posted(self) -> dict:
        return self._posted

    def runtime(self) -> dict:
        return self._runtime

    def save(self, posted, runtime) -> None:
        """Called after every finished post: only new/changed posted ids are sent now."""
        if runtime is not None:
            self._runtime = runtime
        if posted is None or self.dry or not self.sink:
            return
        delta = {k: v for k, v in posted.items() if self._synced.get(k) != v}
        if delta:
            self.sink.posted(delta)
            self._synced.update(delta)

    def add_stats(self, decisions: list[dict], nsfw: bool) -> None:
        today = date.today().isoformat()
        for d in decisions:
            row = self.stats.setdefault(d["tag"], ["", 0, 0, d["status"], 0, today])
            if d.get("en"):
                row[0] = d["en"]
            row[1] += 1
            row[2] += 1 if nsfw else 0
            row[3] = d["status"]
            row[4] += 1 if d["used"] else 0

    def flush(self) -> None:
        """End of run: remaining posted ids, runtime, tag stats."""
        if self.dry or not self.sink:
            return
        self.save(self._posted, self._runtime)
        self.sink.blob("rt", self._runtime)
        rows = [[tag, *r] for tag, r in self.stats.items()]
        for i in range(0, len(rows), self.STATS_GROUP):
            self.sink.blob(f"st{i // self.STATS_GROUP}", rows[i:i + self.STATS_GROUP])
