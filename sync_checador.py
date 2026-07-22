# -*- coding: utf-8 -*-
"""
sync_checador.py
-----------------
Sincroniza automáticamente las checadas y los empleados de tus relojes checadores
(ZKTeco / BM160 y compatibles, como los que administra AccessPROTime.Net) con la
nube (Firebase) que usa tu app de Control de Asistencia.

Corre este programa en una computadora que esté en la MISMA RED que los checadores.
No necesitas tocar nada de la app web: en cuanto este programa suba datos a Firebase,
tu app los va a mostrar solos la próxima vez que la abras o le des "Actualizar".

Cada checada y cada empleado se guardan como su propio documento en Firebase (en vez
de un solo bloque gigante), así no hay límite de cuántas checadas se pueden subir.
Además, este programa recuerda localmente (en "sync_state.json") lo que ya subió, para
no volver a subir lo mismo cada vez y no gastar de más tu cuota gratuita de Firebase.

===========================================================================
 INSTALAR (una sola vez) — abre "Símbolo del sistema" (cmd) y escribe:

    pip install pyzk firebase-admin

===========================================================================
 CONFIGURAR — edita la sección "CONFIGURA ESTO" más abajo:

   1. DEVICES: la lista de tus checadores (nombre, IP y puerto).

   2. SERVICE_ACCOUNT_FILE: la ruta al archivo de credenciales de Firebase
      (Configuración del proyecto > Cuentas de servicio > Generar nueva
      clave privada, en https://console.firebase.google.com).

   3. INTERVALO_MINUTOS: cada cuántos minutos quieres que sincronice solo.
      Ponle 0 si solo quieres que corra una vez y se cierre.

===========================================================================
 EJECUTAR:

    python sync_checador.py

 (o dale doble clic a "ejecutar.bat" si lo tienes en la misma carpeta)
===========================================================================
"""

import json
import os
import time
from datetime import datetime

from zk import ZK
import firebase_admin
from firebase_admin import credentials, firestore


# ============================================================
#                     CONFIGURA ESTO
# ============================================================

DEVICES = [
    {"nombre": "HAUSBAU", "ip": "192.168.1.100", "puerto": 4370},
    # {"nombre": "FABRICA",   "ip": "192.168.1.202", "puerto": 4370},
    # {"nombre": "PINTURAS",  "ip": "192.168.1.170", "puerto": 4370},
    # {"nombre": "OFICINA",   "ip": "192.168.80.202","puerto": 4370},
]

SERVICE_ACCOUNT_FILE = "firebase-credenciales.json"

INTERVALO_MINUTOS = 15   # 0 = correr una sola vez y salir

ESTADO_LOCAL = "sync_state.json"   # aquí se recuerda qué ya se subió (no lo borres)

# ============================================================


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def conectar_firebase():
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def cargar_estado():
    if os.path.exists(ESTADO_LOCAL):
        try:
            with open(ESTADO_LOCAL, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("checadas_subidas", [])
                data.setdefault("empleados_nombres", {})
                return data
        except Exception:
            pass
    return {"checadas_subidas": [], "empleados_nombres": {}}


def guardar_estado(estado):
    with open(ESTADO_LOCAL, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False)


def subir_en_lotes(db, coleccion, documentos, merge=False):
    """documentos: lista de tuplas (doc_id, data_dict). Sube en lotes de máximo 400.
    merge=True combina con lo que ya exista en vez de reemplazar todo el documento
    (importante para empleados, para no borrar depto/puesto/foto/etc. ya capturados en la app)."""
    CHUNK = 400
    for i in range(0, len(documentos), CHUNK):
        batch = db.batch()
        for doc_id, data in documentos[i:i + CHUNK]:
            ref = db.collection(coleccion).document(doc_id)
            if merge:
                batch.set(ref, data, merge=True)
            else:
                batch.set(ref, data)
        batch.commit()


def leer_dispositivo(nombre, ip, puerto):
    """Se conecta a un checador y regresa (usuarios, checadas)."""
    log(f"Conectando a '{nombre}' ({ip}:{puerto}) ...")
    zk = ZK(ip, port=puerto, timeout=15, password=0, force_udp=False, ommit_ping=False)
    conn = None
    usuarios, checadas = [], []
    try:
        conn = zk.connect()
        conn.disable_device()
        usuarios = conn.get_users()
        checadas = conn.get_attendance()
        conn.enable_device()
        log(f"  '{nombre}': {len(usuarios)} usuarios, {len(checadas)} checadas en el dispositivo.")
    except Exception as e:
        log(f"  ERROR conectando a '{nombre}' ({ip}): {e}")
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass
    return usuarios, checadas


def sync_once():
    db = conectar_firebase()
    estado = cargar_estado()

    checadas_subidas = set(estado["checadas_subidas"])
    nombres_conocidos = dict(estado["empleados_nombres"])  # id -> nombre ya subido

    empleados_a_subir = []
    checadas_a_subir = []
    total_emp_nuevos = 0
    total_checadas_nuevas = 0

    for dev in DEVICES:
        usuarios, checadas = leer_dispositivo(dev["nombre"], dev["ip"], dev["puerto"])

        # --- Empleados: solo se vuelve a subir si es nuevo o si cambió el nombre ---
        for u in usuarios:
            pin = str(u.user_id).strip()
            nombre = (u.name or "").strip().upper()
            if not pin:
                continue
            if pin not in nombres_conocidos:
                empleados_a_subir.append((pin, {
                    "id": pin,
                    "nombre": nombre or pin,
                    "fecha": "",
                    "depto": "",
                    "puesto": "",
                    "sueldoHora": 0,
                    "turnoId": "",
                    "codigoId": "",
                    "foto": "",
                    "activo": True,
                }))
                nombres_conocidos[pin] = nombre or pin
                total_emp_nuevos += 1
            elif nombre and nombres_conocidos[pin] != nombre:
                # Solo actualiza el nombre; no reinicia el resto de los datos
                # (la app conserva lo demás porque usa merge, ver nota abajo)
                empleados_a_subir.append((pin, {"id": pin, "nombre": nombre}))
                nombres_conocidos[pin] = nombre

        # --- Checadas: solo se suben las que no se habían subido antes ---
        for a in checadas:
            pin = str(a.user_id).strip()
            dt = a.timestamp
            ts = int(dt.timestamp() * 1000)
            key = f"{pin}_{ts}"
            if key in checadas_subidas:
                continue
            nombre = nombres_conocidos.get(pin, pin)
            checadas_a_subir.append((key, {
                "id": pin,
                "nombre": nombre,
                "fecha": dt.strftime("%Y-%m-%d"),
                "hora": dt.strftime("%H:%M:%S"),
                "ts": ts,
            }))
            checadas_subidas.add(key)
            total_checadas_nuevas += 1

    if empleados_a_subir:
        log(f"Subiendo {len(empleados_a_subir)} empleado(s) nuevos/actualizados ...")
        subir_en_lotes(db, "bm160sync_employees", empleados_a_subir, merge=True)

    if checadas_a_subir:
        log(f"Subiendo {len(checadas_a_subir)} checada(s) nuevas ...")
        subir_en_lotes(db, "bm160sync_punches", checadas_a_subir)

    estado["checadas_subidas"] = list(checadas_subidas)
    estado["empleados_nombres"] = nombres_conocidos
    guardar_estado(estado)

    log(f"Listo: {total_checadas_nuevas} checadas nuevas, {total_emp_nuevos} empleados nuevos.")
    log(f"Total ya sincronizado en este equipo: {len(checadas_subidas)} checadas, {len(nombres_conocidos)} empleados.")


if __name__ == "__main__":
    log("=== Sincronizador de checador iniciado ===")
    while True:
        try:
            sync_once()
        except Exception as e:
            log(f"ERROR durante la sincronización: {e}")

        if INTERVALO_MINUTOS <= 0:
            log("INTERVALO_MINUTOS es 0 — se sincronizó una vez y el programa termina.")
            break

        log(f"Esperando {INTERVALO_MINUTOS} minutos para la siguiente sincronización... (Ctrl+C para detener)\n")
        try:
            time.sleep(INTERVALO_MINUTOS * 60)
        except KeyboardInterrupt:
            log("Detenido por el usuario.")
            break
