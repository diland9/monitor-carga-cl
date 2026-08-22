#!/usr/bin/env python3
"""
Verifica la integridad de los datos capturados.

Corre esto en tu computador, dentro de la carpeta del repositorio, para
comprobar que la captura esta funcionando bien antes de usar las cifras.

    python scripts/verifica.py
    python scripts/verifica.py --detalle     # ademas lista los huecos uno a uno

Revisa siete cosas:

  1. Cadencia real: cuanto se atrasa el cron de GitHub Actions en la practica.
  2. Huecos: tramos sin captura, que dejan la serie sin cobertura.
  3. Capturas incompletas: menos conectores de los esperados.
  4. Coherencia de largos: que el string de estados calce con el parque.
  5. Estados desconocidos: codigos fuera de 0-3.
  6. Consistencia entre lo consolidado y lo que ve el dashboard.
  7. Cifras principales, para contrastarlas con el dashboard.
"""

import argparse
import gzip
import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
NOMBRE = ["DISPONIBLE", "OCUPADO", "FUERA DE LINEA", "NO DISPONIBLE"]


def cargar_todo():
    """Junta historico consolidado y dia en curso, igual que el dashboard."""
    partes = []
    for p in sorted((RAIZ / "historico").glob("*.parquet")):
        partes.append(pd.read_parquet(p))
    for carpeta in sorted((RAIZ / "serie").glob("*")):
        if not carpeta.is_dir():
            continue
        filas = []
        for f in sorted(carpeta.glob("*.txt")):
            try:
                ts, est = f.read_text().strip().split("\n")[:2]
                filas.append({"ts": ts, "estados": est})
            except ValueError:
                print(f"  AVISO: archivo ilegible {f}")
        if filas:
            partes.append(pd.DataFrame(filas))
    if not partes:
        return pd.DataFrame()
    df = (pd.concat(partes, ignore_index=True)
          .drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detalle", action="store_true")
    args = ap.parse_args()

    ser = cargar_todo()
    if ser.empty:
        print("No hay datos todavia.")
        return

    ids = json.loads((RAIZ / "estado" / "orden.json").read_text())
    N = len(ids)

    print("=" * 66)
    print("VERIFICACION DE DATOS")
    print("=" * 66)
    print(f"Capturas totales : {len(ser):,}")
    print(f"Ventana          : {ser.ts.min()}  ->  {ser.ts.max()}")
    print(f"Duracion         : {ser.ts.max() - ser.ts.min()}")
    print(f"Dias con datos   : {ser.ts.dt.date.nunique()}")
    print(f"Conectores       : {N:,}")

    # ---- 1. Cadencia real -------------------------------------------------
    g = ser.ts.diff().dt.total_seconds().div(60).dropna()
    print("\n--- 1. Cadencia real del cron ---")
    print(f"  nominal 5 min | mediana {g.median():.1f} | media {g.mean():.1f}")
    print(f"  minimo {g.min():.1f} | maximo {g.max():.1f}")
    for lim, txt in [(6, "puntual (<6 min)"), (11, "aceptable (6-11)"),
                     (21, "atrasado (11-21)"), (1e9, "muy atrasado (>21)")]:
        pass
    b = pd.cut(g, [0, 6, 11, 21, 1e9],
               labels=["<6 min", "6-11", "11-21", ">21"])
    for k, v in b.value_counts().sort_index().items():
        print(f"    {k:>8}: {v:>5,}  ({100*v/len(g):>5.1f}%)")
    if g.median() > 8:
        print("  OJO: el cron se esta atrasando de forma sistematica.")
        print("       El FU sigue siendo valido porque cada captura se pondera")
        print("       por los minutos que cubre, pero las duraciones de sesion")
        print("       pierden precision.")

    # ---- 2. Huecos ---------------------------------------------------------
    huecos = ser[g.reindex(ser.index).fillna(0) > 21]
    print(f"\n--- 2. Huecos mayores a 21 min: {len(huecos)} ---")
    if len(huecos):
        cob = 100 * (1 - g[g > 21].sum() / g.sum())
        print(f"  Cobertura temporal efectiva: {cob:.1f}%")
        if args.detalle:
            for i in huecos.index:
                print(f"    {ser.ts[i-1]} -> {ser.ts[i]}  ({g[i]:.0f} min)")
        else:
            print(f"  El mayor fue de {g.max():.0f} min. Usa --detalle para verlos todos.")

    # ---- 3. Capturas incompletas ------------------------------------------
    largos = ser.estados.str.len()
    print(f"\n--- 3. Coherencia de largos ---")
    print(f"  Parque actual: {N} conectores")
    vc = largos.value_counts().sort_index()
    for k, v in vc.items():
        marca = "  <- actual" if k == N else "  <- parque distinto"
        print(f"    largo {k}: {v:,} capturas{marca}")
    if len(vc) > 1:
        print("  Los largos distintos son normales: ocurren cuando entran o salen")
        print("  conectores del registro. El dashboard descarta los que no calzan.")

    # ---- 4. Estados desconocidos ------------------------------------------
    todos = "".join(ser.estados)
    raros = set(todos) - set("0123")
    print(f"\n--- 4. Codigos de estado ---")
    for k in "0123":
        c = todos.count(k)
        print(f"    {k} {NOMBRE[int(k)]:<16}: {c:>10,}  ({100*c/len(todos):>5.1f}%)")
    if raros:
        print(f"  ERROR: codigos desconocidos {raros}")

    # ---- 5. Contraste con lo que sirve el dashboard -----------------------
    print("\n--- 5. Archivos que lee el dashboard ---")
    for nombre in ["meta.json.gz", "historico.json.gz", "hoy.json.gz", "live.json"]:
        p = RAIZ / "docs" / "data" / nombre
        if not p.exists():
            print(f"    {nombre:<20} FALTA")
            continue
        kb = p.stat().st_size / 1024
        try:
            if nombre.endswith(".gz"):
                d = json.loads(gzip.open(p, "rt", encoding="utf-8").read())
            else:
                d = json.loads(p.read_text())
            if "caps" in d:
                extra = f"{len(d['ts'])} capturas"
            elif "ids" in d:
                extra = f"{len(d['ids'])} conectores"
            else:
                extra = f"ts {d.get('ts','?')}"
            print(f"    {nombre:<20} {kb:>7.1f} KB  {extra}")
        except Exception as e:
            print(f"    {nombre:<20} ILEGIBLE: {e}")

    total_dash = 0
    for nombre in ["historico.json.gz", "hoy.json.gz"]:
        p = RAIZ / "docs" / "data" / nombre
        if p.exists():
            d = json.loads(gzip.open(p, "rt", encoding="utf-8").read())
            total_dash += len(d.get("ts", []))
    print(f"  Capturas en disco: {len(ser):,} | referidas por el dashboard: {total_dash:,}")
    if total_dash < len(ser) * 0.95:
        print("  OJO: el dashboard esta viendo menos capturas de las que hay.")
        print("       Corre el workflow 'Consolidar y publicar' para regenerarlo.")

    # ---- 6. Cifras principales --------------------------------------------
    M = np.array([list(x) for x in ser[largos == N].estados])
    if not len(M):
        print("\nSin capturas del parque actual para calcular cifras.")
        return
    tsv = ser[largos == N].ts.reset_index(drop=True)
    T = len(tsv)
    gg = tsv.diff().dt.total_seconds().div(60).to_numpy()[1:]
    w = np.zeros(T)
    if T == 1:
        w[0] = 5.0
    else:
        w[0], w[-1] = gg[0] / 2, gg[-1] / 2
        if T > 2:
            w[1:-1] = (gg[:-1] + gg[1:]) / 2

    cod = M.astype("U1")
    min_est = {k: float(w[(cod == str(k)).T].sum()) if False else
               float(sum(w[i] * (cod[i] == str(k)).sum() for i in range(T)))
               for k in range(4)}
    mo, md = min_est[1], min_est[0]

    inm = np.array([len(set(cod[:, j])) == 1 for j in range(N)])
    noop = np.array([inm[j] and cod[0, j] in ("2", "3") for j in range(N)])

    print("\n--- 6. Cifras para contrastar con el dashboard ---")
    print(f"  Ventana ponderada     : {w.sum()/60:.2f} h")
    print(f"  Factor de utilizacion : {100*mo/(mo+md):.2f}%")
    print(f"  Horas-conector cargando: {mo/60:.1f}")
    print(f"  Inmoviles (sin cambios): {int(inm.sum()):,} de {N:,} "
          f"({100*inm.sum()/N:.1f}%)")
    print(f"    excluidos del universo A: {int(noop.sum()):,}")
    print(f"  Universo A (operativos): {N - int(noop.sum()):,}")
    print(f"  Universo B (todos)     : {N:,}")

    print("\nSi estas cifras coinciden con las del dashboard, los datos estan bien.")


if __name__ == "__main__":
    main()
