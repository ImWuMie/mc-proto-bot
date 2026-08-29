"""Built-in ``en_us`` patterns for the translate keys a chat client sees.

Vanilla never sends the resolved sentence: a chat line, a join notice, or a
death message arrives as ``{"translate": key, "with": [...]}`` and the client
formats it from its own language file.  Shipping Mojang's whole ``en_us.json``
is neither possible nor useful here, so this module carries the keys a bot
actually reads: chat decoration, whispers, join/leave, advancements, sleep,
and the full vanilla death-message set.

Patterns use Java's ``MessageFormat``-style markers -- ``%s`` for the next
argument and ``%1$s`` for a specific one -- which is exactly what the language
file contains, so entries can be copied verbatim.

Unknown keys are not an error: :func:`protobot.text.plain_text` falls back to
the server-provided ``fallback`` and then to the key itself.  Servers with
custom keys can add them:

.. code-block:: python

    from protobot.translations import register_translations, load_translations

    register_translations({"myserver.welcome": "Welcome, %s!"})
    load_translations("en_us.json")   # a full Mojang language file
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

__all__ = ["TRANSLATIONS", "register_translations", "load_translations"]

#: Chat decoration, whispers, join/leave, advancements, sleeping.
_CHAT: dict[str, str] = {
    "chat.type.text": "<%s> %s",
    "chat.type.emote": "* %s %s",
    "chat.type.announcement": "[%s] %s",
    "chat.type.admin": "[%s: %s]",
    "chat.type.team.text": "%s <%s> %s",
    "chat.type.team.sent": "-> %s <%s> %s",
    "commands.message.display.incoming": "%s whispers to you: %s",
    "commands.message.display.outgoing": "You whisper to %s: %s",
    "multiplayer.player.joined": "%s joined the game",
    "multiplayer.player.joined.renamed": "%s (formerly known as %s) joined the game",
    "multiplayer.player.left": "%s left the game",
    "chat.type.advancement.task": "%s has made the advancement %s",
    "chat.type.advancement.goal": "%s has reached the goal %s",
    "chat.type.advancement.challenge": "%s has completed the challenge %s",
    "sleep.not_possible": "No amount of rest can pass this night",
    "sleep.players_sleeping": "%s/%s players sleeping",
    "sleep.skipping_night": "Sleeping through this night",
    "block.minecraft.bed.no_sleep": "You can only sleep at night",
    "block.minecraft.bed.obstructed": "This bed is obstructed",
    "block.minecraft.bed.occupied": "This bed is occupied",
    "block.minecraft.bed.not_safe": "You may not rest now; there are monsters nearby",
    "block.minecraft.bed.too_far_away": "You may not rest now; the bed is too far away",
    "multiplayer.disconnect.kicked": "Kicked by an operator",
    "multiplayer.disconnect.idling": "You have been idle for too long!",
    "multiplayer.disconnect.server_shutdown": "Server closed",
    "multiplayer.disconnect.duplicate_login": "You logged in from another location",
    "multiplayer.disconnect.not_whitelisted": "You are not white-listed on this server!",
    "multiplayer.disconnect.banned": "You are banned from this server",
    "multiplayer.disconnect.server_full": "The server is full!",
    "disconnect.timeout": "Timed out",
    "disconnect.spam": "Kicked for spamming",
    "death.fell.accident.generic": "%1$s fell from a high place",
    "death.fell.accident.ladder": "%1$s fell off a ladder",
    "death.fell.accident.vines": "%1$s fell off some vines",
    "death.fell.accident.weeping_vines": "%1$s fell off some weeping vines",
    "death.fell.accident.twisting_vines": "%1$s fell off some twisting vines",
    "death.fell.accident.scaffolding": "%1$s fell off scaffolding",
    "death.fell.accident.other_climbable": "%1$s fell while climbing",
    "death.fell.assist": "%1$s was doomed to fall by %2$s",
    "death.fell.assist.item": "%1$s was doomed to fall by %2$s using %3$s",
    "death.fell.finish": "%1$s fell too far and was finished by %2$s",
    "death.fell.finish.item": "%1$s fell too far and was finished by %2$s using %3$s",
    "death.fell.killer": "%1$s was doomed to fall",
}

#: The whole vanilla death-message set (``death.attack.*``).
_DEATHS: dict[str, str] = {
    "death.attack.anvil": "%1$s was squashed by a falling anvil",
    "death.attack.anvil.player": "%1$s was squashed by a falling anvil while fighting %2$s",
    "death.attack.arrow": "%1$s was shot by %2$s",
    "death.attack.arrow.item": "%1$s was shot by %2$s using %3$s",
    "death.attack.badRespawnPoint.message": "%1$s was killed by %2$s",
    "death.attack.cactus": "%1$s was pricked to death",
    "death.attack.cactus.player": "%1$s walked into a cactus while trying to escape %2$s",
    "death.attack.cramming": "%1$s was squished too much",
    "death.attack.cramming.player": "%1$s was squashed by %2$s",
    "death.attack.dragonBreath": "%1$s was roasted in dragon's breath",
    "death.attack.dragonBreath.player": "%1$s was roasted in dragon's breath by %2$s",
    "death.attack.drown": "%1$s drowned",
    "death.attack.drown.player": "%1$s drowned while trying to escape %2$s",
    "death.attack.dryout": "%1$s died from dehydration",
    "death.attack.dryout.player": "%1$s died from dehydration while trying to escape %2$s",
    "death.attack.even_more_magic": "%1$s was killed by even more magic",
    "death.attack.explosion": "%1$s blew up",
    "death.attack.explosion.player": "%1$s was blown up by %2$s",
    "death.attack.explosion.player.item": "%1$s was blown up by %2$s using %3$s",
    "death.attack.fall": "%1$s hit the ground too hard",
    "death.attack.fall.player": "%1$s hit the ground too hard while trying to escape %2$s",
    "death.attack.fallingBlock": "%1$s was squashed by a falling block",
    "death.attack.fallingBlock.player": "%1$s was squashed by a falling block while fighting %2$s",
    "death.attack.fallingStalactite": "%1$s was skewered by a falling stalactite",
    "death.attack.fallingStalactite.player": "%1$s was skewered by a falling stalactite while fighting %2$s",
    "death.attack.fireball": "%1$s was fireballed by %2$s",
    "death.attack.fireball.item": "%1$s was fireballed by %2$s using %3$s",
    "death.attack.fireworks": "%1$s went off with a bang",
    "death.attack.fireworks.item": "%1$s went off with a bang due to a firework fired from %3$s by %2$s",
    "death.attack.fireworks.player": "%1$s went off with a bang while fighting %2$s",
    "death.attack.flyIntoWall": "%1$s experienced kinetic energy",
    "death.attack.flyIntoWall.player": "%1$s experienced kinetic energy while trying to escape %2$s",
    "death.attack.freeze": "%1$s froze to death",
    "death.attack.freeze.player": "%1$s was frozen to death by %2$s",
    "death.attack.generic": "%1$s died",
    "death.attack.generic.player": "%1$s died because of %2$s",
    "death.attack.genericKill": "%1$s was killed",
    "death.attack.genericKill.player": "%1$s was killed while fighting %2$s",
    "death.attack.hotFloor": "%1$s discovered the floor was lava",
    "death.attack.hotFloor.player": "%1$s walked into the danger zone due to %2$s",
    "death.attack.inFire": "%1$s went up in flames",
    "death.attack.inFire.player": "%1$s walked into fire while fighting %2$s",
    "death.attack.inWall": "%1$s suffocated in a wall",
    "death.attack.inWall.player": "%1$s suffocated in a wall while fighting %2$s",
    "death.attack.indirectMagic": "%1$s was killed by %2$s using magic",
    "death.attack.indirectMagic.item": "%1$s was killed by %2$s using %3$s",
    "death.attack.lava": "%1$s tried to swim in lava",
    "death.attack.lava.player": "%1$s tried to swim in lava to escape %2$s",
    "death.attack.lightningBolt": "%1$s was struck by lightning",
    "death.attack.lightningBolt.player": "%1$s was struck by lightning while fighting %2$s",
    "death.attack.mace_smash": "%1$s was smashed by %2$s",
    "death.attack.mace_smash.item": "%1$s was smashed by %2$s with %3$s",
    "death.attack.magic": "%1$s was killed by magic",
    "death.attack.magic.player": "%1$s was killed by magic while trying to escape %2$s",
    "death.attack.mob": "%1$s was slain by %2$s",
    "death.attack.mob.item": "%1$s was slain by %2$s using %3$s",
    "death.attack.onFire": "%1$s burned to death",
    "death.attack.onFire.item": "%1$s was burned to a crisp while fighting %2$s wielding %3$s",
    "death.attack.onFire.player": "%1$s was burned to a crisp while fighting %2$s",
    "death.attack.outOfWorld": "%1$s fell out of the world",
    "death.attack.outOfWorld.player": "%1$s didn't want to live in the same world as %2$s",
    "death.attack.outsideBorder": "%1$s left the confines of this world",
    "death.attack.outsideBorder.player": "%1$s left the confines of this world while fighting %2$s",
    "death.attack.player": "%1$s was slain by %2$s",
    "death.attack.player.item": "%1$s was slain by %2$s using %3$s",
    "death.attack.sonic_boom": "%1$s was obliterated by a sonically-charged shriek",
    "death.attack.sonic_boom.item": "%1$s was obliterated by a sonically-charged shriek while trying to escape %2$s wielding %3$s",
    "death.attack.sonic_boom.player": "%1$s was obliterated by a sonically-charged shriek while trying to escape %2$s",
    "death.attack.stalagmite": "%1$s was impaled on a stalagmite",
    "death.attack.stalagmite.player": "%1$s was impaled on a stalagmite while fighting %2$s",
    "death.attack.starve": "%1$s starved to death",
    "death.attack.starve.player": "%1$s starved to death while fighting %2$s",
    "death.attack.sting": "%1$s was stung to death",
    "death.attack.sting.item": "%1$s was stung to death by %2$s using %3$s",
    "death.attack.sting.player": "%1$s was stung to death by %2$s",
    "death.attack.sweetBerryBush": "%1$s was poked to death by a sweet berry bush",
    "death.attack.sweetBerryBush.player": "%1$s was poked to death by a sweet berry bush while trying to escape %2$s",
    "death.attack.thorns": "%1$s was killed while trying to hurt %2$s",
    "death.attack.thorns.item": "%1$s was killed by %3$s while trying to hurt %2$s",
    "death.attack.thrown": "%1$s was pummeled by %2$s",
    "death.attack.thrown.item": "%1$s was pummeled by %2$s using %3$s",
    "death.attack.trident": "%1$s was impaled by %2$s",
    "death.attack.trident.item": "%1$s was impaled by %2$s with %3$s",
    "death.attack.wither": "%1$s withered away",
    "death.attack.wither.player": "%1$s withered away while fighting %2$s",
    "death.attack.witherSkull": "%1$s was shot by a skull from %2$s",
    "death.attack.witherSkull.item": "%1$s was shot by a skull from %2$s using %3$s",
}

#: The built-in table. ``register_translations`` / ``load_translations``
#: extend it in place, so running plugins and the core decoder share one
#: table instead of each passing its own.
TRANSLATIONS: dict[str, str] = {**_CHAT, **_DEATHS}


def register_translations(mapping: Mapping[str, str]) -> None:
    """Add or override translation patterns (server-specific keys)."""

    for key, pattern in mapping.items():
        TRANSLATIONS[str(key)] = str(pattern)


def load_translations(path: str | Path) -> int:
    """Merge a Mojang-style ``en_us.json`` and return how many keys were added.

    Only string values are taken; anything else in the file is ignored, so a
    resource pack's language file is as usable as the vanilla one.
    """

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("a language file must be a JSON object")
    added = {
        str(key): value
        for key, value in data.items()
        if isinstance(value, str)
    }
    register_translations(added)
    return len(added)
