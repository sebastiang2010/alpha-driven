# Desarrollo autónomo de agente de Market Making para Binance Futures

*Versión revisada (v2). Resumen de los cambios principales al final de esta introducción.*

## Objetivo

Desarrollar, de forma autónoma, una estrategia algorítmica de **market making adaptativo** para operar en **Binance Futures**, inicialmente sobre un único par:

```text
XRPUSDC
```

La estrategia debe orientarse a realizar operaciones **maker**, utilizando órdenes límite `POST_ONLY` (o el equivalente que exponga la API — en Binance Futures, típicamente el time-in-force `GTX`) cuando el instrumento lo permita, aprovechando la comisión reducida de maker frente a taker.

El objetivo de esta fase **no es maximizar ganancias**, sino determinar si la estrategia puede generar una expectativa positiva después de considerar:

* comisión maker;
* slippage;
* adverse selection;
* spread;
* pérdidas por inventario;
* funding;
* latencia;
* cancelaciones/reemplazos;
* riesgo de liquidación;
* volatilidad.

La estrategia solo puede aumentar su exposición de forma progresiva, y únicamente si los resultados muestran rentabilidad y estabilidad sostenidas en el tiempo — nunca a partir de una única operación ganadora.

**Cambios principales respecto de la versión original:**

1. Nueva Sección 0 (reglas no negociables): testnet obligatorio salvo autorización explícita, condiciones de "detenerse y preguntar", checkpoints/`STATUS.md`, presupuesto de riesgo como constantes de config, manejo de credenciales, y errores conocidos del bot A-S a evitar.
2. Se corrigieron dos fórmulas que habían quedado corrompidas al pegar LaTeX en Markdown: reservation price (Sección 8) y función de reward (Sección 18) — en ambas se había perdido o mal interpretado un signo de resta.
3. Reconexión con backoff para el WebSocket (Sección 2), prueba explícita del kill switch (Sección 13), y tests unitarios obligatorios para el Risk Engine (Sección 12).
4. El escalamiento de nivel (Sección 21) ahora exige aprobación humana explícita además de los criterios cuantitativos.
5. Nota sobre leverage moderado vs. máximo (Sección 4) y sobre convivencia con los otros bots activos en la misma VPS (Sección 0.3).
6. Checklist de cierre nuevo (Sección 24).

---

# 0. Reglas no negociables y protocolo del agente (leer antes de empezar)

Esta sección tiene prioridad sobre cualquier otra parte del documento. Ante un conflicto, gana esta sección.

## 0.1 Testnet primero, mainnet solo con autorización explícita

* Todo el desarrollo, las pruebas y el Nivel 0 (dry-run) deben correr contra **Binance Futures Testnet**, con claves de API de testnet, separadas de cualquier clave de producción ya en uso.
* No enviar ninguna orden a mainnet durante esta sesión autónoma — ni siquiera de tamaño mínimo — salvo que ya exista autorización explícita, otorgada por fuera de este documento, antes de empezar.
* Si al cabo de la sesión el sistema queda listo para pasar a Nivel 1 en mainnet, **detenerse y reportarlo como pendiente de aprobación**; no ejecutar automáticamente.

## 0.2 Condiciones de "detenerse y preguntar"

El agente debe pausar y pedir confirmación humana explícita — no asumir ni improvisar — ante cualquiera de estos casos:

* antes de la primera orden que comprometa fondos reales (mainnet);
* antes de subir de un Nivel de exposición al siguiente (Sección 21);
* si el kill switch se activa una vez;
* si falta en `API_binance_futuros.py` alguna función necesaria y se evalúa escribir una implementación paralela;
* si aparece un estado de posición u orden que no pueda reconciliarse automáticamente;
* si se necesitan credenciales nuevas o rotación de claves existentes.

## 0.3 Checkpoints, trazabilidad y convivencia con otros procesos

* Commits pequeños y frecuentes, uno por módulo o hito funcional, con mensajes descriptivos — así el progreso es revisable sin esperar el final de la sesión.
* Mantener un archivo `STATUS.md` actualizado cada 15–20 minutos: qué se hizo, qué falta, errores encontrados, decisiones pendientes de aprobación humana.
* Mantener una lista de tareas explícita, actualizada durante toda la sesión.
* En la misma VPS corren otros bots en producción (grid bot, bot A-S en BTC/USDC). Antes de arrancar, confirmar que el proceso nuevo corre aislado (venv/proceso propio), con su propio directorio de logs, y que no comparte puertos, archivos ni el límite de rate-limit de la cuenta con los procesos ya activos. No tocar ni reiniciar los procesos existentes.

## 0.4 Presupuesto de riesgo explícito

Estos valores son constantes en `config.py`, no números que el agente decida sobre la marcha:

```text
MAX_DAILY_LOSS_USDC        = <definir>
MAX_POSITION_NOTIONAL_USDC = <definir>
MAX_DRAWDOWN_PCT           = <definir>
MAX_LEVERAGE_USED           = <definir>   # no necesariamente el máximo que permite Binance
```

Si no están definidos al momento de empezar, el agente debe proponer valores conservadores, marcarlos explícitamente en `STATUS.md` como "propuesta pendiente de confirmación", y no operar con fondos reales hasta que se confirmen.

## 0.5 Credenciales

* Solo claves de testnet en esta fase.
* Nunca hardcodear ni loguear claves — tampoco en `agent_decisions.jsonl`, logs de texto, ni mensajes de commit.
* Confirmar que el mecanismo de configuración (`.env` o equivalente) está en `.gitignore` antes del primer commit.

## 0.6 Errores conocidos a evitar

En el bot A-S ya en producción (BTC/USDC, Spot y Futures) aparecieron antes bugs de estas familias. Prestar atención explícita a que no se repitan acá, e idealmente cubrir cada punto con un test o assertion:

* convenciones de volatilidad/tiempo inconsistentes (mezclar sigma anualizada con sigma efectiva, aplicar √T dos veces);
* multiplicadores o tamaños de inventario mal propagados entre el cálculo de quotes y el envío de órdenes;
* IDs de órdenes compartidos entre bid/ask sin purgar correctamente (acumulación de órdenes fantasma);
* llamadas de sincronización de estado/inventario en cada ciclo sin control de frecuencia (riesgo de rate-limit);
* condiciones de carrera sobre listas de IDs de órdenes compartidas entre tareas concurrentes.

---

# 1. API de Binance

Existe un archivo ya desarrollado:

```text
API_binance_futuros.py
```

Este archivo debe considerarse la interfaz principal con Binance Futures.

**No crear una segunda implementación de la API si las funciones existentes son suficientes.**

Primero analizar completamente:

```text
API_binance_futuros.py
```

Identificar las funciones disponibles para:

* conexión;
* autenticación;
* obtener precio;
* obtener profundidad;
* obtener posiciones;
* obtener balance;
* enviar órdenes;
* cancelar órdenes;
* modificar/reemplazar órdenes;
* consultar órdenes;
* consultar fills;
* obtener información del símbolo;
* obtener leverage;
* establecer leverage;
* obtener restricciones de cantidad;
* obtener tick size;
* obtener step size.

Utilizar las funciones existentes siempre que sea posible. Si falta una función necesaria, documentar el gap explícitamente (qué falta, para qué se necesita) antes de decidir implementarla — ver la condición de "detenerse y preguntar" en 0.2 si la solución no es evidente.

---

# 2. WebSocket

En el directorio del proyecto existen ejemplos de utilización de WebSocket.

Antes de implementar nada:

1. localizar todos los ejemplos relacionados con WebSocket;
2. analizarlos;
3. identificar cómo se reciben:
   * order book;
   * trades;
   * ticker;
   * mark price;
   * best bid/ask;
   * actualizaciones de órdenes;
4. reutilizar la arquitectura existente cuando sea apropiado.

La estrategia debe utilizar **datos en tiempo real mediante WebSocket**, evitando depender de polling REST para información que pueda obtenerse mediante streaming.

REST/API debe utilizarse principalmente para:

* configuración;
* consulta de estado;
* envío/cancelación de órdenes;
* recuperación ante errores;
* reconciliación del estado.

Implementar reconexión automática con backoff exponencial ante desconexiones, con un número máximo de reintentos; si se supera ese máximo, activar el kill switch (Sección 13) en lugar de reintentar indefinidamente. Verificar heartbeats/pings según lo que exponga la API, para detectar conexiones que parecen vivas pero dejaron de recibir datos.

---

# 3. Instrumento

Trabajar exclusivamente, por ahora, con:

```text
XRPUSDC
```

en:

```text
Binance Futures
```

Antes de realizar cualquier operación, consultar dinámicamente las especificaciones reales del instrumento:

* quantity precision;
* price precision;
* tick size;
* step size;
* minimum quantity;
* minimum notional;
* maximum quantity;
* leverage permitido;
* modo de margen;
* estado del símbolo.

**No asumir estos valores manualmente.** Mantener el símbolo como parámetro de configuración (no hardcodeado), para facilitar extenderlo a otros pares más adelante.

---

# 4. Leverage

Configurar el **máximo leverage permitido por Binance para XRPUSDC**, pero utilizar inicialmente una exposición nominal extremadamente pequeña.

IMPORTANTE:

> Máximo leverage NO significa utilizar el máximo capital disponible.

El leverage solamente debe utilizarse para permitir una posición pequeña con poco margen.

La cantidad inicial debe ser:

```text
mínima cantidad de XRP permitida por Binance
```

o, si la cantidad mínima no permite una ejecución segura:

```text
la menor cantidad válida inmediatamente superior
```

La estrategia debe determinar automáticamente esta cantidad a partir de los filtros del símbolo.

**Nota:** si el objetivo de usar leverage alto es sobre todo reducir el margen inmovilizado (y no aumentar exposición), evaluar fijar un leverage moderado (por ejemplo 5x–10x) en vez del máximo permitido para esta primera validación con fondos reales. Con el notional mínimo la diferencia de margen es marginal, pero un leverage menor deja más margen de error ante un bug de cálculo de tamaño. Es una sugerencia, no una regla dura — mantener el máximo si ya se evaluó el trade-off. En cualquier caso, `MAX_POSITION_NOTIONAL_USDC` (Sección 0.4) debe aplicarse como tope duro en el Risk Engine, independiente del leverage configurado en la cuenta.

---

# 5. Primera etapa: validación

Durante la primera etapa, con la posición mínima posible, el agente debe verificar:

* si las órdenes realmente reciben fill;
* tiempo hasta fill;
* porcentaje de fills;
* PnL bruto;
* PnL neto;
* comisión;
* funding;
* spread capturado;
* adverse selection;
* inventario;
* drawdown;
* frecuencia de cancelaciones;
* frecuencia de reemplazos;
* latencia.

No aumentar tamaño simplemente porque una operación individual resulte ganadora.

---

# 6. Estrategia

Implementar un sistema de:

```text
Adaptive Market Making
+ Short-Term Alpha
+ Inventory Management
+ Risk Controller
```

La estrategia debe ser híbrida.

No utilizar una red neuronal que simplemente produzca:

```text
BUY / SELL / HOLD
```

La IA debe controlar principalmente:

```text
bid distance
ask distance
bid size
ask size
inventory skew
order lifetime
```

---

# 7. Market State

Construir continuamente un estado del mercado utilizando información del WebSocket. Como mínimo considerar:

```text
best bid / best ask / mid price
spread / spread %
order book imbalance
bid depth / ask depth
trade flow (buy volume / sell volume)
short-term volatility
price momentum
trade arrival rate
microprice
inventory / current position
unrealized PnL / realized PnL
```

Cuando sea posible, calcular:

```text
MicroPrice = (Ask * BidSize + Bid * AskSize) / (BidSize + AskSize)
Imbalance  = (BidVolume - AskVolume) / (BidVolume + AskVolume)
```

---

# 8. Reservation Price

Utilizar inicialmente un modelo matemático tipo Avellaneda–Stoikov como referencia, pero **no implementarlo de forma rígida**. Usar el concepto de `reservation price`, modificado por inventory, volatility, short-term alpha y order book imbalance/momentum.

Conceptualmente:

```text
r_t = S_t + alpha_t - f(I_t, sigma_t, T - t)
```

donde:

* `S_t` = mid price;
* `alpha_t` = señal de corto plazo;
* `I_t` = inventario;
* `sigma_t` = volatilidad;
* `f(I_t, sigma_t, T-t)` = penalización por riesgo de inventario (análoga a `q * gamma * sigma^2 * (T-t)` en la formulación clásica de Avellaneda-Stoikov).

La IA puede aprender los ajustes sobre este modelo.

> *Nota: esta fórmula había quedado corrompida en la versión original al pegar LaTeX en Markdown — una línea de guiones sueltos se interpretó como separador y se perdió el signo de resta. Queda corregida arriba, en texto plano dentro de un bloque de código para que no vuelva a pasar.*

---

# 9. Bid / Ask

El agente debe determinar dinámicamente `bid price` y `ask price`. No colocar órdenes simplemente en best bid / best ask sin analizar:

* profundidad;
* posición probable en la cola;
* spread;
* probabilidad de fill;
* riesgo de adverse selection;
* dirección probable del precio.

El agente debe buscar maximizar el **Expected Net PnL**, no simplemente la cantidad de fills.

---

# 10. Maker

Priorizar operaciones maker. Cuando sea posible, utilizar `POST_ONLY` o el equivalente disponible en Binance Futures (`GTX`).

Una orden no debe cruzar accidentalmente el spread y convertirse en taker. Antes de enviar una orden, verificar:

```text
expected execution price + current bid/ask + order type
```

Si una operación implicaría ejecución taker cuando la estrategia pretende ser maker:

```text
NO EJECUTAR
```

Registrar el motivo.

---

# 11. Inventory Management

El inventario debe ser una variable central de la estrategia.

```text
inventory = 0        → neutral
inventory > 0        → posición long
inventory < 0        → posición short
```

Cuando el inventario crece demasiado en long, el agente debe reducir la agresividad del BID y aumentar la del ASK. Cuando crece demasiado en short, hacer lo contrario.

Nunca permitir que el inventario crezca indefinidamente.

---

# 12. Risk Engine

Crear un módulo independiente:

```text
risk_engine.py
```

La IA puede proponer operaciones, pero el Risk Engine puede rechazarlas. Debe controlar como mínimo:

```text
maximum position
maximum inventory
maximum daily loss
maximum drawdown
maximum unrealized loss
maximum order size
maximum number of open orders
maximum exposure
maximum volatility
```

Si cualquiera de los límites críticos se supera:

```text
cancel all orders
reduce/close position if necessary
disable new entries
```

Los límites deben leerse de las constantes definidas en 0.4, no quedar hardcodeados dentro de este módulo. Escribir tests unitarios que verifiquen que el Risk Engine efectivamente rechaza propuestas que violan cada límite — no asumir que la lógica es correcta solo por inspección visual del código.

---

# 13. Kill Switch

Implementar un mecanismo de emergencia. Debe activarse ante:

* pérdida excesiva;
* comportamiento anómalo;
* WebSocket desconectado;
* estado inconsistente entre API y estrategia;
* órdenes desconocidas;
* posición no reconciliada;
* error repetitivo de API;
* precio anómalo;
* pérdida de sincronización;
* incremento inesperado de exposición.

En caso de emergencia:

```text
CANCEL ALL OPEN ORDERS
```

y posteriormente reconciliar la posición real. No asumir que cancelar órdenes implica que no existe posición.

Antes de confiar en el kill switch en cualquier entorno con órdenes activas (incluso en testnet), probarlo explícitamente: simular al menos una condición de disparo (por ejemplo, forzar una desconexión de WebSocket) y verificar que efectivamente cancela las órdenes abiertas y deja el sistema en un estado seguro.

---

# 14. Adverse Selection

Este parámetro debe medirse explícitamente. Para cada fill, comparar `fill_price` contra el precio posterior a +1s, +5s, +10s y +30s:

```text
AdverseSelection = Price_future - Price_fill   # signo ajustado según long/short
```

Determinar si las operaciones maker se están ejecutando principalmente antes de movimientos adversos. Si esto ocurre de forma persistente: aumentar distancia, reducir tamaño, reducir exposición, o detener la estrategia.

> *Nota: fórmula reescrita en texto plano por el mismo motivo que en la Sección 8.*

---

# 15. Métricas

Registrar continuamente:

```text
timestamp, mid price, bid, ask, spread, inventory, position,
order price, order size, fill price, fill size, order lifetime,
PnL, fees, funding, net PnL, drawdown, volatility, imbalance,
microprice, adverse selection
```

Guardar los datos en archivos para análisis posterior:

```text
logs/
    market_data/
    orders/
    fills/
    pnl/
    risk/
    decisions/
```

---

# 16. Journal del agente

Crear además un registro de decisiones:

```text
agent_decisions.jsonl
```

Cada decisión debe registrar:

```json
{
    "timestamp": "...",
    "regime": "...",
    "inventory": 0,
    "mid_price": 0,
    "spread": 0,
    "imbalance": 0,
    "alpha": 0,
    "bid_price": 0,
    "ask_price": 0,
    "bid_size": 0,
    "ask_size": 0,
    "reason": "...",
    "expected_pnl": 0,
    "risk_score": 0
}
```

Esto permitirá analizar después por qué el agente tomó cada decisión. (Recordar 0.5: nunca incluir credenciales en este archivo.)

---

# 17. Machine Learning / RL

No comenzar inmediatamente con Deep RL. Primero construir una versión funcional basada en reglas + modelos estadísticos + short-term alpha + parámetros adaptativos.

Una vez que exista suficiente información real de mercado (`market state → action → fill → PnL`), evaluar usar RL para optimizar spread, inventory skew, order size y order lifetime.

El RL **no** debe tener control directo sobre:

```text
maximum leverage
maximum position
maximum loss
kill switch
```

Esos parámetros pertenecen al Risk Engine.

---

# 18. Función objetivo

Evaluar la estrategia mediante:

```text
Reward = NetPnL
         - lambda_1 * InventoryRisk
         - lambda_2 * AdverseSelection
         - lambda_3 * Drawdown
         - lambda_4 * VolatilityRisk
```

donde:

```text
NetPnL = GrossPnL - TradingFees - Funding - Slippage
```

La comisión maker debe incluirse explícitamente. No considerar rentable una operación si `GrossPnL > 0` pero `NetPnL < 0`.

> *Nota: igual que en la Sección 8, esta fórmula se corrompió al pegarla — los términos `- lambda_i` se habían convertido en encabezados Markdown (`##`). Corregida arriba.*

---

# 19. Criterio de rentabilidad

La estrategia debe demostrar `NET PnL > 0` después de maker fees, funding y slippage, y preferentemente expectancy positiva durante una cantidad estadísticamente razonable de operaciones.

Una única operación ganadora no constituye evidencia de rentabilidad.

---

# 20. Alcance de la sesión autónoma

Trabajar de forma autónoma durante aproximadamente **2 horas**, dentro de los límites de la Sección 0. El agente debe:

1. inspeccionar el código existente;
2. estudiar `API_binance_futuros.py`;
3. estudiar los ejemplos de WebSocket ya presentes en el proyecto;
4. estudiar la estructura del proyecto y de la VPS (ver 0.3);
5. implementar la estrategia;
6. escribir y correr pruebas (incluyendo las de 0.6, 12 y 13);
7. corregir errores encontrados, sin detenerse ante el primero;
8. correr backtests/simulaciones si hay datos disponibles;
9. operar en **Testnet** — Nivel 0 y, como máximo, Nivel 1; nunca mainnet sin la autorización de 0.1;
10. monitorear resultados;
11. ajustar parámetros;
12. documentar resultados y dejar `STATUS.md` con el estado final y las decisiones pendientes de aprobación humana.

No detenerse ante el primer error de implementación: investigar y corregir de forma autónoma, dentro de los límites de la Sección 0.

---

# 21. Escalamiento

El agente no debe aumentar el tamaño inmediatamente. Usar una política:

```text
Nivel 0 → simulación / dry-run
Nivel 1 → mínima cantidad XRP
Nivel 2 → mínima cantidad × 2
Nivel 3 → mínima cantidad × 4
...
```

Subir de nivel requiere que se cumplan **todos** estos criterios **y además** una confirmación humana explícita — cumplir los criterios es condición necesaria pero no suficiente:

```text
Net PnL positivo
AND drawdown ≤ MAX_DRAWDOWN_PCT (0.4)
AND adverse selection controlado
AND risk limits respetados
AND cantidad suficiente de operaciones
```

Si la estrategia deja de ser rentable, reducir exposición — nunca aumentar el riesgo para intentar recuperar pérdidas.

---

# 22. Restricción fundamental

El objetivo de esta sesión es descubrir si existe una ventaja estadística explotable en XRPUSDC mediante market making maker — no generar una gran ganancia. La prioridad es:

```text
1. Seguridad
2. Correctitud
3. Datos
4. Medición
5. Rentabilidad
6. Optimización
7. Escalamiento
```

Nunca invertir este orden.

---

# 23. Entregables

Al finalizar la sesión, generar:

```text
STATUS.md

strategy/
    market_maker.py
    market_state.py
    alpha_model.py
    inventory_manager.py
    risk_engine.py
    execution_engine.py
    config.py

logs/
    market_data/
    orders/
    fills/
    pnl/
    decisions/

reports/
    performance_report.md
    trades.csv
    pnl.csv
```

`performance_report.md` debe incluir:

**Performance:** total trades, filled orders, fill rate, gross PnL, fees, funding, net PnL, average/median trade, profit factor, maximum drawdown, Sharpe.

**Market making:** average spread captured, average order lifetime, maker percentage, adverse selection, inventory exposure, average/maximum inventory.

**Risk:** maximum position, maximum leverage used, maximum drawdown, maximum unrealized loss, número de eventos de kill-switch.

**Conclusión:** clasificar el resultado como `PROFITABLE`, `PROMISING BUT INSUFFICIENT DATA` o `NOT PROFITABLE`, con justificación cuantitativa. Incluir además un resumen de los checkpoints/commits de la sesión (0.3) y confirmar que se cumplió el checklist de la Sección 24.

---

# 24. Checklist de cierre

Antes de reportar la tarea como terminada:

* [ ] Todo corrió sobre Testnet; no se operó en mainnet sin autorización registrada (0.1).
* [ ] El kill switch fue probado al menos una vez y funcionó como se esperaba (13).
* [ ] Los límites del Risk Engine están cubiertos por tests unitarios que pasan (12).
* [ ] Se revisaron explícitamente los errores conocidos de 0.6 y quedaron cubiertos por tests o assertions.
* [ ] `agent_decisions.jsonl` tiene registros coherentes y legibles de al menos una sesión de prueba.
* [ ] `reports/performance_report.md` existe y clasifica el resultado, con justificación cuantitativa.
* [ ] `STATUS.md` refleja el estado final y lista explícitamente las decisiones pendientes de aprobación humana (por ejemplo, pasar a Nivel 1 en mainnet).
* [ ] No se modificaron ni eliminaron archivos existentes sin necesidad (revisar `git diff` / `git log`).
* [ ] No hay credenciales ni claves en el código, los logs, ni el historial de commits.
* [ ] El proceso nuevo no interfirió con el grid bot ni con el bot A-S ya activos en la misma VPS.

---

# Regla final

No modificar ni eliminar archivos existentes sin necesidad. Antes de modificar: leer, analizar, entender. Después: implementar, probar, verificar.

Mantener todas las credenciales fuera del código fuente, según lo indicado en 0.5.

La estrategia debe poder detenerse de inmediato mediante el kill switch, y debe ser posible reconstruir después, con precisión, qué decisiones tomó el agente y por qué.
