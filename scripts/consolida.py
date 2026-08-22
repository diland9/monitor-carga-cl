#!/usr/bin/env python3
"""
Consolida la serie del dia y reconstruye el historico del dashboard.

Corre una vez al dia desde GitHub Actions. Hace tres cosas:

  1. Empaqueta los ~288 archivos sueltos de serie/AAAA-MM-DD/ en un unico
     historico/AAAA-MM-DD.parquet y borra los sueltos. Esto mantiene el
     repositorio chico: sin la consolidacion, el arbol acumula 288 archivos
     nuevos por dia indefinidamente.

  2. Reconstruye docs/data/historico.json.gz, que alimenta el dashboard.

  3. Escribe docs/data/resumen.json con las cifras de portada.

Uso:
    python scripts/consolida.py                # consolida dias cerrados
    python scripts/consolida.py --dias 7       # historico de los ultimos 7 dias
    python scripts/consolida.py --sin-borrar   # no elimina los archivos sueltos
"""

import argparse
import gzip
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

TZ_CL = timezone(timedelta(hours=-4))
RAIZ = Path(__file__).resolve().parent.parent
NOMBRE = ["DISPONIBLE", "OCUPADO", "FUERA DE LINEA", "NO DISPONIBLE"]



def leer_dia_suelto(carpeta):
    """Lee los .txt de una carpeta serie/AAAA-MM-DD/."""
    filas = []
    for p in sorted(carpeta.glob("*.txt")):
        try:
            ts, estados = p.read_text().strip().split("\n")[:2]
            filas.append({"ts": ts, "estados": estados})
        except ValueError:
            print(f"  archivo ilegible, se omite: {p.name}")
    return pd.DataFrame(filas)


def consolidar(borrar=True):
    """Empaqueta cada dia cerrado en un parquet y limpia los sueltos."""
    hoy = datetime.now(TZ_CL).strftime("%Y-%m-%d")
    (RAIZ / "historico").mkdir(exist_ok=True)
    hechos = []
    for carpeta in sorted((RAIZ / "serie").glob("*")):
        if not carpeta.is_dir():
            continue
        fecha = carpeta.name
        if fecha == hoy:
            continue                      # el dia en curso se deja abierto
        df = leer_dia_suelto(carpeta)
        if df.empty:
            continue
        destino = RAIZ / "historico" / f"{fecha}.parquet"
        if destino.exists():
            prev = pd.read_parquet(destino)
            df = (pd.concat([prev, df])
                  .drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
        df.to_parquet(destino, index=False, compression="zstd")
        hechos.append((fecha, len(df), destino.stat().st_size))
        if borrar:
            shutil.rmtree(carpeta)
    for f, n, kb in hechos:
        print(f"  {f}: {n} capturas -> historico/{f}.parquet ({kb/1024:.0f} KB)")
    if not hechos:
        print("  no hay dias cerrados por consolidar")
    return hechos


def cargar_serie(dias=None):
    """Junta el historico consolidado con el dia en curso."""
    partes = []
    for p in sorted((RAIZ / "historico").glob("*.parquet")):
        partes.append(pd.read_parquet(p))
    for carpeta in sorted((RAIZ / "serie").glob("*")):
        if carpeta.is_dir():
            d = leer_dia_suelto(carpeta)
            if not d.empty:
                partes.append(d)
    if not partes:
        return pd.DataFrame()
    df = (pd.concat(partes, ignore_index=True)
          .drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
    df["ts"] = pd.to_datetime(df["ts"])
    if dias:
        corte = df["ts"].max() - pd.Timedelta(days=dias)
        df = df[df["ts"] >= corte].reset_index(drop=True)
    return df


def pesos(ts):
    """Minutos que representa cada captura, por la regla del trapecio.

    Necesario porque el cron de GitHub Actions no es puntual: se retrasa y a
    veces omite corridas. Ponderar por el tiempo real que cubre cada captura
    hace que el factor de utilizacion sea correcto pese a la cadencia irregular.
    """
    n = len(ts)
    if n == 1:
        return np.array([5.0])
    g = ts.diff().dt.total_seconds().div(60).to_numpy()[1:]
    w = np.zeros(n)
    w[0], w[-1] = g[0] / 2, g[-1] / 2
    if n > 2:
        w[1:-1] = (g[:-1] + g[1:]) / 2
    return w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=None)
    ap.add_argument("--sin-borrar", action="store_true")
    args = ap.parse_args()

    print("== Consolidando dias cerrados ==")
    consolidar(borrar=not args.sin_borrar)

    print("\n== Reconstruyendo el historico ==")
    ser = cargar_serie(args.dias)
    if ser.empty:
        print("  sin datos todavia")
        return

    ids = json.loads((RAIZ / "estado" / "orden.json").read_text())
    N = len(ids)

    # Descarta capturas cuyo largo no calza con el orden vigente. Ocurre en la
    # captura inmediatamente anterior a un alta o baja de conectores.
    largos = ser["estados"].str.len()
    malas = int((largos != N).sum())
    if malas:
        print(f"  {malas} capturas con largo distinto al parque actual, se omiten")
        ser = ser[largos == N].reset_index(drop=True)

    w = pesos(ser["ts"])
    g = ser["ts"].diff().dt.total_seconds().div(60).to_numpy()[1:]
    mediana = float(np.median(g)) if len(g) else 5.0
    huecos = int((g > mediana * 1.8).sum()) if len(g) else 0
    print(f"  {len(ser)} capturas | {w.sum()/60:.1f} h | intervalo mediano "
          f"{mediana:.1f} min | huecos {huecos}")

    # Historico: mismo formato que hoy.json.gz (ts + capturas), sin metadatos.
    # Los metadatos viven en meta.json.gz, que escribe captura.py una vez al dia.
    # Mantenerlos separados evita duplicarlos y permite que el dashboard una
    # historico + dia en curso sin conflictos.
    data = {
        "meta": {
            "fuente": "cargadorespublicos.cl/api/data (Ministerio de Energia / SEC)",
            "generado": datetime.now(TZ_CL).strftime("%Y-%m-%d %H:%M"),
            "inicio": ser["ts"].min().strftime("%Y-%m-%d %H:%M"),
            "fin": ser["ts"].max().strftime("%Y-%m-%d %H:%M"),
            "n_ciclos": len(ser),
            "intervalo_min": round(mediana, 1),
            "horas": round(float(w.sum() / 60), 2),
            "huecos": huecos,
            "n_conectores": N,
        },
        "ts": [t.strftime("%Y-%m-%d %H:%M:%S") for t in ser["ts"]],
        "caps": ser["estados"].tolist(),
    }

    salida = RAIZ / "docs" / "data" / "historico.json.gz"
    salida.parent.mkdir(parents=True, exist_ok=True)
    crudo = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.open(salida, "wb", compresslevel=9) as f:
        f.write(crudo)
    print(f"  historico.json.gz: {salida.stat().st_size/1024/1024:.2f} MB "
          f"(sin comprimir {len(crudo)/1024/1024:.2f} MB)")

    # Series por conector, solo para el resumen de portada
    M = np.array([list(x) for x in ser["estados"]])
    regs = [{"s": "".join(M[:, j])} for j in range(N)]

    # ---- resumen de portada -------------------------------------------------
    mo = md = mf = 0
    inmoviles = {k: 0 for k in NOMBRE}
    n_inm = 0
    for r in regs:
        s = r["s"]
        a = np.frombuffer(s.encode(), dtype=np.uint8) - 48
        mo += float(w[a == 1].sum())
        md += float(w[a == 0].sum())
        mf += float(w[a == 2].sum())
        if len(set(s)) == 1:
            n_inm += 1
            inmoviles[NOMBRE[int(s[0])]] += 1

    resumen = {
        "generado": data["meta"]["generado"],
        "ventana": f"{data['meta']['inicio']} a {data['meta']['fin']}",
        "horas": data["meta"]["horas"],
        "capturas": len(ser),
        "conectores": len(regs),
        "inmoviles": n_inm,
        "inmoviles_detalle": inmoviles,
        "fu_global": round(100 * mo / (mo + md), 2) if (mo + md) else None,
        "horas_cargando": round(mo / 60, 1),
        "pct_fuera_linea": round(100 * mf / float(w.sum() * len(regs)), 2),
    }
    (RAIZ / "docs" / "data" / "resumen.json").write_text(
        json.dumps(resumen, ensure_ascii=False, indent=1))
    print(f"  resumen: FU {resumen['fu_global']}% | "
          f"{n_inm} inmoviles de {len(regs)}")


if __name__ == "__main__":
    main()
