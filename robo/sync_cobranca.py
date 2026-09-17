#!/usr/bin/env python
"""Robô de cobrança — roda na nuvem (GitHub Actions) a cada 5 minutos.

Lê a aba de cobrança do mês na planilha do Google (JULHO 26, AGOSTO 26...,
com fallback JUL 26 / COBRANÇA_ATUAL) e atualiza a tabela juridico.cobranca
no banco do site. Assim o financeiro segue preenchendo a planilha e o site
fica no máximo alguns minutos atrás.

Credenciais SOMENTE por variáveis de ambiente (segredos do GitHub):
  GOOGLE_SA_JSON  conteúdo do JSON da conta de serviço (leitora da planilha)
  SHEET_ID        id da planilha
  PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE/PGSSLMODE  banco
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import date

import psycopg
import requests
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly",
          "https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/calendar.events"]
# agendas do Google sincronizadas como reuniões do sistema (pedido da Bruna,
# 20/08/2026) — requer: Calendar API ligada no projeto e a agenda compartilhada
# com a conta-robô (leitor-planilha@migracao-juridico.iam.gserviceaccount.com)
# Cada agenda: cal, quem VÊ (ver), categoria/cor, se é rodízio (rot).
# categoria: pessoal(azul) · sc(plataforma SC, rodízio) · sdr(Nicholas) · manual
# rodízio reancorado em 17/09/2026 (pedido da Bruna): qui 17 = Eduarda,
# sex 18 = Bruna, seg 21 = Danielly, e cicla nos dias úteis a partir daí.
# e-mails que identificam evento criado PELAS HEADS na plataformabde
# (→ dourado "3 heads"); qualquer outro criador = SC rodízio (verde).
HEADS_EMAILS = {
    "advdanielly.vbb@gmail.com", "brunaweckeradv@gmail.com",
    "brunawecker@gmail.com", "eduardaadv3.8@gmail.com",
}
# agendadores do SC → evento verde (rodízio). Qualquer criador que NÃO seja
# head também cai em verde por padrão; esta lista é o núcleo conhecido do SC.
SC_EMAILS = {
    "dflain@gmail.com", "gusalum21@gmail.com",
    "ianmedeiros1012@gmail.com", "vitor10salum@gmail.com",
}
HEADS_ROT = ["Eduarda", "Bruna", "Danielly"]
ROT_ANCORA = date(2026, 9, 17)  # índice 0 = Eduarda (a partir de hoje)
AGENDAS = [
    # SDRs → reuniões do Nicholas, visíveis às heads (contar quantas ele tem)
    {"cal": "nicodemeneghe@gmail.com", "ver": ["Nicholas"], "cat": "sdr"},
    {"cal": "plataformabd3.8@gmail.com",
     "ver": ["Nicholas", "Bruna", "Danielly", "Eduarda"], "cat": "sdr", "dono": "Nicholas"},
    {"cal": "souzademarqueseduarda@gmail.com",
     "ver": ["Nicholas", "Bruna", "Danielly", "Eduarda"], "cat": "sdr", "dono": "Nicholas"},
    # plataforma SC → reuniões das 3 heads, com RODÍZIO diário de responsável
    {"cal": "plataformabde@gmail.com",
     "ver": ["Bruna", "Danielly", "Eduarda"], "cat": "sc", "rot": True},
    # agendas pessoais RESTRITAS (clientes da casa / setores internos)
    {"cal": "brunaweckeradv@gmail.com", "ver": ["Bruna"], "cat": "pessoal"},
    {"cal": "eduardaadv3.8@gmail.com", "ver": ["Eduarda"], "cat": "pessoal"},
    {"cal": "advdanielly.vbb@gmail.com", "ver": ["Danielly"], "cat": "pessoal"},
    # agendas dos assessores (leitura; heads veem pelo seletor "Agenda de")
    {"cal": "ygorberny.villela@gmail.com", "ver": ["Ygor"], "cat": "pessoal"},
    {"cal": "maria.martinsvillela@gmail.com", "ver": ["Malu"], "cat": "pessoal"},
    {"cal": "joaozinho250204@gmail.com", "ver": ["João"], "cat": "pessoal"},
    {"cal": "dudona.meb@gmail.com", "ver": ["Madu"], "cat": "pessoal"},
    {"cal": "laurabueno.grupovillela@gmail.com", "ver": ["Laura"], "cat": "pessoal"},
]


from datetime import timedelta as _td


def _rot_responsavel(d):
    """Rodízio das heads por dia útil (seg-sex), ciclando HEADS_ROT."""
    n, cur = 0, ROT_ANCORA
    while cur < d:
        cur += _td(days=1)
        if cur.weekday() < 5:
            n += 1
    return HEADS_ROT[n % len(HEADS_ROT)]
MESES = ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO",
         "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO"]
ABREV = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
         "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]


def sessao_google():
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    cr = Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(cr)


def ler_aba(sess, aba, render="FORMATTED_VALUE"):
    nome_a1 = aba.replace("'", "''")   # apóstrofo em nome de aba dobra (regra A1)
    rng = urllib.parse.quote(f"'{nome_a1}'", safe="")
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{rng}"
           f"?valueRenderOption={render}&dateTimeRenderOption=FORMATTED_STRING")
    ultimo = None
    for tent in range(4):
        r = sess.get(url, timeout=120)
        if r.status_code == 200:
            return r.json().get("values", [])
        ultimo = f"HTTP {r.status_code}: {r.text[:200]}"
        time.sleep(2 * (tent + 1))
    raise RuntimeError(f"falha ao ler aba {aba}: {ultimo}")


# ------------------------------------------------------------------ COMERCIAL
# Etapa 1 do Comercial (aprovada pela Bruna em 18/08/2026): cada linha de
# pagamento da aba do mês vira um registro com tipo (seção NOVOS/RECORRÊNCIA),
# crédito (coluna ASSESSOR; vazia = head da seção) e a convenção da banca:
# valor digitado como TEXTO = inadimplente crônico, fora da projeção.
NOMES_COMERCIAL = {
    "DANIELLY": "Danielly", "BRUNA": "Bruna", "EDUARDA": "Eduarda", "DUDA": "Eduarda",
    "MARIA EDUARDA": "Madu", "MADU": "Madu", "YGOR": "Ygor",
    "MARIA LUISA": "Malu", "MARIA LUIZA": "Malu", "MALU": "Malu",
    "THIAGO": "Thiago", "NICHOLAS": "Nicholas", "JOAO": "João", "JOÃO": "João",
    "LAURA": "Laura",
}


def _sem_acento(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").upper().strip()


def _quem(nome):
    n = _sem_acento(nome)
    if not n:
        return None
    # nome mais LONGO ganha: "MARIA EDUARDA BASTOS" tem "EDUARDA" dentro e
    # precisa cair na Madu, não na Eduarda (variações reais da planilha)
    for chave in sorted(NOMES_COMERCIAL, key=len, reverse=True):
        if _sem_acento(chave) in n:
            return NOMES_COMERCIAL[chave]
    return None


def _num_br(v):
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v or "").replace("R$", "").strip()
    if not v:
        return 0.0
    v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except Exception:
        return 0.0


def _data_br(v):
    import re as _re
    m = _re.search(r"(\d{2})/(\d{2})/(\d{4})", str(v or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def sincronizar_comercial(sess, aba, mes_iso):
    """Lê a aba do mês em dois modos (formatado + cru) e grava
    juridico.comercial_pagamentos. Célula de valor em TEXTO => fora_projecao."""
    import re as _re
    fmt = ler_aba(sess, aba)
    cru = ler_aba(sess, aba, render="UNFORMATTED_VALUE")
    hdr_i, hdr = None, None
    for i, row in enumerate(fmt[:12]):
        s = "|".join(str(x) for x in row).upper()
        if "CLIENTE" in s and "CNPJ" in s:
            hdr_i, hdr = i, [_sem_acento(h) for h in row]
            break
    if hdr_i is None:
        print(f"comercial: cabeçalho não achado em {aba} — mantendo dados atuais")
        return

    def idx(*nomes):
        for n in nomes:
            for j, h in enumerate(hdr):
                if h == _sem_acento(n):
                    return j
        for n in nomes:
            for j, h in enumerate(hdr):
                if _sem_acento(n) in h:
                    return j
        return None

    iC, iDoc = idx("CLIENTE"), idx("CNPJ/CPF", "CNPJ")
    iId = idx("ID")
    iTipo, iLiq, iBru = idx("TIPO DE COBR."), idx("R$ LIQUIDO"), idx("R$ BRUTO")
    iSt, iForma = idx("STATUS"), idx("FORMA PGTO")
    iDp, iVc = idx("D. PGTO"), idx("VENC.")
    iAdv = idx("ADV")
    # abas novas têm a coluna ASSESSOR; nas antigas (junho/julho) a
    # participação morava em "ASS. JUR" — o exato vem primeiro
    iAss = idx("ASSESSOR")
    if iAss is None:
        iAss = idx("ASS. JUR")
    if iAss is None:
        print(f"comercial: aba {aba} sem coluna ASSESSOR — mantendo dados atuais")
        return

    def cel(rows, r, j):
        row = rows[r] if r < len(rows) else []
        return row[j] if (j is not None and j < len(row)) else ""

    linhas = []
    secao_tipo, secao_head = None, None
    for r in range(hdr_i + 1, len(fmt)):
        cli = str(cel(fmt, r, iC)).strip()
        s = _sem_acento(cli)
        doc = _re.sub(r"\D", "", str(cel(fmt, r, iDoc)))
        # título de seção = a célula do CLIENTE COMEÇA com o texto — vale
        # mesmo com lixo nas células vizinhas (até CNPJ perdido ao lado,
        # caso real da seção da Bruna em agosto/26)
        m = _re.match(r"(NOVOS NEG|RECORREN)", s)
        if m:
            secao_tipo = "NOVO" if m.group(1).startswith("NOVOS") else "RECORRENCIA"
            secao_head = _quem(s.split("-")[-1] if "-" in s else s)
            continue
        if len(doc) < 11:
            continue
        bru_cru, liq_cru = cel(cru, r, iBru), cel(cru, r, iLiq)
        # convenção: valor como TEXTO na planilha = fora da projeção (crônico)
        fora = bool(str(bru_cru).strip()) and not isinstance(bru_cru, (int, float))
        ass_txt = str(cel(fmt, r, iAss)).strip()
        # ex-membro/SDR com nome na coluna: crédito fica no nome dele (não
        # infla a head) — igual ao critério do PDF da reunião
        credito = (_quem(ass_txt)
                   or (ass_txt.title() if ass_txt and "REF" not in ass_txt.upper()
                       and len(ass_txt) > 3 else None)
                   or _quem(cel(fmt, r, iAdv)) or secao_head)
        linhas.append((aba, mes_iso, cli, str(cel(fmt, r, iDoc)).strip(),
                       secao_tipo or "RECORRENCIA", secao_head, credito,
                       _num_br(bru_cru if str(bru_cru).strip() else cel(fmt, r, iBru)),
                       _num_br(liq_cru if str(liq_cru).strip() else cel(fmt, r, iLiq)),
                       _sem_acento(cel(fmt, r, iSt)), _sem_acento(cel(fmt, r, iForma)),
                       _data_br(cel(fmt, r, iDp)), _data_br(cel(fmt, r, iVc)),
                       fora, _sem_acento(cel(fmt, r, iTipo)),
                       str(cel(fmt, r, iId)).strip()))
    if not linhas:
        print(f"comercial: aba {aba} sem lançamentos — mantendo dados atuais")
        return
    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("delete from juridico.comercial_pagamentos where mes = %s", (mes_iso,))
        with cur.copy("""copy juridico.comercial_pagamentos
            (aba,mes,cliente,cnpj_cpf,tipo,secao_head,credito,valor_bruto,
             valor_liquido,status,forma_pgto,data_pgto,venc,fora_projecao,tipo_col,
             id_lanc)
            from stdin""") as cp:
            for ln in linhas:
                cp.write_row(ln)
        # carimbo do PRIMEIRO momento em que cada pagamento foi visto como
        # PAGO — é o que define o corte de sexta 15h do placar
        # chave por PARCELA: id do contrato + data (parcelas repetem o id)
        cur.execute("""insert into juridico.pago_visto (id_lanc)
            select distinct coalesce(id_lanc,'')||'|'||coalesce(data_pgto::text,'')
            from juridico.comercial_pagamentos
            where mes=%s and status='PAGO' and coalesce(id_lanc,'')<>''
            on conflict (id_lanc) do nothing""", (mes_iso,))
        conn.commit()
    print(f"comercial: {len(linhas)} lançamento(s) de {aba}")


def bater_coracao():
    """Registra que o robô rodou (o Painel vigia isso) e tira a fotografia
    diária da equipe: quantas tarefas atrasadas/em dia/ativas/imediatas há
    agora. Como roda a cada 5 min, a última foto do dia é o fechamento do
    dia — é o que o Acompanhamento Diário mostra ao voltar em datas passadas.
    """
    hoje_sp = "(now() at time zone 'America/Sao_Paulo')::date"
    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("""insert into juridico.robo_status (nome, ultima)
                       values ('cobranca', now())
                       on conflict (nome) do update set ultima = excluded.ultima""")
        # atrasadas/imediatas contam só o que está ACIONÁVEL pelo assessor:
        # em correção (com a head) e delegadas (com o receptor — que já conta
        # o impulso dele) ficam de fora (pedido da Danielly, 10/08/2026)
        cur.execute(f"""select count(*)::int,
            count(*) filter (where coalesce(status_tarefa,'') not like '%%EM DIA%%'
                               and coalesce(status_tarefa,'') not like '%%DELEGADA%%'
                               and coalesce(correcao_head,'') = ''
                               and data_revisao_dt < {hoje_sp})::int,
            count(*) filter (where check_ = 'IMEDIATO'
                               and coalesce(status_tarefa,'') not like '%%EM DIA%%'
                               and coalesce(status_tarefa,'') not like '%%DELEGADA%%'
                               and coalesce(correcao_head,'') = '')::int
            from juridico.operacional""")
        total, atrasadas, imediatas = cur.fetchone()
        em_dia = total - atrasadas
        pct = round(100 * em_dia / total) if total else 100
        cur.execute(f"delete from juridico.snapshot_diario where data_dt = {hoje_sp}")
        cur.execute(f"""insert into juridico.snapshot_diario
            (data, data_dt, atrasadas, em_dia, total_ativas, imediatas, pct_em_dia,
             atrasadas_num, em_dia_num, total_ativas_num, imediatas_num, pct_em_dia_num)
            values (to_char({hoje_sp},'DD/MM/YYYY'), {hoje_sp}, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s)""",
            (str(atrasadas), str(em_dia), str(total), str(imediatas), str(pct),
             atrasadas, em_dia, total, imediatas, pct))
        # foto POR PESSOA: feitas (concluídas + impulsos devolvidos + encerradas
        # do dia, cada uma 1x) × pendentes para hoje (mesma régua da carga).
        # A última foto do dia vira o fechamento que o Diário mostra no passado.
        cur.execute(f"""
            with feitas as (
              select autor, count(distinct k) as f from (
                select autor, 'C|'||id_tarefa as k from juridico.historico
                  where data_dt = {hoje_sp} and texto like 'Tarefa concluída pelo site%%'
                union
                select autor, 'I|'||substring(texto from 'Retorno do impulso \\((OP-\\d+)\\)')
                  from juridico.historico
                  where data_dt = {hoje_sp} and texto like '%%Retorno do impulso (OP-%%'
                    and tipo <> 'HISTORICO'
                union
                select autor, 'E|'||id_tarefa from juridico.historico
                  where data_dt = {hoje_sp} and tipo = 'ENCERRAMENTO') x
              where autor not in ('Sistema','Migração') group by autor),
            pend as (
              select assessor, count(*) as p from juridico.operacional
              where coalesce(status_tarefa,'') not like '%%EM DIA%%'
                and coalesce(status_tarefa,'') not like '%%DELEGADA%%'
                and coalesce(correcao_head,'') = ''
                and (data_revisao_dt <= {hoje_sp}
                     or (data_revisao_dt is null and check_ = 'IMEDIATO'))
              group by assessor)
            select coalesce(f.autor, p.assessor), coalesce(f.f,0), coalesce(p.p,0)
            from feitas f full outer join pend p on p.assessor = f.autor""")
        fotos = cur.fetchall()
        cur.execute(f"delete from juridico.snapshot_assessor where data_dt = {hoje_sp}")
        for nome, feitas_n, pend_n in fotos:
            if not (nome or "").strip():
                continue
            cur.execute(f"""insert into juridico.snapshot_assessor
                (data_dt, assessor, feitas, pendentes)
                values ({hoje_sp}, %s, %s, %s)""", (nome.strip(), feitas_n, pend_n))
        conn.commit()


def sincronizar_metas(sess):
    """Metas do mês direto da aba PROJEÇÕES (pedido da Bruna, 15/09/2026):
    'META OFICIAL' vira pessoa=EQUIPE (meta conjunta), a linha HEADS vira a
    meta conjunta das heads e as PARTICIPAÇÕES viram metas individuais.
    Tudo entra liberado — a planilha é a fonte oficial; o lançamento manual
    nas Configurações segue valendo só para mês sem aba de projeções."""
    hoje = date.today()
    ano2 = str(hoje.year)[-2:]
    m = hoje.month - 1
    candidatas = [f"PROJEÇÕES {MESES[m]} {ano2}", f"PROJEÇÕES {MESES[m]} {ano2}'",
                  f"PROJEÇÕES {ABREV[m]} {ano2}", f"PROJEÇÕES {ABREV[m]} {ano2}'"]
    rows = None
    for aba in candidatas:
        try:
            rows = ler_aba(sess, aba)
            break
        except Exception:
            continue
    if not rows:
        print(f"metas: aba de projeções não encontrada ({candidatas[0]}) — mantendo as atuais")
        return
    metas = []
    for r in rows:
        e1 = (str(r[4]).strip().upper() if len(r) > 4 else "")
        if not e1 or e1 in ("PARTICIPAÇÕES", "PARTICIPACOES", "OUTROS"):
            continue
        v = _num_br(r[5] if len(r) > 5 else "")
        if not v:
            continue
        if e1 in ("TOTAIS:", "TOTAIS"):
            # META ALVO do mês = soma das participações (heads + apoio) —
            # é ESSA que vale como meta da equipe (Bruna, 15/09/2026)
            metas.append(("EQUIPE", v))
        elif e1 == "HEADS":
            metas.append(("HEADS", v))
        else:
            pessoa = _quem(e1)
            if pessoa:
                metas.append((pessoa, v))
    if not metas:
        print("metas: aba achada mas sem valores reconhecidos — mantendo as atuais")
        return
    mes_iso = hoje.replace(day=1).isoformat()
    with psycopg.connect() as conn, conn.cursor() as cur:
        for pessoa, v in metas:
            cur.execute("""insert into juridico.config_metas (mes, pessoa, meta, liberada)
                values (%s, %s, %s, true)
                on conflict (mes, pessoa) do update set meta = excluded.meta, liberada = true""",
                (mes_iso, pessoa, v))
        conn.commit()
    print(f"metas: {len(metas)} atualizadas da planilha "
          f"(EQUIPE={dict(metas).get('EQUIPE', 0):.2f})")


CAL_3HEADS = "plataformabde@gmail.com"
# pessoa → agenda Google onde o robô CRIA as reuniões que ela lançou no sistema.
# Requer compartilhamento com permissão "Fazer alterações nos eventos" (escrita).
# O e-mail aqui TAMBÉM precisa estar em AGENDAS (leitura), senão a reunião some.
AGENDA_ESCRITA = {
    "Ygor": "ygorberny.villela@gmail.com",
    "Malu": "maria.martinsvillela@gmail.com",
    "João": "joaozinho250204@gmail.com",
    "Madu": "dudona.meb@gmail.com",
    "Laura": "laurabueno.grupovillela@gmail.com",
}


def _up_q(x):
    import urllib.parse as _u
    return _u.quote(x)


def empurrar_para_google(sess):
    """Compromissos 'todas' criados no sistema (a_empurrar=true) viram eventos
    na agenda Google do plataformabde — aparecem para quem usa o Google direto.
    Marca origem=sistema (p/ a leitura recolorir como 'todas' dourado) e apaga
    as cópias locais (o import as traz de volta como GCAL, sem duplicar)."""
    from datetime import datetime as _dt
    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("""select obs, min(titulo), min(data_dt::text), min(horario),
               min(duracao_min_num)
            from juridico.reunioes
            where categoria='todas' and a_empurrar=true and coalesce(gcal_id,'')=''
              and obs ~ '\[g[a-z0-9]+\]'
            group by obs""")
        grupos = cur.fetchall()
    for obs, titulo, data_dt, horario, dur in grupos:
        try:
            hh, mm = (horario or "09:00").split(":")[0:2]
            ini = f"{data_dt}T{int(hh):02d}:{int(mm):02d}:00-03:00"
            fim_dt = _dt.fromisoformat(ini) + _td(minutes=int(dur or 30))
            ev = {"summary": titulo,
                  "start": {"dateTime": ini, "timeZone": "America/Sao_Paulo"},
                  "end": {"dateTime": fim_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
                  "extendedProperties": {"private": {"origem": "sistema-3heads"}}}
            r = sess.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{CAL_3HEADS}/events",
                json=ev, timeout=30)
            if r.status_code >= 300:
                print(f"empurrar: falhou ({r.status_code}) p/ '{titulo}'")
                continue
            gid = r.json().get("id", "")
            with psycopg.connect() as conn, conn.cursor() as cur:
                # apaga as cópias locais — o import as recria a partir do Google
                cur.execute("delete from juridico.reunioes where obs=%s", (obs,))
                conn.commit()
            print(f"empurrar: '{titulo}' criado no Google ({gid}) e cópias locais removidas")
        except Exception as e:
            print(f"empurrar: erro tolerado em '{titulo}': {e}")

    # 2) reuniões PESSOAIS/manuais lançadas no sistema → agenda Google do dono
    from datetime import datetime as _dt2
    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("""select id_reuniao, assessor, titulo, data_dt::text, horario,
               coalesce(duracao_min_num,30)
            from juridico.reunioes
            where a_empurrar=true and coalesce(categoria,'') in ('manual','pessoal')
              and coalesce(gcal_id,'')='' """)
        pend = cur.fetchall()
    for rid, quem, titulo, data_dt, horario, dur in pend:
        cal = AGENDA_ESCRITA.get(quem)
        if not cal:
            # dono sem agenda de escrita: fica só no sistema, não repete a tentativa
            with psycopg.connect() as conn, conn.cursor() as cur:
                cur.execute("update juridico.reunioes set a_empurrar=false where id_reuniao=%s", (rid,))
                conn.commit()
            continue
        try:
            hh, mm = (horario or "09:00").split(":")[0:2]
            ini = f"{data_dt}T{int(hh):02d}:{int(mm):02d}:00-03:00"
            fim_dt = _dt2.fromisoformat(ini) + _td(minutes=int(dur or 30))
            ev = {"summary": titulo,
                  "start": {"dateTime": ini, "timeZone": "America/Sao_Paulo"},
                  "end": {"dateTime": fim_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
                  "extendedProperties": {"private": {"origem": "sistema"}}}
            r = sess.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{_up_q(cal)}/events",
                json=ev, timeout=30)
            if r.status_code >= 300:
                print(f"empurrar pessoal: falhou ({r.status_code}) '{titulo}' de {quem}")
                continue
            with psycopg.connect() as conn, conn.cursor() as cur:
                cur.execute("delete from juridico.reunioes where id_reuniao=%s", (rid,))
                conn.commit()
            print(f"empurrar pessoal: '{titulo}' → agenda de {quem} ({cal})")
        except Exception as e:
            print(f"empurrar pessoal: erro tolerado '{titulo}': {e}")


def sincronizar_agendas(sess):
    """Espelha os eventos das agendas Google (hoje → +7d) em juridico.reunioes,
    com categoria (cor) e, no rodízio SC, o responsável do dia."""
    import urllib.parse as _up
    import hashlib as _hl
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _tz
    sp = _tz("America/Sao_Paulo")
    agora = _dt.now(sp)
    t_min = agora.replace(hour=0, minute=0, second=0).isoformat()
    t_max = (agora + _td(days=7)).isoformat()
    for ag in AGENDAS:
        cal = ag["cal"]
        pessoas = ag.get("ver", [])
        cat = ag.get("cat", "manual")
        caltag = _hl.md5(cal.encode()).hexdigest()[:6]
        url = (f"https://www.googleapis.com/calendar/v3/calendars/{_up.quote(cal)}/events"
               f"?singleEvents=true&orderBy=startTime&maxResults=100"
               f"&timeMin={_up.quote(t_min)}&timeMax={_up.quote(t_max)}")
        r = sess.get(url, timeout=60)
        if r.status_code != 200:
            print(f"agenda {cal}: sem acesso ainda (HTTP {r.status_code}) — "
                  "compartilhar com a conta-robô")
            continue
        eventos = r.json().get("items", [])
        vivos = []
        with psycopg.connect() as conn, conn.cursor() as cur:
            for ev in eventos:
                ini = (ev.get("start") or {}).get("dateTime")
                fim = (ev.get("end") or {}).get("dateTime")
                if not ini or not fim:
                    continue
                d_ini = _dt.fromisoformat(ini).astimezone(sp)
                d_fim = _dt.fromisoformat(fim).astimezone(sp)
                mins = max(15, int((d_fim - d_ini).total_seconds() // 60))
                titulo = (ev.get("summary") or "Reunião (agenda Google)")[:180]
                cr_email = str((ev.get("creator") or {}).get("email", "")).lower()
                origem = ((ev.get("extendedProperties") or {}).get("private") or {}).get("origem", "")
                if ag.get("rot") and (cr_email in HEADS_EMAILS or origem == "sistema-3heads"):
                    cat_ev, resp = "todas", ""      # as 3 heads (dourado)
                elif ag.get("rot"):
                    cat_ev, resp = "sc", _rot_responsavel(d_ini.date())
                else:
                    cat_ev, resp = cat, (ag.get("dono") or "")
                for nome in pessoas:
                    suf = "" if len(pessoas) == 1 else "-" + nome[:8]
                    rid = f"GCAL-{caltag}-" + ev.get("id", "")[:34] + suf
                    vivos.append(rid)
                    cur.execute("""insert into juridico.reunioes
                        (id_reuniao, data, data_dt, assessor, titulo, cliente,
                         horario, duracao_min, duracao_min_num, obs, categoria, responsavel)
                        values (%s,%s,%s,%s,%s,'',%s,%s,%s,%s,%s,%s)
                        on conflict (id_reuniao) do update set
                          data=excluded.data, data_dt=excluded.data_dt,
                          titulo=excluded.titulo, horario=excluded.horario,
                          duracao_min=excluded.duracao_min,
                          duracao_min_num=excluded.duracao_min_num,
                          categoria=excluded.categoria, responsavel=excluded.responsavel""",
                        (rid, d_ini.strftime("%d/%m/%Y"), d_ini.date(), nome, titulo,
                         d_ini.strftime("%H:%M"), str(mins), mins, "agenda Google",
                         cat_ev, resp))
            for nome in pessoas:
                cur.execute("""delete from juridico.reunioes
                    where assessor=%s and id_reuniao like %s
                      and data_dt >= %s and not (id_reuniao = any(%s))""",
                    (nome, f"GCAL-{caltag}-%", agora.date(), vivos or ["x"]))
            conn.commit()
        print(f"agenda {cat} ({cal}): {len(vivos)} espelho(s)")


def main():
    sess = sessao_google()
    hoje = date.today()
    ano2 = str(hoje.year)[-2:]
    m = hoje.month - 1
    candidatas = [f"{MESES[m]} {ano2}", f"{MESES[m]} {ano2}'",
                  f"{ABREV[m]} {ano2}", f"{ABREV[m]} {ano2}'", "COBRANÇA_ATUAL"]

    valores, aba_usada = None, None
    for aba in candidatas:
        try:
            valores = ler_aba(sess, aba)
            aba_usada = aba
            break
        except Exception:
            continue
    if not valores:
        print(f"nenhuma aba do mês encontrada ({candidatas}) — nada a fazer")
        bater_coracao()
        return 0

    hdr_i = None
    for i, row in enumerate(valores[:12]):
        s = "|".join(str(x) for x in row).lower()
        if "cliente" in s and "cnpj" in s:
            hdr_i = i
            break
    if hdr_i is None:
        print(f"cabeçalho não achado na aba {aba_usada} — nada a fazer")
        bater_coracao()
        return 0
    hdr = [str(h).strip() for h in valores[hdr_i]]

    def idx(*nomes):
        for n in nomes:
            for i, h in enumerate(hdr):
                if h.upper() == n.upper():
                    return i
        for n in nomes:
            for i, h in enumerate(hdr):
                if n.upper() in h.upper():
                    return i
        return None

    i_cnpj = idx("CNPJ/CPF", "CNPJ")
    i_cli = idx("CLIENTE", "Coluna 2", "RAZÃO SOCIAL")
    if i_cli is None and i_cnpj:
        i_cli = i_cnpj - 1
    i_prod = idx("PRODUTO")
    i_tipo = idx("TIPO DE COBR")
    i_venc = idx("VENC", "PREVISTA")
    i_val = idx("R$ BRUTO", "VALOR")
    i_st = idx("STATUS", "PAGAMENTO")
    i_adv = idx("ADV")
    i_obs = idx("OBS")

    def cel(row, i):
        return str(row[i]).strip() if (i is not None and i < len(row)) else ""

    def num(v):
        v = v.replace("R$", "").strip()
        if "," in v:
            v = v.replace(".", "").replace(",", ".")
        try:
            return float(v)
        except Exception:
            return None

    def head_de(adv):
        a = adv.upper()
        if "DANIELLY" in a or a.startswith("DANI"):
            return "Danielly"
        if "BRUNA" in a:
            return "Bruna"
        if "EDUARDA" in a or a.startswith("DUDA"):
            return "Eduarda"
        return adv

    linhas_cob = []
    for row in valores[hdr_i + 1:]:
        cnpj = "".join(ch for ch in cel(row, i_cnpj) if ch.isdigit())
        if len(cnpj) < 11:
            continue
        st = cel(row, i_st).upper()
        status = ("PAGO" if st == "PAGO" else
                  "ATRASADO" if "VENC" in st or "INADIMPLEN" in st else
                  "PENDENTE" if ("AG" in st or st == "") else st)
        adv = cel(row, i_adv)
        linhas_cob.append((aba_usada, cel(row, i_cli), cel(row, i_cnpj),
                           cel(row, i_prod), cel(row, i_tipo), cel(row, i_venc),
                           num(cel(row, i_val)), status, adv, head_de(adv),
                           cel(row, i_obs)))

    if not linhas_cob:
        print(f"aba {aba_usada} sem lançamentos válidos — mantendo dados atuais")
        bater_coracao()
        return 0

    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("truncate table juridico.cobranca")
        with cur.copy("""copy juridico.cobranca
            (aba,cliente,cnpj_cpf,produto,tipo_cobr,data_venc,valor_bruto,
             status_pgto,adv,head,obs) from stdin""") as cp:
            for linha in linhas_cob:
                cp.write_row(linha)
        conn.commit()
    # comercial (etapa 1): mesma aba, com seção/tipo/crédito/fora-da-projeção
    try:
        sincronizar_comercial(sess, aba_usada, hoje.replace(day=1).isoformat())
    except Exception as e:
        print(f"comercial: falhou sem afetar a cobrança — {e}")
    # metas do mês (aba PROJEÇÕES) — meta conjunta + individuais
    try:
        sincronizar_metas(sess)
    except Exception as e:
        print(f"metas: falhou sem afetar o resto — {e}")
    # agenda do Google → reuniões do sistema (métrica do dia do Nicholas)
    try:
        empurrar_para_google(sess)
    except Exception as e:
        print(f"empurrar agenda: falhou sem afetar o resto — {e}")
    try:
        sincronizar_agendas(sess)
    except Exception as e:
        print(f"agenda: falhou sem afetar o resto — {e}")
    bater_coracao()
    print(f"ok: {len(linhas_cob)} lançamento(s) da aba '{aba_usada}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
