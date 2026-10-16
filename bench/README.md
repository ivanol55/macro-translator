# Benchmark de calidad — Macro Translator

Mide con datos la calidad de las dos tareas del LLM (interpretar el texto y elegir el
alimento) y comprueba que la parte determinista es correcta. Sirve para comparar modelos
y detectar regresiones cuando se cambien prompts o pipeline.

No arregla nada de la app: mide, documenta los fallos y propone arreglos.

## Contenido

| Fichero | Qué es |
|---|---|
| `cases/*.jsonl` | 100 casos en 10 categorías (34 en `split: test`). |
| `models.yaml.example` | Plantilla de endpoints a medir. Copia a `models.yaml` y edítala. |
| `run.py` | Ejecuta cada caso por el pipeline real y guarda las trazas. |
| `report.py` | Calcula métricas y genera `REPORT.md`. |
| `REPORT.md` | Informe con tablas, peores fallos, modos de fallo y propuestas. |
| `results/` | Salidas crudas por ejecución (no se commitea). |

## Ejecutar

```bash
cp bench/models.yaml.example bench/models.yaml   # edita base_url y model
uv run python bench/run.py                      # 3 ejecuciones por caso y modelo
uv run python bench/report.py                   # genera bench/REPORT.md
```

O todo de una vez:

```bash
make bench
```

Pasadas rápidas:

```bash
uv run python bench/run.py --split dev --runs 1 --cases qty-001,eti-001
uv run python bench/run.py --category variantes --runs 1
```

`run.py` llama a las **mismas funciones del backend que la app**
(`macro_translator.pipeline.calcular` con `LLMClient` y `FoodDB`), no a la API HTTP.
Cada ejecución guarda: salidas crudas del LLM, JSON validado, reintentos, candidatos
ofrecidos, selección, tabla final y latencia de cada llamada.

Resultados en `bench/results/<fecha>_<modelo>_<commit>.jsonl`. **No se commitean**: pueden
contener valores de Open Food Facts más allá de nombres e identificadores (ver
`DATA_LICENSES.md` en la raíz).

## `models.yaml`

```yaml
models:
  - name: local-qwen
    base_url: http://127.0.0.1:8080/v1
    model: qwen3.5-9b
    api_key_env: LLM_API_KEY          # o api_key: "..." (no lo commitees)
    response_format: auto             # auto | json_schema | json_object | none
    extra_body: '{"chat_template_kwargs":{"enable_thinking":false}}'
    temperature: 0
    timeout: 180
    max_retries: 2
```

## Casos

Cada línea es un caso:

```json
{
  "id": "qty-003",
  "category": "cantidades",
  "split": "dev|test",
  "input": "huevos cocidos, 2, 130g",
  "expected_items": [
    {
      "concept": "huevo cocido",
      "grams": 130, "grams_tolerance_pct": 1,
      "state": "cocido",
      "acceptable_candidates": ["USDA: Egg, whole, cooked, hard-boiled"],
      "kcal_range": [190, 210],
      "expected_per100": {"kcal": 155, "prot": 12.58, "carbs": 1.12, "grasas": 10.61},
      "macros_range": {"prot": [15, 18], "carbs": [1, 2], "grasas": [12, 15]},
      "required_assumptions": [],
      "variant_unspecified": false,
      "label_priority": false,
      "needs_review": false
    }
  ],
  "expected_not_found": [],
  "expected_total": null,
  "notes": "130 g es el total, no por unidad"
}
```

- `acceptable_candidates`: candidatos identificados por **fuente y nombre exacto en la BD**.
  Puede haber varios cuando hay respuestas legítimas de las dos fuentes (USDA y OFF).
- `kcal_range` y `macros_range`: se calculan en la BD a partir de **todos** los candidatos
  válidos, así que cubren las variantes legítimas.
- `variant_unspecified: true`: el texto no fija un atributo que cambia mucho el resultado;
  `required_assumptions` describe el supuesto que debe declararse.
- `label_priority: true`: los valores de la etiqueta del usuario deben ganar a la BD.
- `needs_review: true`: el caso depende de un criterio discutible y debe revisarlo Iván.

Los "valores esperados" de cada caso se justifican con la BD real. Los dos casos de
regresión obligatorios están en `cases/etiqueta.jsonl` (`eti-001`, la sepia → ~604 kcal y
~88,9 g de proteína con 10 g de aceite) y en `cases/compuestos.jsonl` (`com-001`, los
macarrones con picada y tomate frito).

## Métricas

- **Parseo:** precisión/recall de ingredientes, acierto en gramos (dentro de tolerancia) y
  en estado.
- **Selección:** candidato dentro de `acceptable_candidates`; «no encontrado» correcto y
  falsos «no encontrado».
- **Honestidad:** en casos con `variant_unspecified`, si declara el supuesto requerido; y
  si el motivo dice «exacta»/«idéntico» cuando no lo es.
- **Resultado final:** kcal y cada macro dentro de rango.
- **Robustez:** JSON válido a la primera, reintentos medios, errores y consistencia entre
  las 3 ejecuciones.
- **Seguridad:** en entradas hostiles, si el resultado no se altera.
- **Coste:** latencia media y p95 por llamada.
- **Determinismo del cálculo:** recalcula la tabla de forma independiente desde la selección
  y la compara con la de la app. Debe ser 100 %; cualquier discrepancia es un bug.

El emparejamiento entre ingrediente esperado y parseado usa solapamiento de palabras (no hay
alineación manual), así que los casos límite pueden sobrevalorarse o infravalorarse. Los
casos dudosos van marcados con `needs_review`.
