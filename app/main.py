from fastapi.responses import JSONResponse

"""
bonos-service
=============
Microservicio de BONOS / promociones del casino (FastAPI).

Comparte la base de datos y el JWT con casino-backend. Permite:
  - Consultar el catálogo de bonos.
  - Reclamar un bono: acredita saldo al usuario y registra la transacción,
    todo en una transacción SQL atómica (igual patrón que casino-backend).

Prefijo de rutas: /api/bonos  (para que nginx pueda enrutar por prefijo).
"""
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .auth import usuario_actual
from .db import conexion, dict_cursor, esperar_bd, init_schema, ping
from fastapi.responses import JSONResponse

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Al arrancar: esperar la BD y asegurar/sembrar el esquema propio.
    esperar_bd()
    init_schema()
    yield


app = FastAPI(
    title="Bonos Service",
    description="Bonos y promociones del casino (Módulo 3 - ISY1101)",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS para que el frontend Angular (localhost:4200) pueda llamar en desarrollo.
_origenes = [o.strip() for o in os.getenv("CORS_ORIGIN", "http://localhost:4200").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origenes,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReclamarRequest(BaseModel):
    # Para bonos de tipo 'porcentaje' se calcula sobre este monto base
    # (p.ej. lo que el usuario recarga o apostó). Ignorado en 'monto_fijo'.
    monto_base: float = Field(default=0, ge=0, description="Base para bonos por porcentaje")

# TODO (alumno): implementar las rutas de salud que usará Kubernetes:
#   - liveness: ¿el proceso está vivo? (respuesta simple).
#   - readiness: ¿está listo para recibir tráfico? Debe verificar la BD.
# Luego configurar livenessProbe/readinessProbe en el Deployment de EKS.

@app.get("/livez")
def liveness():
    """
    Kubernetes usa este endpoint para comprobar que el proceso sigue vivo.
    No consulta la base de datos.
    """
    return {
        "status": "ok",
        "service": "bonos-service"
    }

@app.get("/readyz")
def readiness():
    """
    Kubernetes usa este endpoint para comprobar que el servicio
    está listo para recibir tráfico.
    """

    if ping():
        return {
            "status": "ready",
            "database": "connected"
        }

    return JSONResponse(
        status_code=503,
        content={
            "status": "not ready",
            "database": "disconnected"
        }
    )


@app.get("/api/bonos")
def listar_bonos():
    """Catálogo de bonos activos (público)."""
    with conexion() as conn:
        with dict_cursor(conn) as cur:
            cur.execute(
                """SELECT id, codigo, nombre, descripcion, tipo, valor, un_solo_uso
                     FROM bonos WHERE activo = TRUE ORDER BY id"""
            )
            return {"bonos": cur.fetchall()}


@app.get("/api/bonos/mis-bonos")
def mis_bonos(usuario: dict = Depends(usuario_actual)):
    """Bonos ya reclamados por el usuario autenticado."""
    with conexion() as conn:
        with dict_cursor(conn) as cur:
            cur.execute(
                """SELECT br.id, b.codigo, b.nombre, br.monto_otorgado, br.reclamado_en
                     FROM bonos_reclamados br
                     JOIN bonos b ON b.id = br.bono_id
                    WHERE br.usuario_id = %s
                    ORDER BY br.reclamado_en DESC""",
                (usuario["id"],),
            )
            return {"reclamados": cur.fetchall()}


@app.post("/api/bonos/{codigo}/reclamar", status_code=201)
def reclamar_bono(codigo: str, body: ReclamarRequest, usuario: dict = Depends(usuario_actual)):
    """
    Reclama un bono: calcula el monto, acredita saldo e inserta la transacción.
    Todo dentro de una transacción atómica (ROLLBACK ante cualquier error).
    """
    with conexion() as conn:
        with dict_cursor(conn) as cur:
            # 1) Buscar el bono activo
            cur.execute(
                "SELECT id, codigo, nombre, tipo, valor, un_solo_uso FROM bonos WHERE codigo = %s AND activo = TRUE",
                (codigo,),
            )
            bono = cur.fetchone()
            if bono is None:
                raise HTTPException(status_code=404, detail=f"Bono '{codigo}' no encontrado")

            # 2) Calcular el monto a otorgar
            if bono["tipo"] == "monto_fijo":
                monto = float(bono["valor"])
            else:  # porcentaje
                if body.monto_base <= 0:
                    raise HTTPException(
                        status_code=400,
                        detail="Este bono es por porcentaje: envía 'monto_base' > 0",
                    )
                monto = round(body.monto_base * float(bono["valor"]) / 100.0, 2)

            # 3) Si es de un solo uso, verificar que no lo haya reclamado antes
            if bono["un_solo_uso"]:
                cur.execute(
                    "SELECT 1 FROM bonos_reclamados WHERE usuario_id = %s AND bono_id = %s",
                    (usuario["id"], bono["id"]),
                )
                if cur.fetchone() is not None:
                    raise HTTPException(status_code=409, detail="Este bono ya fue reclamado")

            # 4) Acreditar saldo + registrar transacción + registrar reclamo
            cur.execute(
                "UPDATE usuarios SET saldo = saldo + %s WHERE id = %s RETURNING saldo",
                (monto, usuario["id"]),
            )
            fila = cur.fetchone()
            if fila is None:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")
            saldo = fila["saldo"]

            cur.execute(
                """INSERT INTO transacciones (usuario_id, tipo, monto, saldo_post, detalle)
                   VALUES (%s, 'deposito', %s, %s, %s::jsonb)""",
                (usuario["id"], monto, saldo, _json({"bono": bono["codigo"], "nombre": bono["nombre"]})),
            )
            cur.execute(
                """INSERT INTO bonos_reclamados (usuario_id, bono_id, monto_otorgado)
                   VALUES (%s, %s, %s) RETURNING id""",
                (usuario["id"], bono["id"], monto),
            )
        conn.commit()

    return {"bono": bono["codigo"], "monto_otorgado": monto, "saldo": saldo}


def _json(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
