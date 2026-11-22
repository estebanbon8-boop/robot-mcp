# robot-mcp

Serveur **MCP** pour piloter **Autodesk Robot Structural Analysis Professional** depuis **Claude** (Claude Desktop en mode Chat, ou Claude Code), via l'API COM RobotOM.

> **Statut : expérimental.** Écrit pour Robot 2027, pas encore testé sur toutes les versions. **Vérifie toujours les résultats à la main** (équilibre, ordre de grandeur, wL²/8…). L'outil supprime les erreurs d'API, pas les erreurs de modélisation.

## Ce que Claude peut faire

| Domaine | Outils |
|---|---|
| Projet | `robot_status`, `robot_new_project`, `robot_open_project`, `robot_save_project` |
| Géométrie | `robot_add_nodes`, `robot_add_bars`, `robot_delete`, `robot_list_model` |
| Propriétés | `robot_set_section`, `robot_set_material`, `robot_set_support` |
| Charges | `robot_add_load_case`, `robot_add_self_weight`, `robot_add_nodal_force`, `robot_add_bar_uniform_load`, `robot_add_bar_point_load`, `robot_add_combination`, `robot_list_cases` |
| Calcul | `robot_calculate` (enregistre automatiquement le projet, résultats inclus) |
| Résultats | `robot_bar_forces`, `robot_node_reactions` (avec somme pour contrôle d'équilibre), `robot_node_displacements` |
| Diagrammes | `robot_export_diagrams` — courbes M, V, N, flèche en **PNG** + **CSV** (Excel), avec extremums |
| Accès complet | `robot_api_search`, `robot_api_describe` (explorent l'API Robot réellement installée) + `robot_run_python` (exécute n'importe quelle commande RobotOM : relâchements, flambement, modal, neige/vent, panneaux, dimensionnement acier/BA…) |

## Projets Robot normaux, que tu reprends à la main

Claude construit un **vrai modèle Robot** dans la fenêtre Robot ouverte : tu le vois se construire, et tu peux le modifier toi-même à tout moment.
- Enregistrement en `.rtd` : par défaut dans `Documents\Robot-MCP\` (ou le chemin demandé), après création, modification et calcul.
- Tu le rouvres dans Robot comme n'importe quel projet. Si les résultats n'apparaissent pas à la réouverture, clique sur **Calculer**.
- Courbes dans Robot : **Résultats > Diagrammes pour barres**. Courbes exportées par Claude : dossier `diagrammes\` à côté du projet.

## Couverture des fonctionnalités

Les outils dédiés couvrent le flux courant (barres, charges, combinaisons, calcul, résultats, diagrammes). **Tout le reste de l'API RobotOM** est accessible via `robot_run_python`, après que Claude a cherché les noms exacts avec `robot_api_search` / `robot_api_describe`.
Limite : ce que l'API COM d'Autodesk n'expose pas (certaines boîtes de dialogue ou modules) reste à faire dans l'interface de Robot.

## Unités (important)

- **Entrées** : m, kN, kN/m, kN·m. Le serveur convertit en N pour l'API.
- **Sorties** : kN, kN·m, mm, mrad.
- **Signes** : charges en repère global, **Z négatif = vers le bas**. Effort normal Robot **FX > 0 = compression**.
- Exception : `robot_run_python` travaille directement en unités de l'API (**N, N·m, m**).

## Installation (Windows)

Prérequis : Robot installé et **lancé une fois** ; `uv` (`winget install --id astral-sh.uv -e`) ; Git.

```powershell
cd $HOME
git clone https://github.com/estebanbon8-boop/robot-mcp.git
cd robot-mcp
# Quitter Claude Desktop d'abord (icône près de l'horloge > Quitter)
powershell -ExecutionPolicy Bypass -File .\install.ps1 -WhatIf   # aperçu
powershell -ExecutionPolicy Bypass -File .\install.ps1           # installation
```

Le script crée un Python **x64** (obligatoire : Robot est x64, même sur Windows ARM), installe le serveur, vérifie les outils, puis déclare `robot-mcp` dans Claude Desktop et Claude Code (avec sauvegarde `*.bak-robotmcp` des configs).

Déclaration manuelle (si besoin), dans `claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "robot-mcp": {
      "command": "C:\\Users\\<toi>\\robot-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "robot_mcp"]
    }
  }
}
```

## Premier test

1. Ouvre Robot.
2. Dans Claude : *« Appelle robot_status »*.
3. Puis : *« Nouveau portique 2D : poutre IPE 240 en acier de 6 m sur deux appuis (rotule + appui simple), 10 kN/m vers le bas + poids propre. Calcule, donne M_max, réactions et flèche, et vérifie avec wL²/8. »*
   Attendu : M ≈ 46,4 kN·m, R ≈ 30,9 kN, flèche ≈ 21,3 mm.

## Pièges connus de Robot

- Fermer toute boîte de dialogue dans Robot avant `robot_calculate` (sinon blocage).
- Rien n'est sur disque avant `robot_save_project` ; après réouverture d'un fichier, relancer le calcul.
- Un numéro de cas inexistant renvoie des zéros : vérifier avec `robot_list_cases`.
- Noms de matériaux/sections = noms exacts de la base chargée dans Robot (ex. `S 355`, `IPE 240`).
- Supprimer/recréer une barre fait perdre ses relâchements.

## Licence

MIT. Projet indépendant, non affilié à Autodesk.
