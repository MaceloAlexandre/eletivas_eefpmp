from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from typing import Optional
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
import os


# ── Segurança ─────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480  # 8 horas

USERS = {
    "admin":      "$2b$12$Hp5VLW9t/XT0fIbQmfHqB.vd0WolnXdTaT3WH7thBYmHsWFXKhRVO",
    "secretaria": "$2b$12$JoaMQm525wbkANJasKzT7OPFAbA.I8U.W71W3uYKNc6ZeCc3GXM2y",
}

pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth ──────────────────────────────────────────────────────────────────────
class Token(BaseModel):
    access_token: str
    token_type: str

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_token(data: dict) -> str:
    to_encode = data.copy()
    to_encode["exp"] = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    try:
        payload  = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None or username not in USERS:
            raise HTTPException(status_code=401, detail="Token inválido")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")

@app.post("/token", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends()):
    hashed = USERS.get(form.username)
    if not hashed or not verify_password(form.password, hashed):
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
    token = create_token({"sub": form.username})
    return {"access_token": token, "token_type": "bearer"}

# ── DB ────────────────────────────────────────────────────────────────────────
DB = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "dbname":   os.getenv("DB_NAME", "eletivas"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

def get_conn():
    return psycopg2.connect(**DB)

# ── Rotas ─────────────────────────────────────────────────────────────────────

@app.get("/alunos")
def listar_alunos(busca: Optional[str] = None, user: str = Depends(get_current_user)):
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if busca:
        cur.execute("SELECT * FROM alunos WHERE nome ILIKE %s ORDER BY nome", (f"%{busca}%",))
    else:
        cur.execute("SELECT * FROM alunos ORDER BY nome")
    result = cur.fetchall()
    conn.close()
    return result


@app.get("/alunos/{aluno_id}/eletivas")
def eletivas_do_aluno(aluno_id: int, user: str = Depends(get_current_user)):
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            a.nome AS aluno,
            e.nome AS eletiva,
            e.area,
            e.professor,
            r.ano_letivo,
            r.media_final
        FROM registros r
        JOIN alunos  a ON a.id = r.aluno_id
        JOIN eletivas e ON e.id = r.eletiva_id
        WHERE r.aluno_id = %s
        ORDER BY r.ano_letivo, e.area, e.nome
    """, (aluno_id,))
    result = cur.fetchall()
    conn.close()
    return result


@app.get("/eletivas")
def listar_eletivas(
    busca: Optional[str] = None,
    area:  Optional[str] = None,
    ano:   Optional[int] = None,
    user:  str = Depends(get_current_user),
):
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT DISTINCT
            e.id,
            e.nome,
            e.area,
            e.professor,
            r.ano_letivo,
            COUNT(r.id) AS total_alunos
        FROM eletivas e
        JOIN registros r ON r.eletiva_id = e.id
        WHERE
            (%s IS NULL OR e.nome  ILIKE %s)
            AND (%s IS NULL OR e.area  ILIKE %s)
            AND (%s IS NULL OR r.ano_letivo = %s)
        GROUP BY e.id, e.nome, e.area, e.professor, r.ano_letivo
        ORDER BY e.area, e.nome
    """, (
        busca, f"%{busca}%" if busca else None,
        area,  f"%{area}%"  if area  else None,
        ano,   ano,
    ))
    result = cur.fetchall()
    conn.close()
    return result


@app.get("/eletivas/{eletiva_id}/alunos")
def alunos_da_eletiva(
    eletiva_id: int,
    ano:  Optional[int] = None,
    user: str = Depends(get_current_user),
):
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            a.nome AS aluno,
            e.nome AS eletiva,
            e.area,
            e.professor,
            r.ano_letivo,
            r.media_final
        FROM registros r
        JOIN alunos   a ON a.id = r.aluno_id
        JOIN eletivas e ON e.id = r.eletiva_id
        WHERE r.eletiva_id = %s
          AND (%s IS NULL OR r.ano_letivo = %s)
        ORDER BY a.nome
    """, (eletiva_id, ano, ano))
    result = cur.fetchall()
    conn.close()
    return result


@app.get("/buscar")
def busca_geral(
    q:    Optional[str] = None,
    area: Optional[str] = None,
    ano:  Optional[int] = None,
    user: str = Depends(get_current_user),
):
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            a.nome AS aluno,
            e.nome AS eletiva,
            e.area,
            e.professor,
            r.ano_letivo,
            r.media_final
        FROM registros r
        JOIN alunos   a ON a.id = r.aluno_id
        JOIN eletivas e ON e.id = r.eletiva_id
        WHERE
            (%s IS NULL OR a.nome ILIKE %s OR e.nome ILIKE %s)
            AND (%s IS NULL OR e.area ILIKE %s)
            AND (%s IS NULL OR r.ano_letivo = %s)
        ORDER BY a.nome, e.area, e.nome
    """, (
        q, f"%{q}%" if q else None, f"%{q}%" if q else None,
        area, f"%{area}%" if area else None,
        ano, ano,
    ))
    result = cur.fetchall()
    conn.close()
    return result


@app.get("/areas")
def listar_areas(user: str = Depends(get_current_user)):
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT DISTINCT area FROM eletivas ORDER BY area")
    result = cur.fetchall()
    conn.close()
    return [r["area"] for r in result]


@app.get("/anos")
def listar_anos(user: str = Depends(get_current_user)):
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT DISTINCT ano_letivo FROM registros ORDER BY ano_letivo")
    result = cur.fetchall()
    conn.close()
    return [r["ano_letivo"] for r in result]