# Integración A-S offline — diseño V2 para segunda revisión

Fecha: 2026-09-25. Estado: **PROPUESTO; NO APROBADO; NO IMPLEMENTADO**.

Responde a `md/AS_INTEGRATION_DESIGN_REVIEW.md` (`CHANGES_REQUESTED`).
Complementa `md/AS_INTEGRATION_REVIEW_REQUEST.md` y sustituye sus propuestas
abiertas sobre interfaz incremental, reloj, inventario y volatilidad. El resto
de restricciones, validaciones y límites del modelo sigue vigente.

## 1. Alcance y trazabilidad de los cambios solicitados

| Hallazgo del primer dictamen | Respuesta propuesta | Pruebas |
|---|---|---|
| Interfaz incremental sin contrato | Sección 2: avance, comandos, resultados y cierre separados | 13 y 17 |
| Reloj y frecuencia no definidos | Sección 3: reloj entero, fases y calendario fijo | 14 y 18 |
| Feedback de inventario incompleto | Sección 4: estado autoritativo y actualizaciones atómicas | 16 |
| Auditoría de sigma pendiente | Sección 5: fórmulas observadas, inconsistencia y corrección explícita | 6 y 19 |
| Cero sustituido por estado previo | Sección 4: campo obligatorio, sin fallback por falsedad | 15 |
| Ciclo dentro de eventos simultáneos | Sección 3: decisión después de libros y antes de comandos | 14 |

Estos son cambios propuestos por el implementador, no hallazgos cerrados por
el revisor. No se modifica su informe ni el manifiesto de aprobación del núcleo.
La observación del dictamen sobre no modificar el núcleo se interpreta como
no heredar su aprobación: una extensión incremental necesita nueva revisión.
No se creará otro motor de cola o contabilidad para evitar esa revisión.

## 2. Contrato incremental concreto

Firmas conceptuales; no existe todavía esta API:

```python
advance_to(ts_ms: int, market_events: Sequence[Event]) -> ExecutionDelta
apply_commands(ts_ms: int, commands: Sequence[Command]) -> ExecutionDelta
state() -> ExecutionSnapshot
finish(observed_end_ms: int) -> FinalResult
```

- Un coordinador es dueño del calendario y del lector. El motor es dueño de
  timers, órdenes, cola, fills, inventario y cash. La política no recibe el
  motor ni acceso a la captura, timers futuros o markouts.
- `advance_to(t, batch)` procesa todos los eventos externos del tramo desde
  el avance anterior hasta `t`, ambos streams incluidos, y los timers vencidos
  hasta `t`. El batch contiene solo book/trade; no comandos ni timers externos.
  En el primer avance se admiten los eventos del comienzo observado.
- El lector debe cerrar completamente el grupo de timestamp `t` antes de
  entregarlo. Puede mirar la clave del siguiente registro para cerrar el grupo,
  pero nunca entregar su contenido a la política. No dividir un timestamp entre
  dos llamadas. Rechazar eventos tardíos y avance repetido/regresivo.
- El merge del batch y timers usa el orden de la sección 3. Un batch vacío
  permite un ciclo entre eventos, siempre dentro de la cobertura observada.
- `apply_commands(t, commands)` es la única fase de comandos del avance `t`;
  exige que `advance_to(t, ...)` haya concluido. Se llama una vez, incluso con
  lista vacía. No avanza el reloj ni ejecuta timers futuros. Todos los comandos
  llevan exactamente `t`, IDs únicos y un orden serial explícito.
- Los comandos crean timers de latencia estrictamente positiva. Ningún submit
  puede generar un fill en su ciclo de creación. Los rechazos y cancelaciones
  inmediatas se devuelven como eventos; no como cambios de posición.
- `ExecutionDelta` contiene exclusivamente nuevos fills, transiciones de orden
  y la copia del estado posterior a la llamada. Cada evento tiene secuencia
  creciente para impedir doble consumo. `state()` es una copia sin referencias
  mutables al motor: inventario entero, cash entero y órdenes con remanentes,
  estados y timestamps. No expone eventos futuros ni métricas ex post.
- `finish(T)` exige que `T` sea el último timestamp público ya procesado. No
  drena timers posteriores: registra censura y cierra el motor una sola vez.
  No permite llamadas posteriores ni markouts sobre una corrida inválida.
- `Replay.run()` y la nueva interfaz deben compartir el mismo dispatcher y
  validaciones. Una prueba de equivalencia con comandos fijados verifica todos
  los fills, inventario, cash, órdenes y journal. El adaptador de compatibilidad
  no debe cambiar el orden de los comandos de journals antiguos.

La garantía de batch completo pertenece al lector/coordinador; el motor por sí
solo no puede detectar una fila futura que el caller omitió deliberadamente.
Por ello ambos componentes y sus pruebas están dentro de la nueva revisión.

## 3. Reloj, empates, cobertura y ciclos

Todo timestamp es entero en milisegundos. Solo al llamar funciones que esperan
segundos se usa `t / 1000`. No se usa fecha del sistema, sleep ni reloj real.

Orden total propuesto para cada timestamp:

| Fase | Acción |
|---|---|
| 0 | Trades públicos, orden estable del archivo; actualizan ejecución y flujo |
| 1 | Cancelaciones efectivas, orden serial de creación del timer |
| 2 | Llegadas de órdenes, orden serial de creación del timer |
| 3 | Libros públicos, orden estable del archivo y validación de secuencia |
| 4 | Como máximo un ciclo A-S, si corresponde al calendario |
| 5 | Comandos resultantes, en orden serial explícito |

Los ranks son una propuesta para la integración, no los números actuales de
`PRIORITY`. Se conserva el orden relativo del núcleo aprobado. Un trade en `t`
puede llenar antes de una cancelación efectiva en `t`; no llena una llegada en
`t`. Una llegada en `t` usa el libro anterior, no el libro de fase 3 de `t`.
La decisión sí ve todos los resultados de fases 0–3, nunca los posteriores.
El estado de mercado se actualiza después de validar cada evento y antes de
avanzar a la siguiente fase. Un evento inválido aborta sin publicar un resultado
válido ni continuar desde un estado parcialmente aplicado.

Esto es una convención determinista de tiempo de mercado, no evidencia del
orden real de recepción entre streams. Sin timestamps de recepción y latencias
de datos, no puede afirmarse equivalencia con la información del bot en vivo.

Calendario: `t0` es el primer libro válido; `D = 1000*CYCLE_INTERVAL_SEC` debe
ser un entero positivo exacto, congelado en settings. Ciclos en `t0 + k*D`,
incluido k=0, solo hasta el último timestamp público. Con el valor actual D es
5000 ms. El coordinador intercala esos ciclos con todos los grupos de mercado,
sin saltarse trades ni libros entre ciclos.

Calentamiento: `W` es el máximo de las ventanas de mercado y momentum exigidas
por la configuración congelada. Antes de `t0+W` se actualizan features y se
registra `warmup`, pero no se emiten submits. Se exige además al menos tres
muestras de mid con intervalos positivos. No se inventan mids anteriores.
El historial de `AlphaModel.record_mid` se alimenta una vez por ciclo fresco,
también durante warm-up; no una vez por cada trade.

Entre eventos: las ventanas se evalúan respecto de `t` del ciclo, no respecto
del último evento. No se agrega una muestra de mid ficticia en cada ciclo.
Si faltan muestras suficientes se registra `insufficient_samples` sin submits.
Si hay libro obsoleto o gap se invalida la corrida, incluso durante warm-up;
no se oculta como volatilidad cero ni se resetea el inventario. Las reglas de
antigüedad del núcleo siguen aplicándose antes de arrivals y fills.
No se toman decisiones después del EOF ni se fuerza un cierre de posición.

Reemplazo: cancelar deja el lado ocupado mientras viaje la cancelación. Se
recalcula una nueva cotización en el siguiente ciclo programado que encuentre
el lado libre; no se envía automáticamente una cotización vieja al cancelarse.
Esta diferencia frente al reemplazo inmediato del bot se declara y se prueba.

## 4. Inventario: fuente única, parciales y cero

El portafolio virtual comienza plano, cash cero. No se consulta una cuenta.
Por cada fill de `q` lotes al precio `p` ticks el motor actualiza atómicamente:

```text
signo = +1 BUY, -1 SELL
inventory_lots += signo*q
cash_units -= signo*q*p
remaining_lots -= q
```

Cada parcial cuenta inmediatamente, no se espera la ejecución total. Después
de `advance_to(t, ...)` y antes de fase 4 se construye una copia inmutable:

```text
snapshot.inventory_lots = execution_state.inventory_lots
snapshot.inventory = inventory_lots * qty_step        [XRP]
snapshot.cash = cash_units * qty_step * tick_size      [USDC bruto]
snapshot.ts_ms = t
snapshot.book_ts_ms = timestamp del último libro
```

La conversión externa usa valores exactos de settings; la contabilidad conserva
enteros. Si la política necesita float, se convierte solo en su frontera y
se conservan los enteros para validación y auditoría. Inventario debe existir
y ser finito: cero es válido; ausencia/None es un error, no un fallback.
Está prohibido `snapshot.get('inventory') or old_inventory` en esta ruta.

La agresión de inventario y los límites leen ese mismo estado. Si se reutiliza
un InventoryManager, recibe una proyección del estado autoritativo; no suma
otra vez los fills. Las reservas de órdenes vivas/pendientes se pasan aparte,
sin hacerlas pasar por posición ejecutada. Cancelar/rechazar no modifica cash
ni inventario. Ejemplo: BUY parcial 2 lotes, cancelación del resto y SELL 2
dejan posición exactamente cero, visible como cero en el próximo ciclo.

## 5. Auditoría estática de volatilidad y propuesta de corrección

Evidencia inspeccionada, no resultados de simulación:

- `strategy/market_state.py::_compute_volatility` calcula log-retornos de mids,
  desviación muestral y normalización por intervalo medio positivo.
- `strategy/alpha_model.py::quote_distances` multiplica por `sqrt(60/5)` y
  después por `sqrt(CYCLE_INTERVAL_SEC)`: mezcla minutos con segundos.
- `reservation_price` usa horizonte en minutos con sigma por minuto; esa
  conversión temporal sí es consistente si ambas referencias de muestreo coinciden.
- MarketState lee la referencia desde config; AlphaModel declara otra constante
  local de 5 s. La integración debe usar una sola referencia congelada.

Definiciones y fórmulas propuestas (referencia y horizontes en segundos):

```text
r_i = ln(mid_i / mid_(i-1))                     [retorno adimensional]
s_event = stdev_muestral(r_i)                   [retorno por intervalo observado]
dt_bar = media de intervalos positivos          [s]
s_ref = s_event * sqrt(ref_s / dt_bar)          [retorno al horizonte ref_s]
s_cycle = s_ref * sqrt(cycle_s / ref_s)         [retorno al horizonte cycle_s]
s_min = s_ref * sqrt(60 / ref_s)                [retorno al horizonte 1 min]
variance_H = s_ref**2 * (60*H_minutes/ref_s)     [varianza de retorno al horizonte H]
```

Forma equivalente correcta del ciclo: `s_min * sqrt(cycle_s/60)`.
Con ref_s=cycle_s=5, `s_cycle=s_ref`. La fórmula actual da
`sqrt(60)*s_ref`, aproximadamente 7,746 veces ese buffer de volatilidad.
El factor se refiere al componente sigma, NO al spread final, que además tiene
pisos y otros términos. Es un defecto de escala temporal, no una prueba de PnL.

Propuesta para revisión: corregir esa conversión en la futura lógica compartida,
sin modificar ahora el bot. No ocultarla bajo una afirmación de paridad exacta.
No habilitar la fórmula inconsistente como baseline operativo. Los fixtures
pueden documentar el resultado anterior como regresión de un comportamiento
corregido, no como objetivo que deba mantenerse.

Unidades de precio: spread, reserva y distancias están en USDC/XRP, mientras
sigma es un retorno. Para conservar la estructura aditiva existente se propone
declarar K_QUOTE e INVENTORY_SKEW_MULTIPLIER como coeficientes de precio
[USDC/XRP], y GAMMA_INVENTORY_RISK como [USDC/XRP²], con inventario en XRP.
Así K*s_cycle y ratio_notional*skew_coeff*s_cycle son distancias de precio;
gamma*inventory*variance_H también. Es una convención de modelo que debe
aprobar el revisor, no evidencia de calibración ni una derivación canónica A-S.
Si el revisor exige convertir sigma a volatilidad absoluta multiplicando por
mid, eso exige revisar también coeficientes y fixtures; no se hará implícitamente.
La unidad de alpha y sus pesos también debe verificarse antes de implementar
su suma a la reserva; no inferirla solo del nombre de una variable.

Muestreo offline: un mid por timestamp de depth, tomando el último libro válido
del grupo para las features. Todos los libros siguen procesándose en ejecución.
No hay bookTicker independiente en la captura: la fidelidad de muestreo con
producción no está demostrada. No se insertan mids por cada trade ni ciclo.
La normalización por intervalo medio asume escalado difusivo; no corrige sesgo
de muestreo irregular por sí sola. Declarar esa limitación en el reporte.

Observación adicional: `_compute_momentum` poda el mismo `_mid_samples` que
usa volatilidad, con una ventana menor; por ello snapshots repetidos pueden
recortar el historial de sigma. Propuesta: buffer común retenido por la ventana
máxima, cálculos con vistas independientes a cada ventana, sin poda destructiva
entre features. Esto cambia un comportamiento existente y requiere prueba.
La decisión consume features evaluadas en tiempo simulado explícito, no el
`last_update_ts` usado hoy por `get_snapshot()` para fechar sus ventanas.

## 6. Pruebas de aceptación pendientes

Se conservan las pruebas 1–12 del pedido original; ninguna integración se da
por probada. Se incorporan las 13–16 solicitadas por el revisor y se concretan:

13. Avance incremental conserva estado entre llamadas y coincide con run para
    los mismos eventos/comandos; cada delta se consume una vez.
14. Fixture con trade, cancel efectivo, arrival, book y ciclo en el mismo ms:
    trade gana al cancel, arrival usa libro previo, decisión ve book nuevo y
    posición posterior a trade; submit no llena en ese timestamp.
15. Cero explícito permanece cero aunque una proyección anterior sea no nula;
    None/ausencia fallan en la frontera de decisión.
16. Varios parciales entre ciclos actualizan exactamente posición/cash/remanente;
    cerrar la posición devuelve cero, sin doble contabilización en el adaptador.
17. Rechazar timestamps regresivos, batches partidos, comandos fuera de fase y
    llamadas después de finish; comparar censura/timers con replay aprobado.
18. Reloj calendario, warm-up, ciclos sin eventos y EOF; features envejecen al
    tiempo del ciclo, no al último evento; no decisiones después de cobertura.
19. ref=cycle da sigma_cycle=sigma_ref; multiplicar horizonte por 4 duplica sigma;
    conversión por minutos equivale a conversión directa. Verificar dimensiones
    de todos los términos de precio y registrar diferencias con código anterior.
20. Múltiples snapshots/momentum no recortan ventana de sigma. Dos libros en
    un mismo ms aportan una muestra de features, sin perder validación de ambos.

También son obligatorios los tests de no-red, límites notionales, reduce-only
si se usa, conservación de volumen y barrera de revisión del pedido original.
Los tests aquí enumerados son requisitos futuros, no ejecuciones realizadas.

## 7. Solicitud de segunda revisión y siguiente barrera

Leer V2 junto con el pedido original y contrastar las propuestas con el código.
Guardar un NUEVO dictamen en `md/AS_INTEGRATION_DESIGN_REVIEW_V2.md`, sin
sobrescribir el primer informe. Para cada hallazgo anterior indicar cerrado,
parcial o abierto y justificarlo. Evaluar explícitamente la corrección temporal,
las unidades de coeficientes, la ventana compartida y la aproximación sin
bookTicker; no limitarse a comprobar que ahora hay una firma de interfaz.

Usar `DESIGN_APPROVED` o `CHANGES_REQUESTED`. Identificar el modelo solo si
se conoce; el primer informe no informa un nombre/version concreto. Registrar
hashes de los documentos y fuentes efectivamente revisados.

No implementar código, ejecutar datos históricos, consultar API/cuentas,
alterar procesos ni editar el manifiesto aprobado. Solo están permitidas las
pruebas sintéticas existentes indicadas en el pedido original.

Tras aprobación de diseño: implementación y tests, nueva revisión independiente
del código integrado y hashes, y recién después eventual replay histórico.
La aprobación de V2 no aprueba rentabilidad, operación real ni simulación inmediata.
