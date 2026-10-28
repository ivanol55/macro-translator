# Comparativa de rendimiento — selección en paralelo y prompts cortos

> **Documento histórico.** El código de A/B/C1/C2 se retiró de `main`; estos ajustes ya
> no existen en la app. Este informe se conserva como evidencia y como aviso: no
> reintentar sin leer [`FAILED_EXPERIMENTS.md`](FAILED_EXPERIMENTS.md) y sin un cambio
> sustancial de arquitectura.

Fecha: 2026-10-28
Base: `main` en `4ccaf95`
Rama del experimento: `perf/parallel-selection` (eliminada)

| Cambio | Commit histórico |
|---|---|
| A — selección por ingrediente en paralelo | `cb340dc` |
| B — ronda 2 especulativa | `e66444a` |
| C1 — claves JSON cortas | `e176d1a` |
| C2 — `motivo` corto | `d3f5997` |
| Documentación de los ajustes | `94a4e9e` |

## Veredicto

**Ninguno de los cuatro cambios cumple las dos puertas.** A y B son más lentos; C1 baja
la latencia en la muestra pareada pero rompe un caso obligatorio; C2 no baja la latencia
y también rompe un caso obligatorio. La recomendación es **no añadir estos ajustes**: el
comportamiento actual (batch, una petición de selección por caso) es el que se queda. El
código se retiró, así que no hay variables que activar.

## Qué se probó

Se implementaron y se midieron estos ajustes, y después se retiraron:

| ID | Ajuste | Qué hacía |
|---|---|---|
| A | `SELECT_MODE=per_ingredient` + `LLM_PARALLEL=4` | una petición de selección por ingrediente, hasta 4 en vuelo |
| B | A + `SELECT_SPECULATIVE=1` | mandaba la ronda 2 junto con la 1 y descartaba la respuesta sobrante |
| C1 | `LLM_SHORT_KEYS=1` | claves cortas en el esquema y en los ejemplos del prompt |
| C2 | `SELECT_SHORT_MOTIVO=1` | limitaba `motivo` a unas 12 palabras |

No se tocó el recuento, la elección de alimentos ni la aritmética. Tampoco se
reinició el servidor ni se cambiaron sus flags.

## Servidor

| Dato | Valor |
|---|---|
| Endpoint | `http://127.0.0.1:8081/v1` |
| Modelo | `qwen3.5-9b` (`unsloth/Qwen3.5-9B-GGUF`, `Qwen3.5-9B-IQ4_XS.gguf`) |
| Runtime | llama.cpp `b11641` / `0.6.0-dev` / commit `843e7dcc1` |
| Slots | `--parallel 8`, `--kv-unified`, contexto 32768 por slot |
| GPU | `-ngl 999`, `-fa on`, KV `q8_0` |
| Pesos / mmproj | 5.168.653.536 B / 918.166.080 B |
| VRAM en carga | 7.192.264.704–7.193.321.472 B de 8.573.157.376 B (~1,38 GB libres) |
| Uso GPU | ~99 % durante el bench |

El servidor es compartido con Open WebUI. Por eso las comparaciones que importan son
las pareadas: baseline y candidato medidos seguidos, en la misma ventana.

## Método

- 143 casos de `bench/cases/*.jsonl` (66 `dev`, 77 `test`).
- Mismo modelo, cuantización, `temperature=0`, `LLM_EXTRA_BODY` y base de datos.
- Baseline completa: 143 casos × 3 ejecuciones = 429 ejecuciones, 0 errores,
  determinismo del cálculo 100 %.
- Para A/B: baseline completa de `main` y ejecuciones del candidato inmediatamente
  después. A se midió en 5 casos con 3 ejecuciones (`qty-001`…`qty-005`) más un smoke
  de 5 casos (`com-001`, `eti-001`, `hos-001`, `qty-001`, `var-001`). B se midió en
  esos mismos 5 casos con 1 ejecución.
- Para C1/C2: 10 casos pareados (1 ejecución) con baseline antes y después, más 3
  ejecuciones de confirmación en `com-001`, `eti-001` y `var-001`, que son los casos
  donde cambió la selección.
- Los casos se ejecutan en serie. Nunca hay dos configuraciones a la vez.
- Puerta de precisión: ninguna regresión estable por caso y métricas globales iguales
  o mejores.
- Puerta de velocidad: media de `total_latency_s` por caso al menos un 15 % menor que
  la baseline de la misma ventana y p95 no mayor.

### Baseline completa (`main`)

| Métrica | Valor |
|---|---:|
| Casos | 143 |
| Ejecuciones | 429 |
| Latencia media por caso | 12,97 s |
| p50 por caso | 11,27 s |
| p95 por caso | 21,08 s |
| p95 por llamada | 9,23 s |
| Llamadas por caso | 2,21 |
| Errores | 0 |
| JSON válido a la primera | 99,5 % |
| Determinismo del cálculo | 100 % |
| Tabla final dentro de rango | 87,7 % |
| Selección válida | 71,3 % |
| Acierto en gramos | 99,2 % |

La baseline pareada de 10 casos se midió en una ventana más cargada del servidor
(media 19,94 s/caso). Las dos pasadas pareadas, antes y después de C1, coincidieron
caso por caso dentro de ~0,1 s, así que la comparación es válida; lo que no se puede
hacer es mezclar su media absoluta con la de la baseline completa.

## Resultados de latencia

### A — una petición por ingrediente, 4 en paralelo

9 casos. Baseline completa de `main`, medida en la misma ventana.

| Caso | main (s) | A (s) | Δ |
|---|---:|---:|---:|
| `com-001` | 32,6 | 43,5 | +33,3 % |
| `eti-001` | 31,9 | 31,7 | −0,7 % |
| `hos-001` | 12,1 | 13,8 | +14,4 % |
| `qty-001` | 11,7 | 11,6 | −0,3 % |
| `qty-002` | 11,0 | 11,0 | −0,3 % |
| `qty-003` | 13,9 | 14,4 | +3,1 % |
| `qty-004` | 11,9 | 11,6 | −2,3 % |
| `qty-005` | 16,7 | 19,2 | +15,4 % |
| `var-001` | 11,5 | 15,6 | +36,0 % |
| **Media** | **16,92** | **19,16** | **+13,2 %** |

p95 por caso: 32,65 s → 43,54 s. **Rechazado por velocidad.** En los casos de un solo
ingrediente A hace exactamente la misma petición que el batch y no gana nada; en los de
varios ingredientes duplica el prompt de sistema y el `motivo` de cada ingrediente, y el
servidor comparte la misma GPU. El resultado es más trabajo total, no menos.

### B — A + ronda 2 especulativa

5 casos, 1 ejecución.

| Caso | main (s) | B (s) | Δ |
|---|---:|---:|---:|
| `com-001` | 32,6 | 52,4 | +60,6 % |
| `eti-001` | 31,9 | 42,2 | +32,4 % |
| `hos-001` | 12,1 | 19,2 | +59,1 % |
| `qty-001` | 11,7 | 17,2 | +47,7 % |
| `var-001` | 11,5 | 18,6 | +62,2 % |
| **Media** | **19,95** | **29,93** | **+50,0 %** |

p95 por caso: 32,65 s → 52,42 s. **Rechazado por velocidad.** B añade peticiones que
casi nunca se aprovechan y compite por los mismos slots.

### C1 — claves JSON cortas

10 casos pareados. Baseline pareada de la misma ventana.

| Caso | main (s) | C1 (s) | Δ |
|---|---:|---:|---:|
| `com-001` | 41,5 | 30,6 | −26,2 % |
| `com-002` | 18,2 | 17,0 | −6,6 % |
| `eti-001` | 39,8 | 24,1 | −39,5 % |
| `gen-001` | 12,0 | 11,5 | −3,6 % |
| `hos-001` | 13,7 | 14,6 | +6,9 % |
| `nf-001` | 18,5 | 19,9 | +7,7 % |
| `off-001` | 12,4 | 11,0 | −11,3 % |
| `qty-001` | 10,9 | 9,7 | −11,0 % |
| `qty-005` | 19,2 | 10,7 | −44,1 % |
| `var-001` | 13,2 | 11,7 | −11,3 % |
| **Media** | **19,94** | **16,09** | **−19,3 %** |

p95 por caso: 41,47 s → 30,59 s. La velocidad **sí** pasa la puerta en esta muestra,
pero la precisión no. Parte de la ganancia no es solo ahorro de tokens: en `qty-005` y
`eti-001` C1 elige en la ronda 1 donde `main` devolvía `null` y necesitaba una segunda
petición. Es un cambio de comportamiento, no solo de formato.

**Rechazado por precisión.**

### C2 — `motivo` corto

10 casos pareados. Baseline pareada de la misma ventana.

| Caso | main (s) | C2 (s) | Δ |
|---|---:|---:|---:|
| `com-001` | 41,5 | 35,3 | −14,9 % |
| `com-002` | 18,2 | 19,1 | +4,6 % |
| `eti-001` | 39,8 | 40,3 | +1,2 % |
| `gen-001` | 12,0 | 13,1 | +9,5 % |
| `hos-001` | 13,7 | 13,9 | +1,8 % |
| `nf-001` | 18,5 | 16,4 | −11,2 % |
| `off-001` | 12,4 | 12,6 | +1,3 % |
| `qty-001` | 10,9 | 11,3 | +2,9 % |
| `qty-005` | 19,2 | 17,9 | −6,9 % |
| `var-001` | 13,2 | 13,2 | −0,1 % |
| **Media** | **19,94** | **19,30** | **−3,2 %** |

p95 por caso: 41,47 s → 40,29 s. **Rechazado por velocidad y por precisión.** El
`motivo` ya era corto en la mayoría de los casos; limitarlo no compensa el coste de
parsear y generar el resto del JSON.

## Precisión

### C1: regresión estable en `com-001`

`com-001` es el caso obligatorio de macarrones con ternera picada y tomate frito. En
las 3 ejecuciones de confirmación, C1 deja la tabla final en **2/3 ingredientes**
(`main`: 3/3):

- ejecución 0: `Tomate frito` → `null` (falso «no encontrado»);
- ejecuciones 1 y 2: `Ternera picada` → `null` (falso «no encontrado»).

| Métrica (10 casos) | main | C1 |
|---|---:|---:|
| Tabla final dentro de rango | 100,0 % | 96,3 % |
| Falsos «no encontrado» | 0,0 % | 10,0 % |
| Supuesto declarado (variantes) | 75,0 % | 58,3 % |
| Selección válida | 81,5 % | 84,0 % |
| JSON válido a la primera | 100,0 % | 100,0 % |
| Determinismo del cálculo | 100,0 % | 100,0 % |

Cambios de selección:

- `com-001` ing 0 (ternera): `USDA: Beef, ground, 80% lean meat / 20% fat, raw`
  (válido) → `null` en 2 de 3 ejecuciones; en la restante,
  `USDA: Beef, grass-fed, ground, raw` (no válido).
- `com-001` ing 2 (tomate frito): `OFF: Tomate frito` → `null` en 1 de 3 ejecuciones.
- `eti-001` ing 1: `OFF: Aceite de oliva` → `USDA: Oil, olive, salad or cooking`
  (ambos válidos).
- `qty-005` ing 0: `OFF: Aceite de oliva` → `USDA: Oil, olive, salad or cooking`
  (ambos válidos).

La ganancia de latencia de C1 no compensa un falso «no encontrado» estable en un caso
obligatorio. **Rechazado.**

### C2: regresión estable en `eti-001`

`eti-001` es el caso obligatorio de sepia con etiqueta y huevos cocidos. En las 3
ejecuciones de confirmación, C2 elige **`USDA: Egg, whole, cooked, fried`** en vez de
`USDA: Egg, whole, cooked, hard-boiled`:

| Dato | main | C2 |
|---|---:|---:|
| Huevo elegido | hard-boiled | fried |
| Tabla final | 3/3 | 2/3 |
| Total kcal | 604,2–605,8 | 659,1 |
| Rango esperado | 596–612 | fuera |
| Grasas totales | 25,8 | 31,3 |
| Rango esperado | 25,0–26,6 | fuera |

| Métrica (10 casos) | main | C2 |
|---|---:|---:|
| Tabla final dentro de rango | 100,0 % | 96,3 % |
| Total dentro de rango | 100,0 % | 0,0 % |
| Selección válida | 81,5 % | 90,7 % |
| Falsos «no encontrado» | 0,0 % | 0,0 % |
| JSON válido a la primera | 100,0 % | 100,0 % |
| Determinismo del cálculo | 100,0 % | 100,0 % |

Cambios de selección:

- `eti-001` ing 2: `USDA: Egg, whole, cooked, hard-boiled` →
  `USDA: Egg, whole, cooked, fried` (**fuera del rango**).
- `var-001` ing 0: `USDA: Beef, grass-fed, ground, raw` →
  `USDA: Beef, ground, 80% lean meat / 20% fat, raw` (ambos aceptables).

El cambio de `com-001` (ternera 80/20) no es atribuible a C2: la baseline completa
también elige 80/20 en las 3 ejecuciones. La diferencia que aparece en la muestra
pareada es ruido de la ventana, no del ajuste.

**Rechazado.**

### A y B: selección distinta, sin error de pipeline

En la muestra de A/B no hubo errores de pipeline, reintentos ni `DET-MISMATCH`. A y B
cambian la elección de ternera de `com-001` a `USDA: Beef, grass-fed, ground, raw`, que
no está entre los candidatos aceptables del caso, pero la tabla final sigue dentro de
rango. No importa para la decisión: ambos se rechazan por velocidad.

## Por qué no hay ganancia

- **A/B:** el servidor tiene 8 slots, pero una sola GPU. Cuatro peticiones simultáneas
  no reducen el trabajo total: lo aumentan. Cada ingrediente repite el prompt de sistema
  y genera su propio `motivo`; el batch procesa el prompt una vez y genera todos los
  motivos en una sola respuesta.
- **C1:** las claves cortas ahorran tokens de salida y de prompt, y en la muestra
  pareada sí baja la latencia. Pero el modelo cambia de elección en casos concretos; en
  `com-001` provoca un falso «no encontrado» en las 3 ejecuciones. La ganancia no es
  separable del cambio de comportamiento.
- **C2:** el `motivo` no era el cuello de botella. La mayor parte del tiempo se va en
  procesar el prompt y generar el JSON completo, no en las palabras del motivo.

## Reproducir

El código se retiró. Para repetir el experimento habría que reimplementar los ajustes en
una rama nueva, medir la baseline completa y aplicar las mismas puertas. El baseline se
regenera con:

```bash
export PATH="$HOME/.local/bin:$PATH"
export LLM_API_KEY=...
uv run python bench/run.py --models bench/models.yaml --runs 3
```

Los resultados van a `bench/results/` y no se commitean.

## Limitaciones

- La baseline completa se midió entera (143 × 3). A se midió en 9 casos (5 con 3
  ejecuciones y 4 con 1), B en 5 casos con 1 ejecución, y C1/C2 en 10 casos pareados
  más 3 ejecuciones de confirmación en los casos donde cambió la selección.
- No se completó la matriz de 6 configuraciones × 143 × 3 porque, con estos resultados,
  no podía cambiar la decisión: A y B ya eran más lentos, C1 y C2 ya tenían regresiones
  estables en casos obligatorios. Ejecutarla habría costado ~6 h más.
- El servidor es compartido con Open WebUI. Las medias absolutas cambian entre ventanas;
  por eso se usan comparaciones pareadas y se evita mezclar la baseline completa con la
  pareada.
- `eti-001` no siempre da 604,2 kcal: según elija aceite OFF o USDA (ambos válidos),
  da 604,2 o 605,8 kcal. La aritmética es determinista; lo que varía es el candidato
  válido elegido. C1 elige USDA de forma estable (604,2), pero rompe `com-001`; C2
  rompe `eti-001` al elegir huevo frito.
