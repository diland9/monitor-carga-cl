# Monitor de la red de carga pública · Chile

Captura cada 5 minutos el estado de los conectores de carga pública de Chile
desde el API de [cargadorespublicos.cl](https://cargadorespublicos.cl), el mapa
del Ministerio de Energía alimentado por las declaraciones que los operadores
hacen a la SEC. Publica un panel en vivo y el histórico acumulado.

Todo corre en GitHub: captura con Actions, almacenamiento en el repositorio,
dashboard en Pages. Sin servidores ni costos.

**Dashboard:** `https://TU-USUARIO.github.io/TU-REPO/`

---

## Cómo funciona

```
GitHub Actions (cron */5)  →  scripts/captura.py  →  serie/  +  docs/data/live.json
                                                         │
GitHub Actions (diario)    →  scripts/consolida.py  →  historico/  +  docs/data/historico.json.gz
                                                         │
                                                    GitHub Pages → docs/index.html
```

**Cada 5 minutos** se consulta el API y se guarda el estado de todos los
conectores como un string de un carácter por conector (`0` disponible,
`1` ocupado, `2` fuera de línea, `3` no disponible). Son unos 2 KB por captura.
También se sobrescribe `docs/data/live.json` para el panel en vivo.

**Una vez al día** los ~288 archivos sueltos del día anterior se empaquetan en
un Parquet y se borran, se reconstruye el histórico del dashboard y se despliega
Pages.

---

## Puesta en marcha

1. Crea un repositorio **público** y sube estos archivos.
2. **Settings → Actions → General → Workflow permissions** → marca
   *Read and write permissions*. Sin esto el bot no puede hacer commit.
3. **Settings → Pages → Source** → selecciona *GitHub Actions*.
4. Ve a **Actions**, abre *Captura* y dale **Run workflow** para la primera
   corrida manual. Verifica que aparezca `serie/` con un archivo.
5. Abre *Consolidar y publicar* y dale **Run workflow** para desplegar el sitio.

Desde ahí queda solo. El cron de captura arranca en el siguiente ciclo.

---

## Estructura

| Ruta | Qué contiene |
|---|---|
| `estado/orden.json` | Los ids de conectores, en el orden del string de estados. Solo se reescribe cuando hay altas o bajas. |
| `serie/AAAA-MM-DD/HHMMSS.txt` | Una captura. Dos líneas: timestamp y string de estados. Se borra al consolidar. |
| `historico/AAAA-MM-DD.parquet` | Todas las capturas de un día, consolidadas. |
| `metadatos/AAAA-MM-DD.parquet` | Snapshot completo con los 49 campos. Uno por día. |
| `docs/data/live.json` | Estado actual, para el panel en vivo. Se sobrescribe cada captura. |
| `docs/data/historico.json.gz` | Serie completa para el dashboard. Se reconstruye a diario. |
| `docs/index.html` | El dashboard. |

---

## Dos advertencias importantes

### El cron de GitHub Actions no es puntual

El mínimo son 5 minutos, pero en la práctica se retrasa entre 5 y 20 minutos, y
bajo carga alta GitHub **omite corridas**. Esto no invalida el análisis: el
factor de utilización pondera cada captura por los minutos que realmente
representa, con la regla del trapecio, así que la cadencia irregular se maneja
correctamente. Lo que se pierde es precisión en la duración de las sesiones.

El panel en vivo muestra la antigüedad de la última captura y avisa en ámbar
cuando pasa de 12 minutos.

Si necesitas cadencia exacta, esto tiene que correr en Cloud Run con Cloud
Scheduler, no en GitHub Actions.

### El repositorio crece

Git conserva cada versión de cada archivo para siempre. Por eso cada captura
escribe un archivo **nuevo** en vez de modificar uno existente, y por eso se
guarda solo el estado y no los 49 campos.

| Enfoque | 30 días |
|---|---:|
| Snapshot completo cada 5 min | ~1,2 GB |
| Solo el estado (lo que hace esto) | ~15 MB |

Aun así crece. Cuando pase de unos cientos de MB, aplasta la historia:

```bash
git checkout --orphan limpio
git add -A
git commit -m "historia consolidada"
git branch -D main
git branch -m main
git push -f origin main
```

Los datos quedan intactos; se pierde el registro de commits, que aquí no aporta
nada.

**Los workflows programados se desactivan solos** tras 60 días sin actividad en
el repositorio. Como este commitea cada 5 minutos, no aplica, pero tenlo
presente si pausas la captura.

---

## Operación

```bash
# capturas de hoy
ls serie/$(date +%F)/ | wc -l

# pausar: Actions → Captura → ··· → Disable workflow
# reanudar: Enable workflow

# consolidar y reconstruir a mano
python scripts/consolida.py

# histórico de los últimos 7 días solamente
python scripts/consolida.py --dias 7
```

Para trabajar con los datos fuera del dashboard:

```python
import pandas as pd, json
hist = pd.concat([pd.read_parquet(p) for p in sorted(Path("historico").glob("*.parquet"))])
ids  = json.loads(open("estado/orden.json").read())
# hist["estados"][k][j] es el estado del conector ids[j] en la captura k
```

---

## Cómo se calcula el factor de utilización

```
FU = minutos en OCUPADO ÷ (minutos en OCUPADO + minutos en DISPONIBLE) × 100
```

**El denominador excluye el tiempo fuera de línea y no disponible.** Un conector
caído no está "disponible y sin uso": simplemente no hay información sobre su
uso. Si se contara como disponible, los conectores más caídos aparecerían como
los menos utilizados, que es exactamente al revés.

**Cada captura pesa por los minutos que representa**, no por igual. Es lo que
permite tolerar la cadencia irregular de GitHub Actions.

**Mide tiempo, no energía.** Un conector ocupado puede estar entregando 3 kW o
150 kW.

---

## Límites de los datos

**`FUERA DE LINEA` no significa averiado.** Agrupa equipos sin comunicación con
el backend del operador, instalaciones declaradas pero no energizadas
(`conectado_a_red = false`), y operadores que simplemente no reportan estado en
tiempo real. En una medición de referencia de 6 horas, el 36% del parque estuvo
en ese estado de forma permanente.

**Un conector que nunca cambia de estado no aporta información de uso.** El
dashboard los separa en cuatro casos, porque significan cosas distintas: siempre
fuera de línea, siempre no disponible, siempre disponible (capacidad ociosa
real, que sí entra al análisis con FU cero), y siempre ocupado.

**Las diferencias entre operadores mezclan calidad de equipo con calidad de
reporte.** Un CPO con mala integración a la SEC aparece con más tiempo fuera de
línea aunque su hardware esté igual de operativo. El API no permite separarlo.

**Sesiones cortas invisibles.** Una carga más breve que el intervalo de muestreo
puede empezar y terminar entre dos capturas.

**La disponibilidad medida es la que ve el usuario en la app**, no la
disponibilidad técnica del equipo.

---

## Fuente y licencia

Datos: Ministerio de Energía de Chile / Superintendencia de Electricidad y
Combustibles, vía el API público de cargadorespublicos.cl. Este repositorio no
está afiliado a ninguna de esas instituciones.
