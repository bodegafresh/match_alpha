# Cómo agregar una nueva competición a Match Alpha

Desde junio 2026, la carga de `competitions` y `competition_seasons` es
**migration-first**.

La fuente de verdad operativa para alta de temporadas es:

```
supabase/migrations/*.sql
```

El catálogo en código puede seguir existiendo para comportamiento de aplicación,
pero **no debe ser el mecanismo principal para crear filas en DB** de nuevas
competiciones o temporadas.

---

## Arquitectura: el flujo completo

```
migration SQL  ──►  DB (competitions, competition_seasons)
               │
               ▼
    ingesta ESPN / API_FOOTBALL / SPORTMONKS / FOOTBALL_DATA
               │
               ▼
    matches + match_participants + standings + tournament_slots
               │
               ▼
    slot_resolver  →  bracket correcto automaticamente
               │
               ▼
    feature_snapshots  →  model_predictions  →  betting_decisions  →  EV+
```

**Regla de oro:** nuevas temporadas/competiciones se agregan por migración SQL
idempotente. Evitar depender de `seed_competition_catalog` para altas en
producción.

---

## Formatos de competición disponibles

| `format_code` | Ejemplo | Descripción |
|---|---|---|
| `SINGLE_TABLE_LEAGUE` | Liga chilena, Premier League | Todos juegan contra todos, tabla única |
| `GROUPS_THEN_KNOCKOUT` | Mundial, Libertadores | Fase de grupos + eliminación directa |
| `LEAGUE_PHASE_THEN_KNOCKOUT` | Champions League 2024+ | Fase liga (Swiss) + playoffs + eliminatoria |
| `DOMESTIC_CUP` | Copa Chile, FA Cup | Eliminación directa pura desde ronda 1 |
| `TWO_LEG_KNOCKOUT` | Copa Sudamericana (solo KO) | Eliminación directa con partidos de ida y vuelta |

Si tu liga usa Apertura + Clausura, se modelan como **dos entradas separadas**
en el catálogo con slugs distintos (ver ejemplo abajo).

---

## Paso 1: Agregar la entrada al catálogo

Edita `app/competitions/catalog.py`.

### Ejemplo A — Liga chilena (Apertura 2026)

```python
def chile_apertura_stages() -> list[StageConfig]:
    return [
        # Fase regular: tabla única, todos vs todos (ida y vuelta)
        StageConfig(
            stage_code="LEAGUE_REGULAR",
            stage_name="Torneo Apertura",
            stage_order=1,
            stage_type="LEAGUE_PHASE",
            rules={
                "view_type": "LEAGUE_TABLE",
                "rounds": "DOUBLE_ROUND_ROBIN",
                "teams": 16,
                "tie_breakers": ["points", "goal_difference", "goals_for", "wins", "head_to_head"],
                "relegation": {
                    "method": "ACCUMULATED_TABLE",   # Chile usa tabla acumulada, no solo la temporada
                    "relegated_positions": [15, 16],
                    "playoff_positions": [13, 14],
                },
                "qualifies": {
                    "top_n_to_liguilla": 8,           # top 8 pasan a la Liguilla
                },
            },
        ),
        # Liguilla (playoff final): cuartos, semis, final — partidos únicos en cancha neutral
        StageConfig("QUARTER_FINAL", "Cuartos de final", 2, "KNOCKOUT", {
            **bracket_rules(4, legs=2),
            "legs": 2,
            "away_goals_rule": False,
        }),
        StageConfig("SEMI_FINAL", "Semifinal", 3, "KNOCKOUT", bracket_rules(2, legs=2)),
        StageConfig("FINAL", "Final", 4, "FINAL", {
            **bracket_rules(1, legs=1),  # final única en cancha neutral
            "neutral_venue": True,
        }),
    ]

# Agregar al COMPETITION_CATALOG dict:
"chile-apertura-2026": CompetitionCatalogEntry(
    slug="chile-apertura-2026",
    competition_slug="chile-primera",          # mismo competition_slug = misma entidad competitiva
    name="Torneo Apertura 2026",
    competition_type="LEAGUE",
    domain_type="DOMESTIC_LEAGUE",
    format_code="SINGLE_TABLE_LEAGUE",
    season_label="Apertura 2026",
    country_code="CL",
    region="South America",
    confederation="CONMEBOL",
    tier=1,
    is_international=False,
    starts_at="2026-02-01T00:00:00Z",
    ends_at="2026-07-15T23:59:59Z",
    timezone_name="America/Santiago",
    source=SourceConfig(
        primary="API_FOOTBALL",
        secondary=["SPORTMONKS", "ESPN"],
        external_ids={
            "API_FOOTBALL": "265",      # League ID de API-Football para Primera División Chile
            "ESPN": "chi.1",            # slug ESPN
        },
        capabilities={
            "API_FOOTBALL": ["fixtures", "standings", "teams", "venues", "players", "events", "stats", "odds"],
            "SPORTMONKS": ["fixtures", "standings", "teams", "venues", "players", "events", "stats"],
            "ESPN": ["fixtures", "scores"],
        },
    ),
    ui_navigation=["matches", "standings", "teams"],
    default_view="matches",
    stages=chile_apertura_stages(),
    # Sin groups para liga de tabla única
),
```

### Ejemplo B — Champions League 2025/2026 (temporada actual)

```python
# UCL ya tiene ucl_stages() definido. Solo agrega la entrada de temporada:
"ucl-2025-2026": CompetitionCatalogEntry(
    slug="ucl-2025-2026",
    competition_slug="uefa-champions-league",  # misma entidad que ucl-2026-2027
    name="UEFA Champions League",
    competition_type="CUP",
    domain_type="CONTINENTAL_CLUB",
    format_code="LEAGUE_PHASE_THEN_KNOCKOUT",
    season_label="2025/2026",
    country_code=None,
    region="Europe",
    confederation="UEFA",
    tier=1,
    is_international=True,
    starts_at="2025-09-17T00:00:00Z",
    ends_at="2026-05-30T23:59:59Z",
    timezone_name="UTC",
    source=SourceConfig(
        primary="FOOTBALL_DATA",
        secondary=["SPORTMONKS", "API_FOOTBALL", "ESPN"],
        external_ids={
            "FOOTBALL_DATA": "CL",      # siempre "CL" para UCL en football-data.org
            "API_FOOTBALL": "2",        # League ID UCL en API-Football
            "ESPN": "uefa.champions",
        },
        capabilities={
            "FOOTBALL_DATA": ["fixtures", "results", "standings", "teams"],
            "SPORTMONKS": ["fixtures", "standings", "teams", "venues", "players", "lineups", "events", "stats"],
            "API_FOOTBALL": ["fixtures", "standings", "teams", "venues", "players", "lineups", "events", "stats", "odds"],
            "ESPN": ["fixtures", "scores"],
        },
    ),
    ui_navigation=["matches", "league_phase", "teams", "bracket"],
    default_view="matches",
    stages=ucl_stages(),   # reutiliza la definición de etapas ya existente
),
```

---

## Paso 2: Correr el seed

Una vez que agregaste la entrada al catálogo, ejecuta el job de seed.
Esto crea las filas en `competitions`, `competition_seasons`, `competition_stages` y `competition_groups`.

**Opción A — Via API (producción):**
```bash
curl -X POST https://match-alpha.onrender.com/api/v1/jobs/seed_competition_catalog/run \
  -H "X-Internal-Key: $INTERNAL_JOB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"competition": "chile-apertura-2026"}'
```

**Opción B — Via script Python (desarrollo):**
```python
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection
from app.competitions.service import seed_competition_catalog

async def main():
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        result = await seed_competition_catalog(conn, competition="chile-apertura-2026")
        await conn.commit()
        print(result)

asyncio.run(main())
```

---

## Paso 3: Configurar la ingesta

Después del seed, el pipeline de ingesta necesita saber el ID externo correcto
para llamar a la fuente de datos. Eso viene de `external_ids` en el catálogo.

```python
# Ya configurado en source.external_ids — el ingester lo lee automáticamente:
external_ids={
    "API_FOOTBALL": "265",   # Liga chilena
    "FOOTBALL_DATA": "CL",   # UCL
}
```

Para verificar que el external_id está correctamente mapeado:
```bash
curl "https://match-alpha.onrender.com/api/v1/competitions/chile-apertura-2026/status"
```

---

## Campos que determinan el comportamiento del sistema

### En `StageConfig.rules` — lo que controla el slot_resolver y el bracket

| Campo | Tipo | Efecto |
|---|---|---|
| `view_type` | str | `GROUP_TABLES`, `LEAGUE_TABLE`, `BRACKET_ROUND`, `TWO_LEG_TIE` |
| `legs` | int | 1 = partido único, 2 = ida y vuelta |
| `away_goals_rule` | bool | Si aplica regla de gol de visita (falso en torneos modernos) |
| `extra_time` | bool | Si hay prórroga en eliminación directa |
| `penalties` | bool | Si hay penales como desempate |
| `qualifies.top_n_per_group` | int | Cuántos por grupo clasifican (normalmente 2) |
| `qualifies.best_third_places` | int | Cuántos mejores terceros clasifican (8 en el Mundial) |
| `tie_breakers` | list | Orden de criterios para desempatar en tabla |
| `relegation` | dict | Configura descenso / playoff de permanencia |

### En `CompetitionCatalogEntry` — lo que controla el modelo predictivo

| Campo | Efecto en el modelo |
|---|---|
| `is_international=True` | Usa `ELO_INTERNATIONAL`, ignora `ELO_DOMESTIC` |
| `is_international=False` | Usa `ELO_DOMESTIC` + `ELO_GLOBAL`, ignora `ELO_INTERNATIONAL` |
| `confederation` | Ajusta el K-factor de ELO (UEFA/CONMEBOL/FIFA tienen distintos K) |
| `timezone_name` | Convierte kickoff_at para mostrar hora local correcta |
| `competition_type` | `LEAGUE` vs `CUP` vs `TOURNAMENT` cambia el contexto de presión de etapa |

---

## Qué pasa automáticamente cuando el catálogo está bien

| Componente | Qué hace sin intervención |
|---|---|
| `slot_resolver` | Asigna ganadores de grupo / segundos / mejores terceros a los cruces del bracket |
| `standings_refresh` | Calcula posiciones respetando los `tie_breakers` del stage |
| `feature_snapshot_build` | Usa `is_international` para elegir el tipo de ELO correcto |
| `model_recompute` | Aplica Poisson con los lambdas correctos según el contexto |
| `ev_decision` | Genera decisiones PAPER_ONLY/BETTABLE para los partidos de esta liga |
| Vista UI `bracket` | Aparece solo si hay stage con `view_type: BRACKET_ROUND` |
| Vista UI `standings` | Aparece si hay stage con `view_type: LEAGUE_TABLE` o `GROUP_TABLES` |

---

## Checklist — Liga chilena (Apertura/Clausura)

```
[ ] Agregar "chile-apertura-2026" al COMPETITION_CATALOG en catalog.py
[ ] Agregar "chile-clausura-2026" al COMPETITION_CATALOG (si aplica)
[ ] Verificar external_ids para API_FOOTBALL (ID "265" para Primera División)
[ ] Correr seed: POST /jobs/seed_competition_catalog/run {"competition": "chile-apertura-2026"}
[ ] Verificar en DB: SELECT slug FROM competition_seasons WHERE competition_season_id IS NOT NULL;
[ ] Correr primera ingesta: POST /jobs/worldcup_daily_refresh/run (o el job equivalente multi-liga)
[ ] Verificar partidos: GET /api/v1/web/matches?season=chile-apertura-2026
[ ] Verificar tabla: GET /api/v1/web/standings?season=chile-apertura-2026
```

## Checklist — Champions League

```
[ ] Agregar "ucl-2025-2026" al COMPETITION_CATALOG (si la temporada aún no existe)
[ ] Verificar external_ids (FOOTBALL_DATA: "CL", API_FOOTBALL: "2")
[ ] Correr seed: POST /jobs/seed_competition_catalog/run {"competition": "ucl-2025-2026"}
[ ] Correr ingesta inicial (fuente primaria = FOOTBALL_DATA)
[ ] Verificar fase liga: GET /api/v1/web/standings?season=ucl-2025-2026
[ ] En la fase KO, verificar que el bracket se arme solo vía slot_resolver
[ ] No tocar tournament_slots manualmente — en UCL no hay "mejores terceros", el slot_resolver
    asigna ganadores/perdedores de cada partido automáticamente
```

---

## Casos especiales que sí requieren migración SQL

Estos son los únicos casos donde una migración es legítima y necesaria:

1. **Draw matrix oficial (ej. Mundial)** — cuando hay un sorteo oficial que determina
   qué mejor tercero va contra qué grupo, y ese sorteo no es computable por algoritmo.
   Esto se hace UNA VEZ después del sorteo real.
   Ver: `supabase/migrations/022_best_third_draw_matrix.sql`

2. **Nuevo `format_code` no soportado** — si tu liga tiene un formato que no existe
   en `competition_format.py` (ej. "APERTURA_CLAUSURA_WITH_SUPER_FINAL"), hay que
   agregar el normalizer en Python, no parchear la DB.

3. **Cambio de reglas mid-season** — si una federación cambia las reglas en plena
   temporada (raro, pero ocurre). Se crea un nuevo stage con las nuevas reglas
   y se migran los partidos afectados.

**Nada más debería requerir SQL manual.** Si encuentras que necesitas un UPDATE
directo para que algo funcione, es síntoma de un campo faltante en `catalog.py`
o un bug en el resolver — corrígelo ahí, no en la DB.
