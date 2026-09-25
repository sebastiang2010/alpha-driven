# Revisión independiente — reconstrucción de ejecución maker

Estado: **PENDIENTE DE REVISIÓN**. Preparado el 2026-09-25.

Verificación del implementador: **64 pruebas sintéticas/regresión aprobadas**.
Incluyen un recorrido completo del CLI con archivos ficticios temporales. La
aprobación usada en ese fixture es local al test y no altera el manifiesto real.

## Pedido para pegar en OpenCode

Revisa esta implementación como segundo modelo independiente, antes de ejecutar
cualquier simulación sobre capturas reales. Lee este documento, `AGENTS.md` y
los archivos listados abajo. Busca errores de causalidad, contabilidad, cola,
volumen y cobertura; agrega o propone contraejemplos sintéticos. Puedes ejecutar
exclusivamente las pruebas sintéticas indicadas. No leas credenciales ni importes
`API_binance_futuros.py`; no abras red, no captures datos, no envíes órdenes ni
cambies la estrategia operativa. No ejecutes los runners con archivos de `data/`.

Entrega hallazgos con severidad, archivo/línea, reproducción mínima e impacto.
Clasifica el resultado como `APPROVED` o `CHANGES_REQUESTED`; separa errores de
implementación de supuestos de modelado. No modifiques el código ni declares la
revisión aprobada si quedan hallazgos bloqueantes. Guarda tu informe en
`md/RECONSTRUCTION_INDEPENDENT_REVIEW.md`. Un resultado aprobado se refiere solo
al núcleo y al replay de journals descritos aquí, no a rentabilidad ni a mainnet.

## Archivos a revisar

- `research/chronological_execution.py`: núcleo secuencial offline.
- `scripts/replay_reviewed_execution.py`: lector de CSV/JSONL y barrera de revisión.
- `tests/test_chronological_execution.py`: pruebas sintéticas del núcleo y barrera.
- `strategy/execution_reconstruction.py`: dependencia existente, sin cambios;
  se reutilizan `QueuePositionTracker` y `PartialFillEngine`.
- `tests/test_execution_reconstruction.py`: regresiones de esa dependencia.
- `md/reconstruction_review.json`: estado pendiente, no habilitar sin revisión.

Comando permitido:

```powershell
python -m pytest -q tests/test_chronological_execution.py tests/test_execution_reconstruction.py
```

## Contexto y alcance exacto

El piloto anterior seleccionaba `fill_prob >= 0.5` y usaba el instante de la
cotización como instante de fill. Además, reducía esa probabilidad después de
mirar cancelaciones futuras. No era una reconstrucción temporal de ejecución.
El nuevo componente exige trades posteriores y volumen suficiente en la cola.

Esta entrega prepara **el núcleo de ejecución y un runner para un journal de
órdenes causal**. Aún no adapta `_compute_quotes` de A-S ni genera ese journal
a partir de la captura; tampoco compara filtros, ajusta parámetros o realiza
un backtest de rentabilidad. La integración de cotizaciones A-S deberá revisarse
antes de afirmar que se está reproduciendo el bot. No usar los CSV del piloto
como si fueran un journal de decisiones A-S.

La captura previa permanece detenida. No se corrió este código contra datos
históricos de mercado. Las pruebas usan únicamente fixtures pequeños en memoria
y archivos temporales para comprobar el bloqueo de revisión.

## Contrato y supuestos para cuestionar

1. Precios y cantidades se convierten exactamente a ticks y lotes enteros.
   El runner exige tick/step explícitos con procedencia; no consulta specs ni
   asume las actuales. No redondea silenciosamente. La cantidad mínima/notional
   legal debe verificarse al generar el journal; este núcleo no certifica las
   restricciones de Binance. El límite en lotes es virtual, no cambia config.
2. Latencias positivas y explícitas de colocación/cancelación. Son supuestos,
   no medidas de red. El tiempo del exchange sustituye al de recepción local:
   la captura carece de un reloj de llegada al bot utilizable.
3. Empates de milisegundo: trades, cancelaciones efectivas, llegadas de órdenes,
   libros, comandos; dentro de cada categoría, orden del input. Es una convención
   ambigua, no evidencia del orden real. Trade en el ms de llegada no llena;
   trade en el ms de cancelación todavía puede llenar.
4. Se revalida post-only al llegar la orden. Si cruza, se rechaza. Si el precio
   está fuera de la profundidad conocida, se rechaza como cola desconocida.
   Nivel vacío dentro del rango visible/interior del spread comienza con cola 0.
5. Cola inicial = TODO el volumen público visible, sin restar nuestra orden.
   El helper antiguo resta `our_qty`: el adaptador compensa pasando
   `visible + our_qty`. Cantidades acotadas garantizan exactitud al convertirlas
   a float en el helper. Revisar que esto no esconda supuestos incorrectos.
6. Solo trades agresores al precio EXACTO consumen cola y luego nuestra cantidad.
   No se usa la caída del depth para descontar volumen otra vez; no se estima
   avance por cancelaciones. Nuevos volúmenes no saltan delante de nuestra orden.
   El snapshot puede sobreestimar cola si ya hubo consumo antes de nuestra llegada.
7. Un trade a precio que atraviesa nuestra orden se registra como
   `unmodelled_trade_through`, sin fill. Esta limitación puede sesgar hacia arriba
   la calidad de los fills al omitir barridos adversos. **No es una cota de PnL**.
   No promover reglas si el revisor considera este supuesto inadecuado. Un modelo
   de barridos y escenarios de cancelación sería una ampliación posterior.
8. Una sola orden pendiente/viva por lado. Reemplazos esperan cancelación efectiva.
   Cada trade ID se consume una sola vez. Cantidades pendientes/vivas se reservan
   en ambos extremos de inventario, sin compensar riesgos con órdenes opuestas.
9. Saltos `pu`, libros cruzados, IDs repetidos, timestamps desordenados o libros
   obsoletos invalidan el replay. No se finge cancelación/flatten en un hueco ni
   se resetea inventario. El runner no publica un reporte válido de ese prefijo.
   No detecta todos los posibles trades perdidos por el feed; no se conoce el
   libro antes del primer snapshot ni se comprueba sincronización REST original.
10. Los markouts se anclan al trade que llenó la orden; observación tardía o
    fin de datos producen `None`, no cero ni interpolación. Convención positiva
    favorable. Misma base para: markout = spread capturado − movimiento adverso.
    El PnL bruto del libro virtual se calcula aparte como cash + inventario × mid;
    no se suman markouts de horizontes solapados ni se computa NetPnL.
11. Al final se censuran órdenes aún abiertas; solo se completan timers con
    timestamp <= último evento. No se fabrica una salida a mercado. Último mid
    y timestamp de valoración quedan explícitos en el resumen.
12. El journal debe contener decisiones causales. El runner comprueba orden y
    formato, pero no puede demostrar por sí solo que quien creó el journal no
    usó futuro. Falta evaluar sensibilidad a latencias, cola y órdenes modificando
    el mercado (se asume tamaño suficientemente pequeño).

## Formatos para una futura corrida (NO ejecutar todavía)

Depth: formato existente `ts_ms,update_id,pu,first_update_id,bid_levels,ask_levels`.
Trades: `ts_ms,price,qty,is_buyer_maker,aggressor_source,trade_id`; agresor OBSERVED.
Se acepta un único segmento continuo con archivos `{symbol}_depth.csv` y
`{symbol}_trades.csv`. No juntar islas ni días sin revisar continuidad.

Journal JSONL, ejemplo exclusivamente sintético:

```json
{"ts_ms": 1000, "kind": "submit", "order_id": "bid-1", "side": "BUY", "price": "1.00", "qty": "5"}
{"ts_ms": 1200, "kind": "cancel", "order_id": "bid-1"}
```

Settings JSON: `symbol`, `specs_source`, `quote_source`, `tick_size`, `qty_step` y
`execution` con `place_ms`, `cancel_ms`, `max_book_age_ms`, `max_gap_ms`,
`max_position_lots`. No hay valores sugeridos para una corrida real en esta entrega.

## Criterios de aprobación y habilitación

- Contraejemplos probados para fill prematuro, cancelación tardía, volumen
  duplicado, requeue parcial, empate temporal, gaps y evaluación con futuro.
- No hay imports/transporte de API en el recorrido del runner.
- Supuestos exact-price/cola sin cancelaciones y alcance sin A-S aceptados
  expresamente o informados como bloqueantes por el revisor.
- Revisión escrita, identificando modelo y versión del código.
- Las correcciones se incorporan aquí y se vuelven a revisar si afectan lógica.

Después de una aprobación real, `md/reconstruction_review.json` debe contener
`decision: APPROVED`, `reviewer`, `review_report`, `blocking_findings: []` y los
SHA256 actuales de todos los `REVIEW_FILES` enumerados en el runner. Cualquier
cambio a esos archivos invalida la aprobación. Es una barrera de procedimiento,
no autenticación criptográfica del revisor: no se debe rellenar para saltarla.

El asistente implementador no completó ni aprobó esta revisión. No arrancar
una nueva captura ni ejecutar sobre `data/` durante esta revisión.
