# Prise en charge du protocole 777 (Minecraft 26.3)

## Résumé

Cette PR ajoute Minecraft 26.3 (protocole 777, data version 5023) aux versions prises en charge. Elle ajoute les tables d'identifiants, la table d'états de bloc et les changements de format réseau propres à cette version. La version par défaut reste 26.2, et les versions 774 à 776 ne changent pas de comportement.

Le détail complet est dans `changes.md`.

## Changement de comportement

- `Bot(..., version="26.3")` et `protobot run` avec `version: "26.3"` se connectent maintenant à un serveur 26.3.
- En 26.3 :
  - Les BitSet sont lus comme des tableaux d'octets.
  - Le spawn info lit les modes de jeu en VarInt et `OPTIONAL_VAR_INT`.
  - Les move-entity sont décodés au nouveau format (chemin linéaire ou par étapes).
  - Le sérialiseur `DYE_COLOR` (43) est reconnu.
  - Accept-teleport renvoie la position et la rotation.
- **Nouvelle règle serveur en 26.3 : une seule position par tick.** Si le bot envoie une deuxième position dans le même tick, il envoie d'abord `client_tick_end`. Sinon, le serveur l'expulse.

## Modules touchés

- `protobot/protocol/versions.py` : `_PLAY_777`, `ConfigurationPacketIds` par version, nouveaux champs `happy_ghast_entity` et `shulker_entity`, tables d'entités pour la 777, entrée `"26.3"`.
- `protobot/client.py` : décodage et encodage spécifiques à la 777, ids de configuration et d'entités lus depuis `VersionSpec`, `_send_position_packet`.
- `protobot/world.py` : le protocole 777 utilise `blocks-26.3`.
- `protobot/data/blocks-26.3.json.gz` : nouveau fichier.
- `tests/test_protocol_777.py` : nouveau fichier.
- `README.md`, `README_zh.md` : 26.3 ajoutée à la liste des versions.

## Tests

```text
py -3.13 -m compileall -q protobot tests     -> OK
py -3.13 -m unittest discover -s tests -t .  -> Ran 28 tests, OK (skipped=1)
```

`pytest` n'est pas installé sur la machine de développement. La suite est en `unittest` pur, donc `uv run pytest` devrait donner le même résultat. Le test ignoré dépend de `cryptography`, qui n'est pas installée ici.

Smoke test contre un serveur vanilla 26.3 local (hors ligne, monde plat, `version="26.3"` sans aucun patch) :

```text
READY play 26.3 777
POS 9.501 -60.0 2.501
CHUNKS 117 ENTITIES 13
SENT {'serverbound_teleport_confirm': 2, 'serverbound_position': 52, 'serverbound_tick_end': 52}
CLOSED False REASON None
```

Dans les journaux du serveur, le message de chat est bien reçu et la déconnexion est propre : pas d'expulsion, pas d'erreur de décodage. Les deux `walk_to` produisent exactement une position par tick.

## Compatibilité

- **Fichier de données.** Le nouveau `blocks-26.3.json.gz` est inclus automatiquement par le motif `*.json.gz` de `pyproject.toml`. En 26.3, presque tous les ids d'états de bloc sont décalés, par exemple l'id 27 est maintenant `poplar_planks`. Utiliser une table 26.2 avec un serveur 26.3 fausserait donc les collisions.
- **API.** Les ajouts dans `VersionSpec` ont des valeurs par défaut égales aux anciennes constantes. Un `VersionSpec` construit à la main reste valide.
- **Limites connues :**
  - Le smoke test n'a pas pu tester `summon`/`tp`, faute de droits op sur le serveur de test. Les bateaux et la conduite de véhicule en 26.3 ne sont donc couverts que par l'audit du code source, qui conclut que `move_vehicle` est inchangé sur le fil.
  - `entity_position_sync` (format `PositionPath`) n'est pas lu par le bot. Il faudra l'adapter si le bot commence à le lire.
