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
    if not p_meta.exists():
        df.to_parquet(p_meta, index=False, compression="zstd")
        print(f"metadatos del dia escritos ({p_meta.stat().st_size/1024:.0f} KB)")

    # ---- panel en vivo -------------------------------------------------------
    cnt = [estados.count(str(k)) for k in range(4)]
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

    sitios = (df.assign(ocup=df["estado"].eq("OCUPADO"))
              .groupby(["location_id", "location_nombre", "comuna", "region",
                        "opc", "latitud", "longitud"], dropna=False)
              .agg(n=("connector_id", "size"), ocup=("ocup", "sum"),
                   disp=("estado", lambda s: (s == "DISPONIBLE").sum()))
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
             "op": str(r.opc), "t": int(r.n), "o": int(r.ocup), "d": int(r.disp)}
            for r in sitios.itertuples()
            if pd.notna(r.latitud) and pd.notna(r.longitud)
        ],
    }
    p_live = RAIZ / "docs" / "data" / "live.json"
    p_live.parent.mkdir(parents=True, exist_ok=True)
    p_live.write_text(json.dumps(live, ensure_ascii=False, separators=(",", ":")))

    print(f"[{ts}] {len(df)} conectores | disp={cnt[0]} ocup={cnt[1]} "
          f"fdl={cnt[2]} nd={cnt[3]} | FU={live['fu']}%")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.exceptions.RequestException as e:
        print(f"ERROR de red: {e}", file=sys.stderr)
        sys.exit(1)
