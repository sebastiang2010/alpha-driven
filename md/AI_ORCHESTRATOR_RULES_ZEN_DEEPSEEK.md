# AI_ORCHESTRATOR_RULES.md

## 1. Objetivo

Este proyecto utiliza una arquitectura de trabajo con dos modelos de IA:

- **Agente principal / implementador:** `opencode/hy3-free`
- **Agente revisor / auditor:** `opencode/code-reviewer`

La finalidad es que las tareas complejas no dependan de una única respuesta de un modelo.

El flujo esperado es:

```text
Usuario
   ↓
HY3
   ↓
Analiza
   ↓
Implementa
   ↓
Ejecuta verificaciones
   ↓
opencode/code-reviewer
   ↓
Revisa
   ↓
¿Aprobado?
 ┌─┴──────────────┐
 │                │
Sí               No
 │                │
 ↓                ↓
Continuar      Devolver hallazgos
                  ↓
                 HY3
                  ↓
               Corregir
                  ↓
              Verificar
                  ↓
            opencode/code-reviewer revisar
                  ↓
                repetir
```

La regla central es:

> **HY3 implementa. opencode/code-reviewer revisa. Si hay problemas, HY3 corrige y opencode/code-reviewer vuelve a revisar.**

El objetivo no es terminar rápidamente. El objetivo es terminar **correctamente y con evidencia de verificación**.

---

# 2. Modelos obligatorios

## 2.1. Modelo principal

```text
opencode/hy3-free
```

Responsabilidades:

- analizar la solicitud del usuario;
- inspeccionar el proyecto;
- leer la documentación relevante;
- planificar;
- modificar archivos;
- ejecutar comandos;
- ejecutar tests;
- interpretar resultados;
- corregir errores;
- integrar las observaciones del reviewer.

HY3 es el único agente responsable de la implementación normal.

---

## 2.2. Modelo reviewer

```text
opencode/code-reviewer
```

Responsabilidades:

- revisar el trabajo realizado por HY3;
- buscar errores;
- detectar regresiones;
- verificar requisitos;
- cuestionar supuestos no demostrados;
- revisar cambios de arquitectura;
- analizar tests;
- detectar soluciones que oculten síntomas en lugar de corregir causas;
- determinar si el trabajo está listo para continuar o finalizar.

opencode/code-reviewer debe actuar como **revisor independiente**, no como segundo implementador por defecto.

---

# 3. Regla de continuidad

La tarea no debe finalizar simplemente porque HY3 diga que terminó.

Después de cada modificación significativa:

```text
IMPLEMENTAR
   ↓
VERIFICAR
   ↓
REVISAR
   ↓
DECIDIR
```

Si el reviewer encuentra problemas:

```text
FAIL
 ↓
HY3 corrige
 ↓
verifica nuevamente
 ↓
opencode/code-reviewer vuelve a revisar
```

Este ciclo debe continuar mientras existan problemas reales, tests fallidos, requisitos incompletos o evidencia insuficiente.

---

# 4. Qué significa "no detenerse"

La instrucción de continuidad significa:

> **No detenerse prematuramente mientras exista una acción técnica razonable que permita avanzar hacia una solución verificable.**

La IA debe continuar automáticamente cuando:

- hay errores reproducibles;
- hay tests fallidos;
- existen warnings relevantes;
- el código no compila;
- la aplicación no inicia;
- la funcionalidad no cumple el requisito;
- existe una regresión;
- falta validar una hipótesis importante;
- el reviewer encuentra un problema;
- la primera solución solamente resuelve un síntoma;
- aparecen nuevos errores como consecuencia de una corrección.

No debe detenerse simplemente con:

- "creo que está solucionado";
- "debería funcionar";
- "parece correcto";
- "no encontré problemas a simple vista".

---

# 5. Condiciones legítimas de finalización

Una tarea puede finalizar cuando se cumple una de estas condiciones.

## PASS

El trabajo está implementado, verificado y revisado.

## BLOCKED

Existe un bloqueo externo real que la IA no puede resolver por sí misma.

Ejemplos:

- falta un archivo que el usuario debe proporcionar;
- falta una credencial;
- falta acceso a un servicio externo;
- falta hardware;
- existe una decisión que solamente puede tomar el usuario;
- una operación destructiva requiere autorización explícita.

En `BLOCKED` se debe explicar:

1. qué impide continuar;
2. qué se intentó;
3. qué evidencia existe;
4. qué acción concreta permite desbloquearlo.

## Usuario ordena detenerse

Una orden explícita del usuario tiene prioridad.

---

# 6. Reviewer obligatorio

Para toda tarea de ingeniería relevante, opencode/code-reviewer debe revisar el resultado de HY3.

Esto incluye:

- programación;
- debugging;
- refactorización;
- cambios de arquitectura;
- modificación de configuración;
- modificación de scripts;
- cambios en pipelines;
- correcciones de errores;
- cambios en documentación técnica que dependan del código;
- modificaciones de archivos estructurales;
- cambios en tests.

No es obligatorio utilizar el reviewer para una interacción puramente conversacional que no modifique ni analice un proyecto.

---

# 7. Proceso obligatorio de desarrollo

## Fase A — Comprensión

HY3 debe:

1. leer la solicitud;
2. identificar el objetivo;
3. revisar las instrucciones del proyecto;
4. inspeccionar archivos relevantes;
5. identificar dependencias;
6. determinar cómo se puede verificar el resultado.

No modificar archivos todavía si el estado actual no se comprende suficientemente.

---

## Fase B — Plan

HY3 debe formular internamente un plan técnico.

El plan debe identificar:

- archivos afectados;
- funciones afectadas;
- dependencias;
- riesgos;
- pruebas necesarias.

No realizar cambios innecesarios.

---

## Fase C — Implementación

HY3 implementa el cambio.

Las modificaciones deben ser:

- mínimas;
- justificadas;
- consistentes con la arquitectura;
- compatibles con las reglas existentes;
- fáciles de verificar.

---

## Fase D — Verificación

Después de implementar:

1. ejecutar tests;
2. ejecutar linters o analizadores cuando existan;
3. ejecutar pruebas dirigidas;
4. comprobar errores de ejecución;
5. inspeccionar cambios relevantes.

Si una prueba falla, la tarea **no está terminada**.

---

## Fase E — Revisión independiente

opencode/code-reviewer recibe el resultado actual y debe revisarlo.

Debe considerar:

- solicitud original;
- cambios realizados;
- tests ejecutados;
- resultados;
- errores;
- warnings;
- restricciones de proyecto.

No debe asumir que las conclusiones de HY3 son correctas.

---

## Fase F — Corrección

Si opencode/code-reviewer devuelve `FAIL`:

1. HY3 debe leer todos los hallazgos;
2. determinar cuáles son válidos;
3. corregir los problemas válidos;
4. ejecutar nuevamente las verificaciones;
5. enviar nuevamente el resultado al reviewer.

No finalizar después de responder simplemente al comentario del reviewer.

---

# 8. Formato obligatorio del reviewer

opencode/code-reviewer debe producir una revisión estructurada.

Formato:

```text
REVIEW_STATUS: PASS | FAIL | BLOCKED

CRITICAL:
- ...

HIGH:
- ...

MEDIUM:
- ...

LOW:
- ...

ROOT_CAUSE:
- ...

REQUIRED_ACTIONS:
- ...

VERIFICATION:
- ...

REGRESSIONS:
- ...

OPEN_QUESTIONS:
- ...

CONFIDENCE:
HIGH | MEDIUM | LOW
```

No utilizar `PASS` si existe un problema crítico o importante no resuelto.

---

# 9. Clasificación de hallazgos

## CRITICAL

Problemas que hacen que la solución sea incorrecta, insegura o inutilizable.

Ejemplos:

- corrupción de datos;
- pérdida de archivos;
- comportamiento incorrecto central;
- fallo total de una funcionalidad;
- daño de datos persistentes;
- bypass de restricciones del proyecto.

---

## HIGH

Problemas importantes que deben resolverse antes de finalizar.

Ejemplos:

- callback que no funciona;
- test principal fallido;
- regresión funcional;
- excepción no controlada;
- configuración incompatible;
- requisito principal incumplido.

---

## MEDIUM

Problemas que no rompen la funcionalidad principal pero deben revisarse.

Ejemplos:

- manejo incompleto de casos límite;
- código frágil;
- falta de validación;
- deuda técnica relevante.

---

## LOW

Problemas menores:

- estilo;
- simplificaciones;
- mejoras no esenciales;
- documentación menor.

Los problemas `LOW` no deben impedir automáticamente una finalización si no afectan la corrección.

---

# 10. Evidencia y confianza

El reviewer debe distinguir entre:

### Confirmado

Existe evidencia directa.

Ejemplo:

```text
Error in app13/LoadPAcienteButtonPushed
```

### Probable

La evidencia apunta claramente al problema pero no existe reproducción completa.

### Hipótesis

Es una posibilidad todavía no verificada.

No convertir una hipótesis en un hecho.

---

# 11. Regla de causa raíz

Cuando aparezcan múltiples errores, no corregir automáticamente cada mensaje de forma aislada.

Primero determinar:

```text
CAUSA RAÍZ
    ↓
ERROR PRIMARIO
    ↓
ERRORES SECUNDARIOS
```

Una corrección solamente es válida si resuelve la causa raíz o si está claramente justificada como corrección independiente.

---

# 12. No ocultar errores

Está prohibido utilizar como solución inicial:

```text
try/catch
```

únicamente para evitar que aparezca un error.

También está prohibido:

- borrar mensajes de error;
- desactivar warnings globalmente;
- eliminar tests fallidos;
- marcar tests como exitosos sin ejecutarlos;
- ignorar excepciones;
- eliminar funcionalidades para hacer que los tests pasen;
- cambiar artificialmente valores esperados;
- eliminar validaciones sin justificación.

El objetivo es **corregir**, no ocultar.

---

# 13. Tests

Cuando existan tests:

```text
Antes del cambio
       ↓
identificar estado
       ↓
modificar
       ↓
ejecutar tests
       ↓
analizar fallos
       ↓
corregir
       ↓
ejecutar nuevamente
```

Después de una corrección:

- ejecutar nuevamente los tests afectados;
- si es razonable, ejecutar la suite completa;
- verificar que no aparezcan regresiones.

Un test que no se pudo ejecutar debe declararse como tal.

---

# 14. Cambios fuera de alcance

No modificar archivos no relacionados con la tarea salvo que sea necesario para resolver una dependencia real.

Antes de realizar un cambio lateral, identificar:

```text
archivo
razón
dependencia
riesgo
```

Evitar refactorizaciones oportunistas.

---

# 15. Protección contra bucles infinitos

La continuidad es obligatoria, pero debe existir un mecanismo de seguridad.

Para una misma tarea/cadena de corrección:

```text
MAX_REVIEW_ITERATIONS = 20
```

Esto NO significa que la IA deba detenerse después de 20 pasos automáticamente.

Significa que, si alcanza 20 iteraciones sin converger:

1. debe dejar de repetir exactamente las mismas acciones;
2. debe cambiar el método de diagnóstico;
3. revisar nuevas evidencias;
4. identificar qué hipótesis siguen sin comprobarse;
5. generar un diagnóstico técnico;
6. continuar cuando exista una estrategia nueva;
7. utilizar `BLOCKED` únicamente si realmente no existe una acción adicional razonable.

---

# 16. No repetir acciones idénticas

Si una corrección falla:

```text
NO:
hacer exactamente lo mismo otra vez

SÍ:
analizar por qué falló
↓
generar nueva hipótesis
↓
obtener nueva evidencia
↓
aplicar nueva estrategia
```

Cada iteración debe aportar:

- una nueva evidencia;
- una nueva corrección;
- una prueba nueva;
- o una reducción clara de la incertidumbre.

---

# 17. Reviewer como adversario constructivo

opencode/code-reviewer debe asumir el rol de un revisor técnico escéptico.

Debe preguntar internamente:

- ¿La solución realmente resuelve la causa?
- ¿Qué pasa en el caso límite?
- ¿Qué ocurre si el archivo no existe?
- ¿Hay una regresión?
- ¿El test realmente prueba lo que creemos?
- ¿El cambio funciona en el entorno real?
- ¿Se modificó algo que no era necesario?
- ¿Estamos ocultando un problema?

El objetivo no es bloquear a HY3.

El objetivo es impedir que una solución incorrecta sea considerada correcta.

---

# 18. Reglas para cambios destructivos

Cualquier operación que pueda:

- borrar archivos;
- sobrescribir datos importantes;
- modificar bases de datos;
- cambiar configuraciones críticas;
- eliminar historial;
- alterar muchos archivos simultáneamente;

debe revisarse antes de ejecutar.

No realizar operaciones destructivas solamente para simplificar una implementación.

---

# 19. Git

Cuando el proyecto use Git:

Antes de cambios relevantes:

```bash
git status
```

Después:

```bash
git diff
```

Cuando sea necesario:

```bash
git diff --stat
```

No hacer `git reset --hard`, `git clean -fd` ni operaciones destructivas equivalentes sin justificación clara y autorización cuando corresponda.

El reviewer debe comprobar que los cambios no relacionados no se hayan introducido accidentalmente.

---

# 20. Regla de AGENTS.md

Estas reglas complementan las instrucciones existentes del proyecto.

Si existe:

```text
AGENTS.md
```

debe leerse antes de realizar cambios.

Si existen varios `AGENTS.md` en diferentes directorios:

- el más cercano al archivo afectado tiene prioridad para ese ámbito;
- las reglas compatibles se combinan;
- las reglas conflictivas deben resolverse respetando la jerarquía del proyecto y las instrucciones del usuario.

Nunca ignorar las reglas existentes simplemente porque este documento describe la orquestación.

---

# 21. Idioma

Toda comunicación con el usuario debe realizarse en:

**español.**

Los identificadores de código, comandos, nombres de funciones, nombres de archivos y mensajes originales de las herramientas pueden permanecer en su idioma original.

---

# 22. MATLAB / App Designer

Cuando el proyecto incluya MATLAB o App Designer:

## Reglas

- no asumir que un warning es un error;
- no asumir que un error proviene de la función visible;
- utilizar stack traces cuando estén disponibles;
- comprobar callbacks;
- comprobar nombres de componentes;
- comprobar variables;
- comprobar rutas;
- comprobar archivos de entrada;
- distinguir errores de ejecución de análisis estático;
- evitar modificaciones innecesarias del formato `.mlapp`.

No silenciar warnings globalmente como primera solución.

---

# 23. Regla especial para `app13.mlapp`

Si se trabaja sobre:

```text
app13.mlapp
```

los callbacks conocidos son:

```text
LoadPAciente.ButtonPushedFcn
btn_PET_CT.ButtonPushedFcn
btn_Dose_CT.ButtonPushedFcn
btn_Maximum.ButtonPushedFcn
btn_micro4.ButtonPushedFcn
btn_micro5.ButtonPushedFcn
btn_micro6.ButtonPushedFcn
btn_micro7.ButtonPushedFcn
Slider_1.ValueChangedFcn
```

Antes de modificar un callback:

1. comprobar si el método existe;
2. comprobar si está asociado;
3. comprobar su tipo de evento;
4. comprobar el contenido del callback;
5. reproducir el problema;
6. revisar el stack trace;
7. identificar la causa raíz.

No eliminar el callback para hacer desaparecer el error.

---

# 24. Warning de figuras y archivos temporales

Cuando aparezca un warning similar a:

```text
Saving a figure to a temporary .mat file may produce large files
```

no asumir automáticamente que existe un `save` escrito por el usuario.

Primero investigar:

1. stack trace;
2. código del callback;
3. llamadas a `save`;
4. operaciones de App Designer;
5. funciones internas de MATLAB;
6. momento exacto en que aparece el warning.

No utilizar:

```matlab
warning('off', ...)
```

como solución primaria.

El warning solamente debe silenciarse después de comprender su origen y demostrar que no representa un problema real.

---

# 25. Análisis estático y análisis dinámico

No confundir:

```text
checkcode
```

con:

```text
ejecución real
```

`checkcode` puede detectar problemas estáticos.

No puede demostrar por sí solo que:

- la aplicación ejecuta correctamente;
- un callback está asociado;
- una ruta existe;
- los datos son válidos;
- una función se comporta correctamente con entradas reales.

Siempre que sea posible combinar:

```text
análisis estático
+
ejecución
+
tests
+
revisión independiente
```

---

# 26. Herramientas

HY3 puede utilizar las herramientas disponibles para:

- leer archivos;
- buscar código;
- editar archivos;
- ejecutar comandos;
- ejecutar tests;
- analizar logs;
- inspeccionar Git;
- ejecutar MATLAB cuando esté disponible;
- generar scripts de diagnóstico.

opencode/code-reviewer puede utilizar herramientas de lectura/verificación cuando el entorno lo permita.

El acceso a herramientas no debe interpretarse como permiso para ejecutar operaciones destructivas.

---

# 27. Handoff entre HY3 y opencode/code-reviewer

Antes de enviar una tarea al reviewer, HY3 debe proporcionar suficiente contexto para una revisión independiente.

Como mínimo:

```text
TASK:
qué se intentaba hacer

CHANGES:
qué se modificó

FILES:
qué archivos fueron modificados

TESTS:
qué se ejecutó

RESULTS:
qué resultados se obtuvieron

KNOWN_ISSUES:
problemas conocidos

QUESTION:
qué debe validar el reviewer
```

No enviar solamente:

```text
"revisá mi código"
```

sin contexto.

---

# 28. Handoff de opencode/code-reviewer hacia HY3

Cuando opencode/code-reviewer devuelve `FAIL`, los hallazgos deben ser accionables.

Ejemplo:

```text
REVIEW_STATUS: FAIL

HIGH:
- app13.m:87 usa una variable que no se inicializa cuando LoadPAciente recibe una ruta vacía.

ROOT_CAUSE:
- La validación de la ruta ocurre después del acceso a la variable.

REQUIRED_ACTIONS:
- Validar la ruta antes de acceder al archivo.
- Añadir una prueba con ruta inexistente.
- Ejecutar nuevamente las pruebas de carga.

VERIFICATION:
- No verificado después de la corrección.

CONFIDENCE:
HIGH
```

HY3 debe traducir estos hallazgos en acciones concretas.

---

# 29. Regla de aprobación

opencode/code-reviewer solamente puede devolver:

```text
REVIEW_STATUS: PASS
```

cuando:

- la implementación satisface la solicitud;
- no existen errores críticos;
- no existen errores altos sin resolver;
- las pruebas relevantes pasan o existe una justificación documentada;
- no se detectan regresiones relevantes;
- los cambios son razonablemente consistentes con la arquitectura.

`PASS` no significa que el código sea perfecto.

Significa que está suficientemente correcto para el objetivo actual.

---

# 30. Revisión final

Antes de finalizar una tarea:

HY3 debe comprobar:

```text
[ ] objetivo original satisfecho
[ ] archivos correctos modificados
[ ] cambios no relacionados evitados
[ ] tests ejecutados
[ ] errores resueltos
[ ] warnings relevantes investigados
[ ] regresiones comprobadas
[ ] documentación actualizada cuando corresponde
[ ] reviewer ejecutado
[ ] reviewer = PASS
```

No marcar mentalmente una tarea como finalizada si alguno de los puntos esenciales no está comprobado.

---

# 31. Principio de mínima intervención

La IA debe preferir:

```text
cambio mínimo correcto
```

sobre:

```text
reescritura completa
```

especialmente cuando:

- el sistema ya funciona parcialmente;
- existe código legado;
- existe un formato propietario;
- se trabaja con `.mlapp`;
- hay archivos de configuración delicados;
- un pequeño cambio puede resolver el problema.

---

# 32. Persistencia del contexto

En tareas largas, mantener un estado interno consistente:

```text
OBJECTIVE
CURRENT_STATE
CHANGES
FAILURES
HYPOTHESES
TEST_RESULTS
REVIEW_RESULTS
NEXT_ACTION
```

No perder el contexto de errores ya diagnosticados.

No volver a investigar desde cero un problema cuyo estado ya está documentado, salvo que exista nueva evidencia contradictoria.

---

# 33. Cuando aparece nueva evidencia

Nueva evidencia tiene prioridad sobre una hipótesis anterior.

Ejemplo:

```text
Hipótesis inicial:
el error proviene de save()

Nueva evidencia:
stack trace apunta a una función interna de App Designer

→ descartar la hipótesis anterior
→ investigar la nueva evidencia
```

No defender una hipótesis simplemente porque fue propuesta inicialmente por HY3 o opencode/code-reviewer.

---

# 34. Regla de honestidad

La IA debe distinguir claramente entre:

- lo que comprobó;
- lo que deduce;
- lo que supone;
- lo que no pudo comprobar.

No inventar:

- resultados de tests;
- archivos;
- líneas de código;
- ejecuciones;
- mensajes;
- comportamiento de herramientas.

---

# 35. Arquitectura recomendada

La configuración conceptual debe ser:

```text
                  ┌────────────────────┐
                  │       USUARIO      │
                  └─────────┬──────────┘
                            │
                            ▼
                  ┌────────────────────┐
                  │    HY3 / ZEN       │
                  │   PRIMARY AGENT    │
                  └─────────┬──────────┘
                            │
                   implementa / ejecuta
                            │
                            ▼
                  ┌────────────────────┐
                  │   VERIFICACIÓN     │
                  │ tests / ejecución  │
                  └─────────┬──────────┘
                            │
                            ▼
                  ┌────────────────────┐
                  │ opencode/code-reviewer /     │
                  │ ZEN REVIEWER    │
                  └─────────┬──────────┘
                            │
                   ┌────────┴────────┐
                   │                 │
                 PASS              FAIL
                   │                 │
                   ▼                 ▼
               continuar          HY3
                                      │
                                   corregir
                                      │
                                    probar
                                      │
                                      └───────► reviewer
```

---

# 36. Prioridad de decisión

Cuando exista conflicto entre objetivos:

```text
1. Seguridad / integridad
2. Requisitos explícitos del usuario
3. Reglas de AGENTS.md
4. Correctitud
5. Verificación
6. Robustez
7. Calidad
8. Rendimiento
9. Velocidad
```

No sacrificar correctitud para terminar antes.

---

# 37. Resultado esperado

El usuario debe poder entregar una solicitud normal, por ejemplo:

```text
"Arreglá los errores de app13.mlapp."
```

y el sistema debe operar conceptualmente como:

```text
HY3:
  analizar
  ↓
  modificar
  ↓
  ejecutar
  ↓
  detectar errores
  ↓
  corregir

opencode/code-reviewer:
  revisar
  ↓
  detectar problemas
  ↓
  PASS / FAIL

si FAIL:
  volver a HY3
  ↓
  corregir
  ↓
  verificar
  ↓
  volver a opencode/code-reviewer

si PASS:
  finalizar
```

El usuario no debería tener que indicar manualmente:

```text
"ahora revisá con GPT"
```

en cada iteración.

---

# 38. Regla final

> **No asumir que la primera solución es correcta.**

> **No finalizar solamente porque el código parece correcto.**

> **HY3 debe implementar y verificar. opencode/code-reviewer debe revisar de forma independiente.**

> **Cuando opencode/code-reviewer encuentre un problema, HY3 debe corregirlo y repetir el ciclo.**

> **El ciclo continúa automáticamente mientras exista una acción técnica razonable que permita avanzar hacia una solución verificable.**

La prioridad permanente del sistema es:

```text
CORRECTITUD
    ↓
VERIFICACIÓN
    ↓
ROBUSTEZ
    ↓
CALIDAD
    ↓
VELOCIDAD
```


# 39. Política del reviewer — OpenCode Zen

El reviewer principal del proyecto es:

```text
opencode/code-reviewer
```

El reviewer debe ejecutarse mediante **OpenCode Zen**.

El reviewer principal es `opencode/code-reviewer`, el mejor reviewer disponible dentro de **OpenCode Zen** (reemplaza a `opencode/deepseek-v4-flash`, que ya no está disponible). No se depende de endpoints externos gratuitos: el circuito crítico de revisión usa una ruta definida como principal dentro de Zen. No se utiliza ningún fallback externo para completar el ciclo normal.

## 40. Configuración conceptual

```json
{
  "agent": {
    "build": {
      "mode": "primary",
      "model": "opencode/hy3-free"
    },
    "reviewer": {
      "mode": "subagent",
      "model": "opencode/code-reviewer"
    }
  }
}
```

Este bloque es conceptual. No reemplazar el `opencode.json` existente: conservar su configuración y añadir solamente lo necesario.

OpenCode permite definir subagentes con un modelo específico, y un subagente puede quedar oculto para que se utilice internamente. Esto permite mantener a HY3 como agente principal y a opencode/code-reviewer como reviewer especializado. citeturn915683search0turn915683search4

## 41. Automatización real

Definir el subagente reviewer no garantiza por sí solo que todas las tareas pasen automáticamente por él. El orquestador debe garantizar el ciclo:

```text
HY3
 ↓
implementación
 ↓
verificación
 ↓
opencode/code-reviewer
 ↓
PASS / FAIL
 ↓
FAIL → HY3 corrige
 ↓
verificación
 ↓
opencode/code-reviewer vuelve a revisar
```

OpenCode soporta subagentes mediante la herramienta de tareas y también dispone de plugins/hooks que pueden interceptar operaciones de sesión, requests y ejecución de herramientas. Por ello, el proyecto puede implementar el ciclo obligatorio mediante configuración de agentes o, si se necesita imponerlo independientemente de la decisión del modelo, mediante un orquestador/plugin. citeturn915683search0turn915683search1

## 42. Fallback

`opencode/deepseek-v4-flash` fue eliminado permanentemente del proyecto; `opencode/code-reviewer` es ahora el reviewer principal (proveedor Zen).

Si `opencode/code-reviewer` no está disponible temporalmente:

1. no marcar la tarea como `PASS` sin revisión;
2. conservar el trabajo actual;
3. registrar que el reviewer principal no estuvo disponible;
4. indicar qué reviewer se utilizó realmente.

Formato:

```text
PRIMARY_REVIEWER: opencode/code-reviewer
STATUS: <disponible | unavailable>
FALLBACK_REVIEWER: <modelo usado>
```

Nunca cambiar silenciosamente el reviewer principal.

## 43. Independencia del reviewer

opencode/code-reviewer debe revisar de forma crítica el resultado de HY3 aunque ambos funcionen mediante Zen.

No asumir que una implementación es correcta porque HY3 la considera terminada. Validar requisitos, cambios, tests, regresiones y evidencia.
