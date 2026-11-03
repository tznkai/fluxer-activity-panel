# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Static kind-to-plugin mapping. Adding a plugin is one module plus one line here."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import aiohttp

from fluxer_panel.config import Config
from fluxer_panel.sources.base import HostManager, Probe

MANAGER_FACTORIES: dict[str, Callable[[aiohttp.ClientSession, Config], HostManager | None]] = {}
PROBES: dict[str, Probe] = {}


@dataclass(frozen=True)
class Sources:
    managers: Mapping[str, HostManager]  # only configured kinds
    probes: Mapping[str, Probe]


def build_sources(session: aiohttp.ClientSession, config: Config) -> Sources:
    managers: dict[str, HostManager] = {}
    for kind, factory in MANAGER_FACTORIES.items():
        manager = factory(session, config)
        if manager is not None:
            managers[kind] = manager
    return Sources(managers=managers, probes=dict(PROBES))


def known_manager_kinds() -> frozenset[str]:
    return frozenset(MANAGER_FACTORIES)
