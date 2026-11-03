# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Static kind-to-plugin mapping. Adding a plugin is one module plus one line here."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import aiohttp

from fluxer_panel.config import Config
from fluxer_panel.sources import a2s as a2s_probe
from fluxer_panel.sources import minecraft, pterodactyl
from fluxer_panel.sources.base import HostManager, Probe


@dataclass(frozen=True)
class ManagerPlugin:
    create: Callable[[aiohttp.ClientSession, Config], HostManager | None]  # returns None when not configured
    ref_hint: str  # the ref the kind expects, shown in `!card help fields`

    def __post_init__(self) -> None:
        if not self.ref_hint.strip():
            raise ValueError("a manager plugin needs a ref hint")


MANAGER_PLUGINS: dict[str, ManagerPlugin] = {
    "pterodactyl": ManagerPlugin(pterodactyl.create, pterodactyl.REF_HINT),
}
PROBES: dict[str, Probe] = {
    "a2s": a2s_probe.A2SProbe(),
    "minecraft_bedrock": minecraft.BedrockProbe(),
    "minecraft_java": minecraft.JavaProbe(),
}


@dataclass(frozen=True)
class Sources:
    managers: Mapping[str, HostManager]  # only configured kinds
    probes: Mapping[str, Probe]


def build_sources(session: aiohttp.ClientSession, config: Config) -> Sources:
    managers: dict[str, HostManager] = {}
    for kind, plugin in MANAGER_PLUGINS.items():
        manager = plugin.create(session, config)
        if manager is not None:
            managers[kind] = manager
    return Sources(managers=managers, probes=dict(PROBES))


def known_manager_kinds() -> frozenset[str]:
    return frozenset(MANAGER_PLUGINS)


def manager_ref_hints() -> dict[str, str]:
    return {kind: plugin.ref_hint for kind, plugin in MANAGER_PLUGINS.items()}
