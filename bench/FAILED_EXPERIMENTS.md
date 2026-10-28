# Experimentos de rendimiento descartados

Fecha: 2026-10-28
Estado: **histórico**. El código de los cuatro experimentos se retiró. `main` no tiene
`LLM_PARALLEL`, `SELECT_MODE`, `SELECT_SPECULATIVE`, `LLM_SHORT_KEYS` ni
`SELECT_SHORT_MOTIVO`. No hay nada que activar.

## TL;DR

No intentes paralelizar la selección por ingrediente ni acortar las claves o el `motivo`
en esta arquitectura (una GPU, llama.cpp con 8 slots). Se implementó, se midió y no
funciona:

- **A** (una petición por ingrediente, 4 en paralelo): **+13,2 %** de latencia media.
- **B** (A + ronda 2 especulativa): **+50,0 %**.
- **C1** (claves JSON cortas): **−19,3 %** de latencia, pero rompe `com-001` con un falso
  «no encontrado» en 3/3 ejecuciones.
- **C2** (`motivo` corto): **−3,2 %** y rompe `eti-001` eligiendo huevo frito
  (659,1 kcal frente a 596–612).

El detalle, las tablas y las diferencias de selección están en
[`PERF_COMPARISON.md`](PERF_COMPARISON.md).

## Por qué falló

1. **Una sola GPU no paraleliza de verdad.** Los 8 slots de llama.cpp comparten la misma
   computación. Cuatro peticiones simultáneas no reducen el trabajo total: lo aumentan.
   Cada ingrediente repite el prompt de sistema y genera su propio `motivo`; el batch
   procesa el prompt una vez y genera todos los motivos en una sola respuesta.
2. **La concurrencia no es gratis.** En los casos de varios ingredientes, las peticiones
   concurrentes tardan aproximadamente el doble cada una; el tiempo de pared no baja.
3. **Las claves cortas cambian el comportamiento del modelo.** Ahorran tokens, pero C1
   eligió `null` donde `main` elegía un candidato válido. La ganancia de velocidad no se
   puede separar del cambio de selección.
4. **El `motivo` no era el cuello de botella.** La mayor parte del tiempo se va en
   procesar el prompt y generar el JSON completo, no en las palabras del motivo.

## Canarios obligatorios

Cualquier intento futuro debe pasar estos dos casos **3/3** antes de mirar la latencia:

- `com-001`: macarrones con ternera picada y tomate frito. C1 lo rompió.
- `eti-001`: sepia con etiqueta y huevos cocidos. C2 lo rompió.

Después, el bench completo de 143 casos × 3 ejecuciones y las dos puertas:

- precisión: ninguna regresión estable por caso y métricas globales iguales o mejores;
- velocidad: media de `total_latency_s` por caso ≥15 % menor y p95 no mayor.

## Cuándo tendría sentido reintentarlo

Solo si cambia la arquitectura de forma sustancial:

- varias GPUs o varios servidores con computación independiente (no solo más slots en la
  misma GPU);
- un runtime con decodificación realmente paralela o mucho más rápida;
- un modelo que sea robusto a cambios de formato de prompt, validado con el bench;
- una arquitectura que reparta parseo y selección entre workers sin duplicar el prompt.

Incluso entonces: reimplementar, medir la baseline completa y repetir las puertas. No
basta con que «parezca más rápido».

## Evidencia

- Baseline completa en `main`: 143 casos × 3 ejecuciones, 12,97 s/caso de media,
  p95 21,08 s, 0 errores, determinismo del cálculo 100 %.
- Los JSONL crudos están en `bench/results/` y no se commitean; se regeneran con
  `uv run python bench/run.py --runs 3`.
- La rama `perf/parallel-selection` y sus commits (`cb340dc`, `e66444a`, `e176d1a`,
  `d3f5997`, `94a4e9e`, `1efc8a9`) se eliminaron. No forman parte de `main`.
