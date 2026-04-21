import os
import json
from typing import Any, List, Dict
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from supabase import create_client, Client
from google import genai
from google.genai import types
from dotenv import load_dotenv
import stripe

# ==========================================
# 1. CONFIGURACIÓN INICIAL
# ==========================================
load_dotenv()

SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY")
STRIPE_SECRET_KEY   = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID     = os.getenv("STRIPE_PRICE_ID", "price_1TNi4nCSdv7aogFNHp8XAqjc")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("⚠️  STRIPE_SECRET_KEY no encontrado en .env")

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    print("⚠️  SUPABASE_URL inválido en .env")
    SUPABASE_URL = "https://proyecto-dummy.supabase.co"
    SUPABASE_KEY = "llave-dummy"

app = FastAPI(
    title="Psicogestor API",
    description="Backend Psicogestor 2.0 — Dev Group Studio",
    version="2.0.1"
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
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ==========================================
# 2. FRONTEND ESTÁTICO
# ==========================================
# Asegúrate de que exista la carpeta public/ con tus HTML
app.mount("/app", StaticFiles(directory="public", html=True), name="public")

@app.get("/")
async def root():
    return RedirectResponse(url="/app/login.html")

# ==========================================
# 3. MODELOS PYDANTIC
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
    texto: str  # IMPORTANTE: debe llamarse "texto" para coincidir con el frontend

# ==========================================
# 4. HELPER: VERIFICAR USUARIO Y PLAN
# ==========================================

def verificar_token_y_plan(token: str, plan_requerido: str = "pro") -> str:
    """
    Verifica el token JWT de Supabase.
    Devuelve el user_id si es válido.
    Lanza HTTPException si el token es inválido o el plan no es suficiente.
    """
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
            print(f"[DEBUG] Usuario {user_id} → plan detectado: '{plan_db}'")

            if plan_db != "pro":
                raise HTTPException(
                    status_code=403,
                    detail=f"Esta función requiere Plan Pro. Tu plan actual: '{plan_db}'. Actualiza en Configuración."
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
    # Verificamos que sea un usuario real (aunque sea plan gratis)
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    user_id = verificar_token_y_plan(token, plan_requerido="gratis")
    
    try:
        # Forzamos que el profesional_id sea el del token, no el que manden en el JSON
        datos = paciente.model_dump()
        datos["profesional_id"] = user_id
        
        respuesta = supabase.table("pacientes").insert(datos).execute()
        return {"status": "success", "mensaje": "Expediente creado", "datos": respuesta.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.get("/api/pacientes")
async def listar_pacientes(request: Request):
    # Ya no ocupamos doctor_id en la URL, lo sacamos del Token
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
async def analizar_sesion(sesion: SesionAnalisis) -> dict[str, Any]:
    if not ai_client:
        raise HTTPException(status_code=500, detail="Gemini API no configurada")

    prompt_sistema = """
Eres un asistente clínico para psicólogos. Analiza esta sesión SOAP.
Devuelve ÚNICAMENTE un JSON con esta estructura exacta, sin markdown ni texto extra:
{
    "ia_resumen_ejecutivo": "Un párrafo de máximo 3 líneas.",
    "ia_nube_conceptos": ["concepto1", "concepto2"],
    "ia_puntaje_animo": 8,
    "ia_puntaje_ansiedad": 4,
    "ia_alerta_riesgo": false
}
"""
    texto = f"S: {sesion.s}\nO: {sesion.o}\nA: {sesion.a}\nP: {sesion.p}"

    modelos = ["gemini-3.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
    for modelo in modelos:
        try:
            response = ai_client.models.generate_content(
                model=modelo,
                contents=texto,
                config=types.GenerateContentConfig(
                    system_instruction=prompt_sistema,
                    response_mime_type="application/json",
                )
            )
            analisis_json = json.loads(response.text)
            return {"status": "success", "analisis": analisis_json}
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "503" in err_str or "UNAVAILABLE" in err_str:
                continue
            raise HTTPException(status_code=500, detail=f"Error IA: {err_str}")
    raise HTTPException(status_code=429, detail="Cuota de Gemini agotada.")

# ==========================================
# 8. ENDPOINT — SOAP AUTOMÁTICO ✅ PRINCIPAL
# ==========================================

@app.post("/api/soap/estructurar")
async def estructurar_soap(payload: SoapTextoLibre, request: Request) -> dict[str, Any]:
    """
    Recibe texto libre de notas de sesión y devuelve estructura SOAP.
    Requiere Plan Pro.
    Body JSON esperado: { "texto": "..." }
    Header esperado: Authorization: Bearer <supabase_access_token>
    """
    if not ai_client:
        raise HTTPException(status_code=500, detail="Gemini API no configurada. Revisa GEMINI_API_KEY en .env")

    # Extraer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere Authorization: Bearer <token>")

    token = auth_header.split(" ", 1)[1]

    # Verificar usuario y plan Pro
    user_id = verificar_token_y_plan(token, plan_requerido="pro")

    # Validar texto
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
        print(f"[SOAP] Procesando nota para usuario {user_id}. Longitud: {len(texto)} chars")

        # Modelos en orden de preferencia (fallback automático)
        modelos_candidatos = [
        "gemini-3.0-flash",
        "gemini-2.5-flash",
        "gemini-1.5-flash",
        ]

        response = None
        ultimo_error = None

        for modelo in modelos_candidatos:
            try:
                print(f"[SOAP] Intentando con modelo: {modelo}")
                response = ai_client.models.generate_content(
                    model=modelo,
                    contents=texto,
                    config=types.GenerateContentConfig(
                        system_instruction=prompt_sistema,
                        response_mime_type="application/json",
                    )
                )
                print(f"[SOAP] ✅ Éxito con modelo: {modelo}")
                break  # Si funciona, salir del loop
            except Exception as e:
                ultimo_error = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "503" in err_str or "UNAVAILABLE" in err_str:
                    print(f"[SOAP] ⚠️ Cuota agotada en {modelo}, probando siguiente...")
                    continue  # Intentar con el siguiente modelo
                else:
                    # Error distinto a cuota — no tiene caso reintentar
                    raise e

        if response is None:
            raise HTTPException(
                status_code=429,
                detail="Cuota de Gemini API agotada en todos los modelos disponibles. Activa facturación en https://ai.google.dev o espera unos minutos e intenta de nuevo."
            )

        raw = response.text.strip()

        # Limpiar por si Gemini agrega markdown
        if raw.startswith("```"):
            partes = raw.split("```")
            raw = partes[1] if len(partes) > 1 else raw
            raw = raw.replace("json", "", 1).strip()

        soap = json.loads(raw)

        # Garantizar que todas las claves existen
        for key in ("s", "o", "a", "p"):
            if key not in soap or not soap[key]:
                soap[key] = "No se menciona en la nota."

        print(f"[SOAP] ✅ Estructurado correctamente para usuario {user_id}")
        return {"status": "success", "soap": soap}

    except HTTPException:
        raise  # Re-lanzar HTTPExceptions sin modificar
    except json.JSONDecodeError as e:
        print(f"[SOAP] ❌ Error JSON: {response.text if response else 'sin respuesta'}")
        raise HTTPException(status_code=500, detail=f"Error al parsear respuesta de IA: {str(e)}")
    except Exception as e:
        err_str = str(e)
        print(f"[SOAP] ❌ Error general: {err_str}")
        # Mensaje amigable para error de cuota
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "503" in err_str or "UNAVAILABLE" in err_str:
            raise HTTPException(
                status_code=429,
                detail="Cuota de Gemini API agotada. Activa facturación en https://aistudio.google.com o espera unos minutos."
            )
        raise HTTPException(status_code=500, detail=f"Error al procesar con IA: {err_str}")

# ==========================================
# 9. ENDPOINT — CHAT LucIA
# ==========================================

@app.post("/api/chat")
async def chat_lucia(chat: MensajeChat, request: Request) -> dict[str, Any]:
    if not ai_client:
        raise HTTPException(status_code=500, detail="Gemini API no configurada")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere Authorization: Bearer <token>")

    token = auth_header.split(" ", 1)[1]
    user_id = verificar_token_y_plan(token, plan_requerido="pro")

    # Cargar contexto clínico del profesional
    try:
        pacientes_db = supabase.table("pacientes") \
            .select("id, nombre, motivo_consulta") \
            .eq("profesional_id", user_id) \
            .execute()

        sesiones_db = supabase.table("sesiones") \
            .select("paciente_id, fecha, subjetivo, analisis, plan") \
            .eq("profesional_id", user_id) \
            .order("fecha", desc=True) \
            .limit(30) \
            .execute()

        sesiones_por_paciente: dict = {}
        for s in (sesiones_db.data or []):
            pid = s.get("paciente_id")
            if pid not in sesiones_por_paciente:
                sesiones_por_paciente[pid] = []
            sesiones_por_paciente[pid].append(s)

        contexto = "=== EXPEDIENTES CLÍNICOS ===\n\n"
        for p in (pacientes_db.data or []):
            pid = p.get("id")
            contexto += f"PACIENTE: {p.get('nombre')}\n"
            contexto += f"Motivo: {p.get('motivo_consulta')}\n"
            notas = sesiones_por_paciente.get(pid, [])
            for n in notas[:3]:
                fecha = n.get("fecha", "")[:10]
                contexto += f"  Sesión {fecha}: {n.get('subjetivo','—')} | Plan: {n.get('plan','—')}\n"
            contexto += "\n"
    except Exception as e:
        contexto = f"Error al cargar expedientes: {str(e)}"

    prompt_sistema = f"""
Eres LucIA, asistente de IA clínica de Psicogestor.
Hablas con un psicólogo profesional. Sé directo, clínico y conciso.
No te presentes en cada mensaje. Responde con precisión.

CONTEXTO DE EXPEDIENTES:
{contexto}
"""

    modelos = ["gemini-3.0-flash", "gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
    for modelo in modelos:
        try:
            response = ai_client.models.generate_content(
                model=modelo,
                contents=chat.mensaje,
                config=types.GenerateContentConfig(system_instruction=prompt_sistema)
            )
            return {"status": "success", "respuesta": response.text}
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "503" in err_str or "UNAVAILABLE" in err_str:
                print(f"[CHAT] Cuota agotada en {modelo}, probando siguiente...")
                continue
            raise HTTPException(status_code=500, detail=err_str)
    raise HTTPException(
        status_code=429,
        detail="Cuota de Gemini API agotada. Activa facturacion en https://aistudio.google.com"
    )

# ==========================================
# 10. ENDPOINT — CALENDARIO .ICS
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
            "PRODID:-//Dev Group Studio//Psicogestor 2.0//ES",
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
# 11. ENDPOINTS — STRIPE / PAGOS
# ==========================================

@app.post("/api/crear-checkout")
async def crear_checkout(request: Request):
    # 1. EXTRAER TOKEN Y VERIFICAR SESIÓN
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Debes iniciar sesión para realizar un pago.")
    
    token = auth_header.split(" ", 1)[1]
    # Verificamos que el usuario existe (plan_requerido="gratis" porque aún no es Pro)
    user_id = verificar_token_y_plan(token, plan_requerido="gratis")

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe no configurado en el servidor.")

    try:
        base_url = str(request.base_url).rstrip("/")
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            mode="subscription",
            success_url=f"{base_url}/app/dashboard.html?pago=exitoso",
            cancel_url=f"{base_url}/app/checkout.html?pago=cancelado",
            client_reference_id=user_id  # Usamos el ID verificado del token
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
        print("[STRIPE] Suscripción cancelada.")

    return {"status": "success"}

# ==========================================
# 12. HEALTH CHECK
# ==========================================

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "gemini": bool(ai_client),
        "stripe": bool(STRIPE_SECRET_KEY),
        "supabase": bool(SUPABASE_URL and SUPABASE_KEY)
    }    allow_origins=[
        "https://psicogestor.devgroupstudio.xyz",
        "http://localhost:8000",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inicializar Clientes
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ==========================================
# 2. MONTAR ARCHIVOS FRONTEND Y REDIRECCIÓN
# ==========================================

app.mount("/app", StaticFiles(directory="public", html=True), name="public")

@app.get("/")
async def root():
    return RedirectResponse(url="/app/login.html")

# ==========================================
# 3. MODELOS DE DATOS (PYDANTIC)
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
    texto_libre: str

# ==========================================
# 4. RUTAS DE LA API (ENDPOINTS JSON)
# ==========================================

# --- PACIENTES ---
@app.post("/api/pacientes")
async def registrar_paciente(paciente: PacienteNuevo) -> dict[str, Any]:
    try:
        respuesta = supabase.table('pacientes').insert(paciente.model_dump()).execute()
        return {
            "status": "success",
            "mensaje": "Expediente creado en la base de datos",
            "datos": respuesta.data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al guardar paciente: {str(e)}")

@app.get("/api/pacientes/{doctor_id}")
async def listar_pacientes(doctor_id: str) -> dict[str, Any]:
    try:
        respuesta = supabase.table('pacientes').select('*').eq('profesional_id', doctor_id).execute()
        return {
            "status": "success",
            "pacientes": respuesta.data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener lista: {str(e)}")

# --- PLANTILLAS ---
@app.post("/api/plantillas")
async def crear_plantilla(plantilla: PlantillaCreate) -> dict[str, Any]:
    try:
        respuesta = supabase.table("plantillas").insert({
            "profesional_id": plantilla.profesional_id,
            "nombre_plantilla": plantilla.nombre_plantilla,
            "campos": plantilla.campos
        }).execute()
        return {
            "status": "success",
            "mensaje": "Plantilla guardada correctamente",
            "data": respuesta.data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al guardar plantilla: {str(e)}")

@app.get("/api/plantillas/{profesional_id}")
async def obtener_plantillas(profesional_id: str) -> dict[str, Any]:
    try:
        respuesta = supabase.table("plantillas").select("*").eq("profesional_id", profesional_id).execute()
        return {
            "status": "success",
            "plantillas": respuesta.data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener plantillas: {str(e)}")

# --- CITAS Y SESIONES ---
@app.post("/api/citas")
async def crear_cita(cita: CitaCreate) -> dict[str, Any]:
    try:
        fecha_hora_iso = f"{cita.fecha}T{cita.hora}:00-07:00"
        payload = {
            "paciente_id": cita.paciente,
            "profesional_id": cita.profesional_id,
            "fecha_hora": fecha_hora_iso,
            "tipo_consulta": cita.tipo,
            "modalidad": cita.modalidad,
            "notas_previas": cita.notas,
            "sync_google": cita.sync,
            "estado": "programada"
        }
        respuesta = supabase.table('citas').insert(payload).execute()
        return {
            "status": "success",
            "mensaje": "Cita guardada correctamente",
            "datos": respuesta.data
        }
    except Exception as e:
        print(f"Error al crear cita: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/sesiones/analizar")
async def analizar_sesion(sesion: SesionAnalisis) -> dict[str, Any]:
    if not ai_client:
        raise HTTPException(status_code=500, detail="Falta la llave de Gemini API")

    prompt_sistema = """
    Eres un asistente clínico para psicólogos. Analiza esta sesión SOAP.
    Devuelve ÚNICAMENTE un JSON con esta estructura exacta:
    {
        "ia_resumen_ejecutivo": "Un párrafo resumido de máximo 3 líneas.",
        "ia_nube_conceptos": ["concepto1", "concepto2"],
        "ia_puntaje_animo": 8,
        "ia_puntaje_ansiedad": 4,
        "ia_alerta_riesgo": false
    }
    """
    texto_analizar = f"S: {sesion.s}\nO: {sesion.o}\nA: {sesion.a}\nP: {sesion.p}"

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=texto_analizar,
            config=types.GenerateContentConfig(
                system_instruction=prompt_sistema,
                response_mime_type="application/json",
            )
        )
        analisis_json = json.loads(response.text)
        return {
            "status": "success",
            "mensaje": "Análisis completado",
            "analisis": analisis_json
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


# --- ORÁCULO IA (CON CONTEXTO CLÍNICO COMPLETO) ---
@app.post("/api/chat")
async def chat_oraculo(chat: MensajeChat, request: Request) -> dict[str, Any]:
    if not ai_client:
        raise HTTPException(status_code=500, detail="Falta la llave de Gemini API en el .env")

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Acceso denegado: Falta el token de seguridad")

    token = auth_header.split(" ")[1]
    supabase_usuario: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    supabase_usuario.postgrest.auth(token)

    try:
        pacientes_db = supabase_usuario.table('pacientes').select('id, nombre, motivo_consulta, terapia_previa').execute()
        sesiones_db = supabase_usuario.table('sesiones').select('paciente_id, fecha, via, subjetivo, objetivo, analisis, plan').order('fecha', desc=True).execute()

        sesiones_agrupadas = {}
        if sesiones_db.data:
            for sesion in sesiones_db.data:
                pid = sesion.get('paciente_id')
                if pid not in sesiones_agrupadas:
                    sesiones_agrupadas[pid] = []
                sesiones_agrupadas[pid].append(sesion)

        contexto_pacientes = "Expedientes y notas SOAP actuales:\n\n"
        if pacientes_db.data:
            for p in pacientes_db.data:
                pid = p.get('id')
                contexto_pacientes += f"=== PACIENTE: {p.get('nombre')} ===\n"
                contexto_pacientes += f"Motivo: {p.get('motivo_consulta')}\n"
                notas_paciente = sesiones_agrupadas.get(pid, [])
                if notas_paciente:
                    for nota in notas_paciente:
                        contexto_pacientes += f"- Sesión {nota.get('fecha')}: S:{nota.get('subjetivo')} | P:{nota.get('plan')}\n"
                contexto_pacientes += "\n"
        else:
            contexto_pacientes += "No hay pacientes registrados.\n"

    except Exception as e:
        contexto_pacientes = "Error al leer base de datos clínica."

    prompt_sistema = f"""
    Eres LucIA, la asistente de inteligencia artificial de Psicogestor (Dev Group Studio). 
    Estás hablando con un psicólogo profesional. 
    
    REGLAS DE COMPORTAMIENTO:
    1. Ve directo al grano. NO te presentes ni saludes diciendo "Soy LucIA" en tus respuestas, a menos que sea estrictamente necesario.
    2. Mantén un tono clínico, profesional, empático y preciso.
    3. Responde de forma concisa; no des explicaciones largas a menos que se te pida un análisis detallado.
    
    Usa este contexto para responder dudas sobre los pacientes:
    {contexto_pacientes}
    """

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=chat.mensaje,
            config=types.GenerateContentConfig(system_instruction=prompt_sistema)
        )
        return {"status": "success", "respuesta": response.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- SOAP AUTOMÁTICO ---
@app.post("/api/soap/estructurar")
async def estructurar_soap(payload: SoapTextoLibre, request: Request) -> dict[str, Any]:
    if not ai_client:
        raise HTTPException(status_code=500, detail="Falta la llave de Gemini API en el .env")

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Acceso denegado: Falta el token de seguridad")

    texto = payload.texto_libre.strip()
    if not texto:
        raise HTTPException(status_code=400, detail="El texto no puede estar vacío")

    prompt_sistema = """
Eres un asistente clínico experto en psicología.
A partir del texto libre de notas de sesión que recibirás, extrae y organiza la información en formato SOAP.

Devuelve ÚNICAMENTE un JSON válido con esta estructura exacta, sin texto adicional, sin markdown, sin explicaciones:
{
  "s": "Subjetivo: lo que refiere el paciente (síntomas, quejas, sentimientos expresados por él/ella)",
  "o": "Objetivo: lo que el clínico observa (comportamiento, actitud, lenguaje no verbal, puntualidad, apariencia)",
  "a": "Análisis: evaluación clínica, hipótesis diagnóstica, interpretación del estado actual",
  "p": "Plan: próximos pasos, tareas para el paciente, técnicas a aplicar, frecuencia de sesiones"
}

Si alguna sección no tiene información suficiente en el texto, escribe una oración breve indicando que no se mencionó en la nota.
"""

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=texto,
            config=types.GenerateContentConfig(
                system_instruction=prompt_sistema,
                response_mime_type="application/json",
            )
        )
        raw = response.text.strip().replace("```json", "").replace("```", "").strip()
        soap = json.loads(raw)
        for key in ("s", "o", "a", "p"):
            if key not in soap:
                soap[key] = "Sin información suficiente en la nota."
        return {"status": "success", "soap": soap}

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Gemini no devolvió un JSON válido. Intenta de nuevo.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al procesar con IA: {str(e)}")


# --- RUTA DE SUSCRIPCIÓN DE CALENDARIO ---
@app.get("/api/calendario/{profesional_id}/psicogestor.ics")
async def feed_calendario(profesional_id: str, request: Request):
    try:
        print(f"--- SOLICITANDO CALENDARIO PARA: {profesional_id} ---")

        respuesta = supabase.table('citas').select('*, pacientes(nombre)').eq('profesional_id', profesional_id).eq('estado', 'programada').execute()
        citas = respuesta.data

        print(f"CITAS ENCONTRADAS: {len(citas)}")
        if len(citas) == 0:
            print("OJO: Supabase regresó 0 citas. Revisa si hay citas 'programadas' o si la llave bloquea la lectura (RLS).")

        base_url = str(request.base_url).rstrip("/")
        webcal_url = base_url.replace("https://", "webcal://").replace("http://", "webcal://")

        ics = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Dev Group Studio//Psicogestor 2.0//ES",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:Psicogestor - Agenda",
            "X-WR-TIMEZONE:UTC",
            f"X-WR-CALID:{webcal_url}/api/calendario/{profesional_id}/psicogestor.ics",
            "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
            "X-PUBLISHED-TTL:PT15M"
        ]

        for cita in citas:
            print(f"Procesando cita ID: {cita.get('id')}")
            fecha_dt = datetime.fromisoformat(cita['fecha_hora'].replace('Z', '+00:00'))
            start = fecha_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            end = (fecha_dt.astimezone(timezone.utc) + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

            paciente_data = cita.get('pacientes')
            if isinstance(paciente_data, list) and len(paciente_data) > 0:
                nombre_p = paciente_data[0].get('nombre', 'Paciente')
            elif isinstance(paciente_data, dict):
                nombre_p = paciente_data.get('nombre', 'Paciente')
            else:
                nombre_p = "Paciente"

            modalidad = cita.get('modalidad') or 'Presencial'
            notas_crudas = cita.get('notas_previas') or 'Sin notas'
            notas = str(notas_crudas).replace('\n', '\\n').replace('\r', '')

            ics.extend([
                "BEGIN:VEVENT",
                f"UID:cita_{cita['id']}@psicogestor.com",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{start}",
                f"DTEND:{end}",
                f"SUMMARY:Sesión con {nombre_p}",
                f"DESCRIPTION:Modalidad: {modalidad}\\nNotas: {notas}",
                f"LOCATION:{modalidad}",
                "END:VEVENT"
            ])

        ics.append("END:VCALENDAR")
        calendar_str = "\r\n".join(ics)
        print("--- CALENDARIO GENERADO CON ÉXITO ---")

        return Response(
            content=calendar_str,
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
        error_real = traceback.format_exc()
        print(f"!!! ERROR FATAL AL GENERAR CALENDARIO !!!\n{error_real}")
        raise HTTPException(status_code=500, detail=f"Error en Python: {str(e)}")
