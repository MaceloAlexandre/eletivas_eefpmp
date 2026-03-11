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

# ── Segurança ──────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "chave-local-dev-trocar-em-prod")
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480

USERS = {
    "admin":      "$2b$12$Hp5VLW9t/XT0fIbQmfHqB.vd0WolnXdTaT3WH7thBYmHsWFXKhRVO",
    "secretaria": "$2b$12$JoaMQm525wbkANJasKzT7OPFAbA.I8U.W71W3uYKNc6ZeCc3GXM2y",
}

pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Modelos ────────────────────────────────────────────────────────────────────
class Token(BaseModel):
    access_token: str
    token_type: str

class AlunoSerieUpdate(BaseModel):
    serie: Optional[str] = None

class RegistroUpdate(BaseModel):
    serie:    Optional[str] = None
    semestre: Optional[int] = None

SERIES_VALIDAS    = ["6º ano", "7º ano", "8º ano", "9º ano"]
SEMESTRES_VALIDOS = [1, 2]

# ── Auth ───────────────────────────────────────────────────────────────────────
def verify_password(plain, hashed): return pwd_context.verify(plain, hashed)

def create_token(data):
    d = data.copy()
    d["exp"] = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(d, SECRET_KEY, algorithm=ALGORITHM)

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
    return {"access_token": create_token({"sub": form.username}), "token_type": "bearer"}

# ── DB ─────────────────────────────────────────────────────────────────────────
DB = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME",     "eletivas"),
    "user":     os.getenv("DB_USER",     "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

def get_conn():
    return psycopg2.connect(**DB)

# ── Opções ─────────────────────────────────────────────────────────────────────
@app.get("/series")
def listar_series():
    return SERIES_VALIDAS

@app.get("/semestres")
def listar_semestres():
    return SEMESTRES_VALIDOS

# ── Alunos ─────────────────────────────────────────────────────────────────────
@app.get("/alunos")
def listar_alunos(busca: Optional[str] = None, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if busca:
        cur.execute("SELECT * FROM alunos WHERE nome ILIKE %s ORDER BY nome", (f"%{busca}%",))
    else:
        cur.execute("SELECT * FROM alunos ORDER BY nome")
    r = cur.fetchall(); conn.close(); return r

@app.get("/alunos/{aluno_id}/eletivas")
def eletivas_do_aluno(aluno_id: int, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.nome AS aluno, e.nome AS eletiva, e.area, e.professor,
               r.ano_letivo, r.semestre, r.serie, r.media_final
        FROM registros r
        JOIN alunos   a ON a.id = r.aluno_id
        JOIN eletivas e ON e.id = r.eletiva_id
        WHERE r.aluno_id = %s
        ORDER BY r.ano_letivo, r.semestre, e.area, e.nome
    """, (aluno_id,))
    r = cur.fetchall(); conn.close(); return r

@app.patch("/alunos/{aluno_id}/serie")
def atualizar_serie_aluno(aluno_id: int, body: AlunoSerieUpdate, user: str = Depends(get_current_user)):
    """Atualiza a série em TODOS os registros do aluno."""
    if body.serie and body.serie not in SERIES_VALIDAS:
        raise HTTPException(400, f"Série inválida. Opções: {SERIES_VALIDAS}")
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("UPDATE registros SET serie = %s WHERE aluno_id = %s", (body.serie, aluno_id))
    cur.execute("SELECT nome FROM alunos WHERE id = %s", (aluno_id,))
    aluno = cur.fetchone()
    conn.commit(); conn.close()
    return {"ok": True, "aluno": aluno["nome"] if aluno else None, "serie": body.serie}

@app.patch("/registros/atualizar")
def atualizar_registro(
    aluno_id:   int,
    ano_letivo: int,
    semestre_atual: Optional[int] = None,
    body: RegistroUpdate = None,
    user: str = Depends(get_current_user),
):
    """Atualiza serie e/ou semestre dos registros de um aluno em um ano."""
    if body is None: body = RegistroUpdate()
    if body.serie    and body.serie    not in SERIES_VALIDAS:    raise HTTPException(400, "Série inválida")
    if body.semestre and body.semestre not in SEMESTRES_VALIDOS: raise HTTPException(400, "Semestre inválido")

    sets, params = [], []
    if body.serie    is not None: sets.append("serie = %s");    params.append(body.serie)
    if body.semestre is not None: sets.append("semestre = %s"); params.append(body.semestre)
    if not sets: return {"ok": True, "updated": 0}

    where = "aluno_id = %s AND ano_letivo = %s"
    params += [aluno_id, ano_letivo]
    if semestre_atual is not None:
        where += " AND semestre = %s"; params.append(semestre_atual)

    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"UPDATE registros SET {', '.join(sets)} WHERE {where}", params)
    updated = cur.rowcount
    conn.commit(); conn.close()
    return {"ok": True, "updated": updated}

# ── Eletivas ───────────────────────────────────────────────────────────────────
@app.get("/eletivas")
def listar_eletivas(
    busca:    Optional[str] = None,
    area:     Optional[str] = None,
    ano:      Optional[int] = None,
    semestre: Optional[int] = None,
    user:     str = Depends(get_current_user),
):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT e.id, e.nome, e.area, e.professor,
               r.ano_letivo, r.semestre, COUNT(r.id) AS total_alunos
        FROM eletivas e
        JOIN registros r ON r.eletiva_id = e.id
        WHERE (%s IS NULL OR e.nome      ILIKE %s)
          AND (%s IS NULL OR e.area      ILIKE %s)
          AND (%s IS NULL OR r.ano_letivo = %s)
          AND (%s IS NULL OR r.semestre   = %s)
        GROUP BY e.id, e.nome, e.area, e.professor, r.ano_letivo, r.semestre
        ORDER BY e.area, e.nome
    """, (busca, f"%{busca}%" if busca else None,
          area,  f"%{area}%"  if area  else None,
          ano,   ano, semestre, semestre))
    r = cur.fetchall(); conn.close(); return r

@app.get("/eletivas/{eletiva_id}/alunos")
def alunos_da_eletiva(
    eletiva_id: int,
    ano:      Optional[int] = None,
    semestre: Optional[int] = None,
    user:     str = Depends(get_current_user),
):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.id AS aluno_id, a.nome AS aluno, e.nome AS eletiva, e.area, e.professor,
               r.ano_letivo, r.semestre, r.serie, r.media_final
        FROM registros r
        JOIN alunos   a ON a.id = r.aluno_id
        JOIN eletivas e ON e.id = r.eletiva_id
        WHERE r.eletiva_id = %s
          AND (%s IS NULL OR r.ano_letivo = %s)
          AND (%s IS NULL OR r.semestre   = %s)
        ORDER BY r.serie NULLS LAST, a.nome
    """, (eletiva_id, ano, ano, semestre, semestre))
    r = cur.fetchall(); conn.close(); return r

# ── Busca geral ────────────────────────────────────────────────────────────────
@app.get("/buscar")
def busca_geral(
    q:        Optional[str] = None,
    area:     Optional[str] = None,
    ano:      Optional[int] = None,
    semestre: Optional[int] = None,
    serie:    Optional[str] = None,
    user:     str = Depends(get_current_user),
):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.id AS aluno_id, a.nome AS aluno,
               e.id AS eletiva_id, e.nome AS eletiva,
               e.area, e.professor,
               r.ano_letivo, r.semestre, r.serie, r.media_final
        FROM registros r
        JOIN alunos   a ON a.id = r.aluno_id
        JOIN eletivas e ON e.id = r.eletiva_id
        WHERE (%s IS NULL OR a.nome ILIKE %s OR e.nome ILIKE %s)
          AND (%s IS NULL OR e.area      ILIKE %s)
          AND (%s IS NULL OR r.ano_letivo = %s)
          AND (%s IS NULL OR r.semestre   = %s)
          AND (%s IS NULL OR r.serie      ILIKE %s)
        ORDER BY a.nome, e.area, e.nome
    """, (q, f"%{q}%" if q else None, f"%{q}%" if q else None,
          area,    f"%{area}%"  if area  else None,
          ano,     ano,
          semestre, semestre,
          serie,   f"%{serie}%" if serie else None))
    r = cur.fetchall(); conn.close(); return r

# ── Filtros ────────────────────────────────────────────────────────────────────
@app.get("/areas")
def listar_areas(user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT DISTINCT area FROM eletivas ORDER BY area")
    r = cur.fetchall(); conn.close(); return [x["area"] for x in r]

@app.get("/anos")
def listar_anos(user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT DISTINCT ano_letivo FROM registros ORDER BY ano_letivo")
    r = cur.fetchall(); conn.close(); return [x["ano_letivo"] for x in r]

# ── Turmas ─────────────────────────────────────────────────────────────────────

class TurmaCreate(BaseModel):
    nome:       str
    serie:      str
    ano_letivo: int

class TurmaUpdate(BaseModel):
    nome:       Optional[str] = None
    serie:      Optional[str] = None
    ano_letivo: Optional[int] = None

@app.get("/turmas")
def listar_turmas(ano: Optional[int] = None, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT t.id, t.nome, t.serie, t.ano_letivo,
               COUNT(at2.aluno_id) AS total_alunos
        FROM turmas t
        LEFT JOIN aluno_turma at2 ON at2.turma_id = t.id
        WHERE (%s IS NULL OR t.ano_letivo = %s)
        GROUP BY t.id, t.nome, t.serie, t.ano_letivo
        ORDER BY t.ano_letivo DESC, t.serie, t.nome
    """, (ano, ano))
    r = cur.fetchall(); conn.close(); return r

@app.post("/turmas")
def criar_turma(body: TurmaCreate, user: str = Depends(get_current_user)):
    if body.serie not in SERIES_VALIDAS:
        raise HTTPException(400, f"Série inválida. Opções: {SERIES_VALIDAS}")
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "INSERT INTO turmas (nome, serie, ano_letivo) VALUES (%s, %s, %s) RETURNING *",
            (body.nome.strip(), body.serie, body.ano_letivo)
        )
        r = cur.fetchone(); conn.commit(); conn.close(); return r
    except Exception:
        conn.rollback(); conn.close()
        raise HTTPException(400, "Turma já existe com esse nome/série/ano.")

@app.patch("/turmas/{turma_id}")
def editar_turma(turma_id: int, body: TurmaUpdate, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sets, params = [], []
    if body.nome is not None:
        sets.append("nome = %s"); params.append(body.nome.strip())
    if body.serie is not None:
        if body.serie not in SERIES_VALIDAS: raise HTTPException(400, "Série inválida")
        sets.append("serie = %s"); params.append(body.serie)
    if body.ano_letivo is not None:
        sets.append("ano_letivo = %s"); params.append(body.ano_letivo)
    if not sets: conn.close(); return {"ok": True}
    params.append(turma_id)
    cur.execute(f"UPDATE turmas SET {', '.join(sets)} WHERE id = %s RETURNING *", params)
    r = cur.fetchone(); conn.commit(); conn.close()
    if not r: raise HTTPException(404, "Turma não encontrada")
    return r

@app.delete("/turmas/{turma_id}")
def excluir_turma(turma_id: int, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM turmas WHERE id = %s", (turma_id,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/turmas/{turma_id}/alunos")
def alunos_da_turma(turma_id: int, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT a.id, a.nome
        FROM aluno_turma at2
        JOIN alunos a ON a.id = at2.aluno_id
        WHERE at2.turma_id = %s
        ORDER BY a.nome
    """, (turma_id,))
    r = cur.fetchall(); conn.close(); return r

@app.get("/turmas/{turma_id}/eletivas")
def eletivas_da_turma(turma_id: int, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT ano_letivo, nome, serie FROM turmas WHERE id = %s", (turma_id,))
    turma = cur.fetchone()
    if not turma: conn.close(); raise HTTPException(404, "Turma não encontrada")
    cur.execute("""
        SELECT a.id AS aluno_id, a.nome AS aluno,
               e.nome AS eletiva, e.area, e.professor,
               r.semestre, r.media_final
        FROM aluno_turma at2
        JOIN alunos a ON a.id = at2.aluno_id
        LEFT JOIN registros r ON r.aluno_id = a.id AND r.ano_letivo = %s
        LEFT JOIN eletivas e ON e.id = r.eletiva_id
        WHERE at2.turma_id = %s
        ORDER BY a.nome, e.area, e.nome
    """, (turma["ano_letivo"], turma_id))
    r = cur.fetchall(); conn.close()
    return {"turma": dict(turma), "registros": r}

@app.post("/turmas/{turma_id}/alunos/{aluno_id}")
def adicionar_aluno_turma(turma_id: int, aluno_id: int, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT serie, ano_letivo FROM turmas WHERE id = %s", (turma_id,))
    turma = cur.fetchone()
    if not turma: conn.close(); raise HTTPException(404, "Turma não encontrada")
    try:
        cur.execute("INSERT INTO aluno_turma (aluno_id, turma_id) VALUES (%s, %s)", (aluno_id, turma_id))
        cur.execute("UPDATE registros SET serie = %s WHERE aluno_id = %s AND ano_letivo = %s",
            (turma["serie"], aluno_id, turma["ano_letivo"]))
        updated = cur.rowcount
        conn.commit(); conn.close()
        return {"ok": True, "serie": turma["serie"], "registros_atualizados": updated}
    except Exception as e:
        conn.rollback(); conn.close()
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(400, "Aluno já está nesta turma.")
        raise HTTPException(500, str(e))

@app.delete("/turmas/{turma_id}/alunos/{aluno_id}")
def remover_aluno_turma(turma_id: int, aluno_id: int, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT ano_letivo FROM turmas WHERE id = %s", (turma_id,))
    turma = cur.fetchone()
    if not turma: conn.close(); raise HTTPException(404, "Turma não encontrada")
    cur.execute("DELETE FROM aluno_turma WHERE aluno_id = %s AND turma_id = %s", (aluno_id, turma_id))
    cur.execute("UPDATE registros SET serie = NULL WHERE aluno_id = %s AND ano_letivo = %s",
        (aluno_id, turma["ano_letivo"]))
    conn.commit(); conn.close()
    return {"ok": True}

# ── Novo Registro ──────────────────────────────────────────────────────────────

class NovoRegistro(BaseModel):
    # Aluno
    aluno_id:    Optional[int] = None
    aluno_nome:  Optional[str] = None   # se novo
    aluno_serie: Optional[str] = None   # se novo (para vincular à turma)

    # Eletiva
    eletiva_id:       Optional[int] = None
    eletiva_nome:     Optional[str] = None  # se nova
    eletiva_professor:Optional[str] = None  # se nova
    eletiva_area:     Optional[str] = None  # se nova

    # Registro
    ano_letivo: int
    semestre:   int
    media_final: Optional[float] = None

import unicodedata, re

def normalizar_nome(nome: str) -> str:
    n = unicodedata.normalize("NFKD", nome.upper().strip())
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.replace("Ç", "C")
    n = re.sub(r"\s+", " ", n)
    return n

AREAS_VALIDAS = ["Matematica", "Linguagens", "Ciencias da Natureza", "Ciencias Humanas"]

@app.post("/registros")
def criar_registro(body: NovoRegistro, user: str = Depends(get_current_user)):
    if body.semestre not in SEMESTRES_VALIDOS:
        raise HTTPException(400, "Semestre inválido.")

    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # ── 1. Resolve aluno ───────────────────────────────────────────────────
        if body.aluno_id:
            cur.execute("SELECT id, nome FROM alunos WHERE id = %s", (body.aluno_id,))
            aluno = cur.fetchone()
            if not aluno: raise HTTPException(404, "Aluno não encontrado.")
        elif body.aluno_nome:
            nome_norm = normalizar_nome(body.aluno_nome)
            cur.execute("SELECT id, nome FROM alunos WHERE nome = %s", (nome_norm,))
            aluno = cur.fetchone()
            if not aluno:
                cur.execute("INSERT INTO alunos (nome) VALUES (%s) RETURNING id, nome", (nome_norm,))
                aluno = cur.fetchone()
        else:
            raise HTTPException(400, "Informe aluno_id ou aluno_nome.")

        aluno_id = aluno["id"]

        # ── 2. Série: busca turma do aluno no ano ou usa a fornecida ───────────
        serie = None
        cur.execute("""
            SELECT t.serie FROM aluno_turma at2
            JOIN turmas t ON t.id = at2.turma_id
            WHERE at2.aluno_id = %s AND t.ano_letivo = %s
            LIMIT 1
        """, (aluno_id, body.ano_letivo))
        turma_row = cur.fetchone()

        if turma_row:
            serie = turma_row["serie"]
        elif body.aluno_serie:
            # Aluno novo sem turma: vincula à turma da série/ano se existir
            serie = body.aluno_serie
            cur.execute("""
                SELECT id FROM turmas
                WHERE serie = %s AND ano_letivo = %s
                LIMIT 1
            """, (serie, body.ano_letivo))
            turma_para_vincular = cur.fetchone()
            if turma_para_vincular:
                try:
                    cur.execute("INSERT INTO aluno_turma (aluno_id, turma_id) VALUES (%s, %s)",
                        (aluno_id, turma_para_vincular["id"]))
                except Exception:
                    pass  # já está na turma

        # ── 3. Resolve eletiva ─────────────────────────────────────────────────
        if body.eletiva_id:
            cur.execute("SELECT id FROM eletivas WHERE id = %s", (body.eletiva_id,))
            el = cur.fetchone()
            if not el: raise HTTPException(404, "Eletiva não encontrada.")
            eletiva_id = body.eletiva_id
        elif body.eletiva_nome:
            if not body.eletiva_professor or not body.eletiva_area:
                raise HTTPException(400, "Para nova eletiva informe também professor e área.")
            if body.eletiva_area not in AREAS_VALIDAS:
                raise HTTPException(400, f"Área inválida. Opções: {AREAS_VALIDAS}")
            cur.execute("SELECT id FROM eletivas WHERE nome = %s AND area = %s",
                (body.eletiva_nome.strip(), body.eletiva_area))
            el = cur.fetchone()
            if el:
                eletiva_id = el["id"]
            else:
                cur.execute(
                    "INSERT INTO eletivas (nome, area, professor) VALUES (%s, %s, %s) RETURNING id",
                    (body.eletiva_nome.strip(), body.eletiva_area, body.eletiva_professor.strip())
                )
                eletiva_id = cur.fetchone()["id"]
        else:
            raise HTTPException(400, "Informe eletiva_id ou eletiva_nome.")

        # ── 4. Cria registro ───────────────────────────────────────────────────
        cur.execute("""
            INSERT INTO registros (aluno_id, eletiva_id, ano_letivo, semestre, media_final, serie)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (aluno_id, eletiva_id, ano_letivo, semestre)
            DO UPDATE SET media_final = EXCLUDED.media_final, serie = EXCLUDED.serie
            RETURNING *
        """, (aluno_id, eletiva_id, body.ano_letivo, body.semestre, body.media_final, serie))
        registro = cur.fetchone()

        # ── 5. Garante série nos outros registros do aluno neste ano ───────────
        if serie:
            cur.execute("UPDATE registros SET serie = %s WHERE aluno_id = %s AND ano_letivo = %s",
                (serie, aluno_id, body.ano_letivo))

        conn.commit()
        conn.close()
        return {
            "ok": True,
            "aluno": aluno["nome"],
            "eletiva_id": eletiva_id,
            "serie": serie,
            "registro": dict(registro)
        }

    except HTTPException:
        conn.rollback(); conn.close(); raise
    except Exception as e:
        conn.rollback(); conn.close()
        raise HTTPException(500, str(e))

@app.get("/alunos/buscar-nome")
def buscar_aluno_por_nome(
    nome: str,
    ano_letivo: Optional[int] = None,
    user: str = Depends(get_current_user)
):
    """Busca aluno por nome e retorna sua turma no ano informado (se houver)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, nome FROM alunos WHERE nome ILIKE %s ORDER BY nome LIMIT 10", (f"%{nome}%",))
    alunos = cur.fetchall()
    result = []
    for a in alunos:
        turma = None
        if ano_letivo:
            cur.execute("""
                SELECT t.id, t.nome, t.serie, t.ano_letivo
                FROM aluno_turma at2
                JOIN turmas t ON t.id = at2.turma_id
                WHERE at2.aluno_id = %s AND t.ano_letivo = %s
                LIMIT 1
            """, (a["id"], ano_letivo))
            turma = cur.fetchone()
        result.append({"id": a["id"], "nome": a["nome"], "turma": dict(turma) if turma else None})
    conn.close()
    return result

@app.get("/eletivas/buscar-nome")
def buscar_eletiva_por_nome(nome: str, user: str = Depends(get_current_user)):
    """Busca eletiva por nome para autocomplete."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, nome, area, professor FROM eletivas WHERE nome ILIKE %s ORDER BY nome LIMIT 10", (f"%{nome}%",))
    r = cur.fetchall(); conn.close(); return r

class EletivaUpdate(BaseModel):
    nome:      Optional[str] = None
    area:      Optional[str] = None
    professor: Optional[str] = None

@app.get("/eletivas/todas")
def listar_todas_eletivas(user: str = Depends(get_current_user)):
    """Lista todas as eletivas únicas (sem duplicar por ano/semestre)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, nome, area, professor FROM eletivas ORDER BY area, nome")
    r = cur.fetchall(); conn.close(); return r

@app.patch("/eletivas/{eletiva_id}")
def editar_eletiva(eletiva_id: int, body: EletivaUpdate, user: str = Depends(get_current_user)):
    conn = get_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sets, params = [], []
    if body.nome      is not None:
        sets.append("nome = %s");      params.append(body.nome.strip())
    if body.area      is not None:
        if body.area not in AREAS_VALIDAS: raise HTTPException(400, f"Área inválida. Opções: {AREAS_VALIDAS}")
        sets.append("area = %s");      params.append(body.area)
    if body.professor is not None:
        sets.append("professor = %s"); params.append(body.professor.strip())
    if not sets: conn.close(); return {"ok": True}
    params.append(eletiva_id)
    cur.execute(f"UPDATE eletivas SET {', '.join(sets)} WHERE id = %s RETURNING *", params)
    r = cur.fetchone(); conn.commit(); conn.close()
    if not r: raise HTTPException(404, "Eletiva não encontrada")
    return r

@app.get("/areas/lista")
def listar_areas_fixas():
    return AREAS_VALIDAS