# Revisión de diseño — integración A-S con reconstrucción de ejecución

Fecha: 2026-09-25.
Estado: **DISEÑO PENDIENTE DE REVISIÓN; INTEGRACIÓN AÚN NO IMPLEMENTADA**.

## Pedido para el modelo revisor en OpenCode

Revisa este diseño para conectar las cotizaciones del bot A-S con el motor
de reconstrucción de ejecución ya aprobado. Contrástalo con el código local
listado abajo. Identifica omisiones, errores de causalidad, diferencias frente
al bot actual, problemas de unidades y puntos que necesitan pruebas sintéticas.

Esta es una revisión de diseño, no una aprobación de código inexistente. No
implementes cambios, no ejecutes simulaciones sobre `data/`, no reinicies la
captura ni accedas a la API, credenciales o cuentas. Puedes ejecutar las pruebas
sintéticas existentes del núcleo indicadas abajo. No cambies el manifiesto de
aprobación del núcleo ni amplíes su alcance a esta integración.

Guarda el informe en `md/AS_INTEGRATION_DESIGN_REVIEW.md`. Incluye:

1. Veredicto `DESIGN_APPROVED` o `CHANGES_REQUESTED`.
2. Hallazgos clasificados por severidad, con referencia a esta sección o al
   archivo y línea del código existente, impacto y solución propuesta.
3. Arquitectura recomendada, contratos de entrada/salida y decisiones pendientes.
4. Pruebas sintéticas obligatorias antes de revisar la implementación.
5. Modelo revisor, fecha y versión/huellas de los archivos realmente inspeccionados.

La aprobación de este diseño permite preparar la implementación. La nueva
implementación deberá revisarse independientemente antes de correrla sobre las
capturas; así se respeta la condición del usuario de revisión antes de ejecución.

## 1. Punto de partida comprobado

- Núcleo aprobado: `research/chronological_execution.py`.
- Runner aprobado: `scripts/replay_reviewed_execution.py`.
- Informe: `md/RECONSTRUCTION_INDEPENDENT_REVIEW.md`.
- Manifiesto: `md/reconstruction_review.json`, con cuatro hashes verificados.
- Commit de la entrega del núcleo: `c2dbb64`.
- 64 pruebas sintéticas/regresión aprobadas en esa entrega.
- El núcleo usa ticks/lotes enteros, latencias, cola visible completa y fills
  parciales causados por trades posteriores al precio exacto de la orden.
- El runner consume un journal de comandos; **no genera decisiones A-S**.
- `logs/decisions/agent_decisions.jsonl` contiene 11.744 decisiones entre el
  9 y el 22 de agosto de 2026. No corresponde a la captura de septiembre y
  tampoco tiene el contrato de comandos submit/cancel requerido por el runner.
- Dataset disponible: `data/shadow_l2_capture_20260924/2026-09-24/`, unas
  4 h 11 min, 123.184 snapshots y 116.900 trades públicos. No se utilizará
  durante esta revisión. No se solicita una nueva captura.

El worktree contiene cambios y archivos ajenos a esta entrega. Revisar el
estado local efectivo y conservar esos cambios; no resetear, limpiar ni incluir
todo el repositorio en un commit.

## 2. Objetivo de la integración

Reproducir, en un proceso offline, las decisiones del bot sobre eventos históricos
y su ejecución hipotética, con retroalimentación de inventario. Obtener una
línea base trazable antes de comparar reglas contra selección adversa.

La primera entrega integrada conserva las reglas existentes: no incorpora un
filtro nuevo de volatilidad, OBI o caducidad ni optimiza parámetros. Las variantes
se evaluarían después de validar la línea base.

No se admite reemplazar las cotizaciones A-S por órdenes al touch y presentar
el resultado como si reprodujera el bot. Tampoco usar `fill_prob >= 0.5` como
prueba de ejecución.

## 3. Arquitectura propuesta: un solo reloj y retroalimentación

Secuencia conceptual:

```text
eventos L2/trades -> estado de mercado causal
                         |
                  ciclo de decisión A-S
                         |
                 validación y comandos
                         |
              reconstrucción de ejecución
                         |
               fills / inventario / cash
                         |
                 próximo ciclo A-S
```

El journal debe ser una SALIDA de esta interacción. No se debe precalcular toda
la sesión de cotizaciones con inventario cero y ejecutar después: los fills
anteriores cambian el inventario, el tamaño y las cotizaciones posteriores.

Propuesta a revisar:

- Un coordinador offline consume eventos por orden temporal y mantiene el reloj
  del replay. Entrega a la política únicamente estado ya observable y resultados
  de ejecución disponibles hasta ese instante.
- Los ciclos de decisión se programan con la frecuencia congelada de configuración,
  no cada N filas arbitrarias. Se define el inicio, calentamiento y el tratamiento
  de ciclos sin un libro suficientemente reciente.
- El núcleo debe admitir interacción incremental con la política. Su método
  `Replay.run()` actual es de una sola corrida: evaluar una interfaz `step`/callback
  sin duplicar la lógica de cola, timers ni contabilidad.
- Toda modificación del núcleo o runner aprobado invalida sus hashes y exige
  nueva revisión. No modificar los hashes para simular una aprobación heredada.
- Para eventos simultáneos se conserva inicialmente el orden documentado del
  núcleo: trades, cancelaciones efectivas, llegadas, libros, comandos. El revisor
  debe precisar dónde entra el ciclo de decisión y evitar que use un evento aún
  no procesado del mismo milisegundo.
- El inventario observado por el siguiente ciclo proviene exclusivamente de fills
  reconstruidos, incluyendo parciales. Un rechazo o cancelación no altera posición.

## 4. Reutilización de la lógica existente

Archivos y responsabilidades a inspeccionar:

| Archivo existente | Punto relevante |
|---|---|
| `strategy/market_state.py` | Callbacks, ventanas, muestreo, `get_snapshot`, volatilidad y flujo |
| `strategy/alpha_model.py` | `record_mid`, `compute_alpha`, `reservation_price`, `quote_distances`, estimación de costes |
| `strategy/market_maker.py` | `_compute_quotes`, `_manage_orders`, `_place_order`, `_replace_order`, estado por ciclo |
| `strategy/inventory_manager.py` | Agresión por lado, inventario y registro de fills |
| `strategy/risk_engine.py` | Límites/rechazos existentes y entradas necesarias para evaluación offline |
| `strategy/execution_engine.py` | Normalización de precios/cantidades, validaciones y semántica reduce-only; inspección estática |
| `strategy/config.py` | Frecuencia, parámetros de riesgo, spreads, exposición y costes |
| `research/chronological_execution.py` | Reloj, timers, fills, reservas de posición y contabilidad |
| `scripts/replay_reviewed_execution.py` | Validación de entradas, procedencia, revisión y salidas |

Preferir extraer/adaptar interfaces puras y probar equivalencia contra las
funciones existentes. No copiar fórmulas y luego mantener dos estrategias con
el mismo nombre. No inicializar `MarketMaker`/`ExecutionEngine` de forma que
active API, hilos, WebSockets o logs operativos. La elección concreta debe
justificarse después de inspeccionar efectos secundarios de imports/constructores.

Puntos específicos observados en el código que deben resolverse:

- `quote_distances` acepta `now_sec` pero usa `time.time()` si no se proporciona;
  `_compute_quotes` actualmente no propaga ese parámetro. También hay reloj real
  en la gestión de edades. Todo ello debe recibir tiempo simulado explícito.
- `_compute_quotes` usa `snapshot.get("inventory") or self.inventory.inventory`:
  inventario cero explícito no debe sustituirse por un valor previo distinto.
  Una corrección de comportamiento debe documentarse y probarse; no ocultarla
  como una reproducción idéntica de la versión anterior.
- El cálculo de tamaños usa `effective_exposure_multiplier`, agresión por lado,
  límite de tamaño y minNotional. No basta reutilizar solo `AlphaModel`.
- `_manage_orders` decide expiración, cancelación por lado inválido y reemplazo
  por movimiento del mid. El núcleo mantiene el lado ocupado hasta cancelación
  efectiva: resolver esa diferencia frente a `_replace_order` y comprobar fills
  que ocurren mientras la cancelación viaja.
- `MarketState` normaliza sigma a un intervalo de referencia; `AlphaModel`
  reescala sigma. Auditar dimensiones y factores de tiempo antes de reutilizarlos.
  No introducir silenciosamente una fórmula nueva ni usar sigma por tick como
  si fuera sigma por 5 s.
- La captura tiene depth y trades; no incluye el flujo bookTicker independiente
  que recibe el bot. Definir la aproximación de estado y frecuencia de muestreo,
  y documentar qué equivalencia con producción no puede demostrarse.

## 5. Contratos de datos y parámetros

- Snapshot de decisión: timestamp, último libro conocido, mid/spread/microprice,
  imbalance y flujo con ventanas causales, sigma con unidad documentada,
  inventario actualizado y reglas del símbolo explícitas.
- Comandos: timestamp de decisión, ID único, lado, precio/cantidad normalizados,
  propósito submit/cancel y motivo. Conservar también la cotización sin normalizar
  para auditar los efectos de tick y step.
- Aplicar reglas maker de redondeo verificadas contra el engine existente;
  validar tick, step, minQty y minNotional antes de emitir el comando. No deducir
  specs legales solo por observar incrementos en un CSV. Registrar fuente/fecha;
  si no hay evidencia histórica, identificar el supuesto y su limitación.
- El `max_position_lots` del núcleo no sustituye al límite en USDC del bot.
  Validar notional con el precio vigente e incluir cantidades pendientes y vivas
  en los escenarios de inventario, sin compensar órdenes opuestas aún no llenadas.
- Especificar y probar semántica reduce-only si la política la necesita. El núcleo
  aprobado no tiene ese contrato; no afirmar equivalencia operativa omitiéndolo.
- Crear un snapshot de configuración del experimento; no cambiar los valores
  globales de `strategy/config.py` ni aumentar exposición para habilitar el replay.
  El tamaño de simulación Nivel 0 ya está aprobado, sin habilitar órdenes reales.
- Fees y funding tienen procedencia explícita. Una estimación de coste usada por
  la política para decidir no es lo mismo que costes cobrados en la contabilidad.

## 6. Integridad, métricas y límites del modelo

- Gaps, secuencia inválida o libro obsoleto: registrar fallo de cobertura según
  el contrato revisado. No coser sesiones, resetear inventario o inventar cierres.
- Conservar censura al final de la muestra; no inventar liquidez para cerrar.
- Registrar intentos de órdenes, aceptaciones/rechazos, parciales, órdenes llenadas,
  tiempo de cotización, inventario, mark-to-mid bruto y cobertura por horizonte.
- Separar número de eventos de fill, número de órdenes con fill y cantidad ejecutada.
  No denominarlos indistintamente fill rate.
- Markout positivo = favorable; separar spread capturado y movimiento adverso
  usando un mismo denominador. No sumar horizontes superpuestos como beneficios.
- Comparar PnL bruto y costes únicamente cuando su definición esté completa;
  no llamar NetPnL a un proxy de spread menos markout.
- Publicar contador/tasa de `unmodelled_trade_through`, rechazos por profundidad
  desconocida y antigüedad del libro. El modelo exact-price puede omitir justamente
  fills adversos: cualquier mejora de filtros se interpreta con esa limitación.
- Cola sin cancelaciones y latencias fijas son hipótesis. Definir sensibilidad
  posterior, sin elegir el escenario que arroje mayor rentabilidad.

## 7. Pruebas sintéticas requeridas antes de ejecutar capturas

1. Una compra parcial actualiza el inventario del próximo ciclo y modifica el
   skew/tamaño esperado. Cancelaciones y rechazos no cambian inventario.
2. Un cero explícito de inventario se conserva aunque exista estado anterior.
3. Cambiar o anexar precios futuros no altera decisiones/fills anteriores al corte.
4. El mismo input/configuración produce el mismo journal independientemente de
   la fecha del sistema, velocidad de ejecución y zona horaria.
5. Casos de referencia comparan reserva, distancias, tamaños y flags con la lógica
   existente, enumerando cualquier diferencia intencional.
6. Series controladas verifican unidades y escalado temporal de volatilidad,
   warm-up, ventanas de flujo e inclusión/exclusión de eventos simultáneos.
7. No hay segunda orden del mismo lado durante cancelación pendiente; un fill
   intermedio modifica tanto remanente como inventario antes de reemplazar.
8. Bordes tick/step/minQty/minNotional, rechazo GTX a la llegada y profundidad
   desconocida no producen órdenes/fills artificiales.
9. El tope notional se mantiene al reservar pendientes; un cambio de precio no
   permite confundir límite de lotes con límite en USDC. Reduce-only no invierte
   una posición si se incorpora a la interfaz.
10. Gaps y final de captura no reinician silenciosamente el portafolio.
11. Fallos de red simulados como trampas de test comprueban que imports y ejecución
    offline no intentan conexiones ni inicializan clientes operativos.
12. La barrera de revisión cubre TODOS los archivos nuevos/modificados y dependencias
    relevantes de la política; una modificación invalida la autorización de ejecución.

Pruebas existentes que pueden ejecutarse durante esta revisión de diseño:

```powershell
python -m pytest -q tests/test_chronological_execution.py tests/test_execution_reconstruction.py
```

Las doce pruebas de integración anteriores son requisitos; **todavía no existen
ni se afirma que hayan pasado**.

## 8. Entrega esperada y condición de ejecución

El revisor entrega primero el dictamen de diseño y sus correcciones. El
implementador prepara después el adaptador, coordinador, pruebas y manifiesto
de procedencia. Esos archivos y cualquier dependencia modificada vuelven a
revisión independiente. Solo tras resolver hallazgos y verificar los hashes de
la implementación integrada se ejecutaría el baseline sobre la muestra guardada.

No se solicita operar en mainnet, consultar cuentas, levantar capturas nuevas,
entrenar ML ni optimizar reglas como parte de este documento.
