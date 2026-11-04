"""
Configuracion Django de SICGA. Todo lo sensible llega por variables de entorno
(o por backend/.env, ver .env.example).
"""
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _cargar_dotenv(ruta: Path) -> None:
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def _lista(nombre: str, defecto: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(nombre, defecto).split(",") if x.strip()]


def _db_desde_dsn(dsn: str) -> dict:
    u = urlparse(dsn)
    # Bases en la nube (p. ej. Neon) piden ?sslmode=require: los parámetros pasan tal cual a psycopg
    opciones = {k: v[-1] for k, v in parse_qs(u.query).items()}
    # Si PostgreSQL está apagado, avisar en segundos en lugar de quedarse esperando (en Windows, conectar a un puerto
    # cerrado de «localhost» se reintenta en ::1 y 127.0.0.1 durante minutos)
    opciones.setdefault("connect_timeout", "5")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": u.path.lstrip("/"),
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),
        "HOST": u.hostname or "localhost",
        "PORT": str(u.port or 5432),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": opciones,
    }


_cargar_dotenv(BASE_DIR / ".env")

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() in ("1", "true", "yes")
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or ("dev-insegura-solo-local" if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError("Defina DJANGO_SECRET_KEY (obligatoria con DJANGO_DEBUG=false)")


# --- Carpetas de datos (base, fotos, copias, registros) ------------------------------
# Nunca en la carpeta temporal de Windows: el Sensor de almacenamiento borra lo que no se usa.
def _carpeta_datos() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SGA-GALLEGO"
    return BASE_DIR.parent


DATOS_DIR = Path(os.environ.get("SGA_DATOS_DIR") or _carpeta_datos())


# --- Red local (otras computadoras del club) ------------------------------------------
# En Access cada computadora abría el mismo archivo .accdb y las fotos de una carpeta compartida de otra PC
# (\\DESKTOP-2GPI84A\...\FOTOS SICGA). Aquí una sola computadora es el servidor y las demás entran con el navegador
# (http://NOMBRE-DE-LA-PC:8000). SGA_RED_LOCAL=1 en backend/.env lo activa en la computadora servidor
# (manage.py servir escucha entonces en la red; ver deploy/windows/).
RED_LOCAL = os.environ.get("SGA_RED_LOCAL", "0").lower() in ("1", "true", "yes")
PUERTO = int(os.environ.get("SGA_PUERTO", "8000"))


def _direcciones_locales() -> list[str]:
    """Direcciones IPv4 privadas de esta computadora (192.168.x.x, 10.x.x.x, 172.16-31.x.x)."""
    import ipaddress
    import socket

    direcciones = set()
    try:
        for familia, *_, destino in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ipaddress.ip_address(destino[0])
            if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                direcciones.add(str(ip))
    except OSError:
        pass
    return sorted(direcciones)


def _hosts_red_local() -> list[str]:
    import socket

    nombre = socket.gethostname()
    return [nombre, nombre.lower(), f"{nombre.lower()}.local", *_direcciones_locales()]


ALLOWED_HOSTS = _lista("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
if RED_LOCAL:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, *(h for h in _hosts_red_local() if h not in ALLOWED_HOSTS)]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "corsheaders",
    "core",
]

MIDDLEWARE = [
    "core.seguridad.ArchivosDelDiseno",          # /assets/ comprimidos y en caché (primero: comprime al final)
    "django.middleware.security.SecurityMiddleware",
    "core.seguridad.CabecerasSeguridad",      # CSP, Permissions-Policy, no indexar, sin caché en /api/
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sicga.urls"
WSGI_APPLICATION = "sicga.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASES = {"default": _db_desde_dsn(os.environ.get("PG_DSN", "postgresql://postgres@localhost:5432/sicga"))}

# Contraseñas: Argon2id (recomendado por OWASP). Las guardadas con PBKDF2 siguen sirviendo y se convierten
# a Argon2 la próxima vez que el usuario entra.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "es"
TIME_ZONE = "America/Caracas"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Fotos de socios y beneficiarios. Se sirven solo con autenticacion. Van con los demás datos (no dentro del
# programa), así una actualización del programa no las toca.
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT") or DATOS_DIR / "media")
MEDIA_URL = "media/"
DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024

# Frontend React compilado (npm run build). Django lo sirve en "/" -> un solo enlace.
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", BASE_DIR.parent / "frontend" / "dist"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- API REST -----------------------------------------------------------------
# Login con el nombre de usuario (sin distinguir mayusculas) o con el email
AUTHENTICATION_BACKENDS = ["core.autenticacion.UsuarioOEmailBackend"]

REST_FRAMEWORK = {
    # Sesión del navegador en cookie HttpOnly; «Authorization: Token» para programas (core/seguridad.py).
    # La API navegable y el admin de Django (sesión de Django) solo existen en desarrollo.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Primero la cabecera Authorization (un sitio ajeno no puede enviarla); si no viene, la cookie
        "core.autenticacion.TokenConVencimiento",
        "core.autenticacion.CookieSesion",
        *(["rest_framework.authentication.SessionAuthentication"] if DEBUG else []),
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("SGA_LIMITE_ANONIMO", "120/min"),
        "user": os.environ.get("SGA_LIMITE_USUARIO", "1200/min"),
        "entrada": os.environ.get("SGA_LIMITE_ENTRADA", "20/min"),      # intentos de entrar, por IP
        "pesado": os.environ.get("SGA_LIMITE_PESADO", "40/hour"),       # copias, restaurar, cargar Access
    },
    "DEFAULT_PERMISSION_CLASSES": ["core.permissions.AccesoTotalOSoloLectura"],
    "DEFAULT_PARSER_CLASSES": [
        "core.parsers.JSONObjeto",                      # el cuerpo JSON debe ser un objeto {...}
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "EXCEPTION_HANDLER": "core.exceptions.manejador_excepciones",
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "core.pagination.Paginacion",
    "PAGE_SIZE": 50,
    # Los importes viajan como string ("1200.0000") para no perder precision en JS;
    # el renderizador hace lo mismo con los Decimal de las respuestas armadas a mano
    "COERCE_DECIMAL_TO_STRING": True,
    "DEFAULT_RENDERER_CLASSES": [
        "core.renderers.RenderizadorJSON",
        *(["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    ],
    "DATE_FORMAT": "%Y-%m-%d",
}

# --- CORS (frontend React en otro puerto/dominio) -------------------------------
CORS_ALLOWED_ORIGINS = _lista("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000")

# --- Sesión y cabeceras de seguridad (core/seguridad.py) ----------------------------
SGA_SESION_HORAS = int(os.environ.get("SGA_SESION_HORAS", "12"))
SGA_INTENTOS_MAX = int(os.environ.get("SGA_INTENTOS_MAX", "5"))          # fallos seguidos antes de bloquear
SGA_BLOQUEO_MINUTOS = int(os.environ.get("SGA_BLOQUEO_MINUTOS", "15"))
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Strict"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# --- Servidor en internet (deploy/instalar.sh) -----------------------------------
# Detrás de Caddy (HTTPS): el proxy avisa por X-Forwarded-Proto que la conexión original era segura.
CSRF_TRUSTED_ORIGINS = _lista("DJANGO_CSRF_TRUSTED_ORIGINS")
DETRAS_DE_PROXY = os.environ.get("DJANGO_DETRAS_DE_PROXY", "false").lower() in ("1", "true", "yes")
if DETRAS_DE_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # En internet todo va por HTTPS: lo que llegue por HTTP se redirige y el navegador recuerda usar HTTPS
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^login$"]       # comprobación de salud del proveedor (Render), por HTTP interno
    SECURE_HSTS_SECONDS = int(os.environ.get("SGA_HSTS_SEGUNDOS", "31536000"))
    REST_FRAMEWORK["NUM_PROXIES"] = 1           # IP real del cliente para los límites de intentos


# --- Copias, registros y caché (dentro de DATOS_DIR) ----------------------------------
RESPALDOS_DIR = Path(os.environ.get("SGA_RESPALDOS_DIR") or DATOS_DIR / "respaldos")
REGISTROS_DIR = Path(os.environ.get("SGA_REGISTROS_DIR") or DATOS_DIR / "registros")

# Caché de los intentos de entrada y los límites de uso.
# - Linux (servidor en internet, gunicorn con varios procesos): en archivos, compartida entre los procesos.
# - Windows: en memoria. Allí el sistema corre en un solo proceso, y la caché en archivos de Django falla con dos
#   solicitudes a la vez (Windows no deja reemplazar un archivo que otro está leyendo: WinError 32).
# - Desarrollo y pruebas (DEBUG): en memoria, para que una prueba no bloquee a la siguiente.
if os.name != "nt" and (os.environ.get("SGA_CACHE_DIR") or not DEBUG):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                          "LOCATION": str(Path(os.environ.get("SGA_CACHE_DIR") or DATOS_DIR / "cache")),
                          "OPTIONS": {"MAX_ENTRIES": 5000}}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "OPTIONS": {"MAX_ENTRIES": 5000}}}
# Tasa del dólar del BCV consultada sola en bcv.org.ve (core/tasa_bcv.py); 0 la desactiva (pruebas, sin internet)
TASA_BCV_AUTOMATICA = os.environ.get("SGA_TASA_BCV", "1").lower() in ("1", "true", "yes")
# Copias diarias/semanales/anuales hechas por el propio servidor (0 en servidores de prueba)
RESPALDOS_AUTOMATICOS = os.environ.get("SGA_RESPALDOS_AUTOMATICOS", "1").lower() in ("1", "true", "yes")
RESPALDOS_MAX_SUBIDA_MB = int(os.environ.get("SGA_RESPALDOS_MAX_MB", "2048"))
# Base a la que se conecta para crear/renombrar la base del sistema al restaurar
RESPALDOS_BD_MANTENIMIENTO = os.environ.get("SGA_BD_MANTENIMIENTO", "postgres")
# Carpeta de pg_dump/pg_restore si no se encuentran solos (en Windows se buscan en Program Files)
PG_BIN_DIR = os.environ.get("SGA_PG_BIN", "")

# Registro de seguridad (entradas, fallos, bloqueos, restauraciones, cargas desde Access, cambios de usuarios):
# REGISTROS_DIR/seguridad.log, 5 archivos de 1 MB. Sin claves ni datos personales de los socios.
_REGISTRO_EN_ARCHIVO = bool(os.environ.get("SGA_REGISTROS_DIR")) or not DEBUG
if _REGISTRO_EN_ARCHIVO:
    REGISTROS_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"fecha": {"format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s"}},
    "handlers": {
        "consola": {"class": "logging.StreamHandler"},
        **({"archivo": {"class": "logging.handlers.RotatingFileHandler", "filename": str(REGISTROS_DIR / "seguridad.log"),
                        "maxBytes": 1_000_000, "backupCount": 5, "encoding": "utf-8", "formatter": "fecha"},
            "errores": {"class": "logging.handlers.RotatingFileHandler", "filename": str(REGISTROS_DIR / "errores.log"),
                        "maxBytes": 1_000_000, "backupCount": 5, "encoding": "utf-8", "formatter": "fecha"}}
           if _REGISTRO_EN_ARCHIVO else {}),
    },
    "loggers": {
        "sga": {"handlers": ["consola", *(["archivo"] if _REGISTRO_EN_ARCHIVO else [])], "level": "INFO"},
        # Errores 500 con su detalle: en el servidor (consola y errores.log), nunca en la respuesta al navegador
        "django.request": {"handlers": ["consola", *(["errores"] if _REGISTRO_EN_ARCHIVO else [])], "level": "ERROR",
                           "propagate": False},
    },
}
