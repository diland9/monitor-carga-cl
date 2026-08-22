#!/usr/bin/env python3
"""
Exporta los datos capturados a formatos comodos para analisis externo.

El repositorio guarda la serie de forma compacta -un caracter por conector por
captura- porque asi cabe en git sin inflarlo. Ese formato es eficiente pero
incomodo para trabajar. Este script lo traduce.

Uso:
    python scripts/exportar.py
    python scripts/exportar.py --desde 2026-08-22 --hasta 2026-08-25
    python scripts/exportar.py --csv                 # ademas de parquet
    python scripts/exportar.py --solo panel,eventos

Salidas en export/ (carpeta ignorada por git, no se sube al repositorio):

  panel.parquet          Una fila por conector y captura. Es la tabla base.
                         Columnas: capturado_en, connector_id, estado, peso_min
  eventos.parquet        Una fila por cambio de estado, con toda la
                         identificacion del conector y la duracion del estado
                         anterior.
  episodios.parquet      Tramos continuos en un mismo estado, con duracion.
  conectores.parquet     Una fila por conector: FU, horas cargando, cargas,
                         % fuera de linea, clasificacion operativa.
  capturas.parquet       Una fila por captura: totales por estado y cadencia.
  metadatos.parquet      Los 49 campos del API, ultima version conocida.

Requiere: pandas, pyarrow
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "export"
EST = ["DISPONIBLE", "OCUPADO", "FUERA DE LINEA", "NO DISPONIBLE"]

# Operadores de prueba del registro, sin infraestructura real detras.
# Debe coincidir con OPC_EXCLUIDOS en captura.py.
OPC_EXCLUIDOS = {"nmaes99"}


def cargar_serie(desde=None, hasta=None):
    """Junta el historico consolidado con los dias aun sin consolidar."""
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
                print(f"  aviso: archivo ilegible {f.name}")
        if filas:
            partes.append(pd.DataFrame(filas))
    if not partes:
        return pd.DataFrame()

    df = (pd.concat(partes, ignore_index=True)
          .drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
    df["ts"] = pd.to_datetime(df["ts"])
    if desde:
        df = df[df["ts"] >= pd.Timestamp(desde)]
    if hasta:
        df = df[df["ts"] <= pd.Timestamp(hasta) + pd.Timedelta(days=1)]
    return df.reset_index(drop=True)


def pesos(ts):
    """Minutos que representa cada captura (regla del trapecio).

    El cron de GitHub Actions no es puntual, asi que las capturas no estan
    igualmente espaciadas. Ponderar por el tiempo real que cubre cada una es lo
    que hace que el factor de utilizacion sea correcto pese a esa irregularidad.
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
    ap.add_argument("--desde", help="AAAA-MM-DD")
    ap.add_argument("--hasta", help="AAAA-MM-DD")
    ap.add_argument("--csv", action="store_true", help="ademas de parquet")
    ap.add_argument("--incluir-prueba", action="store_true",
                    help="no excluir los operadores de prueba")
    ap.add_argument("--solo", help="lista separada por comas de que exportar")
    args = ap.parse_args()
    quiero = set(args.solo.split(",")) if args.solo else None

    ser = cargar_serie(args.desde, args.hasta)
    if ser.empty:
        sys.exit("No hay datos en ese rango.")

    ids = json.loads((RAIZ / "estado" / "orden.json").read_text())
    N = len(ids)

    largos = ser["estados"].str.len()
    if (largos != N).any():
        print(f"  {int((largos != N).sum())} capturas de un parque distinto, se omiten")
        ser = ser[largos == N].reset_index(drop=True)

    metas = sorted((RAIZ / "metadatos").glob("*.parquet"))
    if not metas:
        sys.exit("Falta metadatos/. Corre una captura primero.")
    meta = (pd.read_parquet(metas[-1])
            .drop_duplicates("connector_id").set_index("connector_id"))

    excluidos = set()
    if not args.incluir_prueba:
        for cid in ids:
            if cid in meta.index:
                o = str(meta.loc[cid].get("opc", "")).strip().lower()
                if o in OPC_EXCLUIDOS:
                    excluidos.add(cid)
        if excluidos:
            print(f"  {len(excluidos)} conectores excluidos (operador de prueba)")

    w = pesos(ser["ts"])
    T = len(ser)
    print(f"\n{T:,} capturas | {ser.ts.min()} -> {ser.ts.max()} | "
          f"{w.sum()/60:.1f} h | {N:,} conectores")

    SALIDA.mkdir(exist_ok=True)
    M = np.frombuffer("".join(ser["estados"]).encode(), dtype=np.uint8) - 48
    M = M.reshape(T, N)                      # capturas x conectores

    keep = np.array([cid not in excluidos for cid in ids])
    ids_k = [c for c, k in zip(ids, keep) if k]
    Mk = M[:, keep]
    Nk = len(ids_k)

    def guardar(nombre, df):
        if quiero and nombre not in quiero:
            return
        df.to_parquet(SALIDA / f"{nombre}.parquet", index=False, compression="zstd")
        linea = f"  {nombre}.parquet: {len(df):,} filas"
        if args.csv:
            df.to_csv(SALIDA / f"{nombre}.csv", index=False, encoding="utf-8-sig")
            linea += "  (+ csv)"
        print(linea)

    # --- atributos por conector, para adjuntar a cada tabla ------------------
    cols_id = ["location_id", "location_nombre", "direccion", "comuna", "region",
               "opc", "estandar", "formato", "tipo_corriente", "pot_max_kw",
               "tension_max_v", "corriente_max_a", "marca", "modelo",
               "empresa_distribuidora", "institucion_privada", "conectado_a_red",
               "folio_IRVE", "irve_confirmado", "latitud", "longitud",
               "precio_clp_kwh", "evse_uid", "evse_id"]
    cols_id = [c for c in cols_id if c in meta.columns]
    attrs = meta.loc[[c for c in ids_k if c in meta.index], cols_id].reset_index()
    attrs["latitud"] = pd.to_numeric(attrs.get("latitud"), errors="coerce")
    attrs["longitud"] = pd.to_numeric(attrs.get("longitud"), errors="coerce")
    kw = pd.to_numeric(attrs["pot_max_kw"], errors="coerce").fillna(0)
    attrs["velocidad"] = pd.cut(kw, [-1, 49.999, 149.999, 1e9],
                                labels=["LENTO", "RAPIDO", "ULTRARRAPIDO"])

    print("\nEscribiendo en export/")
    guardar("metadatos", pd.read_parquet(metas[-1]))

    # --- panel largo ---------------------------------------------------------
    if not quiero or "panel" in quiero:
        panel = pd.DataFrame({
            "capturado_en": np.repeat(ser["ts"].to_numpy(), Nk),
            "connector_id": np.tile(np.array(ids_k), T),
            "estado_cod": Mk.reshape(-1),
            "peso_min": np.repeat(w, Nk),
        })
        panel["estado"] = pd.Categorical.from_codes(panel["estado_cod"], EST)
        panel = panel.drop(columns="estado_cod")
        guardar("panel", panel)
        del panel

    # --- resumen por captura -------------------------------------------------
    cap = pd.DataFrame({
        "capturado_en": ser["ts"],
        "peso_min": w,
        "conectores": Nk,
    })
    for k, nom in enumerate(EST):
        cap[nom.lower().replace(" ", "_")] = (Mk == k).sum(axis=1)
    cap["fu_pct"] = (100 * cap["ocupado"] /
                     (cap["ocupado"] + cap["disponible"])).round(2)
    cap["min_desde_anterior"] = (cap["capturado_en"].diff()
                                 .dt.total_seconds().div(60).round(1))
    guardar("capturas", cap)

    # --- episodios y eventos -------------------------------------------------
    if not quiero or {"episodios", "eventos"} & (quiero or {"episodios", "eventos"}):
        eps = []
        for j in range(Nk):
            col = Mk[:, j]
            cortes = np.flatnonzero(np.diff(col)) + 1
            ini = np.concatenate(([0], cortes))
            fin = np.concatenate((cortes - 1, [T - 1]))
            for a, b in zip(ini, fin):
                eps.append((ids_k[j], int(col[a]), a, b, b - a + 1,
                            float(w[a:b + 1].sum())))
        ep = pd.DataFrame(eps, columns=["connector_id", "estado_cod",
                                        "i0", "i1", "n_capturas", "dur_min"])
        ep["estado"] = pd.Categorical.from_codes(ep["estado_cod"], EST)
        ep["t_inicio"] = ser["ts"].to_numpy()[ep["i0"]]
        ep["t_fin"] = ser["ts"].to_numpy()[ep["i1"]]
        # Un episodio que toca un borde puede haber empezado antes o seguir
        # despues: su duracion observada es una cota inferior.
        ep["borde"] = (ep["i0"] == 0) | (ep["i1"] == T - 1)
        ep = ep.drop(columns=["estado_cod", "i0", "i1"])
        ep = ep.merge(attrs, on="connector_id", how="left")
        guardar("episodios", ep)

        ev = ep.sort_values(["connector_id", "t_inicio"]).copy()
        ev["estado_anterior"] = ev.groupby("connector_id")["estado"].shift(1)
        ev["dur_anterior_min"] = ev.groupby("connector_id")["dur_min"].shift(1)
        ev = ev[ev["estado_anterior"].notna()].copy()
        ev["transicion"] = (ev["estado_anterior"].astype(str) + " -> " +
                            ev["estado"].astype(str))
        ev["tipo_evento"] = np.select(
            [(ev["estado_anterior"] == "DISPONIBLE") & (ev["estado"] == "OCUPADO"),
             (ev["estado_anterior"] == "OCUPADO") & (ev["estado"] == "DISPONIBLE"),
             ev["estado"] == "FUERA DE LINEA",
             ev["estado_anterior"] == "FUERA DE LINEA"],
            ["INICIO DE CARGA", "TERMINO DE CARGA", "CAIDA", "RECUPERACION"],
            default="OTRO")
        ev = ev.rename(columns={"t_inicio": "capturado_en"}).drop(columns=["t_fin"])
        guardar("eventos", ev)

    # --- resumen por conector ------------------------------------------------
    minutos = np.zeros((Nk, 4))
    for k in range(4):
        minutos[:, k] = ((Mk == k) * w[:, None]).sum(axis=0)
    con = pd.DataFrame(minutos, columns=[f"min_{e.lower().replace(' ','_')}"
                                         for e in EST])
    con.insert(0, "connector_id", ids_k)
    con["min_total"] = minutos.sum(axis=1)
    con["horas_cargando"] = (con["min_ocupado"] / 60).round(3)
    inf = con["min_disponible"] + con["min_ocupado"]
    con["fu_pct"] = np.where(inf > 0, 100 * con["min_ocupado"] / inf, np.nan).round(2)
    con["fu_bruto_pct"] = (100 * con["min_ocupado"] / con["min_total"]).round(2)
    con["pct_fuera_linea"] = (100 * con["min_fuera_de_linea"] / con["min_total"]).round(2)
    con["pct_no_disponible"] = (100 * con["min_no_disponible"] / con["min_total"]).round(2)

    cambios = (np.diff(Mk, axis=0) != 0).sum(axis=0)
    con["cambios_estado"] = cambios
    con["inmovil"] = cambios == 0
    ini_carga = ((Mk[:-1] == 0) & (Mk[1:] == 1)).sum(axis=0)
    con["cargas_iniciadas"] = ini_carga
    primero = Mk[0]
    con["clase_operativa"] = np.where(
        cambios > 0, "ACTIVO",
        np.select([primero == 2, primero == 3, primero == 1],
                  ["SIEMPRE FUERA DE LINEA", "SIEMPRE NO DISPONIBLE",
                   "SIEMPRE OCUPADO"], default="SIEMPRE DISPONIBLE"))
    con["caido_permanente"] = con["inmovil"] & np.isin(primero, [2, 3])
    con = con.merge(attrs, on="connector_id", how="left")
    con["universo_operativo"] = ~con["caido_permanente"]
    con["universo_publico"] = (~con["caido_permanente"]) & \
                              (con.get("institucion_privada") != True)
    guardar("conectores", con)

    # --- cifras de control ---------------------------------------------------
    mo = con["min_ocupado"].sum()
    md = con["min_disponible"].sum()
    print(f"\nCifras de control (deben coincidir con el dashboard):")
    print(f"  Factor de utilizacion  : {100*mo/(mo+md):.2f}%")
    print(f"  Horas-conector cargando: {mo/60:,.1f}")
    print(f"  Caidos permanentes     : {int(con['caido_permanente'].sum()):,}")
    print(f"  Universo A operativos  : {int(con['universo_operativo'].sum()):,}")
    print(f"  Universo B todos       : {len(con):,}")
    print(f"  Universo C publicos    : {int(con['universo_publico'].sum()):,}")
    print(f"\nListo. Archivos en {SALIDA}")


if __name__ == "__main__":
    main()
