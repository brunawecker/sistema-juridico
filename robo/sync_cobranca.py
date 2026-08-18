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
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
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
    iTipo, iLiq, iBru = idx("TIPO DE COBR."), idx("R$ LIQUIDO"), idx("R$ BRUTO")
    iSt, iForma = idx("STATUS"), idx("FORMA PGTO")
    iDp, iVc = idx("D. PGTO"), idx("VENC.")
    iAdv, iAss = idx("ADV"), idx("ASSESSOR")
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
        credito = (_quem(cel(fmt, r, iAss)) or _quem(cel(fmt, r, iAdv)) or secao_head)
        linhas.append((aba, mes_iso, cli, str(cel(fmt, r, iDoc)).strip(),
                       secao_tipo or "RECORRENCIA", secao_head, credito,
                       _num_br(bru_cru if str(bru_cru).strip() else cel(fmt, r, iBru)),
                       _num_br(liq_cru if str(liq_cru).strip() else cel(fmt, r, iLiq)),
                       _sem_acento(cel(fmt, r, iSt)), _sem_acento(cel(fmt, r, iForma)),
                       _data_br(cel(fmt, r, iDp)), _data_br(cel(fmt, r, iVc)),
                       fora, _sem_acento(cel(fmt, r, iTipo))))
    if not linhas:
        print(f"comercial: aba {aba} sem lançamentos — mantendo dados atuais")
        return
    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("delete from juridico.comercial_pagamentos where mes = %s", (mes_iso,))
        with cur.copy("""copy juridico.comercial_pagamentos
            (aba,mes,cliente,cnpj_cpf,tipo,secao_head,credito,valor_bruto,
             valor_liquido,status,forma_pgto,data_pgto,venc,fora_projecao,tipo_col)
            from stdin""") as cp:
            for ln in linhas:
                cp.write_row(ln)
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
    bater_coracao()
    print(f"ok: {len(linhas_cob)} lançamento(s) da aba '{aba_usada}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
