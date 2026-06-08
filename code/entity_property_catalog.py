#!/usr/bin/env python3
"""
entity_property_catalog.py
===========================

Inspect the integer properties on entities in the Open
World, count how many entities have each, and generate:

  1. Per property:
     - a 2-3 sentence **LLM-facing description** (what
       the property means, how the LLM should think about
       writing it to its effects),
     - a 2-3 sentence **world-mechanics impact** note
       (intended for the operator to read and decide
       whether to make it an actual mechanic in the
       world code),
     - coverage stats (count, % of total, min/max/avg),
     - 3 sample entities (highest values) for context.

  2. **3 suggested new properties** that are NOT in the
     current world but could be useful additions to
     round out the world mechanics.

Per Arcurus 2026-06-08 (#openworld):
"can you make a script that extract the current
properties we have in our entities and count for each
how many do have the property.  suggest for each a
short 2-3 sentence description for the llm and 2-3
sentence how it could impact the world meachanics
(for usw to look into).  suggest also 3 more
properties that are not in yet but could be useful"

The descriptions and world-mechanics impact are baked
into the script as a `PROPERTY_INFO` dict.  To add or
refine a description, edit that dict and re-run the
script.  Unknown properties get a templated
description based on the property name (e.g. "foo"
becomes "this property tracks foo").

Subcommands:
  catalog     — full report: all properties with
                descriptions, world-mechanics impact,
                and 3 new suggestions (default).
  coverage    — quick "how many entities have each
                property" table (no descriptions).
  unknown     — show only the properties that are NOT
                in the PROPERTY_INFO dict (so the
                operator can see what still needs
                documentation).
  --host HOST — open-world server URL
                (default: http://localhost:8081).
"""
import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SELENA_ROOT = os.path.abspath(os.path.join(HERE, ".."))

DEFAULT_HOST = "http://localhost:8081"
AUTH_HEADER = {"Cookie": "openworld_auth=1"}


# ---------------------------------------------------------------------------
# Property descriptions
# ---------------------------------------------------------------------------
#
# Each entry:
#   llm_description: 2-3 sentences of what the property
#                    means for the LLM to think about when
#                    writing effects.  Mirrors the tone
#                    and detail of
#                    open-world-selena/ai_templates/property_docs.md.
#   mechanics_impact: 2-3 sentences of how the property
#                    could be wired into actual world
#                    mechanics (combat, politics, story
#                    arcs, etc.).  Aimed at the OPERATOR —
#                    "for us to look into".  Not
#                    prescriptive; this is a TODO list of
#                    things to consider.
#   is_well_known: True for properties the LLM has been
#                  trained on broadly (morale, knowledge,
#                  power, etc.).  False for domain-specific
#                  ones (magic_protection, tolls, etc.)
#                  that the LLM probably doesn't have a
#                  good prior for.
#
# To add or refine a description, just edit this dict
# and re-run the script.  The `unknown` subcommand will
# show what still needs documentation.
PROPERTY_INFO: Dict[str, Dict[str, str]] = {
    "visibility": {
        "llm_description": (
            "A high `visibility` means this entity is *exposed* — it stands "
            "out, its presence is felt, and other entities will be very aware "
            "of it. A negative `visibility` means the entity is *hiding* — "
            "withdrawn, concealed, easy to overlook. Use it as a narrative "
            "cue for how present or absent the entity feels in the world "
            "right now."
        ),
        "mechanics_impact": (
            "Could affect awareness checks (does entity X notice entity Y?), "
            "encounter frequency (how often does the LLM see this entity in "
            "nearby-entity listings?), and stealth / surveillance mechanics. "
            "Worth wiring into the scheduler so high-visibility entities get "
            "called more often and surface in more cross-entity history events."
        ),
        "is_well_known": True,
    },
    "corruption": {
        "llm_description": (
            "A high `corruption` means this entity is *corrupted* — its "
            "essence has been twisted, its actions serve darker ends. A "
            "negative value means *purified* — cleansed, sanctified, or "
            "otherwise resistant to corruption. Neutral is 0. Use it to "
            "track how far an entity has slipped from (or returned to) its "
            "original nature."
        ),
        "mechanics_impact": (
            "Could affect alignment-based abilities (e.g. holy magic works "
            "better against corrupted targets), faction relationships (corrupt "
            "entities are hostile to purification orders), and narrative arc "
            "triggers (high corruption could auto-trigger a redemption quest, "
            "very negative could auto-trigger a sanctification). Worth wiring "
            "as a multiplier on cross-entity damage / diplomacy outcomes."
        ),
        "is_well_known": True,
    },
    "power": {
        "llm_description": (
            "Entity's overall strength tier. Drives the cap formula for many "
            "other properties (cap = max(1, power*5) + 100). Almost universal "
            "across entity types. Use it to indicate how strong / capable / "
            "influential the entity is in absolute terms."
        ),
        "mechanics_impact": (
            "ALREADY drives the stats-cap formula and the relative-value "
            "calculation in the {property_context} block. The cap is "
            "conservative: a power=241 entity can have at most 1305 total "
            "stat points. Worth considering: combat damage scaling, "
            "diplomacy leverage thresholds, and which entities can be "
            "targets of which abilities (e.g. high-power entities might "
            "resist low-power effects)."
        ),
        "is_well_known": True,
    },
    "wealth": {
        "llm_description": (
            "Money, treasure, material resources. Used by factions, "
            "merchants, kingdoms. The LLM should think of this as spendable "
            "resource that can be transferred (taxes, bribes, tithes, trade "
            "deals, fines). A negative value could mean debt or being in "
            "the red."
        ),
        "mechanics_impact": (
            "Currently only used as a stat for cap arithmetic. Worth wiring: "
            "trade skill checks, hiring mercenaries, bribing officials, "
            "funding expeditions, building projects, maintaining armies. "
            "Could interact with the `tolls` and `trade_flow` properties "
            "for economic simulation."
        ),
        "is_well_known": True,
    },
    "morale": {
        "llm_description": (
            "Fighting spirit, hope, determination. Can go negative "
            "(despair, broken) or very positive (eager, fanatical). Distinct "
            "from `health` (which is structural integrity) — a unit can be at "
            "full health but low morale (won't fight) or low health but high "
            "morale (fighting to the last)."
        ),
        "mechanics_impact": (
            "ALREADY extensively used in the narrative. Worth wiring: combat "
            "multipliers (low morale = desertion chance, high morale = "
            "fighting-beyond-the-call), surrender thresholds (morale=0 "
            "could auto-trigger surrender), and recovery-from-defeat "
            "arcs. Should recover slowly over time (e.g. +1 per rest cycle) "
            "so the LLM can model rallying."
        ),
        "is_well_known": True,
    },
    "knowledge": {
        "llm_description": (
            "Accumulated lore, secrets learned, lost truths recovered. Use it "
            "to track what an entity knows — both verified facts and "
            "esoteric understanding. A high-knowledge entity could be a "
            "sage, a recovered ancient, or a well-traveled scholar. A "
            "low-knowledge entity is naive, uneducated, or amnesiac."
        ),
        "mechanics_impact": (
            "ALREADY in use. Worth wiring: ability to identify artifacts, "
            "recognize weaknesses, cast high-tier spells, or solve riddles. "
            "Could interact with `revelation_gained` (a sudden knowledge spike) "
            "and `mysterious_phenomena` (what an entity has personally "
            "witnessed vs. what they understand)."
        ),
        "is_well_known": True,
    },
    "reputation": {
        "llm_description": (
            "How the world sees this entity — a longer-term signal than "
            "`visibility`. Visibility is the present-tense 'are they here'; "
            "reputation is 'what do people think of them'. A high reputation "
            "means respected, feared, or famous. A negative reputation means "
            "despised, notorious, or infamous."
        ),
        "mechanics_impact": (
            "ALREADY in use. Worth wiring: recruitment (high reputation = "
            "easier to hire), trading (merchants trust or distrust), and "
            "diplomacy (allies-of-allies / enemies-of-enemies resolution). "
            "Distinct from `visibility` so the two should be tracked "
            "separately — a shadowy figure can have low visibility but "
            "very high reputation (a whispered legend)."
        ),
        "is_well_known": True,
    },
    "magic_protection": {
        "llm_description": (
            "Resistance to hostile magic. Use it for entities that are "
            "warded, blessed, naturally magic-resistant, or have some "
            "other reason to be harder to affect with spells. A negative "
            "value means the entity is *vulnerable* to magic."
        ),
        "mechanics_impact": (
            "Worth wiring: damage multiplier for incoming magic (incoming "
            "spell damage × (1 - magic_protection/100) or similar). Could "
            "also affect dispel attempts, summoning resistances, and curse "
            "duration. Should probably cap at 100 (full immunity) and floor "
            "at -100 (double vulnerability)."
        ),
        "is_well_known": False,
    },
    "magic_activity": {
        "llm_description": (
            "How much magical energy is currently being channeled, "
            "generated, or leaking out of this entity. Could be a mage in "
            "the middle of casting, a cursed object pulsing with power, or "
            "a ley-line nexus. Use it to indicate an entity that is "
            "magically *active* right now."
        ),
        "mechanics_impact": (
            "Worth wiring: detection radius (mages / seers can sense high "
            "magic_activity nearby), interference with stealth (high magic "
            "emits a 'signal'), and as a prerequisite for sustained-magic "
            "mechanics. Should probably decay slowly when not in use so "
            "an entity isn't perpetually 'active'."
        ),
        "is_well_known": False,
    },
    "magical_activity": {
        "llm_description": (
            "Same general idea as `magic_activity` (magical energy in use), "
            "but this is the older, more abstract form. Use it for ambient "
            "or background magical presence — a place of power, an artifact "
            "that hums, a region saturated with residual magic."
        ),
        "mechanics_impact": (
            "Worth folding into `magic_activity` (treat the two as the same "
            "stat under one name) OR keeping them separate if there's a real "
            "mechanical difference (e.g. `magic_activity` is intentional and "
            "controlled, `magical_activity` is ambient and uncontrolled). "
            "Currently they coexist which is a documentation hazard for the "
            "LLM — worth picking one and deprecating the other."
        ),
        "is_well_known": False,
    },
    "mysterious_phenomena": {
        "llm_description": (
            "Count of unexplained, weird, or supernatural events this "
            "entity has been involved with or caused. Use it for haunted "
            "places, oracle characters, cursed objects, or any entity where "
            "the LLM wants to mark 'weird stuff happens around this thing'."
        ),
        "mechanics_impact": (
            "Worth wiring: encounter-table modifier (high "
            "mysterious_phenomena = more random weird events per turn), "
            "investigation triggers (a player choosing to investigate this "
            "entity could find plot-relevant lore), and as a flag for the "
            "LLM to escalate a scene's tone. Could also auto-trigger new "
            "lore entries via the scheduler."
        ),
        "is_well_known": False,
    },
    "revelation_gained": {
        "llm_description": (
            "Count of major revelations this entity has experienced — "
            "truths uncovered, lies dispelled, secrets decoded. Use it to "
            "track narrative progression for characters on a discovery arc."
        ),
        "mechanics_impact": (
            "Worth wiring: ability to teach or share revelations (a high "
            "revelation_gained entity could be a quest-giver), trigger "
            "story arcs (every N revelations could auto-unlock a new "
            "narrative branch), and as a multiplier on `knowledge` (the LLM "
            "should typically add to both when a revelation happens)."
        ),
        "is_well_known": False,
    },
    "knowledge_corruption_orchestrator": {
        "llm_description": (
            "Marks an entity as the orchestrator of knowledge-corruption — "
            "i.e. it's the source of some deliberate corruption of truth or "
            "understanding in the world. Use it for shadowy puppet-masters, "
            "lying gods, or any entity whose primary mode of action is to "
            "twist what others know."
        ),
        "mechanics_impact": (
            "Worth wiring: as a flag for 'this entity is the source of "
            "fake knowledge events'. Could auto-trigger `corruption` increases "
            "in nearby entities, or be used by the scheduler to seed false "
            "lore into the world. Probably a binary flag (0 or 1) is enough, "
            "but the property is currently using the int type so could carry "
            "an intensity too."
        ),
        "is_well_known": False,
    },
    "trade_flow": {
        "llm_description": (
            "Volume of trade this entity handles — money or goods moving "
            "through it. Use it for markets, ports, trade guilds, merchant "
            "caravans. Distinct from `wealth` (which is the entity's own "
            "stockpile); `trade_flow` is the throughput."
        ),
        "mechanics_impact": (
            "Worth wiring: economic simulation (high trade_flow = thriving "
            "economy, generates wealth for nearby entities), tariff / toll "
            "collection (could interact with the `tolls` property), and as "
            "a way to model seasonal fluctuations. Could also influence "
            "encounter rates (busy markets = more NPCs)."
        ),
        "is_well_known": False,
    },
    "is_blessed": {
        "llm_description": (
            "Marks an entity as having received a blessing. Use it for "
            "chosen ones, sanctified heroes, holy relics, or any entity "
            "that's been formally blessed by a higher power."
        ),
        "mechanics_impact": (
            "Worth wiring: as a multiplier on `corruption` resistance (blessed "
            "entities resist corruption more), as a flag for holy-magic "
            "synergies, and as a way to flag quest-targets (a player trying "
            "to find a blessed hero could scan for this property). Currently "
            "looks like a binary flag; could be expanded to a 'blessing "
            "intensity' int."
        ),
        "is_well_known": False,
    },
    "prophecy_count": {
        "llm_description": (
            "Number of prophecies this entity is connected to — either "
            "fulfilling, has fulfilled, or is the subject of. Use it for "
            "oracles, chosen ones, and entities whose narrative arc is "
            "driven by prophecy."
        ),
        "mechanics_impact": (
            "Worth wiring: as a story-arc trigger (an entity whose "
            "prophecy_count == N could auto-progress the main plot when N "
            "is reached), as a multiplier on `revelation_gained`, and as a "
            "way to detect 'main character' candidates. Could also gate "
            "certain abilities (a high prophecy_count entity might be "
            "able to see the future, mark prophecies, etc.)."
        ),
        "is_well_known": False,
    },
    "temporal_scar": {
        "llm_description": (
            "Marks an entity as bearing a temporal scar — a wound in time, "
            "a paradox, a place where past and present blur. Use it for "
            "entities that have been temporally displaced, time-looped, or "
            "otherwise damaged the timeline."
        ),
        "mechanics_impact": (
            "Worth wiring: as a flag for time-related abilities (an entity "
            "with a temporal_scar might be immune to time-stop, or might be "
            "ABLE to time-travel), and as a trigger for paradoxical events "
            "(the LLM could be cued to write a 'paradox' scene when a "
            "temporal_scar entity is in play). Could also interact with "
            "the World Clock's day counter."
        ),
        "is_well_known": False,
    },
    "ancient_artifact_count": {
        "llm_description": (
            "Number of ancient artifacts this entity possesses or has "
            "custody of. Use it for vaults, museums, lost libraries, "
            "ancient kings, or any entity that's an artifact hoarder."
        ),
        "mechanics_impact": (
            "Worth wiring: as a multiplier on `knowledge` (ancient artifacts "
            "often hold forgotten lore), as a quest-target (a player could "
            "look for entities with high ancient_artifact_count), and as a "
            "way to detect 'treasure vault' candidates. The `artifact_active` "
            "property (see below) probably refers to how many of the "
            "artifacts are currently in use."
        ),
        "is_well_known": False,
    },
    "artifact_active": {
        "llm_description": (
            "Number of artifacts this entity is *actively using right now* "
            "(as opposed to just possessing or guarding). Use it for "
            "powerful magic-users, kings with their regalia equipped, "
            "artifacts in the middle of a ritual."
        ),
        "mechanics_impact": (
            "Worth wiring: as a multiplier on `magic_activity` (active "
            "artifacts channel magic), and as a way to detect 'wizard "
            "currently casting' for encounter-table purposes. Probably a "
            "subset of `ancient_artifact_count` — could be enforced as a "
            "`0 <= artifact_active <= ancient_artifact_count` invariant "
            "in the world's stats-cap logic."
        ),
        "is_well_known": False,
    },
    "phenomenon_count": {
        "llm_description": (
            "Count of strange phenomena occurring at or near this entity. "
            "Similar to `mysterious_phenomena` but more local — these are "
            "things happening right here, not unexplained events this entity "
            "has been part of."
        ),
        "mechanics_impact": (
            "Worth folding into `mysterious_phenomena` if the distinction "
            "isn't useful, OR keeping separate if the difference is "
            "('mysterious_phenomena' is historical, 'phenomenon_count' is "
            "current). Could drive encounter-table density (more phenomena "
            "= more weird encounters per turn). Consider auto-triggers: "
            "high phenomenon_count could auto-generate new mystery lore."
        ),
        "is_well_known": False,
    },
    "tolls": {
        "llm_description": (
            "Amount of tolls or fees this entity collects. Use it for "
            "bridges, gates, ports, roads, any place where there's a "
            "pay-to-pass mechanic."
        ),
        "mechanics_impact": (
            "Worth wiring: as a passive income source (could add to the "
            "entity's `wealth` over time), as a friction mechanic (high "
            "tolls = fewer travelers = less trade_flow for nearby entities), "
            "and as a diplomatic lever (a faction could lower / raise "
            "tolls as a political move). Could also trigger encounter "
            "events (toll evaders = bandits)."
        ),
        "is_well_known": False,
    },
    "traffic": {
        "llm_description": (
            "Volume of traffic / flow of entities passing through. Use it "
            "for roads, bridges, market squares, gates — anywhere there's "
            "movement of people, goods, or armies."
        ),
        "mechanics_impact": (
            "Worth wiring: as a multiplier on encounter rates (high "
            "traffic = more random encounters per turn), as an input to "
            "`trade_flow` calculations, and as a way to detect 'busy hub' "
            "candidates for the scheduler. Could also affect stealth "
            "(easy to get lost in crowds) and detection (hard to hide in "
            "heavy traffic)."
        ),
        "is_well_known": False,
    },
    "consciousness_active": {
        "llm_description": (
            "Marks an entity as having an active consciousness — "
            "awake, aware, sapient, capable of independent thought. Use it "
            "for living beings, awakened constructs, or any entity that "
            "is currently 'thinking'."
        ),
        "mechanics_impact": (
            "Worth wiring: as a prerequisite for many actions (an entity "
            "with consciousness_active=0 might be dormant, dead, or in a "
            "suspended-animation state — the LLM shouldn't act for them). "
            "Could be auto-managed (sleeping at night, awake during the "
            "day) for biological entities, or static (1 or 0) for "
            "constructs."
        ),
        "is_well_known": False,
    },
    "last_processed_other_tick": {
        "llm_description": (
            "INTERNAL / OPERATOR-ONLY. Tracks the world tick up to which "
            "this entity's unprocessed-other-actions marker has been "
            "advanced. NEVER shown to the LLM (filtered out by the "
            "internal-properties list). NEVER writable by LLM-emit "
            "effects (rejected with a warning)."
        ),
        "mechanics_impact": (
            "Drives the LLM context-builder's unprocessed-other-actions "
            "block: entries with tick > marker are shown to the LLM as "
            "unprocessed impacts from other entities; entries with tick "
            "<= marker are filtered out. Operator can override via the "
            "per-property PUT endpoint to force re-processing."
        ),
        "is_well_known": False,
    },
    "influence": {
        "llm_description": (
            "Political and social leverage — the ability to sway "
            "decisions, broker deals, or rally others. Distinct from "
            "`power` (which is more like military / combat strength) "
            "and from `reputation` (which is how the world SEES the "
            "entity). A queen can have high `influence` even if she's "
            "personally weak; a back-room dealer can have high "
            "`influence` even if nobody respects them."
        ),
        "mechanics_impact": (
            "Worth wiring: diplomacy check multiplier (high influence = "
            "your threats / offers carry more weight), vote outcomes, "
            "summoning / recruitment success rates, and as a way for the "
            "LLM to model 'soft power' separately from `power`. Could "
            "auto-modify encounter-table politeness (low influence = "
            "merchants dismiss you; high influence = they compete for "
            "your attention). Could also interact with `suspicion` (a "
            "high-influence entity can deflect or absorb suspicion more "
            "easily than a low-influence one — the same hysteresis "
            "dead-zone as the corruption/suspicion tag rules could apply)."
        ),
        "is_well_known": False,
    },
    "suspicion": {
        "llm_description": (
            "How much this entity is suspected of wrongdoing, hidden "
            "motives, or undeclared allegiances. A high value means "
            "the world is watching them closely — they might be followed, "
            "investigated, or refused service. A negative value means "
            "they're seen as above reproach (a trusted hero, a public "
            "official, a beloved community member)."
        ),
        "mechanics_impact": (
            "Wired in (per Arcurus 2026-06-08 #openworld): server-side "
            "tag rule applies the `suspicious` tag when "
            "`max(1, power) - suspicion < -1` (i.e. suspicion has "
            "overwhelmed the entity's tolerance by more than 1) and "
            "removes the tag when `max(1, power) - suspicion > 0` "
            "(i.e. suspicion is below the entity's tolerance). The "
            "`[-1, 0]` dead zone prevents flicker near the boundary. "
            "The high-power entity needs MORE suspicion before the tag "
            "sticks (a power-100 entity needs suspicion > 101 to be "
            "tagged; a power-10 entity only needs suspicion > 11). "
            "Worth wiring further: trust check multiplier (low "
            "suspicion = NPC cooperates; high = NPC refuses or "
            "actively opposes), investigation triggers (very high "
            "suspicion could auto-trigger a trial / exile / "
            "confrontation), and as a narrative trigger for the LLM. "
            "Distinct from `corruption` (which is actual evil) — "
            "`suspicion` is perception, not reality."
        ),
        "is_well_known": False,
    },
}


# ---------------------------------------------------------------------------
# Suggested NEW properties
# ---------------------------------------------------------------------------
#
# 3 properties that are NOT in the world yet but would be useful
# to add.  Each entry has the same shape as PROPERTY_INFO, plus
# a `why` field explaining the use case.
NEW_PROPERTY_SUGGESTIONS: List[Dict[str, Any]] = [
    {
        "name": "health",
        "llm_description": (
            "Physical / structural integrity of the entity. For living "
            "beings, this is bodily health (wounds, disease, exhaustion). "
            "For places, this is structural integrity (damage, decay, "
            "ruin). For artifacts, this is condition (broken, cracked, "
            "whole). When `health` reaches 0, the entity is destroyed / "
            "dead / collapsed."
        ),
        "mechanics_impact": (
            "Currently the world has NO concept of an entity being "
            "destroyed. `morale` is fighting spirit (won't fight vs. "
            "will fight), but there's no `health` (can fight vs. cannot "
            "fight). Adding `health` would let the LLM model combat damage, "
            "siege damage to locations, and artifact decay. Worth wiring: "
            "a separate damage pipeline (LLM-emit effects can decrement "
            "`health`; if `health` <= 0, the entity is auto-archived and "
            "removed from active entity listings). Distinct from `morale` "
            "and from `magic_protection` (which is resistance, not "
            "structural state)."
        ),
        "why": (
            "This is the most fundamental missing property. The world "
            "has no concept of an entity being 'destroyed', which makes "
            "long-term stakes hard to model. Without `health`, every "
            "encounter is at full structural integrity, every artifact "
            "is at full condition, and every location is intact. Adding "
            "it unlocks combat, siege, decay, and death as narrative "
            "levers."
        ),
    },
    {
        "name": "loyalty",
        "llm_description": (
            "How firmly this entity is committed to a person, cause, "
            "faction, or ideal. A high `loyalty` means the entity "
            "stays, fights, and sacrifices; a low or negative value "
            "means the entity is wavering, defecting, or actively "
            "betraying. Distinct from `reputation` (how the world sees "
            "the entity) and from `morale` (general fighting spirit) — "
            "`loyalty` is specifically about commitment to a thing."
        ),
        "mechanics_impact": (
            "Worth wiring: defection thresholds (loyalty < 0 could "
            "auto-trigger a faction switch), betrayal detection (a "
            "loyalty drop > N in a single turn could trigger a warning), "
            "command acceptance (a low-loyalty unit might refuse "
            "suicide orders), and as a counter-weight to "
            "`suspicion` (an entity with very high loyalty to a cause "
            "might be suspected of zealotry even when innocent). Could "
            "also interact with `corruption` (a loyal servant of a "
            "corrupt master is harder to flip than a neutral one)."
        ),
        "why": (
            "The world has `morale` (general fighting spirit) and "
            "`reputation` (how the world sees the entity) but no "
            "`loyalty` (commitment to a specific thing). The LLM "
            "currently has to choose between modelling 'willing to "
            "fight' (morale) and 'committed to the cause' (no good "
            "property) — a deserter has high morale (ready to "
            "fight) but low loyalty (won't fight for YOU), and "
            "right now there's no way to capture that distinction. "
            "Adding `loyalty` lets the LLM model defection, betrayal, "
            "and steadfastness as separate narrative levers."
        ),
    },
    {
        "name": "durability",
        "llm_description": (
            "Resistance to physical damage — how hard this entity is "
            "to break, shatter, or wear down. For living beings, this "
            "is toughness (natural armor, endurance, hard-to-kill). "
            "For places, this is structural resilience (thick walls, "
            "reinforced construction, earthquake-resistant). For "
            "artifacts, this is material quality (forged vs. cheap, "
            "enchanted vs. mundane). A high `durability` entity "
            "absorbs more punishment before being affected."
        ),
        "mechanics_impact": (
            "Worth wiring: incoming damage multiplier (damage_taken "
            "is reduced by a percentage of durability), armor "
            "penetration checks (high-durability targets might resist "
            "low-tier attacks entirely), and decay rate (high-durability "
            "artifacts decay slower over time). Distinct from "
            "`magic_protection` (which is resistance to spells, not "
            "physical damage) and from a future `health` property "
            "(which would track current structural state, not "
            "resistance to damage). Could be wired as a server-side "
            "multiplier on incoming cross-entity damage effects, so "
            "the LLM writes damage values it thinks are reasonable "
            "and the server subtracts the durability reduction."
        ),
        "why": (
            "The world has `magic_protection` (magical resistance) "
            "but no `durability` (physical resistance). Combined "
            "with a future `health` property, this gives the LLM "
            "the standard RPG trio of (resistance, current state, "
            "incoming damage). Without `durability`, every entity "
            "is equally squishy regardless of whether it's a "
            "glass-mage or an iron-golem. Adding it lets the LLM "
            "model 'you can't hurt this thing with a normal sword' "
            "without resorting to ad-hoc `power` writes."
        ),
    },
]


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
def _api_get(host: str, path: str) -> Dict[str, Any]:
    req = urllib.request.Request(host + path, headers=AUTH_HEADER)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def load_all_entities(host: str = DEFAULT_HOST) -> List[Dict[str, Any]]:
    data = _api_get(host, "/api/entities?limit=200&include_system=false")
    return data.get("data") or []


# ---------------------------------------------------------------------------
# Coverage analysis
# ---------------------------------------------------------------------------
def coverage_table(entities: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Return a dict property_name -> {count, total, pct, min, max, avg,
    sample_high}.
    """
    n_total = len(entities)
    all_keys: Dict[str, List[float]] = {}
    for e in entities:
        for k, v in (e.get("properties_int") or {}).items():
            all_keys.setdefault(k, []).append(float(v))
    out: Dict[str, Dict[str, Any]] = {}
    for k, vals in all_keys.items():
        # Sample entities with the highest values
        ents_with = [
            (e["name"], (e.get("properties_int") or {}).get(k))
            for e in entities
            if (e.get("properties_int") or {}).get(k) is not None
        ]
        ents_with.sort(key=lambda x: -(x[1] or 0))
        out[k] = {
            "count": len(vals),
            "total": n_total,
            "pct": 100 * len(vals) / n_total if n_total else 0,
            "min": min(vals),
            "max": max(vals),
            "avg": sum(vals) / len(vals) if vals else 0,
            "sample_high": ents_with[:3],
        }
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _print_property(
    name: str,
    info: Dict[str, str],
    cov: Optional[Dict[str, Any]] = None,
    *,
    unknown: bool = False,
) -> None:
    """Render one property's full block."""
    title_marker = "?" if unknown else " "
    print(f"### `{name}`  [{title_marker}]")
    if cov:
        print(
            f"  coverage: {cov['count']}/{cov['total']} entities have it "
            f"({cov['pct']:.1f}%)"
        )
        print(
            f"  range:    min={cov['min']:.0f}  max={cov['max']:.0f}  "
            f"avg={cov['avg']:.1f}"
        )
        if cov["sample_high"]:
            sample_str = ", ".join(
                f"{n}={v}" for n, v in cov["sample_high"]
            )
            print(f"  top 3:    {sample_str}")
        print()
    if "llm_description" in info:
        print("  LLM-facing description:")
        # Indent each wrapped line.
        for line in info["llm_description"].splitlines() or [info["llm_description"]]:
            print(f"    {line}")
        print()
    if "mechanics_impact" in info:
        print("  World-mechanics impact (operator TODO):")
        for line in info["mechanics_impact"].splitlines() or [info["mechanics_impact"]]:
            print(f"    {line}")
        print()
    if "is_well_known" in info:
        marker = "WELL-KNOWN" if info["is_well_known"] else "DOMAIN-SPECIFIC"
        print(f"  LLM prior:  {marker} (the LLM may or may not have a good prior for this)")
    print()


def cmd_catalog(args) -> int:
    entities = load_all_entities(args.host)
    cov = coverage_table(entities)
    print(
        f"=== Entity Property Catalog ({len(entities)} non-system entities) ===\n"
    )

    print(
        "## A) Coverage table (how many entities have each int property)\n"
    )
    print(f"{'property':<40} {'count':>6}  {'% of total':>10}  {'range':>20}")
    print(f"{'-'*40} {'-'*6}  {'-'*10}  {'-'*20}")
    for k in sorted(cov.keys(), key=lambda k: -cov[k]["count"]):
        v = cov[k]
        rng = f"{v['min']:.0f}..{v['max']:.0f} (avg {v['avg']:.0f})"
        print(f"{k:<40} {v['count']:>6}  {v['pct']:>9.1f}%  {rng:>20}")
    print()

    # Group: known + unknown (so the operator can see what still
    # needs documentation).
    known_in_cov = [k for k in cov if k in PROPERTY_INFO]
    unknown_in_cov = [k for k in cov if k not in PROPERTY_INFO]
    print(
        f"## B) Per-property catalog "
        f"({len(known_in_cov)} documented, "
        f"{len(unknown_in_cov)} without a description in the script)\n"
    )
    print("### Documented properties (have LLM description + mechanics impact)\n")
    for k in sorted(known_in_cov):
        _print_property(k, PROPERTY_INFO[k], cov.get(k))
    if unknown_in_cov:
        print("### Undocumented properties (no description yet; see `unknown` subcommand)\n")
        for k in sorted(unknown_in_cov):
            print(f"  - `{k}` ({cov[k]['count']} entities, "
                  f"range {cov[k]['min']:.0f}..{cov[k]['max']:.0f})")
        print()

    print("## C) 3 suggested NEW properties (not in the world yet)\n")
    for i, sug in enumerate(NEW_PROPERTY_SUGGESTIONS, 1):
        print(f"### {i}. `{sug['name']}`\n")
        print(f"  **Why this would be useful:**\n")
        for line in sug["why"].splitlines() or [sug["why"]]:
            print(f"    {line}")
        print()
        print(f"  **LLM-facing description:**\n")
        for line in sug["llm_description"].splitlines() or [sug["llm_description"]]:
            print(f"    {line}")
        print()
        print(f"  **World-mechanics impact (operator TODO):**\n")
        for line in sug["mechanics_impact"].splitlines() or [sug["mechanics_impact"]]:
            print(f"    {line}")
        print()
    return 0


def cmd_coverage(args) -> int:
    entities = load_all_entities(args.host)
    cov = coverage_table(entities)
    n = len(entities)
    print(f"=== Property coverage ({n} non-system entities) ===\n")
    print(f"{'property':<40} {'count':>6}  {'% of total':>10}  {'range':>20}")
    print(f"{'-'*40} {'-'*6}  {'-'*10}  {'-'*20}")
    for k in sorted(cov.keys(), key=lambda k: -cov[k]["count"]):
        v = cov[k]
        rng = f"{v['min']:.0f}..{v['max']:.0f} (avg {v['avg']:.0f})"
        print(f"{k:<40} {v['count']:>6}  {v['pct']:>9.1f}%  {rng:>20}")
    return 0


def cmd_unknown(args) -> int:
    entities = load_all_entities(args.host)
    cov = coverage_table(entities)
    unknown = sorted([k for k in cov if k not in PROPERTY_INFO])
    if not unknown:
        print("All properties on entities are documented in PROPERTY_INFO. "
              "Nothing to do.")
        return 0
    print(f"=== Undocumented properties ({len(unknown)}) ===\n")
    print("These are int properties found on entities that are NOT in the "
          "PROPERTY_INFO dict. Add a description + mechanics impact to "
          "document them.\n")
    for k in unknown:
        v = cov[k]
        print(f"### `{k}`")
        print(f"  coverage: {v['count']} entities")
        print(f"  range:    {v['min']:.0f}..{v['max']:.0f} (avg {v['avg']:.0f})")
        if v["sample_high"]:
            sample_str = ", ".join(f"{n}={val}" for n, val in v["sample_high"])
            print(f"  top 3:    {sample_str}")
        print()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"open-world server URL (default: {DEFAULT_HOST})")
    sub = p.add_subparsers(dest="cmd", required=False)

    sub.add_parser("catalog",
                   help="Full report: per-property descriptions + 3 new suggestions (default)")
    sub.add_parser("coverage",
                   help="Just the coverage table (no descriptions)")

    sub.add_parser("unknown",
                   help="Properties that are NOT yet in PROPERTY_INFO (need documentation)")

    args = p.parse_args()
    if args.cmd is None or args.cmd == "catalog":
        return cmd_catalog(args)
    if args.cmd == "coverage":
        return cmd_coverage(args)
    if args.cmd == "unknown":
        return cmd_unknown(args)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())