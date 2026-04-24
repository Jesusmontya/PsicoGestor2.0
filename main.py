import os
import json
from typing import Any, List, Dict
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request, Response, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from supabase import create_client, Client
from groq import Groq
from dotenv import load_dotenv
from fastapi.responses import FileResponse
import stripe


# ==========================================
# 1. CONFIGURACIÓN INICIAL
# ==========================================
load_dotenv()

SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY")
STRIPE_SECRET_KEY   = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID     = os.getenv("STRIPE_PRICE_ID", "price_1TNi4nCSdv7aogFNHp8XAqjc")

if STRIPE_SECRET_KEY:
    # LIMPIEZA DE LA CLAVE STRIPE: Quita espacios y saltos de línea (\n)
    stripe.api_key = STRIPE_SECRET_KEY.strip()
else:
    print("⚠️  STRIPE_SECRET_KEY no encontrado en .env")

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    print("⚠️  SUPABASE_URL inválido en .env")
    SUPABASE_URL = "https://proyecto-dummy.supabase.co"
    SUPABASE_KEY = "llave-dummy"

app = FastAPI(
    title="Psicogestor API",
    description="Backend Psicogestor 2.5 — Dev Group Studio",
    version="2.5.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Clientes globales
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Modelos Groq en orden de preferencia
GROQ_MODELOS = ["llama-3.3-70b-versatile", "llama3-70b-8192", "mixtral-8x7b-32768"]


# ==========================================
# 2. MODELOS PYDANTIC
# ==========================================

class CitaCreate(BaseModel):
    paciente: str
    profesional_id: str
    fecha: str
    hora: str
    tipo: str
    notas: str
    sync: bool
    modalidad: str = "presencial"

class SesionAnalisis(BaseModel):
    paciente_id: str
    via: str
    s: str
    o: str
    a: str
    p: str

class MensajeChat(BaseModel):
    mensaje: str

class PacienteNuevo(BaseModel):
    profesional_id: str
    nombre: str
    fecha_nacimiento: str
    genero: str
    estado_civil: str
    ocupacion: str
    telefono_whatsapp: str
    email: str | None = None
    domicilio: str | None = None
    contacto_emergencia_nombre: str
    contacto_emergencia_tel: str
    motivo_consulta: str
    terapia_previa: bool = False
    notas_alergias_meds: str | None = None

class PlantillaCreate(BaseModel):
    profesional_id: str
    nombre_plantilla: str
    campos: List[Dict[str, Any]]

class SoapTextoLibre(BaseModel):
    texto: str

# ==========================================
# 3. HELPER: LLAMAR A GROQ CON FALLBACK
# ==========================================

def llamar_groq(system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
    if not groq_client:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY no configurada en variables de entorno.")

    kwargs = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.4,
        "max_tokens": 4096, # 🚀 Límite de memoria ampliado para historial profundo
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_error = None
    for modelo in GROQ_MODELOS:
        try:
            kwargs["model"] = modelo
            response = groq_client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
        except Exception as e:
            err_str = str(e)
            print(f"[GROQ] ❌ Falló {modelo}: {err_str[:120]}")
            if "429" in err_str or "rate_limit" in err_str.lower():
                last_error = e
                continue
            raise HTTPException(status_code=500, detail=f"Error de IA: {err_str}")

    raise HTTPException(status_code=429, detail="Cuota de Groq agotada en todos los modelos. Intenta en unos minutos.")

# ==========================================
# 4. HELPER: VERIFICAR USUARIO Y PLAN
# ==========================================

def verificar_token_y_plan(token: str, plan_requerido: str = "pro") -> str:
    try:
        user_response = supabase.auth.get_user(token)
        user_id = user_response.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido o sesión expirada.")

    if plan_requerido == "pro":
        try:
            perfil = supabase.table("perfiles_profesionales") \
                .select("tipo_plan") \
                .eq("id", user_id) \
                .execute()

            if not perfil.data:
                raise HTTPException(status_code=404, detail="Perfil profesional no encontrado.")

            plan_db = str(perfil.data[0].get("tipo_plan", "")).strip().lower()

            if plan_db != "pro":
                raise HTTPException(
                    status_code=403,
                    detail=f"Esta función requiere Plan Pro. Tu plan actual: '{plan_db}'."
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al verificar plan: {str(e)}")

    return user_id

# ==========================================
# 5. ENDPOINTS — PACIENTES
# ==========================================

@app.post("/api/pacientes")
async def registrar_paciente(paciente: PacienteNuevo, request: Request):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    user_id = verificar_token_y_plan(token, plan_requerido="gratis")

    try:
        datos = paciente.model_dump()
        datos["profesional_id"] = user_id
        respuesta = supabase.table("pacientes").insert(datos).execute()
        return {"status": "success", "mensaje": "Expediente creado", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.get("/api/pacientes")
async def listar_pacientes(request: Request):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    user_id = verificar_token_y_plan(token, plan_requerido="gratis")

    try:
        respuesta = supabase.table("pacientes").select("*").eq("profesional_id", user_id).execute()
        return {"status": "success", "pacientes": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

# ==========================================
# 6. ENDPOINTS — PLANTILLAS
# ==========================================

@app.post("/api/plantillas")
async def crear_plantilla(plantilla: PlantillaCreate, request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    user_id = verificar_token_y_plan(token, plan_requerido="pro")

    try:
        respuesta = supabase.table("plantillas").insert({
            "profesional_id": user_id,
            "nombre_plantilla": plantilla.nombre_plantilla,
            "campos": plantilla.campos
        }).execute()
        return {"status": "success", "mensaje": "Plantilla guardada", "data": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.get("/api/plantillas")
async def obtener_plantillas(request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    user_id = verificar_token_y_plan(token, plan_requerido="gratis")

    try:
        respuesta = supabase.table("plantillas").select("*").eq("profesional_id", user_id).execute()
        return {"status": "success", "plantillas": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

# ==========================================
# 7. ENDPOINTS — CITAS Y SESIONES
# ==========================================

@app.post("/api/citas")
async def crear_cita(cita: CitaCreate, request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    user_id = verificar_token_y_plan(token, plan_requerido="gratis")

    try:
        fecha_hora_iso = f"{cita.fecha}T{cita.hora}:00-07:00"
        payload = {
            "paciente_id": cita.paciente,
            "profesional_id": user_id,
            "fecha_hora": fecha_hora_iso,
            "tipo_consulta": cita.tipo,
            "modalidad": cita.modalidad,
            "notas_previas": cita.notas,
            "sync_google": cita.sync,
            "estado": "programada"
        }
        respuesta = supabase.table("citas").insert(payload).execute()
        return {"status": "success", "mensaje": "Cita guardada", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/sesiones/analizar")
async def analizar_sesion(sesion: SesionAnalisis, request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    user_id = verificar_token_y_plan(token, plan_requerido="pro")

    prompt_sistema = """
    Eres un asistente clínico experto. Analiza esta sesión SOAP y devuelve un JSON:
    { "ia_resumen_ejecutivo": "...", "ia_nube_conceptos": [], "ia_alerta_riesgo": false }
    """
    texto = f"S: {sesion.s}\nO: {sesion.o}\nA: {sesion.a}\nP: {sesion.p}"

    try:
        # 1. Llamada a IA para el resumen
        raw = llamar_groq(prompt_sistema, texto, json_mode=True)
        analisis_json = json.loads(raw)

        # 2. Guardar en la tabla 'sesiones' de Supabase
        nueva_sesion = {
            "profesional_id": user_id,
            "paciente_id": sesion.paciente_id,
            "subjetivo": sesion.s,
            "objetivo": sesion.o,
            "analisis": sesion.a,
            "plan": sesion.p,
            "ia_resumen": analisis_json.get("ia_resumen_ejecutivo", "Resumen no disponible"),
            "fecha": datetime.now(timezone.utc).isoformat()
        }
        
        supabase.table("sesiones").insert(nueva_sesion).execute()

        return {"status": "success", "analisis": analisis_json}
    except Exception as e:
        print(f"Error en análisis/guardado: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 8. ENDPOINT — SOAP AUTOMÁTICO
# ==========================================

@app.post("/api/soap/estructurar")
async def estructurar_soap(payload: SoapTextoLibre, request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere Authorization: Bearer <token>")

    token = auth_header.split(" ", 1)[1]
    user_id = verificar_token_y_plan(token, plan_requerido="pro")

    texto = payload.texto.strip()
    if not texto:
        raise HTTPException(status_code=400, detail="El campo 'texto' no puede estar vacío")

    prompt_sistema = """
Eres un asistente clínico experto en psicología.
A partir del texto libre de notas de sesión que recibirás, extrae y organiza la información en formato SOAP.

Devuelve ÚNICAMENTE un JSON válido con esta estructura exacta, sin texto adicional, sin markdown, sin backticks:
{
  "s": "Subjetivo: lo que refiere el paciente en sus propias palabras",
  "o": "Objetivo: lo que el clínico observa conductual y emocionalmente",
  "a": "Análisis: evaluación clínica e interpretación diagnóstica",
  "p": "Plan: próximos pasos, tareas y objetivos terapéuticos"
}

Si alguna sección no tiene información suficiente, indica: "No se menciona en la nota."
"""

    try:
        raw = llamar_groq(prompt_sistema, texto, json_mode=True)
        soap = json.loads(raw)
        for key in ("s", "o", "a", "p"):
            if key not in soap or not soap[key]:
                soap[key] = "No se menciona en la nota."

        return {"status": "success", "soap": soap}

    except HTTPException:
        raise
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Error al parsear respuesta de IA: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al procesar con IA: {str(e)}")

# ==========================================
# 9. ENDPOINT — DICTADO DE VOZ LucIA
# ==========================================

@app.post("/api/lucia/dictar")
async def dictar_nota(request: Request, file: UploadFile = File(...)):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere Authorization: Bearer <token>")

    token = auth_header.split(" ", 1)[1]
    user_id = verificar_token_y_plan(token, plan_requerido="pro")

    if not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un audio válido.")

    if not groq_client:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY no configurada.")

    try:
        audio_data = await file.read()

        transcripcion_resp = groq_client.audio.transcriptions.create(
            file=("audio.webm", audio_data, file.content_type),
            model="whisper-large-v3",
            language="es",
            response_format="text"
        )

        transcripcion = transcripcion_resp if isinstance(transcripcion_resp, str) else transcripcion_resp.text
        return {"status": "success", "transcripcion": transcripcion}

    except Exception as e:
        print(f"[DICTADO] ❌ Error: {e}")
        raise HTTPException(status_code=500, detail="Error procesando el dictado de audio.")
    finally:
        await file.close()

# ==========================================
# 10. ENDPOINT — CHAT LucIA
# ==========================================

@app.post("/api/chat")
async def chat_lucia(chat: MensajeChat, request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No autorizado")

    token = auth_header.split(" ", 1)[1]
    user_id = verificar_token_y_plan(token, plan_requerido="pro")

    try:
        # 1. Jalamos pacientes reales
        pacientes_db = supabase.table("pacientes").select("id, nombre, motivo_consulta").eq("profesional_id", user_id).execute()
        
        # 2. Jalamos las últimas 50 sesiones para dar mucho contexto
        sesiones_db = supabase.table("sesiones").select("*").eq("profesional_id", user_id).order("fecha", desc=True).limit(50).execute()

        # Organizar sesiones por paciente
        sesiones_por_paciente = {}
        for s in (sesiones_db.data or []):
            pid = str(s.get("paciente_id")) # 🔥 Fix: Siempre a string
            if pid not in sesiones_por_paciente:
                sesiones_por_paciente[pid] = []
            sesiones_por_paciente[pid].append(s)

        # 3. Construir el contexto para la IA
        contexto = "SISTEMA CLÍNICO - EXPEDIENTES:\n"
        for p in (pacientes_db.data or []):
            pid = str(p.get("id"))
            nombre_p = p.get('nombre')
            contexto += f"\nPACIENTE: {nombre_p}\nMOTIVO: {p.get('motivo_consulta')}\n"
            
            notas = sesiones_por_paciente.get(pid, [])
            if notas:
                for idx, n in enumerate(notas):
                    contexto += f"- Sesión {idx+1} ({n.get('fecha')[:10]}): S:{n.get('subjetivo')} O:{n.get('objetivo')} A:{n.get('analisis')} P:{n.get('plan')}\n"
            else:
                contexto += "- Sin sesiones previas.\n"

    except Exception as e:
        contexto = f"Error al leer base de datos: {str(e)}"

    prompt_sistema = f"""
Eres LucIA, asistente analítica de Psicogestor. Tienes acceso a los expedientes clínicos del psicólogo.
    
TAREAS:
- Si preguntan por un paciente (ej. Jose Pablo), lee TODAS sus notas disponibles en el historial provisto.
- Realiza diagnósticos presuntivos o sugerencias clínicas basadas en la sección [S] y [O].
- Sugiere cambios en el plan terapéutico [P] si notas estancamiento.
- Sé muy clínica, profesional y detallada en tus apreciaciones.
- Si no hay datos, infórmalo directamente.

CONTEXTO REAL DE LA BASE DE DATOS:
{contexto}
"""

    try:
        respuesta = llamar_groq(prompt_sistema, chat.mensaje, json_mode=False)
        return {"status": "success", "respuesta": respuesta}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 11. ENDPOINT — CALENDARIO .ICS
# ==========================================

@app.get("/api/calendario/{profesional_id}/psicogestor.ics")
async def feed_calendario(profesional_id: str, request: Request):
    try:
        respuesta = supabase.table("citas") \
            .select("*, pacientes(nombre)") \
            .eq("profesional_id", profesional_id) \
            .eq("estado", "programada") \
            .execute()
        citas = respuesta.data or []

        base_url = str(request.base_url).rstrip("/")
        webcal_url = base_url.replace("https://", "webcal://").replace("http://", "webcal://")

        ics = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Dev Group Studio//Psicogestor 2.5//ES",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:Psicogestor - Agenda",
            "X-WR-TIMEZONE:UTC",
            f"X-WR-CALID:{webcal_url}/api/calendario/{profesional_id}/psicogestor.ics",
            "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
            "X-PUBLISHED-TTL:PT15M"
        ]

        for cita in citas:
            try:
                fecha_dt = datetime.fromisoformat(cita["fecha_hora"].replace("Z", "+00:00"))
                start = fecha_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                end   = (fecha_dt.astimezone(timezone.utc) + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

                pac = cita.get("pacientes")
                if isinstance(pac, list) and pac:
                    nombre_p = pac[0].get("nombre", "Paciente")
                elif isinstance(pac, dict):
                    nombre_p = pac.get("nombre", "Paciente")
                else:
                    nombre_p = "Paciente"

                modalidad = cita.get("modalidad") or "Presencial"
                notas     = str(cita.get("notas_previas") or "Sin notas").replace("\n", "\\n").replace("\r", "")

                ics += [
                    "BEGIN:VEVENT",
                    f"UID:cita_{cita['id']}@psicogestor.com",
                    f"DTSTAMP:{stamp}",
                    f"DTSTART:{start}",
                    f"DTEND:{end}",
                    f"SUMMARY:Sesión con {nombre_p}",
                    f"DESCRIPTION:Modalidad: {modalidad}\\nNotas: {notas}",
                    f"LOCATION:{modalidad}",
                    "END:VEVENT"
                ]
            except Exception as e_cita:
                print(f"[CAL] Error en cita {cita.get('id')}: {e_cita}")
                continue

        ics.append("END:VCALENDAR")

        return Response(
            content="\r\n".join(ics),
            media_type="text/calendar",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Content-Disposition": "attachment; filename=psicogestor.ics"
            }
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 12. ENDPOINTS — STRIPE / PAGOS
# ==========================================

@app.post("/api/crear-checkout")
async def crear_checkout(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Debes iniciar sesión para realizar un pago.")

    token = auth_header.split(" ", 1)[1]
    user_id = verificar_token_y_plan(token, plan_requerido="gratis")

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe no configurado en el servidor.")

    DOMAIN = "https://psicogestor.devgroupstudio.xyz"

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            mode="subscription",
            success_url=f"{DOMAIN}/dashboard.html?pago=exitoso",
            cancel_url=f"{DOMAIN}/checkout.html?pago=cancelado",
            client_reference_id=user_id
        )
        return JSONResponse(content={"url": session.url})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/webhook/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Falta webhook secret de Stripe.")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Payload inválido")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma inválida")

    if event["type"] == "checkout.session.completed":
        session      = event["data"]["object"]
        usuario_id   = session.get("client_reference_id")
        customer_id  = session.get("customer")
        customer_email = session.get("customer_details", {}).get("email")

        print(f"[STRIPE] ✅ Pago exitoso — usuario: {usuario_id} — email: {customer_email}")

        if usuario_id:
            try:
                supabase.table("perfiles_profesionales").update({
                    "tipo_plan": "pro",
                    "stripe_customer_id": customer_id
                }).eq("id", usuario_id).execute()
                print(f"[STRIPE] Usuario {usuario_id} actualizado a PRO en Supabase.")
            except Exception as e:
                print(f"[STRIPE] ❌ Error actualizando Supabase: {e}")

    elif event["type"] == "customer.subscription.deleted":
        session = event["data"]["object"]
        customer_id = session.get("customer")

        try:
            supabase.table("perfiles_profesionales") \
                .update({"tipo_plan": "gratis"}) \
                .eq("stripe_customer_id", customer_id) \
                .execute()
            print(f"[STRIPE] ❌ Suscripción cancelada. Cliente bajado a GRATIS: {customer_id}")
        except Exception as e:
            print(f"[STRIPE] Error al procesar cancelación: {e}")

    return {"status": "success"}

# ==========================================
# 13. HEALTH CHECK
# ==========================================

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "groq": bool(groq_client),
        "stripe": bool(STRIPE_SECRET_KEY),
        "supabase": bool(SUPABASE_URL and SUPABASE_KEY)
    }

# ==========================================
# 14. ARCHIVOS ESTÁTICOS AL FINAL (CORRECCIÓN RUTAS)
# ==========================================
app.mount("/", StaticFiles(directory="public", html=True), name="public")
