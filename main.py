import os
import json
from typing import Any
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from supabase import create_client, Client
from google import genai
from google.genai import types
from dotenv import load_dotenv

# ==========================================
# 1. CONFIGURACIÓN INICIAL Y BLINDAJE
# ==========================================
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not SUPABASE_URL or not SUPABASE_URL.startswith("http"):
    print("⚠️ ALERTA: No se pudo leer SUPABASE_URL válido del archivo .env")
    SUPABASE_URL = "https://proyecto-dummy.supabase.co"
    SUPABASE_KEY = "llave-dummy"

app = FastAPI(
    title="Psicogestor API",
    description="Backend para Dev Group Studio",
    version="2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
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
    fecha: str
    hora: str
    tipo: str
    notas: str
    sync: bool

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
        respuesta = supabase.table('pacientes').select('*').eq('doctor_id', doctor_id).execute()
        return {
            "status": "success",
            "pacientes": respuesta.data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener lista: {str(e)}")


# --- CITAS Y SESIONES ---
@app.post("/api/citas")
async def crear_cita(cita: CitaCreate) -> dict[str, Any]:
    try:
        return {
            "status": "success", 
            "mensaje": "Cita recibida",
            "datos": cita.model_dump() 
        }
    except Exception as e:
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
    Te llamas LucIA, asistente de Psicogestor (Dev Group Studio). 
    Hablas con un psicólogo profesional. Tono clínico y preciso.
    Usa este contexto para responder dudas sobre pacientes:
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


# --- NUEVO: RUTA DE SUSCRIPCIÓN DE CALENDARIO (WEBCAL) ---
@app.get("/api/calendario/{profesional_id}/psicogestor.ics")
async def feed_calendario(profesional_id: str):
    try:
        # 1. Consultar citas programadas de este doctor
        # Usamos el cliente 'supabase' global para que el iPhone pueda leerlo sin login
        respuesta = supabase.table('citas').select('*, pacientes(nombre)').eq('profesional_id', profesional_id).eq('estado', 'programada').execute()
        citas = respuesta.data

        # 2. Construir el archivo iCalendar (.ics)
        ics = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Dev Group Studio//Psicogestor 2.0//ES",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:Psicogestor - Mis Citas",
            "X-WR-TIMEZONE:UTC",
            "REFRESH-INTERVAL;VALUE=DURATION:PT15M" # Sugerir actualización cada 15 min
        ]

        for cita in citas:
            # Formatear fechas para el estándar ICS (YYYYMMDDTHHMMSSZ)
            fecha_dt = datetime.fromisoformat(cita['fecha_hora'].replace('Z', '+00:00'))
            start = fecha_dt.strftime("%Y%m%dT%H%M%SZ")
            end = (fecha_dt + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
            stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            
            paciente = cita['pacientes']['nombre'] if cita.get('pacientes') else "Paciente"
            notas = cita.get('notas_previas', 'Sin notas adicionales')
            modalidad = cita.get('modalidad', 'Presencial')

            ics.extend([
                "BEGIN:VEVENT",
                f"UID:cita_{cita['id']}@psicogestor.com",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{start}",
                f"DTEND:{end}",
                f"SUMMARY:Sesión con {paciente}",
                f"DESCRIPTION:Modalidad: {modalidad}\\nNotas: {notas}",
                f"LOCATION:{modalidad}",
                "END:VEVENT"
            ])

        ics.append("END:VCALENDAR")
        calendar_str = "\r\n".join(ics)

        # 3. Retornar con el MIME type correcto para que el celular lo reconozca
        return Response(content=calendar_str, media_type="text/calendar")

    except Exception as e:
        print(f"Error en feed de calendario: {e}")
        raise HTTPException(status_code=500, detail="No se pudo generar el calendario.")