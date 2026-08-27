#!/usr/bin/env python3
"""
Captura el estado de la red publica de carga y lo guarda de forma compacta.

Se ejecuta desde GitHub Actions cada 5 minutos. Cada corrida escribe archivos
NUEVOS y nunca reescribe los anteriores. Eso importa: git guarda una version
completa de cada archivo modificado, asi que reescribir un archivo que crece
haria que el repositorio se inflara sin control.

Que escribe cada corrida:

  serie/AAAA-MM-DD/HHMMSS.txt    2 lineas: timestamp y un string con el estado
                                 de cada conector, un caracter por conector, en
                                 el orden de estado/orden.json. ~2 KB.
  docs/data/live.json            resumen para el panel en vivo. Se sobrescribe.
  metadatos/AAAA-MM-DD.parquet   snapshot completo con los 49 campos. Una vez
                                 al dia, la primera corrida que lo encuentre
                                 ausente.

Codigos de estado: 0 disponible · 1 ocupado · 2 fuera de linea · 3 no disponible
"""

import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

URL = "https://cargadorespublicos.cl/api/data"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "referer": "https://cargadorespublicos.cl/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
}
TZ_CL = timezone(timedelta(hours=-4))
COD = {"DISPONIBLE": "0", "OCUPADO": "1", "FUERA DE LINEA": "2", "NO DISPONIBLE": "3"}
NOMBRE = ["DISPONIBLE", "OCUPADO", "FUERA DE LINEA", "NO DISPONIBLE"]

RAIZ = Path(__file__).resolve().parent.parent
FORZAR_TEXTO = ["propietario_rut", "opc_rut", "evse_id", "medios_pago",
                "medios_activacion", "fecha_conexion", "revision_date",
                "loc_last_updated", "evse_last_updated", "tarifa_last_updated",
                "con_last_updated", "latitud", "longitud"]


def precio_energia(tariffs):
    if not tariffs:
        return None
    for pc in (tariffs[0].get("elements") or {}).get("price_components", []):
        if pc.get("tariff_dimension") == "ENERGIA":
            return pc.get("price")
    return None


def aplanar(data, ts):
    filas = []
    for loc in data:
        c = loc.get("coordinates") or {}
        owner = loc.get("owner") or {}
        opc = loc.get("OPC") or {}
        hor = loc.get("opening_times") or {}
        base_loc = {
            "capturado_en": ts,
            "location_id": loc.get("location_id"),
            "location_nombre": loc.get("name"),
            "direccion": loc.get("address"),
            "comuna": loc.get("commune"),
            "region": loc.get("region"),
            "latitud": c.get("latitude"),
            "longitud": c.get("longitude"),
            "institucion_privada": loc.get("institucion_privada"),
            "tipo_instalacion": loc.get("charging_instalation_type"),
            "parking_type": loc.get("parking_type"),
            "abierto_24_7": hor.get("twentyfourseven"),
            "opc": opc.get("normalized_name"),
            "opc_rut": opc.get("RUT"),
            "propietario": owner.get("name"),
            "propietario_rut": owner.get("RUT"),
            "empresa_distribuidora": loc.get("dx_company"),
            "conectado_a_red": loc.get("connected_to_electrical_grid"),
            "fecha_conexion": loc.get("connection_date"),
            "folio_IRVE": loc.get("folio_IRVE"),
            "irve_confirmado": loc.get("datos_IRVE_confirmados"),
            "revision_date": loc.get("revision_date"),
            "loc_last_updated": loc.get("last_updated"),
        }
        for evse in loc.get("evses") or []:
            base_evse = {
                "evse_uid": evse.get("evse_uid"),
                "evse_id": evse.get("evse_id"),
                "evse_orden": evse.get("order_number"),
                "evse_estado": evse.get("status"),
                "evse_pot_max_kw": evse.get("max_electric_power"),
                "marca": evse.get("brand"),
                "modelo": evse.get("model"),
                "carga_simultanea": evse.get("permite_carga_simultanea"),
                "uso_exclusivo": evse.get("uso_exclusivo"),
                "medios_pago": "|".join(evse.get("payment_capabilities") or []),
                "medios_activacion": "|".join(evse.get("activation_capabilities") or []),
                "evse_last_updated": evse.get("last_updated"),
            }
            for con in evse.get("connectors") or []:
                tar = (con.get("tariffs") or [{}])[0]
                filas.append({
                    **base_loc, **base_evse,
                    "connector_id": con.get("connector_id"),
                    "conector_orden": con.get("order_number"),
                    "estandar": con.get("standard"),
                    "formato": con.get("format"),
                    "tipo_corriente": con.get("power_type"),
                    "pot_max_kw": con.get("max_electric_power"),
                    "tension_max_v": con.get("max_voltage"),
                    "corriente_max_a": con.get("max_amperage"),
                    "estado": con.get("status"),
                    "precio_clp_kwh": precio_energia(con.get("tariffs")),
                    "tarifa_min_clp": tar.get("min_price"),
                    "tarifa_max_clp": tar.get("max_price"),
                    "tarifa_last_updated": tar.get("last_updated"),
                    "con_last_updated": con.get("last_updated"),
                })
    df = pd.DataFrame(filas)
    for col in FORZAR_TEXTO:
        if col in df.columns:
            df[col] = df[col].astype("string")
    return df



CAMPOS_META = {
    "n": "location_nombre", "cm": "comuna", "rg": "region", "op": "opc",
    "es": "estandar", "kw": "pot_max_kw", "tc": "tipo_corriente",
    "la": "latitud", "lo": "longitud", "pr": "precio_clp_kwh",
    "lid": "location_id", "mk": "marca", "dx": "empresa_distribuidora",
    "irve": "folio_IRVE", "pv": "institucion_privada", "dir": "direccion",
}

# Operadores de prueba que el registro publica pero no corresponden a
# infraestructura real. Se marcan como excluidos en los metadatos en vez de
# borrarse de la serie: asi el largo del string de estados no cambia y las
# capturas ya tomadas siguen siendo validas.
#
# La coincidencia es por SUBCADENA, no exacta: en el registro el nombre real
# aparece como "nmaes99 prueba prod patre99 matre99" y va cambiando.
OPC_EXCLUIDOS = ("nmaes99", "patre99", "matre99", "prueba prod")


def es_excluido(nombre):
    n = str(nombre).strip().lower()
    return any(p in n for p in OPC_EXCLUIDOS)


def escribir_meta(df, ids, destino):
    """Metadatos fijos de cada conector, alineados al orden de ids.

    Se reescribe una vez al dia. Va aparte de la serie porque cambia poco y
    pesa mucho mas: mantenerlo separado evita reescribir ~300 KB cada 5 min.
    """
    m = df.set_index("connector_id")
    regs = []
    for cid in ids:
        if cid not in m.index:
            regs.append({"id": int(cid)})
            continue
        r = m.loc[cid]
        d = {"id": int(cid)}
        for corto, largo in CAMPOS_META.items():
            v = r.get(largo)
            if pd.isna(v):
                d[corto] = None
            elif corto in ("la", "lo"):
                try:
                    d[corto] = round(float(v), 5)
                except (TypeError, ValueError):
                    d[corto] = None
            elif corto in ("kw", "pr"):
                d[corto] = float(v)
            elif corto in ("lid", "irve"):
                d[corto] = int(v)
            elif corto == "pv":
                d[corto] = bool(v)
            else:
                d[corto] = str(v)[:70]
        if es_excluido(r.get("opc")):
            d["ex"] = 1
        regs.append(d)
    n_ex = sum(1 for d in regs if d.get("ex"))
    if n_ex:
        print(f"  {n_ex} conectores marcados como excluidos (operador de prueba)")
    payload = {"ids": [int(i) for i in ids], "meta": regs}
    with gzip.open(destino, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


P_HISTORIAL = RAIZ / "estado" / "historial_ids.json"
SIN_DATO = "9"  # conector que todavia no existia (o ya no) en esa epoca


def cargar_historial():
    """Cada entrada es {"desde": ts, "ids": [...]}: el orden de conectores
    vigente desde ese instante hasta el siguiente cambio registrado."""
    if not P_HISTORIAL.exists():
        return []
    return sorted(json.loads(P_HISTORIAL.read_text()), key=lambda e: e["desde"])


def registrar_epoca(ids, ts):
    """Deja constancia de un cambio de parque, para que mas adelante se
    puedan realinear por connector_id las capturas tomadas bajo este orden
    con las de cualquier otro momento. Sin este registro, un alta o baja de
    conectores vuelve incomparable por posicion todo lo capturado antes."""
    hist = cargar_historial()
    hist.append({"desde": ts, "ids": ids})
    P_HISTORIAL.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")))


def reindexar_a(ids_actual, historial, filas):
    """Realinea (ts, estados) al orden de ids_actual usando connector_id en
    vez de posicion. Los conectores que no existian en la epoca de una fila
    quedan en SIN_DATO. Necesario porque el string de estados solo tiene
    sentido bajo el orden vigente cuando se tomo esa captura; una fila cuyo
    largo no calza ni con su propia epoca (dato corrupto) se descarta."""
    epocas = list(historial)
    if not epocas or epocas[-1]["ids"] != ids_actual:
        epocas.append({"desde": "0000-00-00 00:00:00", "ids": ids_actual})
    mapas = {}

    def mapa(i):
        if i not in mapas:
            pos = {cid: p for p, cid in enumerate(epocas[i]["ids"])}
            mapas[i] = ([pos.get(cid) for cid in ids_actual], len(epocas[i]["ids"]))
        return mapas[i]

    out, ei = [], 0
    for ts, est in filas:
        while ei + 1 < len(epocas) and epocas[ei + 1]["desde"] <= ts:
            ei += 1
        m, n = mapa(ei)
        if len(est) != n:
            continue
        out.append((ts, "".join(SIN_DATO if p is None else est[p] for p in m)))
    return out


def escribir_hoy(carpeta_dia, ids, destino):
    """Serie del dia en curso, reconstruida desde los .txt de la jornada.

    Esto es lo que permite ver la evolucion intradiaria sin esperar a la
    consolidacion nocturna. Solo lleva timestamps y estados: los metadatos
    viven en meta.json.gz.
    """
    filas = []
    for p in sorted(carpeta_dia.glob("*.txt")):
        try:
            ts, est = p.read_text().strip().split("\n")[:2]
        except ValueError:
            continue
        filas.append((ts, est))
    filas.sort()
    # Reindexa por si el parque cambio en medio del dia: sin esto, las horas
    # anteriores al cambio desaparecian de "hoy" hasta la consolidacion.
    filas = reindexar_a(ids, cargar_historial(), filas)
    payload = {"ts": [t for t, _ in filas], "caps": [e for _, e in filas]}
    with gzip.open(destino, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return len(filas)


def main():
    ahora = datetime.now(TZ_CL)
    ts = ahora.strftime("%Y-%m-%d %H:%M:%S")
    fecha = ahora.strftime("%Y-%m-%d")

    r = requests.get(URL, headers=HEADERS, timeout=90)
    r.raise_for_status()
    df = aplanar(r.json(), ts)
    if df.empty:
        print("respuesta sin conectores; no se escribe nada")
        return 0

    df = df.dropna(subset=["connector_id"])
    df["connector_id"] = df["connector_id"].astype("int64")
    df = df.drop_duplicates("connector_id").sort_values("connector_id")

    # ---- orden de conectores -------------------------------------------------
    # El string de estados depende de este orden. Solo se reescribe cuando el
    # parque cambia (altas o bajas), lo que ocurre pocas veces al mes.
    ids = df["connector_id"].tolist()
    p_orden = RAIZ / "estado" / "orden.json"
    p_orden.parent.mkdir(parents=True, exist_ok=True)
    previo = json.loads(p_orden.read_text()) if p_orden.exists() else None
    if previo != ids:
        p_orden.write_text(json.dumps(ids, separators=(",", ":")))
        registrar_epoca(ids, ts)
        if previo is None:
            print(f"orden.json creado con {len(ids)} conectores")
        else:
            altas = len(set(ids) - set(previo))
            bajas = len(set(previo) - set(ids))
            print(f"parque cambio: +{altas} altas, -{bajas} bajas -> orden.json actualizado")

    # ---- serie: un archivo por corrida, nunca se reescribe --------------------
    estados = "".join(COD.get(e, "2") for e in df["estado"])
    p_ser = RAIZ / "serie" / fecha / f"{ahora.strftime('%H%M%S')}.txt"
    p_ser.parent.mkdir(parents=True, exist_ok=True)
    p_ser.write_text(f"{ts}\n{estados}\n")

    # ---- metadatos: snapshot completo, uno al dia ----------------------------
    p_meta = RAIZ / "metadatos" / f"{fecha}.parquet"
    p_meta.parent.mkdir(parents=True, exist_ok=True)
    dir_datos = RAIZ / "docs" / "data"
    dir_datos.mkdir(parents=True, exist_ok=True)
    p_meta_json = dir_datos / "meta.json.gz"
    if not p_meta.exists() or not p_meta_json.exists() or previo != ids:
        df.to_parquet(p_meta, index=False, compression="zstd")
        escribir_meta(df, ids, p_meta_json)
        print(f"metadatos actualizados ({p_meta_json.stat().st_size/1024:.0f} KB)")

    # ---- serie del dia en curso, para el dashboard ---------------------------
    n_hoy = escribir_hoy(p_ser.parent, ids, dir_datos / "hoy.json.gz")

    # ---- panel en vivo -------------------------------------------------------
    # El operador de prueba se excluye de los agregados en vivo, igual que en
    # el dashboard, para que las cifras de ambos coincidan.
    df = df[~df["opc"].map(es_excluido)].copy()
    estados_vivo = "".join(COD.get(e, "2") for e in df["estado"])
    cnt = [estados_vivo.count(str(k)) for k in range(4)]
    inf = cnt[0] + cnt[1]
    por = lambda col: (df.groupby(col)["estado"]
                       .agg(n="size",
                            ocupado=lambda s: (s == "OCUPADO").sum(),
                            disponible=lambda s: (s == "DISPONIBLE").sum(),
                            fuera=lambda s: (s == "FUERA DE LINEA").sum())
                       .reset_index().rename(columns={col: "k"}))

    def tabla(col, top=None):
        t = por(col)
        t["fu"] = (100 * t["ocupado"] / (t["ocupado"] + t["disponible"])).round(1)
        t = t.sort_values("n", ascending=False)
        if top:
            t = t.head(top)
        return t.fillna(0).to_dict("records")

    # Tramo de velocidad del sitio: el mas alto entre sus conectores.
    # 0 lento <50 kW · 1 rapido 50-149 · 2 ultrarrapido >=150
    kw = pd.to_numeric(df["pot_max_kw"], errors="coerce").fillna(0)
    df = df.assign(kb=pd.cut(kw, [-1, 49.999, 149.999, 1e9],
                             labels=[0, 1, 2]).astype("int8"))
    sitios = (df.assign(ocup=df["estado"].eq("OCUPADO"))
              .groupby(["location_id", "location_nombre", "comuna", "region",
                        "opc", "latitud", "longitud"], dropna=False)
              .agg(n=("connector_id", "size"), ocup=("ocup", "sum"),
                   disp=("estado", lambda s: (s == "DISPONIBLE").sum()),
                   kb=("kb", "max"),
                   priv=("institucion_privada", "max"))
              .reset_index())

    live = {
        "ts": ts,
        "total": len(df),
        "estados": {NOMBRE[k]: cnt[k] for k in range(4)},
        "fu": round(100 * cnt[1] / inf, 2) if inf else None,
        "sitios": int(df["location_id"].nunique()),
        "por_operador": tabla("opc"),
        "por_region": tabla("region"),
        "por_estandar": tabla("estandar"),
        "cargando": [
            {"n": str(r.location_nombre)[:60], "cm": str(r.comuna),
             "op": str(r.opc), "ocup": int(r.ocup), "tot": int(r.n)}
            for r in sitios[sitios["ocup"] > 0]
            .sort_values("ocup", ascending=False).head(40).itertuples()
        ],
        "mapa": [
            {"la": float(r.latitud), "lo": float(r.longitud),
             "n": str(r.location_nombre)[:60], "cm": str(r.comuna),
             "op": str(r.opc), "t": int(r.n), "o": int(r.ocup),
             "d": int(r.disp), "kb": int(r.kb), "pv": bool(r.priv)}
            for r in sitios.itertuples()
            if pd.notna(r.latitud) and pd.notna(r.longitud)
        ],
    }
    p_live = dir_datos / "live.json"
    p_live.write_text(json.dumps(live, ensure_ascii=False, separators=(",", ":")))

    print(f"[{ts}] {len(df)} conectores | disp={cnt[0]} ocup={cnt[1]} "
          f"fdl={cnt[2]} nd={cnt[3]} | FU={live['fu']}% | "
          f"{n_hoy} capturas hoy")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.exceptions.RequestException as e:
        print(f"ERROR de red: {e}", file=sys.stderr)
        sys.exit(1)
