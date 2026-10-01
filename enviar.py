"""Función serverless de Vercel (Python): recibe los formularios del sitio de la MDCE
y los envía por correo al buzón oficial.

POST /api/enviar   {"formulario": "buzon" | "unete" | "boletin", "datos": {...}}

Variables de entorno (Vercel → Settings → Environment Variables):
  SMTP_HOST        servidor SMTP (por defecto smtp.gmail.com)
  SMTP_PORT        465 (SSL) o 587 (STARTTLS). Por defecto 465
  SMTP_USER        cuenta que envía, p. ej. cabildantesdebogota@gmail.com
  SMTP_PASS        contraseña de aplicación de Google (no la contraseña normal)
  MAIL_TO          destino (por defecto cabildantesdebogota@gmail.com)
  ALLOWED_ORIGINS  dominios autorizados separados por coma, p. ej.
                   https://cabildantes.co,https://www.cabildantes.co,https://mdce.vercel.app
"""
import json
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr
from http.server import BaseHTTPRequestHandler

DESTINO = os.environ.get("MAIL_TO", "cabildantesdebogota@gmail.com")
ORIGENES = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
FORMULARIOS = {"buzon": "Buzón de iniciativas", "unete": "Red de la Mesa", "boletin": "Boletín"}
MAX_BYTES = 24_000
CORREO_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LIMITE = {}  # límite simple por IP mientras la instancia está activa


def origen_permitido(origen: str) -> bool:
    return "*" in ORIGENES or origen.rstrip("/") in ORIGENES


def demasiadas(ip: str, maximo: int = 8, ventana: int = 600) -> bool:
    ahora = time.time()
    lista = [t for t in _LIMITE.get(ip, []) if ahora - t < ventana]
    lista.append(ahora)
    _LIMITE[ip] = lista
    return len(lista) > maximo


def enviar_correo(asunto: str, cuerpo: str, responder_a: str) -> None:
    usuario = os.environ["SMTP_USER"]
    clave = os.environ["SMTP_PASS"]
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    puerto = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = formataddr(("Sitio web MDCE", usuario))
    msg["To"] = DESTINO
    if responder_a:
        msg["Reply-To"] = responder_a
    msg.set_content(cuerpo)
    contexto = ssl.create_default_context()
    if puerto == 465:
        with smtplib.SMTP_SSL(host, puerto, context=contexto, timeout=15) as s:
            s.login(usuario, clave)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, puerto, timeout=15) as s:
            s.starttls(context=contexto)
            s.login(usuario, clave)
            s.send_message(msg)


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        origen = self.headers.get("Origin", "")
        if origen and origen_permitido(origen):
            self.send_header("Access-Control-Allow-Origin", origen)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, codigo: int, obj: dict):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        configurado = bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))
        self._json(200, {"ok": True, "servicio": "MDCE · formularios", "correo_configurado": configurado})

    def do_POST(self):
        origen = self.headers.get("Origin", "")
        if origen and not origen_permitido(origen):
            return self._json(403, {"ok": False, "error": "origen_no_permitido"})
        ip = (self.headers.get("x-forwarded-for") or "").split(",")[0].strip() or "?"
        if demasiadas(ip):
            return self._json(429, {"ok": False, "error": "demasiadas_solicitudes"})
        largo = int(self.headers.get("Content-Length") or 0)
        if largo <= 0 or largo > MAX_BYTES:
            return self._json(413, {"ok": False, "error": "tamano_invalido"})
        try:
            data = json.loads(self.rfile.read(largo).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "json_invalido"})
        formulario = data.get("formulario")
        datos = data.get("datos")
        if formulario not in FORMULARIOS or not isinstance(datos, dict):
            return self._json(400, {"ok": False, "error": "formulario_invalido"})
        if datos.get("_honey"):
            return self._json(200, {"ok": True})  # bot: se descarta en silencio
        correo = str(datos.get("Correo") or "").strip()
        if not CORREO_RE.match(correo):
            return self._json(422, {"ok": False, "error": "correo_invalido"})
        if formulario != "boletin" and datos.get("Autorización") != "Sí":
            return self._json(422, {"ok": False, "error": "sin_autorizacion"})
        if not (os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS")):
            return self._json(503, {"ok": False, "error": "correo_no_configurado"})
        asunto = re.sub(r"[\r\n]+", " ", str(datos.get("_subject") or f"MDCE · {FORMULARIOS[formulario]}"))[:180]
        lineas = [f"{k}: {str(v)[:4000]}" for k, v in datos.items() if not str(k).startswith("_")]
        cuerpo = (
            f"Nuevo mensaje desde el sitio web · {FORMULARIOS[formulario]}\n"
            + "=" * 60 + "\n\n" + "\n".join(lineas)
            + "\n\n" + "-" * 60
            + "\nResponde a este correo para contestarle directamente a quien escribió."
            + "\nTratamiento de datos: Ley 1581 de 2012 · Política en /#/legal/privacidad\n"
        )
        try:
            enviar_correo(asunto, cuerpo, correo)
        except Exception:  # noqa: BLE001 — no exponemos detalles del servidor
            return self._json(502, {"ok": False, "error": "correo_no_enviado"})
        return self._json(200, {"ok": True})
